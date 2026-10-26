"""Concurrency safety for background SD-card ingestion."""

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from cassn.core.ingest_safety import IngestSafetyError, source_evidence, require_no_ingest_holds
from cassn.core.quality_control import append_qc_report
from cassn.core.image_metadata import _parse_reconyx_tags
from cassn.core.wi_split import event_key
from cassn.gui import card_ingest_thread
from cassn.gui.card_ingest_thread import CardIngestThread, IngestHashRegistry
from cassn.gui.wizard import FieldDataWizard


class _DummyReconyx:
    def start(self):
        return self

    def close(self):
        return None

    def parse(self, _path):
        return {}


class _FakeLookups:
    soundhub_config = {}
    plot_metadata = {
        ("Test", 1): {"elevation_m": "1"},
        ("Test", 2): {"elevation_m": "2"},
    }

    def active_deployment_for_label(self, label):
        plot = label[1]
        return {
            "deployment_id": f"UC_Test_plot{plot}_ML_20260101",
            "deployment_start_date": "2026-01-01",
            "deployment_end_date": "2026-01-02",
            "device_id": f"CAM{plot}",
        }


class _AudioLookups(_FakeLookups):
    def active_deployment_for_label(self, label):
        plot = label[1]
        return {
            "deployment_id": f"UC_Test_plot{plot}_BD_20260101",
            "deployment_start_date": "2026-01-01",
            "deployment_end_date": "2026-01-02",
            "device_id": f"AUDIO{plot}",
        }


class _ProcessContext:
    def __init__(self, root, registry, lookups=None):
        self.current_deployment_folder = root
        self.metadata = {"site_short_name": "Test"}
        self.lookups = lookups or _FakeLookups()
        self.file_inventory = []
        self._last_session_save = time.monotonic()
        self.registry = registry

    def log(self, _message):
        return None

    def save_session(self):
        return True

    def _reserve_ingest_hash(self, file_hash, file_type):
        return self.registry.reserve(file_hash, file_type)

    def _release_ingest_hash(self, file_hash, file_type):
        self.registry.release(file_hash, file_type)

    def _append_ingest_qc(self, *args):
        append_qc_report(*args)

    def _ingest_cancelled(self):
        return False

    def _ingest_progress(self, _copied, _filename):
        return None


class _BurstReconyx(_DummyReconyx):
    def parse(self, path):
        position = (int(path.stem[-4:]) - 1) % 3 + 1
        return _parse_reconyx_tags({
            "MakerNotes:Sequence": f"{position} 3",
            "MakerNotes:TriggerMode": "Motion Detection",
        })


def _burst_card(tmp_path, monkeypatch, metadata_path="exiftool"):
    from cassn.gui import wizard

    source = tmp_path / "card"
    source.mkdir()
    for number in range(1, 7):
        (source / f"RCNX{number:04d}.JPG").write_bytes(
            f"synthetic unique frame {number}".encode()
        )

    def exif(path):
        position = (int(path.stem[-4:]) - 1) % 3 + 1
        note = bytearray(43)
        note[40:43] = bytes([ord("M"), position, 3])
        return {}, {0x927C: bytes(note)}

    monkeypatch.setattr(wizard, "EXIF_AVAILABLE", metadata_path == "makernote")
    monkeypatch.setattr(wizard, "extract_exif_data", exif)
    return source, _DummyReconyx() if metadata_path == "makernote" else _BurstReconyx()


def _ingest_camera(context, source, reconyx):
    destination = context.current_deployment_folder / "raw_data/p1_ML"
    destination.mkdir(parents=True, exist_ok=True)
    return FieldDataWizard.process_sd_card_files(
        context, source, destination, 1, "Plot 1", "ML", "p1_ML", reconyx
    )


def _assert_camera_inventory_matches(rows, expected, deployment, source):
    fields = (
        "source_relpath", "new_filename", "storage_relpath",
        "sequence_event_num", "sequence_position", "sequence_total",
        "file_hash_sha256", "file_hash_sha1",
    )
    assert [tuple(row[key] for key in fields) for row in rows] == [
        tuple(row[key] for key in fields) for row in expected
    ]
    assert len({event_key(row["new_filename"]) for row in rows}) == 2
    for row in rows:
        contents = (deployment / row["storage_relpath"]).read_bytes()
        assert contents == (source / row["source_relpath"]).read_bytes()
        assert hashlib.sha256(contents).hexdigest() == row["file_hash_sha256"]
        assert hashlib.sha1(contents).hexdigest() == row["file_hash_sha1"]


