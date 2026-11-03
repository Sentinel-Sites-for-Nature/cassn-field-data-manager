"""Background macOS card discovery and guarded, non-forced ejection."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import plistlib
import subprocess

from PySide6.QtCore import QThread, Signal

DISKUTIL = "/usr/sbin/diskutil"
DISK_CHECK_TIMEOUT = 5


def disk_info(*args):
    try:
        result = subprocess.run(
            [DISKUTIL, *args], capture_output=True, timeout=DISK_CHECK_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"macOS did not finish checking {args[-1]} within {DISK_CHECK_TIMEOUT} seconds. "
            "No eject command was sent. Try again after other card jobs finish."
        ) from exc
    try:
        info = plistlib.loads(result.stdout)
    except plistlib.InvalidFileException as exc:
        detail = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(detail or "macOS returned an unreadable disk-check result.") from exc
    if result.returncode or info.get("Error"):
        raise RuntimeError(
            f"Card check failed: {info.get('ErrorMessage') or 'macOS could not identify the disk.'} "
            "No eject command was sent."
        )
    return info


@dataclass(frozen=True)
class CardIdentity:
    volume: Path
    filesystem_device: int
    partition: str
    disk: str
    uuid: str
    name: str
    registry_ids: tuple[int, int] = ()


def _mounts():
    """Read the kernel mount table without querying each volume."""
    output = subprocess.check_output(["/sbin/mount"], timeout=3, text=True)
    mounts = {}
    for line in output.splitlines():
        if not line.startswith("/dev/disk"):
            continue
        match = re.fullmatch(r"/dev/(disk[0-9]+(?:s[0-9]+)*) on (/.*) \([^)]*\)", line)
        if not match:
            raise ValueError("Cannot read the mounted disk paths.")
        mounts[Path(match[2])] = match[1]
    return mounts


def _registry_ids(partition, disk):
    """IOKit assigns a new entry ID when media disconnects and reconnects."""
    data = subprocess.check_output(
        ["/usr/sbin/ioreg", "-a", "-r", "-c", "IOMedia", "-d", "1"], timeout=3)
    media = {item["BSD Name"]: item for item in plistlib.loads(data) if "BSD Name" in item}
    whole = media.get(disk, {})
    ids = (media.get(partition, {}).get("IORegistryEntryID"), whole.get("IORegistryEntryID"))
    if not (whole.get("Whole") and whole.get("Removable") and all(ids)):
        raise ValueError("Cannot confirm the original removable card.")
    return ids


def card_attachment(volume: Path, filesystem_device: int):
    if not volume.is_mount() or volume.stat().st_dev != filesystem_device:
        raise ValueError("The original card is no longer mounted here.")
    partition = _mounts().get(volume, "")
    match = re.fullmatch(r"(disk[0-9]+)(?:s[0-9]+)?", partition)
    if not match:
        raise ValueError("Cannot confirm the source card's disk.")
    disk = match[1]
    return partition, disk, _registry_ids(partition, disk)


def identify_card(volume: Path, filesystem_device: int, attachment=None) -> CardIdentity:
    partition, disk, registry_ids = attachment or card_attachment(volume, filesystem_device)
    info = disk_info("info", "-plist", partition)
    whole = disk_info("info", "-plist", disk)
    if not (whole.get("WholeDisk") and whole.get("RemovableMedia")
            and whole.get("VirtualOrPhysical") == "Physical"):
        raise ValueError("The source is not a removable physical card.")
    if (info.get("MountPoint") != str(volume) or not info.get("VolumeUUID")
            or info.get("DeviceIdentifier") != partition
            or (info.get("ParentWholeDisk") or partition) != disk):
        raise ValueError("Cannot confirm the source card's identity.")
    if card_attachment(volume, filesystem_device) != (partition, disk, registry_ids):
        raise ValueError("The source card changed during identification.")
    return CardIdentity(volume, filesystem_device, partition, disk,
                        info["VolumeUUID"], info.get("VolumeName") or volume.name, registry_ids)


def _card_mounts(identity):
    mounts = _mounts()
    if (mounts.get(identity.volume) != identity.partition
            or identity.volume.stat().st_dev != identity.filesystem_device):
        raise ValueError("The source card changed. Ejection was cancelled.")
    return {path for path, partition in mounts.items()
            if partition == identity.disk or partition.startswith(identity.disk + "s")}


def eject_card(identity, active_sources, lease_factory=None):
    """Use cached physical eligibility and check the live attachment before ejecting."""
    mounts = _card_mounts(identity)
    if any(Path(source).is_relative_to(mount) for source in active_sources for mount in mounts):
        raise ValueError("Another job still uses this card. Wait for it to finish.")
    lease = lease_factory(mounts) if lease_factory else None
    try:
        if (_card_mounts(identity) != mounts
                or _registry_ids(identity.partition, identity.disk) != identity.registry_ids):
            raise ValueError("The source card changed. Ejection was cancelled.")
        try:
            result = subprocess.run([DISKUTIL, "eject", identity.disk],
                                    capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                "macOS did not confirm ejection within 30 seconds. "
                "Check Finder before removing the card."
            ) from exc
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip()
                               or "macOS could not eject the card.")
        if (identity.volume.is_mount()
                and identity.volume.stat().st_dev == identity.filesystem_device):
            raise RuntimeError("The card is still mounted. Try ejecting it again.")
    finally:
        if lease:
            lease.release()


class CardDiskThread(QThread):
    """Keep diskutil waits off the GUI thread; results contain no widgets."""
    result_ready = Signal(object, str)

    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self.operation = operation

    def run(self):
        try:
            self.result_ready.emit(self.operation(), "")
        except Exception as exc:
            self.result_ready.emit(None, str(exc))
