"""Path-aware Box/local verification tests."""

from types import SimpleNamespace

from cassn.box.client import BoxStorage
from cassn.box.verification import is_orphan_on_box, verify_box_hashes
from cassn.core.hashing import sha1


def _folder(name, item_id):
    return SimpleNamespace(type="folder", name=name, id=item_id)


def _file(name, digest):
    return SimpleNamespace(type="file", name=name, id=name, sha1=digest)


class FakeStorage:
    def __init__(self, tree):
        self.tree = tree

    def iter_folder_items(self, folder_id, *, fields=None):
        del fields
        yield from self.tree.get(folder_id, [])


def test_collect_file_paths_preserves_complete_nested_posix_path():
    storage = BoxStorage(client=None)
    tree = {
        "deploy": [_folder("raw_data", "raw")],
        "raw": [_folder("p1_ML", "device")],
        "device": [_folder("p1_ML_1", "part")],
        "part": [_file("image.jpg", "0" * 40)],
    }
    storage.iter_folder_items = (
        lambda folder_id, fields=None: iter(tree.get(folder_id, []))
    )

    assert storage.collect_file_paths("deploy") == {
        "raw_data/p1_ML/p1_ML_1/image.jpg"
    }


def test_verify_box_hashes_supports_legacy_flat_inventory(tmp_path):
    local_file = tmp_path / "raw_data" / "p1_ML" / "image.jpg"
    local_file.parent.mkdir(parents=True)
    local_file.write_bytes(b"flat image")
    digest = sha1(local_file)
    storage = FakeStorage({
        "deploy": [_folder("raw_data", "raw")],
        "raw": [_folder("p1_ML", "device")],
        "device": [_file("image.jpg", digest)],
    })
    inventory = [{
        "device_label": "p1_ML",
        "new_filename": "image.jpg",
        "file_hash_sha1": digest,
    }]

    ok, summary, issues = verify_box_hashes(
        storage, "deploy", "deployment", inventory, tmp_path
    )

    assert ok
    assert "1 local file(s) checked" in summary
    assert issues == []


def test_verify_box_hashes_supports_nested_split_inventory(tmp_path):
    relative_path = "raw_data/p1_ML/p1_ML_1/image.jpg"
    local_file = tmp_path.joinpath(*relative_path.split("/"))
    local_file.parent.mkdir(parents=True)
    local_file.write_bytes(b"split image")
    digest = sha1(local_file)
    storage = FakeStorage({
        "deploy": [_folder("raw_data", "raw")],
        "raw": [_folder("p1_ML", "device")],
        "device": [_folder("p1_ML_1", "part")],
        "part": [_file("image.jpg", digest)],
    })
    inventory = [{
        "device_label": "p1_ML",
        "new_filename": "image.jpg",
        "storage_relpath": relative_path,
        "file_hash_sha1": digest,
    }]

    ok, summary, issues = verify_box_hashes(
        storage,
        "deploy",
        "deployment",
        inventory,
        tmp_path,
        detect_orphans=True,
    )

    assert ok
    assert "0 unexpected file(s) on Box" in summary
    assert issues == []


def test_verify_box_hashes_reports_complete_nested_path(tmp_path):
    relative_path = "raw_data/p1_ML/p1_ML_2/image.jpg"
    local_file = tmp_path.joinpath(*relative_path.split("/"))
    local_file.parent.mkdir(parents=True)
    local_file.write_bytes(b"local image")
    storage = FakeStorage({
        "deploy": [_folder("raw_data", "raw")],
        "raw": [_folder("p1_ML", "device")],
        "device": [_folder("p1_ML_2", "part")],
        "part": [_file("image.jpg", "0" * 40)],
    })
    inventory = [{
        "device_label": "p1_ML",
        "new_filename": "image.jpg",
        "storage_relpath": relative_path,
        "file_hash_sha1": sha1(local_file),
    }]

    ok, _summary, issues = verify_box_hashes(
        storage, "deploy", "deployment", inventory, tmp_path
    )

    assert not ok
    assert issues == [{"type": "mismatch", "filename": relative_path}]


def test_verify_box_hashes_detects_nested_box_orphan(tmp_path):
    (tmp_path / "raw_data").mkdir()
    storage = FakeStorage({
        "deploy": [_folder("raw_data", "raw")],
        "raw": [_folder("p1_ML", "device")],
        "device": [_folder("p1_ML_1", "part")],
        "part": [_file("unexpected.jpg", "0" * 40)],
    })

    ok, _summary, issues = verify_box_hashes(
        storage,
        "deploy",
        "deployment",
        [],
        tmp_path,
        detect_orphans=True,
    )

    assert not ok
    assert issues == [{
        "type": "extra_on_box",
        "filename": "raw_data/p1_ML/p1_ML_1/unexpected.jpg",
    }]


def test_box_orphan_allowlist_uses_basename_at_any_depth():
    assert not is_orphan_on_box("raw_data/p1_ML/p1_ML_manifest.json")
    assert not is_orphan_on_box("qc/wildlife_insights_images.csv")
    assert is_orphan_on_box("raw_data/p1_ML/p1_ML_1/unexpected.jpg")