@pytest.mark.parametrize("metadata_path", ["exiftool", "makernote"])
@pytest.mark.parametrize("saved_count", [1, 2, 3, 4, 5])
@pytest.mark.parametrize("staged_state", ["healthy", "missing", "truncated", "orphan_tail"])
def test_burst_resume_matches_uninterrupted_ingest(
    tmp_path, monkeypatch, metadata_path, saved_count, staged_state
):
    source, reconyx = _burst_card(tmp_path, monkeypatch, metadata_path)
    control = _ProcessContext(tmp_path / "control", IngestHashRegistry())
    _ingest_camera(control, source, reconyx)
    assert [row["sequence_event_num"] for row in control.file_inventory] == [1, 1, 1, 2, 2, 2]

    deployment = tmp_path / "resumed"
    context = _ProcessContext(deployment, IngestHashRegistry())
    context._ingest_cancelled = lambda: len(context.file_inventory) >= saved_count
    with pytest.raises(InterruptedError, match="cancelled"):
        _ingest_camera(context, source, reconyx)
    session = deployment / "session.json"
    session.write_text(json.dumps(context.file_inventory))
    saved_rows = json.loads(session.read_text())
    last_staged = deployment / saved_rows[-1]["storage_relpath"]
    if staged_state == "missing":
        last_staged.unlink()
    elif staged_state == "truncated":
        last_staged.write_bytes(b"x")
    elif staged_state == "orphan_tail":
        # A crash after copying the next frame but before saving its inventory.
        next_row = control.file_inventory[saved_count]
        (deployment / next_row["storage_relpath"]).write_bytes(
            (source / next_row["source_relpath"]).read_bytes()
        )

    context = _ProcessContext(deployment, IngestHashRegistry(
        row["file_hash_sha256"] for row in saved_rows
    ))
    context.file_inventory = json.loads(session.read_text())
    if staged_state == "orphan_tail":
        before = {p: p.read_bytes() for p in (deployment / "raw_data").rglob("*.jpg")}
        with pytest.raises(IngestSafetyError, match="unrecorded staged"):
            _ingest_camera(context, source, reconyx)
        assert all(p.read_bytes() == data for p, data in before.items())
        assert context.file_inventory == saved_rows
        with pytest.raises(IngestSafetyError):
            require_no_ingest_holds(deployment)
        return
    _ingest_camera(context, source, reconyx)
    assert context.file_inventory[:saved_count] == saved_rows
    _assert_camera_inventory_matches(
        context.file_inventory, control.file_inventory, deployment, source
    )
    assert _ingest_camera(context, source, reconyx) == (0, 0, 0)
    assert len(context.file_inventory) == 6
    assert len(list((deployment / "raw_data/p1_ML").glob("*.jpg"))) == 6


def test_cancelled_burst_worker_checkpoint_resumes_same_events(tmp_path, monkeypatch):
    source, reconyx = _burst_card(tmp_path, monkeypatch)
    monkeypatch.setattr(card_ingest_thread, "ReconyxExtractor", _BurstReconyx)
    control = _ProcessContext(tmp_path / "control", IngestHashRegistry())
    _ingest_camera(control, source, reconyx)
    deployment = tmp_path / "resumed"

    def worker(inventory):
        return CardIngestThread(
            processor=FieldDataWizard.process_sd_card_files,
            source_dir=source, deployment_folder=deployment,
            plot_num=1, plot_label="Plot 1", device_code="ML", device_label="p1_ML",
            metadata={"site_short_name": "Test"}, lookups=_FakeLookups(),
            inventory=inventory,
            hash_registry=IngestHashRegistry(row["file_hash_sha256"] for row in inventory),
        )

    first = worker([])
    checkpoints, results = [], []
    first.rows_ready.connect(lambda _label, rows: checkpoints.extend(rows))
    first.completed.connect(lambda _label, result: results.append(result))
    first.progress.connect(lambda _label, count, *_args: first.cancel() if count == 2 else None)
    first.run()
    assert results[0]["cancelled"]
    assert len(checkpoints) == 2
    session = deployment / "session.json"
    session.write_text(json.dumps(checkpoints))
    saved_rows = json.loads(session.read_text())

    second = worker(saved_rows)
    resumed_rows, results = list(saved_rows), []
    second.rows_ready.connect(lambda _label, rows: resumed_rows.extend(rows))
    second.completed.connect(lambda _label, result: results.append(result))
    second.run()
    assert results[0]["ok"]
    _assert_camera_inventory_matches(resumed_rows, control.file_inventory, deployment, source)


@pytest.mark.parametrize("missing_event", [None, ""])
def test_resume_without_saved_burst_event_keeps_fallback(tmp_path, monkeypatch, missing_event):
    source, reconyx = _burst_card(tmp_path, monkeypatch)
    context = _ProcessContext(tmp_path / "event", IngestHashRegistry())
    context._ingest_cancelled = lambda: len(context.file_inventory) >= 1
    with pytest.raises(InterruptedError):
        _ingest_camera(context, source, reconyx)
    context.file_inventory[0]["sequence_event_num"] = missing_event
    saved = dict(context.file_inventory[0])
    context._ingest_cancelled = lambda: False
    _ingest_camera(context, source, reconyx)
    assert context.file_inventory[0] == saved
    assert [row["sequence_event_num"] for row in context.file_inventory[1:]] == [1, 1, 2, 2, 2]


def test_burst_resume_ignores_other_device_event_numbers(tmp_path, monkeypatch):
    source, reconyx = _burst_card(tmp_path, monkeypatch)
    context = _ProcessContext(tmp_path / "event", IngestHashRegistry())
    other_device = {
        "device_label": "p2_ML", "source_relpath": "RCNX0001.JPG",
        "new_filename": "UC_Test_plot2_ML_20260101_00099_1.jpg",
        "sequence_event_num": 99,
    }
    context.file_inventory = [other_device]
    context._ingest_cancelled = lambda: len(context.file_inventory) >= 2
    with pytest.raises(InterruptedError):
        _ingest_camera(context, source, reconyx)
    context._ingest_cancelled = lambda: False
    _ingest_camera(context, source, reconyx)
    assert context.file_inventory[0] == other_device
    assert [row["sequence_event_num"] for row in context.file_inventory[1:]] == [1, 1, 1, 2, 2, 2]


