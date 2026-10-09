"""macOS removable-media discovery and operator commands using diskutil plists."""
from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

DISKUTIL = "/usr/sbin/diskutil"


def disk_info(*args):
    result = subprocess.run([DISKUTIL, *args], capture_output=True, check=True)
    return plistlib.loads(result.stdout)


def removable_cards():
    cards = []
    for disk in disk_info("list", "-plist", "physical").get("AllDisksAndPartitions", []):
        identifier = disk["DeviceIdentifier"]
        info = disk_info("info", "-plist", identifier)
        if not (info.get("Whole") and info.get("RemovableMedia")
                and info.get("VirtualOrPhysical") == "Physical"):
            continue
        # Unpartitioned cards can carry a filesystem on the whole device.
        volumes = []
        for item in disk.get("Partitions") or [disk]:
            volume = disk_info("info", "-plist", item["DeviceIdentifier"])
            if volume.get("FilesystemType"):
                volumes.append(volume)
        cards.append((identifier, volumes))
    return cards


def mount_cards(cards):
    failed = False
    for disk, volumes in cards:
        if not volumes:
            print(f"{disk}: no recognized filesystem to mount", file=sys.stderr)
            failed = True
        for volume in volumes:
            device = volume["DeviceIdentifier"]
            if volume.get("MountPoint"):
                print(f"{device} is already mounted at {volume['MountPoint']}")
                continue
            mount_point = Path.home() / "Desktop" / f"Card_{device}"
            created = not mount_point.exists()
            try:
                mount_point.mkdir(parents=True, exist_ok=True)
                result = subprocess.run([DISKUTIL, "mount", "-mountPoint", str(mount_point), device])
                if result.returncode:
                    failed = True
                    print(f"Failed to mount {device}", file=sys.stderr)
                    if created:
                        mount_point.rmdir()
                else:
                    print(f"Mounted {device} at {mount_point}")
            except OSError as exc:
                failed = True
                print(f"Failed to mount {device}: {exc}", file=sys.stderr)
    return int(failed)


def eject_cards(cards):
    for index, (disk, volumes) in enumerate(cards, 1):
        names = ", ".join(v.get("VolumeName") or v["DeviceIdentifier"] for v in volumes)
        print(f"{index}) {disk}  {names or '(no recognized volume)'}")
    while True:
        try:
            choice = input("Eject which card? Number, ALL, or Quit: ").strip().lower()
        except EOFError:
            return 0
        if choice in {"quit", "q"}:
            return 0
        if choice == "all":
            selected = cards
        elif choice.isdigit() and 1 <= int(choice) <= len(cards):
            selected = [cards[int(choice) - 1]]
        else:
            print("Invalid choice")
            continue
        failed = False
        for disk, _ in selected:
            result = subprocess.run([DISKUTIL, "eject", disk])
            if result.returncode:
                failed = True
                print(f"Failed to eject {disk}; it may still be mounted.", file=sys.stderr)
            else:
                print(f"Ejected {disk}")
        return int(failed)


def main(action):
    try:
        cards = removable_cards()
        if not cards:
            print("No removable physical media are attached.")
            return 0
        return mount_cards(cards) if action == "mount" else eject_cards(cards)
    except (OSError, subprocess.CalledProcessError, plistlib.InvalidFileException, KeyError) as exc:
        print(f"Could not complete card {action}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
