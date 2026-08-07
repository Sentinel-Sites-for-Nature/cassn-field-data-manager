"""Tests for the WI upload splitter (cassn.core.wi_split).

These pin the split contract: parts never exceed the limit, bursts are never
cut, part folders don't re-match the device suffix, and a split round-trips
cleanly back to the original flat folder.
"""
from pathlib import PurePosixPath

import pytest

from cassn.core.wi_split import (
    DuplicateImageError,
    InvalidLimitError,
    OversizedBurstError,
    SplitCollisionError,
    SplitError,
    apply_device_split,
    event_key,
    find_target_devices,
    is_part_dir_name,
    list_device_images,
    plan_device,
    plan_parts,
    prepare_deployment_for_wi,
    sync_inventory_storage_paths,
    undo_device_split,
    verify_device_split,
)


def _burst_names(device="p1_ML", n_events=10, frames=3):
    names = []
    for e in range(1, n_events + 1):
        for f in range(1, frames + 1):
            names.append(f"UC_Test_plot1_ML_20260101_{e:05d}_{f}.jpg")
    return names


def test_event_key_groups_burst_frames():
    a = "UC_Test_plot1_ML_20260101_00001_1.jpg"
    b = "UC_Test_plot1_ML_20260101_00001_2.jpg"
    c = "UC_Test_plot1_ML_20260101_00002_1.jpg"
    assert event_key(a) == event_key(b)
    assert event_key(a) != event_key(c)


def test_event_key_singleframe_is_its_own_event():
    # A name with no trailing frame index must not be over-grouped.
    x = "UC_Test_plot1_SA_20260101_00042.jpg"
    y = "UC_Test_plot1_SA_20260101_00043.jpg"
    assert event_key(x) != event_key(y)


def test_is_part_dir_name():
    assert is_part_dir_name("p1_ML_1", "p1_ML")
    assert is_part_dir_name("p1_ML_12", "p1_ML")
    assert not is_part_dir_name("p1_ML", "p1_ML")
    assert not is_part_dir_name("p1_ML_x", "p1_ML")


def test_part_dir_never_rematches_device_suffix():
    # The idempotency guarantee: a part folder can't be picked up as a device.
    for suffix in ("_ML", "_SA"):
        assert not f"p1{suffix}_1".endswith(suffix)


def test_plan_parts_respects_limit_and_keeps_bursts():
    names = _burst_names(n_events=100, frames=3)  # 300 images
    parts = plan_parts(names, limit=90, keep_bursts=True)
    assert sum(len(p) for p in parts) == len(names)
    for p in parts:
        assert len(p) <= 90
        # every event in this part is present in full (3 frames)
        counts = {}
        for name in p:
            counts[event_key(name)] = counts.get(event_key(name), 0) + 1
        assert all(c == 3 for c in counts.values())


def test_plan_parts_no_split_when_under_limit():
    names = _burst_names(n_events=5)
    assert plan_parts(names, limit=15000) == [names]


def test_plan_parts_no_split_at_exact_limit():
    names = _burst_names(n_events=5)
    assert len(names) == 15
    assert plan_parts(names, limit=15) == [names]


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_plan_parts_rejects_invalid_limit(limit):
    with pytest.raises(InvalidLimitError, match="positive integer"):
        plan_parts(["image.jpg"], limit=limit)


def test_plan_parts_rejects_single_burst_larger_than_limit():
    names = _burst_names(n_events=1, frames=4)
    with pytest.raises(OversizedBurstError, match="contains 4 images"):
        plan_parts(names, limit=3, keep_bursts=True)


def test_plan_device_and_roundtrip(tmp_path):
    dev = tmp_path / "p1_ML"
    dev.mkdir()
    names = _burst_names(n_events=20, frames=3)  # 60 images
    for n in names:
        (dev / n).write_text("x")
    (dev / "p1_ML_manifest.json").write_text("{}")  # legacy file must be left alone

    plan = plan_device(dev, limit=21, keep_bursts=True)
    assert plan.needs_split
    assert len(plan.parts) == 3  # 60 images / 21, on burst (3) boundaries -> 21,21,18
    assert plan.pending_moves() == 60

    result = apply_device_split(plan, move=True, dry_run=False)
    assert result["verification"].ok
    assert list_device_images(dev) == []  # all images moved down
    assert (dev / "p1_ML_manifest.json").exists()  # legacy file untouched
    moved = sum(len(list((dev / p.name).glob("*.jpg"))) for p in plan.parts)
    assert moved == 60

    # A re-scan sees a fully-split device; re-applying is a no-op.
    re_plan = plan_device(dev, limit=21)
    assert re_plan.fully_split
    assert re_plan.pending_moves() == 0

    undo_device_split(dev, dry_run=False)
    assert sorted(list_device_images(dev)) == sorted(names)
    assert not [p for p in dev.iterdir() if p.is_dir()]  # part dirs removed


