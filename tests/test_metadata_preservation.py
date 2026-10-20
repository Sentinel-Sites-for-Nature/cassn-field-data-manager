"""Regeneration preserves recorded history without retaining stale file facts."""

import csv
from types import SimpleNamespace

import pytest

from cassn.export import metadata_csv as m


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "snapshot_lookup_tables", lambda _: [])
    placement = {"deployment_start_date": "2026-01-01", "deployment_end_date": "2026-01-02"}
    lookups = SimpleNamespace(
        soundhub_config={}, wi_config={}, deployment_for_id=lambda _: placement,
    )
    metadata = {"deployment_event_id": "event", "site_short_name": "Test"}
    inventory = [
        {"deployment_id": "camera", "device_label": "p1_ML", "device_type": "ML",
         "file_type": "image", "new_filename": "image.jpg", "file_hash_sha256": "a" * 64},
        {"deployment_id": "recorder", "device_label": "p1_BD", "device_type": "BD",
         "file_type": "audio", "new_filename": "audio.wav", "file_hash_sha256": "b" * 64},
    ]

    def generate():
        m.write_metadata_outputs(tmp_path, metadata, inventory, [], lookups)

    generate()
    return tmp_path, inventory, lookups, generate


def read(path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write(path, rows):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)


def add_history(path):
    rows = read(path)
    history = {"notes": "Field note, with comma\nand another line"}
    for field in rows[0]:
        if field.startswith(("is_uploaded_to_", "is_submitted_to_")):
            history[field] = "True"
        elif field.endswith(("_uploader", "_submitter")):
            history[field] = "Test Operator"
        elif field.endswith(("_upload_datetime", "_submission_datetime")):
            history[field] = "2026-01-03T00:00:00Z"
    rows[0].update(history)
    write(path, rows)
    return history


def test_regeneration_preserves_history_and_refreshes_generated_fields(deployment):
    root, inventory, lookups, generate = deployment
    paths = [root / f"{kind}_file_metadata.csv" for kind in ("image", "audio")]
    histories = [add_history(path) for path in paths]
    inventory[0].update(recorded_datetime_utc="2026-01-01T20:00:00Z", latitude="35.2")
    lookups.wi_config["bait_type_ML"] = "new bait"
    for _ in range(2):
        generate()
        for path, history in zip(paths, histories):
            row = read(path)[0]
            assert {key: row[key] for key in history} == history
        image = read(paths[0])[0]
        assert image["latitude"] == "35.2"
        assert image["recorded_datetime_utc"] == "2026-01-01T20:00:00Z"
        assert image["bait_type"] == "new bait"


@pytest.mark.parametrize("change", ["hash", "missing_hash", "invalid_hash", "filename", "deployment", "removed"])
@pytest.mark.parametrize("kind,index", [("image", 0), ("audio", 1)])
def test_conflicting_history_leaves_both_csvs_untouched(deployment, change, kind, index):
    root, inventory, _, generate = deployment
    target = root / f"{kind}_file_metadata.csv"
    add_history(target)
    before = {p: p.read_bytes() for p in root.glob("*_file_metadata.csv")}
    if change == "hash":
        inventory[index]["file_hash_sha256"] = "c" * 64
    elif change == "missing_hash":
        inventory[index].pop("file_hash_sha256")
    elif change == "invalid_hash":
        inventory[index]["file_hash_sha256"] = "invalid"
    elif change == "filename":
        inventory[index]["new_filename"] = "renamed"
    elif change == "deployment":
        inventory[index]["deployment_id"] = "different"
    else:
        inventory.pop(index)
    with pytest.raises(ValueError, match=target.name):
        generate()
    assert {p: p.read_bytes() for p in before} == before


def test_new_file_does_not_inherit_history_from_identical_content(deployment):
    root, inventory, _, generate = deployment
    path = root / "image_file_metadata.csv"
    history = add_history(path)
    inventory.append(dict(inventory[0], new_filename="second.jpg"))
    generate()
    first, second = read(path)
    assert first["notes"] == history["notes"]
    assert second["notes"] == ""
    assert second["is_uploaded_to_box"] == "False"


@pytest.mark.parametrize("field,value", [("notes", "note"), ("box_uploader", "operator"),
                                         ("is_uploaded_to_box", "True")])
def test_legacy_missing_hash_with_any_history_is_preserved_by_refusal(deployment, field, value):
    root, _, _, generate = deployment
    path = root / "image_file_metadata.csv"
    rows = read(path)
    rows[0].pop("file_hash_sha256")
    rows[0][field] = value
    write(path, rows)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="image.jpg"):
        generate()
    assert path.read_bytes() == before


def test_legacy_without_history_can_be_regenerated(deployment):
    root, inventory, _, generate = deployment
    path = root / "image_file_metadata.csv"
    rows = read(path)
    rows[0].pop("file_hash_sha256")
    write(path, rows)
    generate()
    assert read(path)[0]["file_hash_sha256"] == inventory[0]["file_hash_sha256"]


@pytest.mark.parametrize("where", ["existing", "generated"])
def test_duplicate_identity_is_rejected_before_writing(deployment, where):
    root, inventory, _, generate = deployment
    path = root / "audio_file_metadata.csv"
    if where == "existing":
        rows = read(path)
        write(path, rows + rows)
    else:
        inventory.append(dict(inventory[1]))
    before = {p: p.read_bytes() for p in root.glob("*_file_metadata.csv")}
    with pytest.raises(ValueError, match="Duplicate"):
        generate()
    assert {p: p.read_bytes() for p in before} == before


