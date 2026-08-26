"""Concurrency safety for background SD-card ingestion."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from cassn.core.quality_control import append_qc_report
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


class _ProcessContext:
    def __init__(self, root, registry):
        self.current_deployment_folder = root
        self.metadata = {"site_short_name": "Test"}
        self.lookups = _FakeLookups()
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


def test_concurrent_cards_deduplicate_identical_media_across_devices(
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

    assert sorted(result for result, _rows in outputs) == [(0, 1, 0), (1, 0, 0)]
    assert sum(len(rows) for _result, rows in outputs) == 1
    assert len(list((event / "raw_data").rglob("*.jpg"))) == 1