@pytest.mark.parametrize("device_code,extension", [("ML", "JPG"), ("BD", "WAV")])
def test_plain_media_resume_keeps_sequential_names(tmp_path, monkeypatch, device_code, extension):
    from cassn.gui import wizard

    monkeypatch.setattr(wizard, "EXIF_AVAILABLE", False)
    source = tmp_path / "card"
    source.mkdir()
    for number in range(3):
        (source / f"20260101_00000{number}.{extension}").write_bytes(f"plain {number}".encode())
    lookups = _AudioLookups() if device_code == "BD" else _FakeLookups()

    def ingest(context):
        destination = context.current_deployment_folder / "raw_data" / f"p1_{device_code}"
        destination.mkdir(parents=True, exist_ok=True)
        return FieldDataWizard.process_sd_card_files(
            context, source, destination, 1, "Plot 1", device_code,
            f"p1_{device_code}", _DummyReconyx(),
        )

    control = _ProcessContext(tmp_path / "control", IngestHashRegistry(), lookups)
    ingest(control)
    partial = _ProcessContext(tmp_path / "resumed", IngestHashRegistry(), lookups)
    partial._ingest_cancelled = lambda: len(partial.file_inventory) >= 1
    with pytest.raises(InterruptedError):
        ingest(partial)
    saved = json.loads(json.dumps(partial.file_inventory))
    resumed = _ProcessContext(partial.current_deployment_folder, IngestHashRegistry(
        row["file_hash_sha256"] for row in saved
    ), lookups)
    resumed.file_inventory = saved
    assert ingest(resumed) == (2, 0, 0)
    assert [row["new_filename"] for row in resumed.file_inventory] == [
        row["new_filename"] for row in control.file_inventory
    ]
    assert all(row["sequence_event_num"] == "" for row in resumed.file_inventory)
    assert ingest(resumed) == (0, 0, 0)


def test_hash_registry_allows_only_one_concurrent_reservation():
    registry = IngestHashRegistry()
    barrier = threading.Barrier(12)

    def reserve_once():
        barrier.wait()
        return registry.reserve("same-hash", "image")

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _index: reserve_once(), range(12)))

    assert results.count(True) == 1
    assert results.count(False) == 11


def test_config_hashes_are_not_deduplicated():
    registry = IngestHashRegistry()

    assert registry.reserve("same-hash", "config")
    assert registry.reserve("same-hash", "config")


def test_concurrent_qc_writes_preserve_every_history_entry(tmp_path):
    def write_device(device_number):
        label = f"p{device_number}_ML"
        for sequence in range(15):
            append_qc_report(
                tmp_path,
                "hash_verification",
                label,
                "pass",
                f"entry {sequence}",
            )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write_device, range(1, 5)))

    report = json.loads((tmp_path / "qc" / "qc_report.json").read_text())
    devices = report["history"]["devices"]
    assert set(devices) == {"p1_ML", "p2_ML", "p3_ML", "p4_ML"}
    assert all(len(entries) == 15 for entries in devices.values())


def test_card_worker_emits_checkpoint_and_success(tmp_path, monkeypatch):
    monkeypatch.setattr(card_ingest_thread, "ReconyxExtractor", _DummyReconyx)
    source = tmp_path / "card"
    source.mkdir()
    (source / "one.jpg").write_bytes(b"one")
    (source / "two.jpg").write_bytes(b"two")

    def processor(context, *_args):
        destination = context.current_deployment_folder / "raw_data/p1_ML"
        (destination / "one.jpg").write_bytes(b"one")
        context.file_inventory.append(
            {
                "device_label": "p1_ML",
                "new_filename": "one.jpg",
                "storage_relpath": "raw_data/p1_ML/one.jpg",
                "file_hash_sha256": "hash-one",
                "file_type": "image",
            }
        )
        context.save_session()
        (destination / "two.jpg").write_bytes(b"two")
        context.file_inventory.append(
            {
                "device_label": "p1_ML",
                "new_filename": "two.jpg",
                "storage_relpath": "raw_data/p1_ML/two.jpg",
                "file_hash_sha256": "hash-two",
                "file_type": "image",
            }
        )
        return 2, 0, 0

    worker = CardIngestThread(
        processor=processor,
        source_dir=source,
        deployment_folder=tmp_path / "event",
        plot_num=1,
        plot_label="Plot 1",
        device_code="ML",
        device_label="p1_ML",
        metadata={},
        lookups=object(),
        inventory=[],
        hash_registry=IngestHashRegistry(),
    )
    checkpoints = []
    results = []
    worker.rows_ready.connect(lambda _label, rows: checkpoints.extend(rows))
    worker.completed.connect(lambda _label, result: results.append(result))

    worker.run()

    assert [row["new_filename"] for row in checkpoints] == ["one.jpg", "two.jpg"]
    assert results == [{
        "ok": True,
        "cancelled": False,
        "expected_file_count": 2,
        "files_copied": 2,
        "duplicates": 0,
        "hash_mismatches": 0,
        "source_dir": str(source),
    }]


