import pathlib
import time
import threading
import os

import pytest

from cassn.ndp.bounded_downloads import SourceBatchFailed, run_bounded


def fail_or_sleep(payload):
    pathlib.Path(payload['marker']).write_text('started')
    if payload['filename'] == 'fail':
        raise OSError(5, 'secret signed capability must not enter status')
    time.sleep(30)
    return dict(ok=True)


def success(payload):
    return dict(ok=True, skipped=True)


def test_failure_does_not_schedule_entire_folder_and_drain_is_bounded(tmp_path):
    files=[dict(filename='fail' if i==0 else str(i), marker=str(tmp_path/str(i))) for i in range(100)]
    events=[];start=time.monotonic()
    with pytest.raises(SourceBatchFailed) as caught:
        run_bounded(fail_or_sleep,files,drain_timeout=.1,file_timeout=10,
                    heartbeat=.02,report=events.append)
    assert time.monotonic()-start<8
    assert len(list(tmp_path.iterdir()))<=3
    assert events and max(len(e['active']) for e in events)<=3
    assert any(e['state']=='draining_after_failure' for e in events)
    assert caught.value.detail['errno']==5
    assert 'secret' not in str(events)


def test_slow_read_has_fresh_heartbeat_then_stops_with_exact_file(tmp_path):
    events=[];start=time.monotonic()
    with pytest.raises(SourceBatchFailed) as caught:
        run_bounded(fail_or_sleep,[dict(filename='blocked',marker=str(tmp_path/'marker'))],
                    workers=1,file_timeout=.6,drain_timeout=.1,heartbeat=.05,report=events.append)
    assert time.monotonic()-start<8
    assert caught.value.detail['error_type']=='SourceFileTimeout'
    assert caught.value.detail['filename']=='blocked'
    assert len(events)>3


def test_matching_files_report_actual_validated_count_and_last_success():
    events=[]
    assert run_bounded(success,[dict(filename=str(i)) for i in range(7)],
                       report=events.append)==7
    assert events[-1]['state']=='complete'
    assert events[-1]['completed']==7
    assert events[-1]['last_success']['skipped']


def test_cancellation_stops_source_children_and_never_starts_the_rest(tmp_path):
    cancel=threading.Event();events=[]
    def report(event):
        events.append(event)
        if event['active']:
            cancel.set()
    files=[dict(filename=str(i),marker=str(tmp_path/str(i))) for i in range(100)]
    with pytest.raises(SourceBatchFailed) as caught:
        run_bounded(fail_or_sleep,files,cancel=cancel,drain_timeout=.1,
                    heartbeat=.02,report=report)
    assert caught.value.detail['error_type']=='SourceCancelled'
    assert len(list(tmp_path.iterdir()))<=3
    for pid in events[-1]['worker_pids']:
        with pytest.raises(ProcessLookupError):
            os.kill(pid,0)
