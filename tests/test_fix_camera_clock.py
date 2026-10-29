"""Synthetic clock changes, restart boundaries and retained-source evidence."""
import hashlib
import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from utils import fix_camera_clock as clock
from cassn.core.ingest_safety import require_no_ingest_holds


@pytest.fixture
def operation(tmp_path, monkeypatch):
    dep = tmp_path / 'deployment'
    dep.mkdir()
    inventory = []
    for index, relative in enumerate(('raw_data/p1_ML/one.jpg', 'raw_data/p1_ML/p1_ML_1/two.jpg')):
        path = dep / relative
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_text(json.dumps({'clock': '2025:01:01 11:00:00', 'changes': 0}))
        inventory.append({'file_type': 'image', 'device_type': 'ML', 'deployment_id': 'camera',
                          'device_label': 'p1_ML', 'new_filename': path.name, 'storage_relpath': relative,
                          'file_hash_sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    session = {'file_inventory': inventory, 'metadata': {'deployment_event_id': 'test', 'site_short_name': 'Test'}, 'devices': []}
    (dep / 'session.json').write_text(json.dumps(session))
    lookups = SimpleNamespace(soundhub_config={}, wi_config={}, deployment_for_id=lambda _: {'deployment_start_date': '2025-01-01', 'deployment_end_date': '2025-01-02'})
    monkeypatch.setattr(clock.LookupTables, 'load', lambda _: lookups)
    monkeypatch.setattr('cassn.export.metadata_csv.snapshot_lookup_tables', lambda _: [])
    monkeypatch.setattr(clock, 'extract_exif_data', lambda path: ({
        'DateTimeOriginal': json.loads(path.read_text())['clock'], 'OffsetTimeOriginal': '-08:00'}, {}))
    calls = []
    def shift(path, *args):
        data = json.loads(path.read_text())
        data['clock'] = (datetime.strptime(data['clock'], '%Y:%m:%d %H:%M:%S') + timedelta(hours=1)).strftime('%Y:%m:%d %H:%M:%S')
        data['changes'] += 1
        path.write_text(json.dumps(data))
        calls.append(path)
        return 1
    monkeypatch.setattr(clock, 'exiftool_shift', shift)
    def run(*extra):
        monkeypatch.setattr('sys.argv', ['fix_camera_clock.py', '--deployment', str(dep), '--devices', 'p1_ML', '--hours', '1', *extra])
        return clock.main()
    def journal():
        home = clock.operation_home(dep)
        active = json.loads((home / 'active.json').read_text())
        op = home / active['operation_id']
        return op, json.loads((op / 'journal.json').read_text())
    return SimpleNamespace(dep=dep, inventory=inventory, run=run, journal=journal, shift=shift, calls=calls)


def image_states(op):
    return [json.loads((op.dep / e['storage_relpath']).read_text()) for e in op.inventory]


def assert_complete(op):
    assert all(data['changes'] == 1 for data in image_states(op))
    path, journal = op.journal()
    assert journal['status'] == 'complete'
    saved = json.loads((op.dep / 'session.json').read_text())
    for entry in saved['file_inventory']:
        target = op.dep / entry['storage_relpath']
        assert entry['file_hash_sha256'] == clock.file_digest(target)
        assert entry['recorded_datetime_utc'] == '2025-01-01T20:00:00Z'
    require_no_ingest_holds(op.dep)
    for original in (path / 'originals').iterdir():
        assert json.loads(original.read_text())['changes'] == 0


def test_mixed_flat_split_exact_preview_success_and_exact_retry(operation, capsys):
    before = {p: p.read_bytes() for p in operation.dep.rglob('*') if p.is_file()}
    assert operation.run('--dry-run') == 0
    assert {p: p.read_bytes() for p in operation.dep.rglob('*') if p.is_file()} == before
    assert not clock.operation_home(operation.dep).exists()
    assert 'raw_data/p1_ML/p1_ML_1/two.jpg' in capsys.readouterr().out
    assert operation.run() == 0
    assert_complete(operation)
    assert operation.run() == 0
    assert len(operation.calls) == 2
    assert_complete(operation)


def test_partial_tool_error_changes_only_working_copy_and_safe_resume(operation, monkeypatch):
    def partial(path, *args):
        operation.shift(path)
        raise subprocess.CalledProcessError(1, 'fake exiftool', stderr='partial tool write')
    monkeypatch.setattr(clock, 'exiftool_shift', partial)
    with pytest.raises(subprocess.CalledProcessError):
        operation.run()
    assert all(s['changes'] == 0 for s in image_states(operation))
    with pytest.raises(ValueError, match='Clock correction unfinished'):
        require_no_ingest_holds(operation.dep)
    monkeypatch.setattr(clock, 'exiftool_shift', operation.shift)
    assert operation.run() == 0
    assert_complete(operation)


@pytest.mark.parametrize('retry', [False, True])
def test_correction_refreshes_stale_date_qc_and_preserves_history(operation, retry):
    path = operation.dep / 'session.json'
    session = json.loads(path.read_text())
    session['metadata'].update(deployment_event_start_date='2025-01-01',
                               deployment_event_end_date='2025-01-02')
    path.write_text(json.dumps(session))
    if retry:
        operation.run()
    for check in ('camera_date_review', 'temporal_plausibility'):
        clock.append_qc_report(operation.dep, check, 'p1_ML', 'error', 'Old date finding')
    operation.run()
    report = json.loads((operation.dep / 'qc/qc_report.json').read_text())
    current = {e['check']: e for e in report['current_state'] if e.get('_device') == 'p1_ML'}
    assert current['camera_date_review']['severity'] == 'pass'
    assert current['temporal_plausibility']['severity'] == 'pass'
    assert 'Old date finding' in json.dumps(report['history'])
    assert len(operation.calls) == 2  # Completed retry never shifts again.


@pytest.mark.parametrize('kind', ['outside', 'reset', 'missing_bounds', 'invalid_timestamp'])
def test_date_refresh_does_not_hide_remaining_problems(tmp_path, kind):
    metadata = {'deployment_event_start_date': '2026-04-29', 'deployment_event_end_date': '2026-09-01'}
    entries = [{'device_label': 'p3_SA', 'file_type': 'image',
                'recorded_datetime': '2026-09-01T11:14:07'}]
    if kind == 'outside':
        entries[0]['recorded_datetime'] = '2026-09-05T11:14:07'
    elif kind == 'reset':
        entries *= 3
    elif kind == 'missing_bounds':
        metadata = {}
    else:
        entries[0]['recorded_datetime'] = ''
        with pytest.raises(ValueError):
            clock.refresh_date_qc(tmp_path, {'metadata': metadata, 'file_inventory': entries}, ['p3_SA'])
        return
    clock.refresh_date_qc(tmp_path, {'metadata': metadata, 'file_inventory': entries}, ['p3_SA'])
    report = json.loads((tmp_path / 'qc/qc_report.json').read_text())
    current = {e['check']: e['severity'] for e in report['current_state']}
    assert current['temporal_plausibility'] == 'warning'
    assert current['camera_date_review'] == ('pass' if kind == 'reset' else 'error')


@pytest.mark.parametrize('boundary', ['before_replace', 'after_replace', 'second_image', 'metadata', 'session', 'completion_hold'])
def test_interrupted_operation_resumes_without_double_shift(operation, monkeypatch, boundary):
    real_copy, real_json, real_export, real_qc = clock.atomic_copy, clock.atomic_json, clock.write_metadata_outputs, clock.append_qc_report
    fired = False
    def copy(src, dst):
        nonlocal fired
        if boundary == 'before_replace' and dst == operation.dep / operation.inventory[0]['storage_relpath'] and not fired:
            fired = True
            raise OSError('before replace')
        return real_copy(src, dst)
    def save(path, data):
        nonlocal fired
        if not fired and ((boundary == 'after_replace' and path.parent.name == 'items' and data.get('status') == 'applied')
                          or (boundary == 'session' and path == operation.dep / 'session.json')):
            fired = True
            raise OSError(boundary)
        return real_json(path, data)
    def shift(path, *args):
        nonlocal fired
        if boundary == 'second_image' and len(operation.calls) == 1 and not fired:
            fired = True
            raise KeyboardInterrupt('cancelled')
        return operation.shift(path, *args)
    def export(*args, **kwargs):
        nonlocal fired
        if boundary == 'metadata' and not kwargs.get('dry_run') and not fired:
            fired = True
            raise OSError('metadata stopped after media')
        return real_export(*args, **kwargs)
    def qc(folder, check, device, severity, message, **kwargs):
        nonlocal fired
        if boundary == 'completion_hold' and check == 'ingest_safety' and device == 'clock_correction' and severity == 'pass' and not fired:
            fired = True
            raise OSError('final QC write failed')
        return real_qc(folder, check, device, severity, message, **kwargs)
    monkeypatch.setattr(clock, 'atomic_copy', copy)
    monkeypatch.setattr(clock, 'atomic_json', save)
    monkeypatch.setattr(clock, 'exiftool_shift', shift)
    monkeypatch.setattr(clock, 'write_metadata_outputs', export)
    monkeypatch.setattr(clock, 'append_qc_report', qc)
    with pytest.raises((OSError, KeyboardInterrupt)):
        operation.run('--only-year', '2025')
    assert fired
    assert operation.run('--only-year', '2025') == 0
    assert_complete(operation)
    assert len(operation.calls) == 2


@pytest.mark.parametrize('change', ['session', 'image', 'different_offset', 'new_operation'])
def test_unfinished_operation_refuses_changed_inputs(operation, monkeypatch, change):
    def stop(*args):
        raise OSError('stopped')
    monkeypatch.setattr(clock, 'exiftool_shift', stop)
    with pytest.raises(OSError):
        operation.run()
    if change == 'session':
        (operation.dep / 'session.json').write_text('{}')
    elif change == 'image':
        (operation.dep / operation.inventory[0]['storage_relpath']).write_text('external change')
    monkeypatch.setattr(clock, 'exiftool_shift', operation.shift)
    with pytest.raises((ValueError, KeyError)):
        operation.run(*({'different_offset': ['--hours', '2'], 'new_operation': ['--new-operation', 'second']}.get(change, [])))
    assert not operation.calls


@pytest.mark.parametrize('invalid', ['missing', 'unsafe', 'duplicate', 'symlink', 'untracked', 'unreadable', 'checksum'])
def test_bad_target_sets_never_modify_media(operation, invalid):
    session_path = operation.dep / 'session.json'
    session = json.loads(session_path.read_text())
    first = operation.dep / operation.inventory[0]['storage_relpath']
    if invalid == 'missing':
        first.unlink()
    elif invalid == 'unsafe':
        session['file_inventory'][0]['storage_relpath'] = '../outside.jpg'
    elif invalid == 'duplicate':
        session['file_inventory'].append(dict(session['file_inventory'][0]))
    elif invalid == 'symlink':
        original = first.with_name('original')
        first.rename(original)
        first.symlink_to(original)
    elif invalid == 'untracked':
        first.with_name('extra.jpg').write_bytes(b'untracked')
    elif invalid == 'unreadable':
        first.write_text('{}')
    else:
        session['file_inventory'][0]['file_hash_sha256'] = '0' * 64
    session_path.write_text(json.dumps(session))
    with pytest.raises((ValueError, KeyError)):
        operation.run()
    assert not operation.calls
    assert not clock.operation_home(operation.dep).exists()


def test_zero_match_guard_is_not_success(operation):
    with pytest.raises(ValueError, match='No images match'):
        operation.run('--only-year', '2024')
    assert not operation.calls


def test_scratch_keeps_nested_layout_and_retains_originals(operation, tmp_path):
    scratch = tmp_path / 'fast'
    assert operation.run('--scratch', str(scratch)) == 0
    assert_complete(operation)
    assert list(scratch.iterdir()) == []


def test_shared_fdm_lease_prevents_correction(operation):
    with clock.correction_lock(operation.dep):
        with pytest.raises(ValueError, match='close FDM'):
            operation.run()
    assert not operation.calls


def test_existing_upload_attempt_blocks_local_rewrite(operation):
    (operation.dep / 'qc').mkdir()
    (operation.dep / 'qc/box_upload_manifest.json').write_text('{}')
    with pytest.raises(ValueError, match='Box upload history'):
        operation.run()
    assert not operation.calls


def test_unresolved_ingest_hold_blocks_rewrite(operation):
    clock.append_qc_report(operation.dep, 'ingest_safety', 'p1_ML', 'error', 'source needs review')
    with pytest.raises(ValueError, match='source needs review'):
        operation.run()
    assert not operation.calls


def test_deliberate_second_correction_requires_explicit_new_operation(operation):
    operation.run()
    operation.run()
    assert len(operation.calls) == 2
    operation.run('--new-operation', 'second')
    operation.run('--new-operation', 'second')
    assert all(s['changes'] == 2 for s in image_states(operation))
    assert len(list(clock.operation_home(operation.dep).glob('*/journal.json'))) == 2


def test_changed_completed_metadata_is_not_silently_declared_complete(operation):
    operation.run()
    (operation.dep / 'image_file_metadata.csv').write_text('external change')
    with pytest.raises(ValueError, match='Completed metadata changed'):
        operation.run()
    assert len(operation.calls) == 2


@pytest.mark.parametrize('stdout', ['0 image files updated\n', '2 image files updated\n', '', '1 image files unchanged\n'])
def test_exiftool_requires_exact_updated_count(tmp_path, monkeypatch, stdout):
    # Exercise the real wrapper rather than the synthetic image editor.
    monkeypatch.setattr(clock.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(stdout=stdout, stderr=''))
    with pytest.raises(ValueError, match='exactly one'):
        clock.exiftool_shift(tmp_path / 'image.jpg', '-AllDates+=0:0:0 1:0:0')


def test_exiftool_nonzero_exit_is_not_suppressed(tmp_path, monkeypatch):
    run = MagicMock(side_effect=subprocess.CalledProcessError(1, 'exiftool'))
    monkeypatch.setattr(clock.subprocess, 'run', run)
    with pytest.raises(subprocess.CalledProcessError):
        clock.exiftool_shift(tmp_path / 'image.jpg', '-AllDates+=0:0:0 1:0:0')
    assert run.call_args.kwargs['check'] is True


def test_added_file_during_interruption_requires_review(operation, monkeypatch):
    monkeypatch.setattr(clock, 'exiftool_shift', MagicMock(side_effect=OSError('stopped')))
    with pytest.raises(OSError):
        operation.run()
    (operation.dep / 'raw_data/p1_ML/new.jpg').write_text('{}')
    with pytest.raises(ValueError, match='file set changed'):
        operation.run()
    assert not operation.calls


def test_reported_success_without_changed_bytes_is_rejected(operation, monkeypatch):
    monkeypatch.setattr(clock, 'exiftool_shift', lambda *args: 1)
    with pytest.raises(ValueError, match='did not change'):
        operation.run()
    assert all(s['changes'] == 0 for s in image_states(operation))


def test_only_year_leaves_other_images_and_inventory_unchanged(operation):
    second = operation.dep / operation.inventory[1]['storage_relpath']
    second.write_text(json.dumps({'clock': '2026:01:01 11:00:00', 'changes': 0}))
    session_path = operation.dep / 'session.json'
    session = json.loads(session_path.read_text())
    session['file_inventory'][1]['file_hash_sha256'] = clock.file_digest(second)
    session_path.write_text(json.dumps(session))
    before = second.read_bytes()
    before_row = dict(session['file_inventory'][1])
    operation.run('--only-year', '2025')
    assert len(operation.calls) == 1
    assert second.read_bytes() == before
    assert json.loads(session_path.read_text())['file_inventory'][1] == before_row
    operation.run('--only-year', '2025')
    assert len(operation.calls) == 1


def test_existing_legacy_manifest_refresh_failure_is_recoverable(operation, monkeypatch):
    manifest = operation.dep / 'raw_data/p1_ML/p1_ML_manifest.json'
    manifest.write_text('{}')
    real = clock.refresh_legacy_device_manifest
    monkeypatch.setattr(clock, 'refresh_legacy_device_manifest', lambda *args: False)
    with pytest.raises(ValueError, match='manifest refresh failed'):
        operation.run()
    monkeypatch.setattr(clock, 'refresh_legacy_device_manifest', real)
    assert operation.run() == 0
    assert_complete(operation)
    assert len(operation.calls) == 2
    assert len(json.loads(manifest.read_text())['files']) == 2


def test_resume_does_not_clear_unrelated_ingest_hold(operation, monkeypatch):
    monkeypatch.setattr(clock, 'exiftool_shift', MagicMock(side_effect=OSError('stopped')))
    with pytest.raises(OSError):
        operation.run()
    clock.append_qc_report(operation.dep, 'ingest_safety', 'p2_ML', 'error', 'different source problem')
    with pytest.raises(ValueError, match='different source problem'):
        operation.run()


def test_named_additional_operation_is_safe_to_resume(operation, monkeypatch):
    operation.run()
    real = clock.atomic_json
    fired = False
    def interrupt(path, record):
        nonlocal fired
        if path.parent.name == 'items' and record.get('status') == 'applied' and not fired:
            fired = True
            raise OSError('saved replacement before progress')
        return real(path, record)
    monkeypatch.setattr(clock, 'atomic_json', interrupt)
    with pytest.raises(OSError):
        operation.run('--new-operation', 'second')
    operation.run('--new-operation', 'second')
    operation.run('--new-operation', 'second')
    assert all(s['changes'] == 2 for s in image_states(operation))
    assert len(operation.calls) == 4


def test_unfinished_correction_blocks_even_forced_cleanup(operation, monkeypatch):
    from cassn.core.staging_cleanup import inspect_deployment_for_cleanup
    monkeypatch.setattr(clock, 'exiftool_shift', MagicMock(side_effect=OSError('stopped')))
    with pytest.raises(OSError):
        operation.run()
    plan = inspect_deployment_for_cleanup(operation.dep, operation.dep.parent, None, 'fake-root', force=True)
    assert not plan.clearable
    assert any('Clock correction unfinished' in reason for reason in plan.reasons)


def test_reinventory_uses_complete_flat_and_nested_inventory_paths(operation):
    rows = [dict(row) for row in operation.inventory]
    assert clock.reinventory(rows, operation.dep) == 2
    assert all(row['file_hash_sha1'] and row['recorded_datetime_utc'] for row in rows)
    with pytest.raises(ValueError, match='Invalid legacy'):
        clock.reinventory([{'new_filename': '../outside.jpg'}], operation.dep)
