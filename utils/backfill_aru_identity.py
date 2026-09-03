#!/usr/bin/env python3
"""Backfill ARU make, model and firmware in existing audio metadata CSVs.

Until 2026-09-02 the app wrote the AudioMoth firmware string into ``ARU_model``
(``AudioMoth-Firmware-Basic 1.11.0``), and app versions before 4.0 also wrote
``AudioMoth`` as the manufacturer. The device itself has always reported all
three correctly in its GUANO chunk: ``Make: Open Acoustic Devices``,
``Model: AudioMoth``, ``Firmware Version: AudioMoth-Firmware-Basic (1.11.0)``.

No media is read. The firmware string is already present in these CSVs, in the
wrong column, so this is a pure column transform:

    ARU_firmware = ARU_model        # moved, never re-derived
    ARU_model    = "AudioMoth"
    ARU_make     = "Open Acoustic Devices"

A row whose ``ARU_model`` is already the plain model keeps a blank firmware
rather than recording ``AudioMoth`` as a firmware version; those are the
``CONFIG.TXT`` rows. All three fields are written on every row, media and
config alike, because they describe the device rather than the file.

The default is a dry run. Pass ``--apply`` to rewrite. Unchanged rows stay
byte-identical: the source header order and line terminator are preserved,
which matters because a few Box metadata files are LF where most are CRLF.
Rerunning is a no-op.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

METADATA_NAME = "audio_file_metadata.csv"
MAKE = "Open Acoustic Devices"
MODEL = "AudioMoth"
FIRMWARE_COLUMN = "ARU_firmware"


@dataclass
class FileResult:
    path: Path
    rows: int
    changed: int
    added_column: bool
    firmware_recovered: int


def _read(path: Path) -> tuple[list[str], list[dict], str, str]:
    """Return the header, rows, line terminator and encoding of one CSV."""
    raw = path.read_bytes()
    encoding = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
    text = raw.decode(encoding)
    terminator = "\r\n" if "\r\n" in text.split("\n", 1)[0] + "\n" else "\n"
    reader = csv.DictReader(io.StringIO(text))
    return list(reader.fieldnames or []), list(reader), terminator, encoding


def _plan(rows: list[dict]) -> tuple[list[dict], int, int]:
    """Return corrected rows, the number changed, and firmware values recovered."""
    changed = 0
    recovered = 0
    out = []
    for row in rows:
        new = dict(row)
        model = (row.get("ARU_model") or "").strip()
        firmware = (row.get(FIRMWARE_COLUMN) or "").strip()

        # Only the first pass moves a value; a populated firmware column means
        # this file has already been migrated.
        if not firmware and model and model != MODEL:
            firmware = model
            recovered += 1

        new[FIRMWARE_COLUMN] = firmware
        new["ARU_model"] = MODEL
        new["ARU_make"] = MAKE
        if any(new.get(k) != row.get(k) for k in ("ARU_make", "ARU_model", FIRMWARE_COLUMN)):
            changed += 1
        out.append(new)
    return out, changed, recovered


def _render(header: list[str], rows: list[dict], terminator: str,
            encoding: str) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=header, lineterminator=terminator)
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode(encoding)


def _write(path: Path, payload: bytes, in_place: bool) -> None:
    """Write the new content, atomically or in place.

    The default replaces the file through a temporary sibling, which is safe
    against a partial write. ``in_place`` truncates and rewrites the existing
    file instead: on a Box Drive (macOS File Provider) mount a replace can
    register as a delete plus a create, giving the file a new Box ID and
    orphaning its version history — which is what made the 2026-08-18 restore
    possible. Preserving that history is worth more here than partial-write
    safety, because these files are small and the caller backs them up first.
    """
    if in_place:
        with open(path, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        return

    handle, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "wb") as tmp:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def process(path: Path, apply: bool, in_place: bool = False) -> FileResult:
    header, rows, terminator, encoding = _read(path)
    if "ARU_model" not in header or "ARU_make" not in header:
        return FileResult(path, len(rows), 0, False, 0)

    added_column = FIRMWARE_COLUMN not in header
    if added_column:
        header = list(header)
        header.insert(header.index("ARU_model") + 1, FIRMWARE_COLUMN)

    corrected, changed, recovered = _plan(rows)
    if apply and (changed or added_column):
        _write(path, _render(header, corrected, terminator, encoding), in_place)
    return FileResult(path, len(rows), changed, added_column, recovered)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--root", type=Path, required=True,
        help="Directory searched recursively for audio_file_metadata.csv "
             "(the CASSN data root, on Box Drive or a local copy)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Rewrite the files. The default is a read-only dry run.",
    )
    parser.add_argument(
        "--in-place", action="store_true",
        help="Truncate and rewrite each file rather than replacing it through a "
             "temporary sibling. Use this on Box Drive, where a replace can cost "
             "the file its Box ID and version history. Back up first.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.expanduser()
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 2

    paths = sorted(root.rglob(METADATA_NAME))
    if not paths:
        print(f"No {METADATA_NAME} found under {root}", file=sys.stderr)
        return 2

    mode = "apply" if args.apply else "dry-run"
    print(f"Mode: {mode}{' (in place)' if args.apply and args.in_place else ''}")
    print(f"Root: {root}\n")
    results = [process(path, args.apply, args.in_place) for path in paths]

    width = max(len(r.path.parent.name) for r in results)
    for r in results:
        note = " +column" if r.added_column else ""
        print(f"  {r.path.parent.name:{width}}  {r.rows:5} rows  "
              f"{r.changed:5} changed  {r.firmware_recovered:5} firmware{note}")

    print(f"\n{len(results)} file(s), {sum(r.rows for r in results)} row(s), "
          f"{sum(r.changed for r in results)} changed, "
          f"{sum(r.firmware_recovered for r in results)} firmware value(s) recovered")
    if not args.apply:
        print("Dry run. Nothing was written. Pass --apply to rewrite.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