def test_card_worker_emits_repaired_existing_row(tmp_path, monkeypatch):
    monkeypatch.setattr(card_ingest_thread, "ReconyxExtractor", _DummyReconyx)
    source = tmp_path / "card"
    source.mkdir()
    existing = {
        "device_label": "p1_BD",
        "new_filename": "recording.wav",
        "storage_relpath": "raw_data/p1_BD/recording.wav",
        "file_hash_sha256": "hash-one",
        "file_type": "audio",
        "recording_duration_sec": "",
    }

    def processor(context, *_args):
        context.file_inventory[0]["recording_duration_sec"] = 32400
        context.save_session()
        return 0, 0, 0

    worker = CardIngestThread(
        processor=processor,
        source_dir=source,
        deployment_folder=tmp_path / "event",
        plot_num=1,
        plot_label="Plot 1",
        device_code="BD",
        device_label="p1_BD",
        metadata={},
        lookups=object(),
        inventory=[existing],
        hash_registry=IngestHashRegistry(),
    )
    checkpoints = []
    worker.rows_ready.connect(lambda _label, rows: checkpoints.extend(rows))

    worker.run()

    assert len(checkpoints) == 1
    assert checkpoints[0]["recording_duration_sec"] == 32400


def test_cancelled_card_worker_reports_retryable_state(tmp_path, monkeypatch):
    monkeypatch.setattr(card_ingest_thread, "ReconyxExtractor", _DummyReconyx)
    source = tmp_path / "card"
    source.mkdir()
    (source / "one.jpg").write_bytes(b"one")
    processor_called = False

    def processor(*_args):
        nonlocal processor_called
        processor_called = True
        return 0, 0, 0

    worker = CardIngestThread(
        processor=processor,
        source_dir=source,
        deployment_folder=tmp_path / "event",
        plot_num=1,
        plot_label="Plot 1",
        device_code="ML",
        device_label="p1_ML",
        metadata={},
        lookups=object(),
        inventory=[],
        hash_registry=IngestHashRegistry(),
    )
    results = []
    worker.completed.connect(lambda _label, result: results.append(result))
    worker.cancel()

    worker.run()

    assert not processor_called
    assert results[0]["ok"] is False
    assert results[0]["cancelled"] is True


def test_failed_card_worker_does_not_report_success(tmp_path, monkeypatch):
    monkeypatch.setattr(card_ingest_thread, "ReconyxExtractor", _DummyReconyx)
    source = tmp_path / "card"
    source.mkdir()
    (source / "one.jpg").write_bytes(b"one")

    registry = IngestHashRegistry()

    def processor(context, *_args):
        assert context._reserve_ingest_hash("uncommitted-hash", "image")
        raise OSError("reader disconnected")

    worker = CardIngestThread(
        processor=processor,
        source_dir=source,
        deployment_folder=tmp_path / "event",
        plot_num=1,
        plot_label="Plot 1",
        device_code="ML",
        device_label="p1_ML",
        metadata={},
        lookups=object(),
        inventory=[],
        hash_registry=registry,
    )
    results = []
    worker.completed.connect(lambda _label, result: results.append(result))

    worker.run()

    assert results[0]["ok"] is False
    assert results[0]["cancelled"] is False
    assert "reader disconnected" in results[0]["error"]
    assert registry.reserve("uncommitted-hash", "image")


def test_disconnected_source_reports_retryable_explanation(tmp_path, monkeypatch):
    monkeypatch.setattr(card_ingest_thread, "ReconyxExtractor", _DummyReconyx)
    source = tmp_path / "card"
    source.mkdir()

    def processor(*_args):
        source.rmdir()
        raise FileNotFoundError("source file vanished")

    worker = CardIngestThread(
        processor=processor,
        source_dir=source,
        deployment_folder=tmp_path / "event",
        plot_num=1,
        plot_label="Plot 1",
        device_code="ML",
        device_label="p1_ML",
        metadata={},
        lookups=object(),
        inventory=[],
        hash_registry=IngestHashRegistry(),
    )
    results = []
    worker.completed.connect(lambda _label, result: results.append(result))

    worker.run()

    assert results[0]["ok"] is False
    assert "Source card disconnected or unmounted" in results[0]["error"]
    assert "completed files have been saved" in results[0]["error"]


def test_two_device_copy_engines_run_concurrently_without_state_collisions(
    tmp_path, monkeypatch
):
    import cassn.gui.wizard as wizard_module

    monkeypatch.setattr(wizard_module, "EXIF_AVAILABLE", False)
    event = tmp_path / "event"
    event.mkdir()
    registry = IngestHashRegistry()
    barrier = threading.Barrier(2)

    def run_device(plot):
        source = tmp_path / f"card{plot}"
        source.mkdir()
        (source / f"image{plot}.jpg").write_bytes(f"unique-{plot}".encode())
        destination = event / "raw_data" / f"p{plot}_ML"
        destination.mkdir(parents=True)
        context = _ProcessContext(event, registry)
        barrier.wait()
        result = FieldDataWizard.process_sd_card_files(
            context,
            source,
            destination,
            plot,
            f"Plot {plot}",
            "ML",
            f"p{plot}_ML",
            _DummyReconyx(),
        )
        return result, context.file_inventory

    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs = list(pool.map(run_device, (1, 2)))

    assert [result for result, _rows in outputs] == [(1, 0, 0), (1, 0, 0)]
    rows = [row for _result, device_rows in outputs for row in device_rows]
    assert {row["device_label"] for row in rows} == {"p1_ML", "p2_ML"}
    assert len(list((event / "raw_data").rglob("*.jpg"))) == 2
    # Both worker threads also wrote their aggregate findings into one valid,
    # lossless QC report.
    report = json.loads((event / "qc" / "qc_report.json").read_text())
    assert set(report["history"]["devices"]) == {"p1_ML", "p2_ML"}


