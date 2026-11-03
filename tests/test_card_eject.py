"""Card ejection uses synthetic mounts and mocked diskutil calls only."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from cassn.gui import card_eject as e


@pytest.fixture
def card(tmp_path, monkeypatch):
    volume = tmp_path / "CARD"
    volume.mkdir()
    device = volume.stat().st_dev
    mounted = {volume}
    monkeypatch.setattr(e.Path, "is_mount", lambda path: path in mounted)
    calls = []
    info = {
        "disk8s1": {"DeviceIdentifier": "disk8s1", "ParentWholeDisk": "disk8",
                      "VolumeUUID": "original", "MountPoint": str(volume), "VolumeName": "CAMERA"},
        "disk8": {"WholeDisk": True, "RemovableMedia": True, "VirtualOrPhysical": "Physical"},
    }
    listing = {volume: "disk8s1", tmp_path / "SECOND": "disk8s2"}
    ids = [17, 18]
    monkeypatch.setattr(e, "_mounts", lambda: listing.copy())
    monkeypatch.setattr(e, "_registry_ids", lambda partition, disk: tuple(ids))
    monkeypatch.setattr(e, "disk_info", lambda *args: info[args[-1]])

    def run(command, **kwargs):
        calls.append(command)
        mounted.clear()
        return SimpleNamespace(returncode=0, stdout="Ejected", stderr="")

    monkeypatch.setattr(e.subprocess, "run", run)
    return SimpleNamespace(volume=volume, device=device, mounted=mounted, calls=calls,
                           info=info, ids=ids, listing=listing, identity=e.identify_card(volume, device))


def test_correct_disk_and_all_partition_leases(card, monkeypatch):
    monkeypatch.setattr(e, "disk_info", lambda *args: pytest.fail("Unexpected diskutil lookup during eject"))
    reserved = []
    released = []
    def reserve(mounts):
        reserved.extend(mounts)
        return SimpleNamespace(release=lambda: released.append(True))
    e.eject_card(card.identity, [], reserve)
    assert card.calls == [[e.DISKUTIL, "eject", "disk8"]]
    assert set(reserved) == {card.volume, card.volume.parent / "SECOND"}
    assert released == [True]


@pytest.mark.parametrize("change", ["unmounted", "attachment", "device"])
def test_changed_or_ineligible_card_never_ejected(card, change):
    identity = card.identity
    if change == "unmounted":
        card.listing.pop(card.volume)
    elif change == "attachment":
        card.ids[0] += 1
    else:
        identity = replace(identity, filesystem_device=identity.filesystem_device + 1)
    with pytest.raises(ValueError):
        e.eject_card(identity, [])
    assert not card.calls


def test_other_job_on_another_partition_blocks_ejection(card):
    other = card.volume.parent / "SECOND"
    other.mkdir()
    card.mounted.add(other)
    card.info[str(other)] = {"DeviceIdentifier": "disk8s2", "ParentWholeDisk": "disk8"}
    with pytest.raises(ValueError, match="Another job"):
        e.eject_card(card.identity, [other / "DCIM"])
    assert not card.calls


def test_job_on_other_card_does_not_block(card):
    other = card.volume.parent / "OTHER"
    other.mkdir()
    card.mounted.add(other)
    card.info[str(other)] = {"DeviceIdentifier": "disk9s1", "ParentWholeDisk": "disk9"}
    e.eject_card(card.identity, [other])
    assert len(card.calls) == 1


def test_identity_rechecked_after_lease(card):
    released = []
    def reserve(mounts):
        card.ids[1] += 1
        return SimpleNamespace(release=lambda: released.append(True))
    with pytest.raises(ValueError, match="changed"):
        e.eject_card(card.identity, [], reserve)
    assert not card.calls
    assert released == [True]


@pytest.mark.parametrize("mode", ["busy", "still_mounted", "timeout"])
def test_failures_reported_and_lease_released(card, monkeypatch, mode):
    released = []
    def run(*args, **kwargs):
        if mode == "timeout":
            raise e.subprocess.TimeoutExpired(args[0], 30)
        return SimpleNamespace(returncode=1 if mode == "busy" else 0, stdout="", stderr="Disk is busy")
    monkeypatch.setattr(e.subprocess, "run", run)
    with pytest.raises((RuntimeError, e.subprocess.TimeoutExpired)):
        e.eject_card(card.identity, [], lambda mounts: SimpleNamespace(release=lambda: released.append(True)))
    assert released == [True]


def test_missing_partition_listing_fails_closed(card):
    card.listing.clear()
    with pytest.raises(ValueError, match="changed"):
        e.eject_card(card.identity, [])
    assert not card.calls


def test_disk_check_timeout_explains_ejection_was_not_attempted(monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        assert kwargs['timeout'] == e.DISK_CHECK_TIMEOUT
        raise e.subprocess.TimeoutExpired(command, kwargs['timeout'])
    monkeypatch.setattr(e.subprocess, 'run', run)
    with pytest.raises(RuntimeError, match='No eject command was sent'):
        e.disk_info('info', '-plist', '/Volumes/QRR-3-BT')
    assert calls == [[e.DISKUTIL, 'info', '-plist', '/Volumes/QRR-3-BT']]


def test_disk_check_reports_macos_plist_error(monkeypatch):
    payload = e.plistlib.dumps({'Error': True, 'ErrorMessage': 'Could not find disk: disk10s1'})
    monkeypatch.setattr(e.subprocess, 'run', lambda *args, **kwargs:
                        SimpleNamespace(returncode=1, stdout=payload, stderr=b''))
    with pytest.raises(RuntimeError, match='Could not find disk: disk10s1'):
        e.disk_info('info', '-plist', 'disk10s1')


@pytest.mark.parametrize("key,value", [("RemovableMedia", False), ("VirtualOrPhysical", "Virtual")])
def test_reject_ineligible_media_at_discovery(card, key, value):
    card.info["disk8"][key] = value
    with pytest.raises(ValueError, match="removable physical"):
        e.identify_card(card.volume, card.device)


def test_eject_timeout_reports_unconfirmed_ejection(card, monkeypatch):
    def run(command, **kwargs):
        raise e.subprocess.TimeoutExpired(command, 30)
    monkeypatch.setattr(e.subprocess, 'run', run)
    with pytest.raises(RuntimeError, match='did not confirm ejection'):
        e.eject_card(card.identity, [])
