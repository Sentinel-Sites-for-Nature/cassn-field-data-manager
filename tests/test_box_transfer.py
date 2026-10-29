"""Upload transport regressions use actual SDK encoding and fake HTTP only."""
import io
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import format_datetime
from types import SimpleNamespace as NS

import pytest
import requests
from box_sdk_gen.networking.box_network_client import BoxNetworkClient
from box_sdk_gen.networking.fetch_options import FetchOptions, MultipartItem
from box_sdk_gen.networking.network import NetworkSession

from cassn.box.transfer import (
    UploadCancelled, UploadNetworkClient, UploadPaused, UploadRuntime, retry_after,
)


class Clock:
    def __init__(self):
        self.now, self.waits = 0., []

    def __call__(self):
        return self.now

    def wait(self, delay):
        self.waits.append(delay)
        self.now += delay
        return False


class HTTP:
    def __init__(self, replies):
        self.replies, self.bodies, self.calls = list(replies), [], 0

    def request(self, **kwargs):
        self.calls += 1
        body = kwargs['data']
        self.bodies.append(body.read() if hasattr(body, 'read') else body)
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        status, headers = item
        response = requests.Response()
        response.status_code, response.url = status, kwargs['url']
        response.headers.update(headers)
        response._content = b'{"code":"synthetic","message":"test"}'
        return response


def setup(replies, *, method='POST', **kwargs):
    clock, http = Clock(), HTTP(replies)
    runtime = UploadRuntime(clock=clock, wait=clock.wait)
    network = UploadNetworkClient(runtime, client_factory=lambda: BoxNetworkClient(http))
    session = NetworkSession(network_client=network)
    options = FetchOptions(url='https://upload.box.com/2.0/files/content', method=method,
                           network_session=session, **kwargs)
    return runtime, network, options, clock, http


@pytest.mark.parametrize('header', ['Retry-After', 'retry-after', 'RETRY-AFTER'])
@pytest.mark.parametrize('kind', ['raw', 'multipart'])
def test_long_wait_and_replay_at_original_file_position(tmp_path, header, kind, monkeypatch):
    monkeypatch.setattr('box_sdk_gen.networking.box_network_client.time.sleep',
                        lambda delay: pytest.fail('SDK must not sleep'))
    path = tmp_path/'file'
    path.write_bytes(b'prefix:synthetic-media')
    with path.open('rb') as stream:
        stream.seek(7)
        kw = ({'file_stream': stream, 'content_type':'application/octet-stream'} if kind=='raw'
              else {'multipart_data':[MultipartItem('file', file_stream=stream, file_name='file')],
                    'content_type':'multipart/form-data'})
        runtime, network, options, clock, http = setup([(429,{header:'120'}),(201,{})], **kw)
        assert network.fetch(options).status == 201
        assert clock.waits == [120]
        assert len(http.bodies) == 2
        assert all(b'synthetic-media' in body and b'prefix:' not in body for body in http.bodies)
        assert runtime.metrics.snapshot()['retried_requests'] == 1


@pytest.mark.parametrize('value,expected', [('0',0), ('120',120), ('-1',None), ('nan',None),
                                           ('inf',None), ('bad',None), (None,None)])
def test_retry_header_values(value, expected):
    assert retry_after({'retry-after':value}) == expected


def test_retry_http_date():
    value = format_datetime(datetime.fromtimestamp(120, timezone.utc), usegmt=True)
    assert retry_after({'retry-after':value}, now=30) == 90
    assert retry_after({'retry-after':value}, now=200) == 0


def test_shared_actor_cooldown_and_cancellation():
    clock=Clock()
    first=UploadRuntime(clock=clock, wait=clock.wait)
    second=UploadRuntime(clock=clock, wait=clock.wait)
    first.bind_actor('test-shared-cooldown')
    second.bind_actor('test-shared-cooldown')
    first.cooldown.defer(120, clock)
    second.wait_cooldown()
    assert clock.waits == [120]
    first.cooldown.defer(120, clock)
    second.cancel.set()
    with pytest.raises(UploadCancelled): second.wait_cooldown()


def test_too_long_wait_pauses_without_retrying_early():
    runtime, network, options, clock, http=setup([(429,{'retry-after':'7200'}),(201,{})])
    with pytest.raises(UploadPaused): network.fetch(options)
    assert http.calls == 1
    assert clock.waits == []