def test_audio_metadata_is_read_from_verified_destination(tmp_path, monkeypatch):
    import cassn.gui.wizard as wizard_module

    event = tmp_path / "event"
    event.mkdir()
    source = tmp_path / "card"
    source.mkdir()
    (source / "20260510_000000.WAV").write_bytes(b"audio bytes")
    destination = event / "raw_data" / "p1_BD"
    destination.mkdir(parents=True)
    seen_paths = []

    def comment(path):
        seen_paths.append(path)
        return {
            "recording_duration_sec": 32400,
            "sample_rate_hz": "48000",
            "gain_setting": "High",
        }

    def guano(path):
        seen_paths.append(path)
        return {"recorded_datetime": "2026-05-10T00:00:00-08:00"}

    monkeypatch.setattr(wizard_module, "parse_audiomoth_wav_comment", comment)
    monkeypatch.setattr(wizard_module, "parse_audiomoth_guano", guano)
    context = _ProcessContext(event, IngestHashRegistry(), _AudioLookups())

    result = FieldDataWizard.process_sd_card_files(
        context,
        source,
        destination,
        1,
        "Plot 1",
        "BD",
        "p1_BD",
        _DummyReconyx(),
    )

    assert result == (1, 0, 0)
    assert len(seen_paths) == 2
    assert all(path.parent == destination for path in seen_paths)
    assert context.file_inventory[0]["recording_duration_sec"] == 32400
    assert context.file_inventory[0]["recorded_datetime"] == "2026-05-10T00:00:00-08:00"


def test_resume_repairs_audio_metadata_from_staged_destination(tmp_path, monkeypatch):
    import cassn.gui.wizard as wizard_module

    event = tmp_path / "event"
    source = tmp_path / "card"
    source.mkdir()
    source_file = source / "20260510_000000.WAV"
    source_file.write_bytes(b"audio bytes")
    destination = event / "raw_data" / "p1_BD"
    destination.mkdir(parents=True)
    staged = destination / "UC_Test_plot1_BD_20260101_00001.wav"
    staged.write_bytes(source_file.read_bytes())
    existing = {
        "device_label": "p1_BD",
        "new_filename": staged.name,
        "storage_relpath": f"raw_data/p1_BD/{staged.name}",
        "source_relpath": source_file.name,
        "file_type": "audio",
        "file_size_bytes": staged.stat().st_size,
        "file_hash_sha256": hashlib.sha256(source_file.read_bytes()).hexdigest(),
        "file_hash_sha1": hashlib.sha1(source_file.read_bytes()).hexdigest(),
        "source_evidence": source_evidence(source_file),
        "deployment_id": "UC_Test_plot1_BD_20260101",
        "recording_duration_sec": "",
    }
    repaired_paths = []

    def repair(entry, path):
        repaired_paths.append(path)
        entry["recording_duration_sec"] = 32400
        return True

    monkeypatch.setattr(
        wizard_module, "refresh_audiomoth_inventory_metadata", repair
    )
    context = _ProcessContext(event, IngestHashRegistry(), _AudioLookups())
    context.file_inventory = [existing]

    result = FieldDataWizard.process_sd_card_files(
        context,
        source,
        destination,
        1,
        "Plot 1",
        "BD",
        "p1_BD",
        _DummyReconyx(),
    )

    assert result == (0, 0, 0)
    assert repaired_paths == [staged]
    assert existing["recording_duration_sec"] == 32400


def test_concurrent_equal_byte_cards_hold_instead_of_silent_success(
    tmp_path, monkeypatch
):
    import cassn.gui.wizard as wizard_module

    monkeypatch.setattr(wizard_module, "EXIF_AVAILABLE", False)
    event = tmp_path / "event"
    event.mkdir()
    registry = IngestHashRegistry()
    barrier = threading.Barrier(2)

    def run_device(plot):
        source = tmp_path / f"card{plot}"
        source.mkdir()
        (source / f"image{plot}.jpg").write_bytes(b"identical-image")
        destination = event / "raw_data" / f"p{plot}_ML"
        destination.mkdir(parents=True)
        context = _ProcessContext(event, registry)
        barrier.wait()
        try:
            result = FieldDataWizard.process_sd_card_files(
                context,
                source,
                destination,
                plot,
                f"Plot {plot}",
                "ML",
                f"p{plot}_ML",
                _DummyReconyx(),
            )
        except IngestSafetyError:
            result = "held"
        return result, context.file_inventory

    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs = list(pool.map(run_device, (1, 2)))

    assert sum(result == "held" for result, _rows in outputs) == 1
    assert sum(result == (1, 0, 0) for result, _rows in outputs) == 1
    assert all((tmp_path / f"card{plot}" / f"image{plot}.jpg").read_bytes() == b"identical-image"
               for plot in (1, 2))
    with pytest.raises(IngestSafetyError, match="Equal-byte"):
        require_no_ingest_holds(event)
    assert sum(len(rows) for _result, rows in outputs) == 1
    assert len(list((event / "raw_data").rglob("*.jpg"))) == 1


def _camera_worker(tmp_path, monkeypatch, *, inventory=(), processor=None):
    monkeypatch.setattr(card_ingest_thread, "ReconyxExtractor", _DummyReconyx)
    import cassn.gui.wizard as wizard_module
    monkeypatch.setattr(wizard_module, "EXIF_AVAILABLE", False)
    source = tmp_path / "card"
    source.mkdir(exist_ok=True)
    worker = CardIngestThread(
        processor=processor or FieldDataWizard.process_sd_card_files,
        source_dir=source, deployment_folder=tmp_path / "event",
        plot_num=1, plot_label="Plot 1", device_code="ML", device_label="p1_ML",
        metadata={"site_short_name": "Test"}, lookups=_FakeLookups(),
        inventory=list(inventory), hash_registry=IngestHashRegistry(),
    )
    return worker