def test_apply_is_resumable_after_partial_move(tmp_path):
    # Simulate an interrupted split: one part complete, the rest still loose.
    dev = tmp_path / "p1_SA"
    dev.mkdir()
    names = _burst_names(device="p1_SA", n_events=20, frames=3)  # 60 images
    for n in names:
        (dev / n).write_text("x")
    plan = plan_device(dev, limit=21)
    first = plan.parts[0]
    (dev / first.name).mkdir()
    import shutil as _sh
    for fn in first.files:  # move only the first part's files
        _sh.move(str(dev / fn), str(dev / first.name / fn))

    # Re-planning sees the partial state and only the remainder is pending.
    resumed = plan_device(dev, limit=21)
    assert not resumed.fully_split
    assert resumed.pending_moves() == 60 - len(first.files)

    apply_device_split(resumed, move=True, dry_run=False)
    assert plan_device(dev, limit=21).fully_split
    placed = sum(len(list((dev / p.name).glob("*.jpg"))) for p in resumed.parts)
    assert placed == 60


def test_plan_rejects_duplicate_filename_across_root_and_part(tmp_path):
    dev = tmp_path / "p1_ML"
    part = dev / "p1_ML_1"
    part.mkdir(parents=True)
    (dev / "duplicate.jpg").write_text("root")
    (part / "duplicate.jpg").write_text("part")

    with pytest.raises(DuplicateImageError, match="duplicate.jpg"):
        plan_device(dev, limit=1)


def test_apply_rejects_destination_created_after_plan(tmp_path):
    dev = tmp_path / "p1_ML"
    dev.mkdir()
    names = _burst_names(n_events=2, frames=3)
    for name in names:
        (dev / name).write_text("source")
    plan = plan_device(dev, limit=3)

    first_destination = dev / plan.parts[0].name / plan.parts[0].files[0]
    first_destination.parent.mkdir()
    first_destination.write_text("collision")

    with pytest.raises(SplitCollisionError, match="Duplicate image filename"):
        apply_device_split(plan, dry_run=False)
    assert (dev / plan.parts[0].files[0]).read_text() == "source"
    assert first_destination.read_text() == "collision"


def test_apply_rejects_source_removed_after_plan(tmp_path):
    dev = tmp_path / "p1_SA"
    dev.mkdir()
    names = _burst_names(n_events=2, frames=3)
    for name in names:
        (dev / name).write_text("x")
    plan = plan_device(dev, limit=3)
    (dev / names[0]).unlink()

    with pytest.raises(SplitCollisionError, match="planned image.s. missing"):
        apply_device_split(plan, dry_run=False)


def test_apply_rejects_copy_mode(tmp_path):
    dev = tmp_path / "p1_ML"
    dev.mkdir()
    for name in _burst_names(n_events=2, frames=3):
        (dev / name).write_text("x")
    plan = plan_device(dev, limit=3)

    with pytest.raises(SplitError, match="Copy mode is not supported"):
        apply_device_split(plan, move=False, dry_run=False)


def test_cancelled_apply_is_resumable_and_verifies_after_resume(tmp_path):
    dev = tmp_path / "p1_ML"
    dev.mkdir()
    names = _burst_names(n_events=10, frames=3)
    for name in names:
        (dev / name).write_text("x")
    plan = plan_device(dev, limit=9)

    checks = 0

    def cancel_after_several_moves():
        nonlocal checks
        checks += 1
        return checks > 7

    partial = apply_device_split(
        plan,
        dry_run=False,
        is_cancelled=cancel_after_several_moves,
    )
    assert partial["cancelled"]
    assert 0 < partial["placed"] < len(names)
    assert partial["verification"] is None

    resumed = plan_device(dev, limit=9)
    completed = apply_device_split(resumed, dry_run=False)
    assert completed["verification"].ok
    assert plan_device(dev, limit=9).fully_split