def test_cancel_wakes_retry_wait():
    runtime, network, options, clock, http=setup([(429,{'retry-after':'120'}),(201,{})])
    def cancel(delay):
        runtime.cancel.set()
        return True
    runtime.wait=cancel
    with pytest.raises(UploadCancelled): network.fetch(options)
    assert http.calls == 1


@pytest.mark.parametrize('status', [400,403,409,412,500])
def test_uncertain_or_permanent_writes_are_not_repeated(status):
    runtime, network, options, clock, http=setup([(status,{}),(201,{})])
    with pytest.raises(Exception): network.fetch(options)
    assert http.calls == 1
    assert not clock.waits


def test_get_transient_failure_retries_but_commit_pending_returns_headers():
    runtime, network, options, clock, http=setup([(503,{}),(200,{})], method='GET')
    assert network.fetch(options).status == 200
    assert http.calls == 2
    runtime, network, options, clock, http=setup([(202,{'retry-after':'120'})])
    reply=network.fetch(options)
    assert reply.status == 202 and retry_after(reply.headers)==120
    assert not clock.waits


def test_read_disconnect_retries_write_disconnect_does_not():
    for method, calls in [('GET',2),('POST',1)]:
        runtime, network, options, clock, http=setup([requests.ConnectionError('lost'),(200,{})], method=method)
        if method=='GET': assert network.fetch(options).status==200
        else:
            with pytest.raises(Exception): network.fetch(options)
        assert http.calls==calls


def test_simultaneous_401_refreshes_shared_auth_once():
    barrier=threading.Barrier(2)
    auth=NS(refreshes=0)
    def refresh(**kwargs): auth.refreshes+=1
    auth.refresh_token=refresh
    class Inner:
        def fetch(self, options):
            if not auth.refreshes:
                barrier.wait(timeout=2)
                exc=RuntimeError('401')
                exc.response_info=NS(status_code=401, headers={})
                raise exc
            return NS(status=200)
    runtime=UploadRuntime(transfers=2)
    network=UploadNetworkClient(runtime, client_factory=Inner)
    session=NetworkSession(network_client=network)
    options=FetchOptions(url='https://api.box.com/2.0/users/me',method='GET',auth=auth,network_session=session)
    with ThreadPoolExecutor(2) as pool:
        assert [f.result().status for f in [pool.submit(network.fetch,options),pool.submit(network.fetch,options)]]==[200,200]
    assert auth.refreshes==1


def test_transfer_and_buffer_limits_release_on_failure():
    runtime=UploadRuntime(transfers=1, buffer_bytes=16)
    with pytest.raises(RuntimeError):
        with runtime.connection(): raise RuntimeError('synthetic')
    with runtime.connection():
        with runtime.connection(): pass  # nested auth does not deadlock
    runtime.reserve_buffer(16)
    runtime.release_buffer(16)
    assert runtime.buffer_used==0
    with pytest.raises(UploadPaused): runtime.reserve_buffer(17)


def test_safe_read_honors_server_503_wait():
    runtime,network,options,clock,http=setup([(503,{'retry-after':'120'}),(200,{})], method='GET')
    assert network.fetch(options).status==200
    assert clock.waits==[120]


def test_programming_error_is_not_retried():
    class Broken:
        def fetch(self,options): raise ValueError('bug')
    runtime,network,options,clock,http=setup([] ,method='GET')
    network.client_factory=Broken
    with pytest.raises(ValueError,match='bug'): network.fetch(options)
    assert not clock.waits


def test_metrics_throttle_checkpoints_and_do_not_log_request_details(tmp_path):
    import json
    from cassn.box.transfer import UploadMetrics
    clock=Clock()
    snapshots=[]
    metrics=UploadMetrics(clock=clock,callback=snapshots.append)
    metrics.output_path=tmp_path/'qc/box_upload_sessions/metrics.json'
    for n in range(1000): metrics.add(acknowledged_bytes=1,request_attempts=1)
    assert len(snapshots)==1
    clock.now=31
    metrics.add(uploaded_files=2)
    saved=json.loads(metrics.output_path.read_text())
    assert saved['acknowledged_bytes']==1000
    assert saved['uploaded_files']==2
    assert 'headers' not in saved and 'url' not in saved
    assert snapshots[-1]['acknowledged_MB_per_second']==pytest.approx(1000/31/1e6)


def test_final_failed_read_does_not_wait_for_an_unused_retry():
    runtime,network,options,clock,http=setup([(503,{'retry-after':'120'})], method='GET')
    network.attempts=1
    with pytest.raises(Exception): network.fetch(options)
    assert clock.waits==[]
    assert http.calls==1