def test_ingest_silently_splits_at_real_wi_limit(tmp_path, monkeypatch):
    from cassn.core.wi_split import plan_device

    def processor(context, _source, destination, *_args):
        for number in range(15003):
            name = f"UC_Test_plot1_ML_20260101_{number + 1:05d}.jpg"
            (destination / name).write_bytes(b"image")
            context.file_inventory.append({
                "device_label": "p1_ML", "new_filename": name, "file_type": "image",
                "file_hash_sha256": str(number),
            })
        return 15003, 0, 0

    worker = _camera_worker(tmp_path, monkeypatch, processor=processor)
    rows, results, logs, progress = [], [], [], []
    worker.rows_ready.connect(lambda _label, batch: rows.extend(batch))
    worker.completed.connect(lambda _label, result: results.append(result))
    worker.log_line.connect(lambda _label, message: logs.append(message))
    worker.progress.connect(lambda *_args: progress.append(_args))
    worker.run()
    assert results[0]["ok"]
    plan = plan_device(worker.deployment_folder / "raw_data/p1_ML")
    assert plan.fully_split
    assert [len(part.files) for part in plan.parts] == [15000, 3]
    assert len(rows) == 30006  # Initial durable rows, then the same identities with moved paths.
    assert all((worker.deployment_folder / row["storage_relpath"]).is_file() for row in rows[15003:])
    assert logs == ["Counting files on source card…"]
    assert len(progress) == 1  # Initial counting only; no split progress.


def test_ingest_recovers_crash_paths_before_missing_file_restoration(tmp_path, monkeypatch):
    from cassn.core.file_transfer import hash_file_with_retries

    source = tmp_path / "card"
    source.mkdir()
    (source / "one.jpg").write_bytes(b"image")
    name = "UC_Test_plot1_ML_20260101_00001.jpg"
    moved = tmp_path / "event/raw_data/p1_ML/p1_ML_1" / name
    moved.parent.mkdir(parents=True)
    moved.write_bytes(b"image")
    hashes = hash_file_with_retries(moved)
    inventory = [{
        "device_label": "p1_ML", "new_filename": name, "file_type": "image",
        "source_relpath": "one.jpg", "file_size_bytes": 5,
        "source_evidence": source_evidence(source / "one.jpg"),
        "deployment_id": "UC_Test_plot1_ML_20260101",
        "file_hash_sha256": hashes.sha256, "file_hash_sha1": hashes.sha1,
        "storage_relpath": f"raw_data/p1_ML/{name}",
    }]
    # Existing Box history lets this tiny fixture retain its part layout.
    qc = tmp_path / "event/qc"
    qc.mkdir()
    (qc / "box_upload_manifest.json").write_text("{}")
    worker = _camera_worker(tmp_path, monkeypatch, inventory=inventory)
    rows, results = [], []
    worker.rows_ready.connect(lambda _label, batch: rows.extend(batch))
    worker.completed.connect(lambda _label, result: results.append(result))
    worker.run()
    assert results[0]["ok"]
    assert results[0]["files_copied"] == 0
    assert len(rows) == 1
    assert rows[0]["storage_relpath"] == f"raw_data/p1_ML/p1_ML_1/{name}"
    assert not (moved.parent.parent / name).exists()


def test_inventory_path_checkpoint_updates_existing_record(tmp_path):
    from types import SimpleNamespace

    row = {
        "device_label": "p1_ML", "new_filename": "one.jpg",
        "storage_relpath": "raw_data/p1_ML/one.jpg", "file_hash_sha256": "hash",
    }
    wizard = SimpleNamespace(
        file_inventory=[dict(row)], seen_file_hashes={"hash"}, card_ingest_threads={},
        save_session=lambda: True, log=lambda _message: None,
    )
    moved = dict(row, storage_relpath="raw_data/p1_ML/p1_ML_1/one.jpg")
    FieldDataWizard._on_card_rows_ready(wizard, "p1_ML", [moved])
    assert wizard.file_inventory == [moved]


def test_failed_inventory_save_prevents_split(tmp_path, monkeypatch):
    def processor(context, _source, destination, *_args):
        (destination / "one.jpg").write_bytes(b"one")
        context.file_inventory.append({
            "device_label": "p1_ML", "new_filename": "one.jpg",
            "file_type": "image", "file_hash_sha256": "hash",
        })
        return 1, 0, 0

    worker = _camera_worker(tmp_path, monkeypatch, processor=processor)
    worker.rows_ready.connect(lambda *_args: worker.mark_checkpoint_failed())
    monkeypatch.setattr(card_ingest_thread, "prepare_device_for_wi", lambda *_args, **_kwargs: pytest.fail("Split ran after failed save"))
    results = []
    worker.completed.connect(lambda _label, result: results.append(result))
    worker.run()
    assert not results[0]["ok"]
    assert "persist copied files" in results[0]["error"]


