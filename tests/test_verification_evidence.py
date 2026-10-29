"""Readiness must be supported by current, durable evidence, never history alone."""
import json
from types import SimpleNamespace

import pytest

from cassn.core.verification_evidence import readiness, verification_context, write_result
from cassn.gui.wizard import FieldDataWizard


@pytest.fixture
def batch(tmp_path):
    inventory = [{'device_label': 'p1_ML', 'new_filename': 'image.jpg',
                  'file_hash_sha256': 'a' * 64, 'file_size_bytes': 7}]
    metadata = {'site_name': 'Test', 'deployment_event_end_date': '2026-01-02'}
    config = SimpleNamespace(field_data_folder_id='test-root', profile='uc')
    return tmp_path, inventory, metadata, config


def passed(batch):
    folder, *args = batch
    context = verification_context(*batch)
    for kind in ('box', 'hash'):
        write_result(folder, kind, context, ok=True, summary=f'{kind} passed')
    assert readiness(folder, *args) == (True, True)
    return context


def test_success_reopens_and_sha1_backfill_does_not_invalidate(batch):
    passed(batch)
    folder, inventory, metadata, config = batch
    inventory[0]['file_hash_sha1'] = 'b' * 40
    assert readiness(folder, json.loads(json.dumps(inventory)), dict(metadata), config) == (True, True)


@pytest.mark.parametrize('kind', ['hash', 'box'])
@pytest.mark.parametrize('outcome', ['failure', 'cancelled', 'pending', 'issues'])
def test_rerun_revokes_older_pass_even_without_issue_details(batch, kind, outcome):
    context = passed(batch)
    write_result(batch[0], kind, context, ok=outcome == 'issues',
                 summary=outcome, pending=outcome == 'pending',
                 issues=[{'type': 'mismatch', 'filename': 'image.jpg'}] if outcome == 'issues' else [])
    hash_ok, upload_ok = readiness(*batch)
    assert not upload_ok
    if kind == 'hash':
        assert not hash_ok
    assert json.loads((batch[0] / 'qc/box_upload_verification.json').read_text())['verified'] is False


@pytest.mark.parametrize('change', ['inventory', 'size', 'path', 'event', 'destination', 'profile', 'folder', 'publication'])
def test_changed_context_cannot_reuse_old_pass(batch, change, tmp_path):
    passed(batch)
    folder, inventory, metadata, config = batch
    if change == 'inventory':
        inventory[0]['file_hash_sha256'] = 'c' * 64
    elif change == 'size':
        inventory[0]['file_size_bytes'] = 8
    elif change == 'path':
        inventory[0]['storage_relpath'] = 'raw_data/p1_ML/p1_ML_1/image.jpg'
    elif change == 'event':
        metadata['deployment_event_end_date'] = '2026-01-03'
    elif change == 'destination':
        config.field_data_folder_id = 'another-root'
    elif change == 'profile':
        config.profile = 'csu'
    elif change == 'folder':
        import shutil
        other = folder / 'another-copy'
        shutil.copytree(folder / 'qc', other / 'qc')
        folder = other
    else:
        (folder / 'qc/box_publication_state.json').write_text(json.dumps(
            {'status': 'published', 'attempt_id': 'new-attempt'}))
    assert readiness(folder, inventory, metadata, config) == (False, False)


@pytest.mark.parametrize('artifact', ['file_hash_verification.json', 'box_file_list_verification.json', 'box_upload_verification.json'])
@pytest.mark.parametrize('content', ['{broken', '[]', '{"verified":true}', '{"context":null,"ok":true}'])
def test_corrupt_and_legacy_evidence_fail_closed(batch, artifact, content):
    passed(batch)
    (batch[0] / 'qc' / artifact).write_text(content)
    assert readiness(*batch)[1] is False


def test_manifest_and_in_session_flags_are_not_readiness_proof(batch):
    folder, inventory, metadata, config = batch
    (folder / 'qc').mkdir()
    (folder / 'qc/box_upload_manifest.json').write_text('{"file_count":1}')
    (folder / 'qc/qc_report.json').write_text(json.dumps({'current_state': [
        {'check': 'file_hash_verification_run', 'severity': 'pass'}]}))
    context = SimpleNamespace(current_deployment_folder=folder, file_inventory=inventory,
                              metadata=metadata, box_config=config, fixity_check_run=True,
                              box_upload_complete=True,
                              device_tree=SimpleNamespace(topLevelItemCount=lambda: 0))
    items = FieldDataWizard._build_checklist_items(context)
    assert not next(i['ok'] for i in items if i['label'] == 'Hash verification passed')
    assert not next(i['ok'] for i in items if i['label'] == 'Box upload completed')


@pytest.mark.parametrize('inventory', [[], [{'new_filename': 'image.jpg'}]])
def test_empty_or_incomplete_inventory_cannot_be_bound(batch, inventory):
    folder, _, metadata, config = batch
    with pytest.raises(ValueError):
        verification_context(folder, inventory, metadata, config)


def test_unpublished_or_unreadable_publication_state_blocks_pass(batch):
    passed(batch)
    for content in ('{"status":"awaiting_provenance"}', '{bad', '[]'):
        (batch[0] / 'qc/box_publication_state.json').write_text(content)
        assert readiness(*batch) == (False, False)


def test_write_failure_first_invalidates_combined_record(batch, monkeypatch):
    from cassn.core import verification_evidence as evidence
    context = passed(batch)
    real_write = evidence.atomic_json
    def fail_result(path, record):
        if path.name == 'file_hash_verification.json':
            raise OSError('synthetic disk failure')
        return real_write(path, record)
    monkeypatch.setattr(evidence, 'atomic_json', fail_result)
    with pytest.raises(OSError):
        write_result(batch[0], 'hash', context, pending=True)
    assert readiness(*batch)[1] is False