def test_malformed_existing_csv_is_not_overwritten(deployment):
    root, _, _, generate = deployment
    path = root / "audio_file_metadata.csv"
    path.write_text('filename,filename\n"unterminated')
    before = {p: p.read_bytes() for p in root.glob("*_file_metadata.csv")}
    with pytest.raises(ValueError, match="audio_file_metadata.csv"):
        generate()
    assert {p: p.read_bytes() for p in before} == before


def test_failed_second_csv_serialization_preserves_originals(deployment, monkeypatch):
    root, _, _, generate = deployment
    before = {p: p.read_bytes() for p in root.glob("*_file_metadata.csv")}
    original = csv.DictWriter.writerows

    def fail_audio(writer, rows):
        if "is_submitted_to_soundhub" in writer.fieldnames:
            raise OSError("synthetic disk failure")
        return original(writer, rows)

    monkeypatch.setattr(csv.DictWriter, "writerows", fail_audio)
    with pytest.raises(OSError, match="synthetic disk failure"):
        generate()
    assert {p: p.read_bytes() for p in before} == before
    assert not list(root.glob(".*.tmp"))


def test_changed_file_without_history_can_be_regenerated(deployment):
    root, inventory, _, generate = deployment
    inventory[0]["file_hash_sha256"] = "c" * 64
    generate()
    row = read(root / "image_file_metadata.csv")[0]
    assert row["file_hash_sha256"] == "c" * 64
    assert row["is_uploaded_to_box"] == "False"


def test_false_status_and_uppercase_hash_are_preserved(deployment):
    root, _, _, generate = deployment
    path = root / "image_file_metadata.csv"
    rows = read(path)
    rows[0].update(file_hash_sha256="A" * 64, is_uploaded_to_box="false", notes="note")
    write(path, rows)
    generate()
    row = read(path)[0]
    assert row["is_uploaded_to_box"] == "false"
    assert row["notes"] == "note"
    assert row["file_hash_sha256"] == "a" * 64


def test_failed_replace_does_not_truncate_originals(deployment, monkeypatch):
    root, _, _, generate = deployment
    before = {p: p.read_bytes() for p in root.glob("*_file_metadata.csv")}

    def fail_replace(*_args):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(m.os, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic replace failure"):
        generate()
    assert {p: p.read_bytes() for p in before} == before
    assert not list(root.glob(".*.tmp"))


def test_dry_run_does_not_write_any_artifact(deployment):
    root, inventory, lookups, _ = deployment
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    m.write_metadata_outputs(
        root, {'deployment_event_id': 'event', 'site_short_name': 'Test'},
        inventory, [], lookups, dry_run=True,
    )
    assert {p: p.read_bytes() for p in root.rglob('*') if p.is_file()} == before


def test_clock_repair_rejects_existing_history_before_changing_images(deployment, monkeypatch):
    import json
    from utils import fix_camera_clock

    root, inventory, lookups, _ = deployment
    add_history(root / 'image_file_metadata.csv')
    image = root / 'raw_data/p1_ML/image.jpg'
    image.parent.mkdir(parents=True)
    image.write_bytes(b'original image')
    session = root / 'session.json'
    session.write_text(json.dumps({
        'metadata': {'deployment_event_id': 'event', 'site_short_name': 'Test'},
        'file_inventory': inventory, 'devices': [],
    }))
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    monkeypatch.setattr(fix_camera_clock.LookupTables, 'load', lambda _: lookups)
    monkeypatch.setattr(fix_camera_clock, 'exiftool_shift', lambda *_: pytest.fail('Image rewrite was attempted'))
    monkeypatch.setattr('sys.argv', ['fix_camera_clock.py', '--deployment', str(root),
                                    '--devices', 'p1_ML', '--hours', '1'])
    with pytest.raises(ValueError, match='cannot preserve history'):
        fix_camera_clock.main()
    assert {p: p.read_bytes() for p in root.rglob('*') if p.is_file()} == before


def test_clock_repair_without_history_regenerates_consistent_metadata(deployment, monkeypatch):
    import hashlib
    import json
    from utils import fix_camera_clock

    root, inventory, lookups, _ = deployment
    image = root / 'raw_data/p1_ML/image.jpg'
    image.parent.mkdir(parents=True)
    image.write_bytes(b'original image')
    session = root / 'session.json'
    session.write_text(json.dumps({
        'metadata': {'deployment_event_id': 'event', 'site_short_name': 'Test'},
        'file_inventory': inventory, 'devices': [],
    }))
    monkeypatch.setattr(fix_camera_clock.LookupTables, 'load', lambda _: lookups)

    def shift(*_):
        image.write_bytes(b'corrected test image')
        return 1

    monkeypatch.setattr(fix_camera_clock, 'exiftool_shift', shift)
    monkeypatch.setattr(fix_camera_clock, 'extract_exif_data', lambda _: (
        {'DateTimeOriginal': '2026:01:01 12:00:00', 'OffsetTimeOriginal': '-08:00'}, {}))
    monkeypatch.setattr('sys.argv', ['fix_camera_clock.py', '--deployment', str(root),
                                    '--devices', 'p1_ML', '--hours', '1'])
    assert fix_camera_clock.main() == 0
    saved = json.loads(session.read_text())['file_inventory'][0]
    exported = read(root / 'image_file_metadata.csv')[0]
    assert saved['file_hash_sha256'] == exported['file_hash_sha256'] == hashlib.sha256(image.read_bytes()).hexdigest()
    assert saved['recorded_datetime_utc'] == exported['recorded_datetime_utc'] == '2026-01-01T20:00:00Z'
    assert exported['is_uploaded_to_box'] == 'False'
