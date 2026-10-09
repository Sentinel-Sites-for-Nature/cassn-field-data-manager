"""Exercise device selection and failure handling without touching real disks."""
from types import SimpleNamespace

from utils import sd_cards


def test_discovery_excludes_fixed_and_virtual_disks_and_finds_all_volumes(monkeypatch):
    devices = {
        'disk1': {'Whole': True, 'RemovableMedia': False, 'VirtualOrPhysical': 'Physical'},
        'disk2': {'Whole': True, 'RemovableMedia': True, 'VirtualOrPhysical': 'Physical'},
        'disk3': {'Whole': True, 'RemovableMedia': True, 'VirtualOrPhysical': 'Virtual'},
        'disk2s1': {'DeviceIdentifier': 'disk2s1', 'Content': 'EFI'},
        'disk2s2': {'DeviceIdentifier': 'disk2s2', 'FilesystemType': 'exfat'},
        'disk2s3': {'DeviceIdentifier': 'disk2s3', 'FilesystemType': 'msdos'},
    }
    listing = {'AllDisksAndPartitions': [
        {'DeviceIdentifier': 'disk1'},
        {'DeviceIdentifier': 'disk2', 'Partitions': [
            {'DeviceIdentifier': name} for name in ('disk2s1', 'disk2s2', 'disk2s3')]},
        {'DeviceIdentifier': 'disk3'},
    ]}
    monkeypatch.setattr(sd_cards, 'disk_info', lambda *args: listing if args[0] == 'list' else devices[args[-1]])
    assert sd_cards.removable_cards() == [('disk2', [devices['disk2s2'], devices['disk2s3']])]


def test_mount_uses_actual_partition_and_skips_mounted_volume(monkeypatch, tmp_path):
    monkeypatch.setattr(sd_cards.Path, 'home', lambda: tmp_path)
    calls = []
    def run(command):
        calls.append(command)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(sd_cards.subprocess, 'run', run)
    assert sd_cards.mount_cards([('disk2', [
        {'DeviceIdentifier': 'disk2s2', 'FilesystemType': 'msdos'},
        {'DeviceIdentifier': 'disk2s3', 'MountPoint': '/Volumes/Already'},
    ])]) == 0
    assert calls == [[sd_cards.DISKUTIL, 'mount', '-mountPoint', str(tmp_path / 'Desktop/Card_disk2s2'), 'disk2s2']]


def test_mount_failure_returns_error_and_removes_only_new_mountpoint(monkeypatch, tmp_path):
    monkeypatch.setattr(sd_cards.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(sd_cards.subprocess, 'run', lambda _: SimpleNamespace(returncode=1))
    existing = tmp_path / 'Desktop/Card_disk2s3'
    existing.mkdir(parents=True)
    volumes = [{'DeviceIdentifier': name} for name in ('disk2s2', 'disk2s3')]
    assert sd_cards.mount_cards([('disk2', volumes)]) == 1
    assert not (tmp_path / 'Desktop/Card_disk2s2').exists()
    assert existing.is_dir()


def test_eject_all_attempts_remaining_disks_and_preserves_failure(monkeypatch, capsys):
    monkeypatch.setattr('builtins.input', lambda _: 'ALL')
    calls = []
    def run(command):
        calls.append(command[-1])
        return SimpleNamespace(returncode=1 if command[-1] == 'disk2' else 0)
    monkeypatch.setattr(sd_cards.subprocess, 'run', run)
    assert sd_cards.eject_cards([('disk2', []), ('disk3', [])]) == 1
    assert calls == ['disk2', 'disk3']
    assert 'Failed to eject disk2' in capsys.readouterr().err


def test_eject_single_failure_returns_error(monkeypatch):
    monkeypatch.setattr('builtins.input', lambda _: '1')
    monkeypatch.setattr(sd_cards.subprocess, 'run', lambda _: SimpleNamespace(returncode=1))
    assert sd_cards.eject_cards([('disk2', [])]) == 1