@pytest.mark.parametrize("stop", ["cancel", "save_failure"])
def test_interrupted_ingest_split_checkpoints_actual_paths(tmp_path, monkeypatch, stop):
    from cassn.core import wi_split

    def processor(context, _source, destination, *_args):
        for number in range(3):
            name = f"image_{number:05d}.jpg"
            (destination / name).write_bytes(b"image")
            context.file_inventory.append({
                "device_label": "p1_ML", "new_filename": name,
                "file_type": "image", "file_hash_sha256": str(number),
            })
        return 3, 0, 0

    worker = _camera_worker(tmp_path, monkeypatch, processor=processor)
    original_prepare = wi_split.prepare_device_for_wi
    monkeypatch.setattr(
        card_ingest_thread, "prepare_device_for_wi",
        lambda *args, **kwargs: original_prepare(*args, limit=2, **kwargs),
    )
    original_move = wi_split.shutil.move

    def move_and_cancel(*args):
        result = original_move(*args)
        worker.cancel()
        return result

    if stop == "cancel":
        monkeypatch.setattr(wi_split.shutil, "move", move_and_cancel)
    batches, results = [], []

    def checkpoint(_label, rows):
        batches.append(rows)
        if stop == "save_failure" and any("p1_ML_" in row.get("storage_relpath", "") for row in rows):
            worker.mark_checkpoint_failed()

    worker.rows_ready.connect(checkpoint)
    worker.completed.connect(lambda _label, result: results.append(result))
    worker.run()
    assert not results[0]["ok"]
    assert results[0]["cancelled"] == (stop == "cancel")
    current = {row["new_filename"]: row for batch in batches for row in batch}
    assert len(current) == 3
    assert any("p1_ML_" in row["storage_relpath"] for row in current.values())
    assert all((worker.deployment_folder / row["storage_relpath"]).is_file() for row in current.values())


def test_prior_box_upload_skips_ingest_reorganization(tmp_path, monkeypatch):
    worker = _camera_worker(tmp_path, monkeypatch, processor=lambda *_args: (0, 0, 0))
    qc = worker.deployment_folder / "qc"
    qc.mkdir(parents=True)
    (qc / "box_upload_manifest.json").write_text("{}")
    monkeypatch.setattr(card_ingest_thread, "prepare_device_for_wi", lambda *_args, **_kwargs: pytest.fail("Uploaded deployment was reorganized"))
    results = []
    worker.completed.connect(lambda _label, result: results.append(result))
    worker.run()
    assert results[0]["ok"]


