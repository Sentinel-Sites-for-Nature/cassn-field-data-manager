#!/usr/bin/env python3
"""Read-only validator for a curated CA-SSN runtime lookup directory."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cassn.config import LOCAL_DATA_DIR  # noqa: E402
from cassn.lookup_sync import validate_lookup_directory  # noqa: E402
from cassn.lookups import LookupSchemaError  # noqa: E402


def validate_curated_lookup_directory(lookup_dir: Path) -> str:
    """Validate ``lookup_dir`` without copying, publishing, or changing files."""
    resolved = lookup_dir.expanduser().resolve()
    _lookups, result = validate_lookup_directory(resolved)
    hashes = ", ".join(
        f"{name}={digest}" for name, digest in sorted(result.hashes.items())
    )
    return (
        f"Valid curated lookup directory: {resolved}\n"
        f"devices={result.devices}; deployments={result.deployments}\n"
        f"sha256: {hashes}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate curated devices.csv and deployments.csv together with the "
            "complete runtime lookup snapshot. This command is read-only."
        )
    )
    parser.add_argument(
        "lookup_dir",
        nargs="?",
        type=Path,
        default=LOCAL_DATA_DIR,
        help=f"lookup directory (default: {LOCAL_DATA_DIR})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        print(validate_curated_lookup_directory(args.lookup_dir))
    except (LookupSchemaError, OSError, ValueError) as exc:
        print(f"Invalid curated lookup directory: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
