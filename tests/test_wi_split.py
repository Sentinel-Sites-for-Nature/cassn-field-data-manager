"""Tests for the WI upload splitter (cassn.core.wi_split).

These pin the split contract: parts never exceed the limit, bursts are never
cut, part folders don't re-match the device suffix, and a split round-trips
cleanly back to the original flat folder.
"""
from cassn.core.wi_split import (
    apply_device_split,
    event_key,
    find_target_devices,
    is_part_dir_name,
    list_device_images,
    plan_device,
    plan_parts,
    undo_device_split,
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


def test_plan_device_and_roundtrip(tmp_path):
    dev = tmp_path / "p1_ML"
    dev.mkdir()
    names = _burst_names(n_events=20, frames=3)  # 60 images
    for n in names:
        (dev / n).write_text("x")
    (dev / "p1_ML_manifest.json").write_text("{}")  # sidecar must be left alone

    plan = plan_device(dev, limit=21, keep_bursts=True)
    assert plan.needs_split
    assert len(plan.parts) == 3  # 60 images / 21, on burst (3) boundaries -> 21,21,18
    assert plan.pending_moves() == 60

    apply_device_split(plan, move=True, dry_run=False)
    assert list_device_images(dev) == []  # all images moved down
    assert (dev / "p1_ML_manifest.json").exists()  # sidecar untouched
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


def test_find_target_devices_prunes_and_skips_parts(tmp_path):
    raw = tmp_path / "UC_X_20260101" / "raw_data"
    (raw / "p1_ML").mkdir(parents=True)
    (raw / "p1_SA").mkdir()
    (raw / "p1_BD").mkdir()          # audio, must be ignored
    (raw / "p1_ML" / "p1_ML_1").mkdir()  # a part folder, must not be a target
    found = {p.name for p in find_target_devices(tmp_path)}
    assert found == {"p1_ML", "p1_SA"}