def test_threaded_ingest_saves_inventory_before_and_after_moves(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from PySide6.QtCore import QObject, Slot, Qt, QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication
    from cassn.core import wi_split

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    original_prepare = wi_split.prepare_device_for_wi
    monkeypatch.setattr(
        card_ingest_thread, "prepare_device_for_wi",
        lambda *args, **kwargs: original_prepare(*args, limit=2, **kwargs),
    )

    def processor(context, _source, destination, *_args):
        for number in range(3):
            name = f"image_{number:05d}.jpg"
            (destination / name).write_bytes(b"image")
            context.file_inventory.append({
                "device_label": "p1_ML", "new_filename": name,
                "file_type": "image", "file_hash_sha256": str(number),
            })
        return 3, 0, 0

    worker = _camera_worker(tmp_path, monkeypatch, processor=processor)
    snapshots = []
    state = SimpleNamespace(
        file_inventory=[], seen_file_hashes=set(),
        card_ingest_threads={"p1_ML": worker}, log=lambda _message: None,
    )
    session = tmp_path / "saved-session.json"

    def save_session():
        session.write_text(json.dumps(state.file_inventory))
        snapshots.append(json.loads(session.read_text()))
        return True

    state.save_session = save_session

    class Writer(QObject):
        @Slot(str, object)
        def receive(self, label, rows):
            FieldDataWizard._on_card_rows_ready(state, label, rows)

    writer = Writer()
    worker.rows_ready.connect(writer.receive, Qt.ConnectionType.BlockingQueuedConnection)
    original_move = wi_split.shutil.move

    def move_after_persisted_inventory(source, destination):
        saved = json.loads(session.read_text())
        assert len(saved) == 3
        return original_move(source, destination)

    monkeypatch.setattr(wi_split.shutil, "move", move_after_persisted_inventory)
    results = []
    worker.completed.connect(lambda _label, result: results.append(result))
    loop = QEventLoop()
    worker.finished.connect(loop.quit)
    timeout = QTimer()
    timeout.setSingleShot(True)
    timeout.timeout.connect(worker.cancel)
    timeout.start(5000)
    worker.start()
    loop.exec()
    assert worker.wait(1000)
    timeout.stop()
    assert results[0]["ok"]
    assert len(snapshots) == 2
    assert len(state.file_inventory) == 3
    assert all((worker.deployment_folder / row["storage_relpath"]).is_file() for row in json.loads(session.read_text()))
    assert app is not None


@pytest.mark.parametrize('legacy', [False, True])
def test_camera_time_resolution_preserves_image_bytes_and_resume(tmp_path, monkeypatch, legacy):
    from cassn.gui import wizard
    from cassn.config import IMAGE_TIMESTAMP_FIELDS

    source = tmp_path / 'card'
    source.mkdir()
    original = source / 'RCNX0001.JPG'
    contents = b'original image bytes with unchanged EXIF'
    original.write_bytes(contents)
    monkeypatch.setattr(wizard, 'EXIF_AVAILABLE', True)
    monkeypatch.setattr(wizard, 'extract_exif_data', lambda _: (
        {'DateTimeOriginal': '2026:07:15 12:00:00', 'OffsetTimeOriginal': '-08:00'}, {}))
    context = _ProcessContext(tmp_path / 'event', IngestHashRegistry())

    def ingest():
        destination = context.current_deployment_folder / 'raw_data/p1_ML'
        destination.mkdir(parents=True, exist_ok=True)
        return FieldDataWizard.process_sd_card_files(
            context, source, destination, 1, 'Plot 1', 'ML', 'p1_ML', _DummyReconyx())

    assert ingest() == (1, 0, 0)
    row = context.file_inventory[0]
    assert row['recorded_datetime'] == '2026-07-15T12:00:00-07:00'
    assert row['recorded_datetime_utc'] == '2026-07-15T19:00:00Z'
    assert row['camera_timestamp_status'] == 'offset_mismatch_corrected'
    assert row['file_hash_sha256'] == hashlib.sha256(contents).hexdigest()
    staged = context.current_deployment_folder / row['storage_relpath']
    assert original.read_bytes() == staged.read_bytes() == contents
    if legacy:
        for key in IMAGE_TIMESTAMP_FIELDS:
            row.pop(key, None)
        row['recorded_datetime'] = '2026-07-15T13:00:00-07:00'
    # Simulate a saved/reloaded session, including pre-fix rows.
    context.file_inventory = json.loads(json.dumps(context.file_inventory))
    before = json.dumps(context.file_inventory, sort_keys=True)
    assert ingest() == (0, 0, 0)
    assert json.dumps(context.file_inventory, sort_keys=True) == before
    # Restoring a missing copy also preserves its saved metadata.
    staged.unlink()
    ingest()
    assert json.dumps(context.file_inventory, sort_keys=True) == before
    assert original.read_bytes() == staged.read_bytes() == contents


def test_small_file_evidence_survives_noop_resume(tmp_path, monkeypatch):
    source, reconyx = _burst_card(tmp_path, monkeypatch)
    context = _ProcessContext(tmp_path / "event", IngestHashRegistry())
    _ingest_camera(context, source, reconyx)
    before = json.loads((context.current_deployment_folder / "qc/qc_report.json").read_text())
    original = next(e for e in before["current_state"] if e["check"] == "file_size_floor")
    assert original["severity"] == "warning"
    assert len(original["findings"]) == 6
    copied, _, _ = _ingest_camera(context, source, reconyx)
    assert copied == 0
    after = json.loads((context.current_deployment_folder / "qc/qc_report.json").read_text())
    current = next(e for e in after["current_state"] if e["check"] == "file_size_floor")
    assert current["severity"] == "warning"
    assert current["findings"] == original["findings"]
    for finding in current["findings"]:
        assert finding["files"][0]["filename"]
        assert "bytes" in finding["message"]


@pytest.mark.parametrize("change", ["new_bytes", "other_card", "missing_evidence", "staged_corruption", "deployment_changed"])
def test_resume_requires_source_evidence_and_matching_bytes(tmp_path, monkeypatch, change):
    import cassn.gui.wizard as wizard_module
    monkeypatch.setattr(wizard_module, "EXIF_AVAILABLE", False)
    source = tmp_path / "card"
    source.mkdir()
    incoming = source / "one.jpg"
    incoming.write_bytes(b"original")
    context = _ProcessContext(tmp_path / "event", IngestHashRegistry())
    assert _ingest_camera(context, source, _DummyReconyx()) == (1, 0, 0)
    row = context.file_inventory[0]
    staged = context.current_deployment_folder / row["storage_relpath"]
    if change == "new_bytes":
        incoming.write_bytes(b"different"[:8])
    elif change == "other_card":
        source = tmp_path / "other-card"
        source.mkdir()
        (source / "one.jpg").write_bytes(b"original")
    elif change == "missing_evidence":
        row.pop("source_evidence")
    elif change == "deployment_changed":
        row["deployment_id"] = "other-deployment"
    else:
        staged.write_bytes(b"corrupt!")
    before = staged.read_bytes()
    with pytest.raises(IngestSafetyError):
        _ingest_camera(context, source, _DummyReconyx())
    assert staged.read_bytes() == before
    assert len(context.file_inventory) == 1
    with pytest.raises(IngestSafetyError):
        require_no_ingest_holds(context.current_deployment_folder)
    # A retry cannot erase an unresolved safety finding, even with original bytes.
    incoming.write_bytes(b"original")
    with pytest.raises(IngestSafetyError):
        _ingest_camera(context, incoming.parent, _DummyReconyx())


def test_repeated_config_destination_preserves_both_sources_and_staged_copy(tmp_path, monkeypatch):
    import cassn.gui.wizard as wizard_module
    monkeypatch.setattr(wizard_module, "EXIF_AVAILABLE", False)
    source = tmp_path / "card"
    source.mkdir()
    (source / "CONFIG1.TXT").write_bytes(b"config one")
    (source / "CONFIG2.TXT").write_bytes(b"config two")
    event = tmp_path / "event"
    dest = event / "raw_data/p1_BD"
    dest.mkdir(parents=True)
    context = _ProcessContext(event, IngestHashRegistry(), _AudioLookups())
    with pytest.raises(IngestSafetyError, match="Destination collision"):
        FieldDataWizard.process_sd_card_files(context, source, dest, 1, "Plot 1", "BD", "p1_BD", _DummyReconyx())
    assert (source / "CONFIG1.TXT").read_bytes() == b"config one"
    assert (source / "CONFIG2.TXT").read_bytes() == b"config two"
    assert len(context.file_inventory) == 1
    assert next(dest.glob("*.txt")).read_bytes() == b"config one"