def test_structural_verification_detects_loose_image(tmp_path):
    dev = tmp_path / "p1_SA"
    dev.mkdir()
    names = _burst_names(n_events=2, frames=3)
    for name in names:
        (dev / name).write_text("x")
    plan = plan_device(dev, limit=3)
    apply_device_split(plan, dry_run=False)

    misplaced = dev / plan.parts[0].name / plan.parts[0].files[0]
    misplaced.rename(dev / misplaced.name)
    verification = verify_device_split(plan)

    assert not verification.ok
    assert any("remain loose" in error for error in verification.errors)
    assert any("differs from plan" in error for error in verification.errors)


def test_find_target_devices_prunes_and_skips_parts(tmp_path):
    raw = tmp_path / "UC_X_20260101" / "raw_data"
    (raw / "p1_ML").mkdir(parents=True)
    (raw / "p1_SA").mkdir()
    (raw / "p1_BD").mkdir()          # audio, must be ignored
    (raw / "p1_ML" / "p1_ML_1").mkdir()  # a part folder, must not be a target
    found = {p.name for p in find_target_devices(tmp_path)}
    assert found == {"p1_ML", "p1_SA"}


def _deployment_inventory(deployment, device_name, names):
    device_dir = deployment / "raw_data" / device_name
    device_dir.mkdir(parents=True)
    inventory = []
    for name in names:
        (device_dir / name).write_bytes(name.encode())
        inventory.append({
            "device_label": device_name,
            "new_filename": name,
            "file_type": "image",
        })
    return device_dir, inventory


def test_prepare_deployment_splits_and_synchronizes_inventory_paths(tmp_path):
    names = _burst_names(n_events=4, frames=3)
    device_dir, inventory = _deployment_inventory(tmp_path, "p1_ML", names)

    result = prepare_deployment_for_wi(tmp_path, inventory, limit=6)

    assert not result["cancelled"]
    assert result["devices_split"] == 1
    assert result["parts"] == 2
    assert result["images_moved"] == 12
    assert list_device_images(device_dir) == []
    for entry in inventory:
        relative_path = entry["storage_relpath"]
        assert relative_path.startswith("raw_data/p1_ML/p1_ML_")
        assert tmp_path.joinpath(*PurePosixPath(relative_path).parts).is_file()


def test_prepare_deployment_cancel_persists_partial_paths_and_resumes(tmp_path):
    names = _burst_names(n_events=4, frames=3)
    _device_dir, inventory = _deployment_inventory(tmp_path, "p1_SA", names)
    cancelled = False

    def stop_after_two(current, _total, _path):
        nonlocal cancelled
        if current == 2:
            cancelled = True

    partial = prepare_deployment_for_wi(
        tmp_path,
        inventory,
        limit=6,
        progress=stop_after_two,
        is_cancelled=lambda: cancelled,
    )

    assert partial["cancelled"]
    assert partial["images_moved"] == 2
    for entry in inventory:
        relative_path = entry["storage_relpath"]
        assert tmp_path.joinpath(*PurePosixPath(relative_path).parts).is_file()

    completed = prepare_deployment_for_wi(tmp_path, inventory, limit=6)
    assert not completed["cancelled"]
    assert completed["images_moved"] == 10
    assert plan_device(tmp_path / "raw_data" / "p1_SA", limit=6).fully_split


def test_prepare_honors_cancellation_requested_on_final_move(tmp_path):
    names = _burst_names(n_events=2, frames=3)
    _device_dir, inventory = _deployment_inventory(tmp_path, "p1_ML", names)
    cancelled = False

    def stop_on_final_move(current, total, _path):
        nonlocal cancelled
        if total > 0 and current == total:
            cancelled = True

    result = prepare_deployment_for_wi(
        tmp_path,
        inventory,
        limit=3,
        progress=stop_on_final_move,
        is_cancelled=lambda: cancelled,
    )

    assert result["images_moved"] == result["total_moves"] == 6
    assert result["cancelled"]


def test_sync_inventory_rejects_uninventoried_image(tmp_path):
    names = _burst_names(n_events=1, frames=3)
    device_dir, inventory = _deployment_inventory(tmp_path, "p1_ML", names)
    plan = plan_device(device_dir, limit=10)
    inventory.pop()

    with pytest.raises(SplitError, match="1 uninventoried file"):
        sync_inventory_storage_paths(tmp_path, plan, inventory)
