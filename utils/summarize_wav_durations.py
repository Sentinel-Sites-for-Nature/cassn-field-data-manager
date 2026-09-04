#!/usr/bin/env python3
"""Summarize durations of ordinary PCM WAV files below one directory."""

from __future__ import annotations

import sys
import wave
from fractions import Fraction
from pathlib import Path


def wav_duration(path: Path) -> Fraction:
    """Return duration in seconds after reading only the WAV header."""
    with wave.open(str(path), "rb") as wav:
        return Fraction(wav.getnframes(), wav.getframerate())


def summarize_wavs(root: Path) -> None:
    total_duration = Fraction(0)
    files_found = 0
    files_measured = 0
    failures: list[tuple[Path, str]] = []

    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.casefold() != ".wav":
            continue

        files_found += 1
        try:
            duration = wav_duration(path)
        except Exception as error:
            failures.append((path, str(error)))
            continue

        files_measured += 1
        total_duration += duration
        print(f"{float(duration):12.3f} seconds  {path}")

    total_seconds = float(total_duration)
    print()
    print(f"WAV files found:       {files_found:,}")
    print(f"Successfully measured: {files_measured:,}")
    print(f"Could not be measured: {len(failures):,}")
    print(f"Total seconds:         {total_seconds:,.3f}")
    print(f"Total minutes:         {total_seconds / 60:,.2f}")
    print(f"Total hours:           {total_seconds / 3600:,.2f}")

    if failures:
        print("\nFiles requiring review:")
        for path, error in failures:
            print(f"  {path}: {error}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(f"Usage: {sys.argv[0]} /path/to/wav/catalog")
    summarize_wavs(Path(sys.argv[1]).resolve())
