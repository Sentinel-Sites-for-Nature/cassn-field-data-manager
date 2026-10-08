#!/usr/bin/env python3
"""Install a thin, Dock-ready macOS launcher for the CA-SSN application."""

from __future__ import annotations

import argparse
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cassn.config import VERSION  # noqa: E402


APP_NAME = "CA-SSN Field Data Manager"
BUNDLE_IDENTIFIER = "org.sentinelsitesfornature.cassn.field-data-manager"
EXECUTABLE_NAME = "cassn-field-data-manager"
DEFAULT_INSTALL_DIR = Path.home() / "Applications"
DEFAULT_LOG_DIR = Path.home() / "Library" / "Logs" / APP_NAME


def _launcher_script(command_path: Path, log_dir: Path) -> str:
    command = shlex.quote(str(command_path))
    logs = shlex.quote(str(log_dir))
    return f'''#!/bin/sh
set -u

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONUNBUFFERED=1

launcher={command}
log_dir={logs}
log_file="$log_dir/launcher.log"

mkdir -p "$log_dir"
if [ -f "$log_file" ] && [ "$(wc -c < "$log_file")" -gt 5242880 ]; then
    mv -f "$log_file" "$log_dir/launcher.previous.log"
fi

{{
    printf '\n[%s] Launching {APP_NAME}\n' "$(date '+%Y-%m-%d %H:%M:%S')"
    "$launcher"
    status=$?
    printf '[%s] Application exited with status %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$status"
}} >> "$log_file" 2>&1

if [ "$status" -ne 0 ]; then
    /usr/bin/osascript -e 'display dialog "CA-SSN Field Data Manager could not start. See ~/Library/Logs/CA-SSN Field Data Manager/launcher.log for details." buttons {{"OK"}} default button "OK" with icon stop'
fi
exit "$status"
'''


def _write_plist(path: Path, *, include_icon: bool) -> None:
    payload: dict[str, object] = {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleDisplayName": APP_NAME,
        "CFBundleExecutable": EXECUTABLE_NAME,
        "CFBundleIdentifier": BUNDLE_IDENTIFIER,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": APP_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
    }
    if include_icon:
        payload["CFBundleIconFile"] = "cassn.icns"
    with path.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=True)


def _build_icns(source_png: Path, destination: Path) -> None:
    """Build a multi-resolution macOS icon using the existing Pillow dependency."""
    with Image.open(source_png) as image:
        image.save(destination, format="ICNS")


def install_command_link(link: Path, target: Path) -> None:
    """Atomically create or refresh one user command symlink."""
    link = Path(link).expanduser()
    target = Path(target).resolve(strict=True)
    link.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(link) and not link.is_symlink():
        raise RuntimeError(f"refusing to replace non-symlink command: {link}")
    temporary = link.with_name(f".{link.name}.tmp")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, link)


def install_macos_app(
    *,
    repo_root: Path = REPO_ROOT,
    install_dir: Path = DEFAULT_INSTALL_DIR,
    log_dir: Path = DEFAULT_LOG_DIR,
    build_icon: bool = True,
    install_links: bool = True,
) -> Path:
    """Build and atomically install the thin ``.app`` bundle."""
    repo_root = Path(repo_root).resolve(strict=True)
    install_dir = Path(install_dir).expanduser()
    install_dir.mkdir(parents=True, exist_ok=True)
    app_path = install_dir / f"{APP_NAME}.app"
    if app_path.is_symlink():
        raise RuntimeError(f"refusing to replace symlinked application: {app_path}")

    app_command = repo_root / "utils" / "cassn-app"
    cleanup_command = repo_root / "utils" / "cassn-clear-staging"
    icon_source = repo_root / "assets" / "cassn_icon_macos.png"
    for required in (app_command, cleanup_command, icon_source):
        if not required.is_file():
            raise RuntimeError(f"required launcher asset is missing: {required}")

    command_dir = Path.home() / ".local" / "bin"
    stable_app_command = command_dir / "cassn-app"
    if install_links:
        install_command_link(stable_app_command, app_command)
        install_command_link(command_dir / "cassn-clear-staging", cleanup_command)
    else:
        stable_app_command = app_command

    temporary_root = Path(
        tempfile.mkdtemp(prefix=".cassn-app-install-", dir=install_dir)
    )
    staged_app = temporary_root / app_path.name
    contents = staged_app / "Contents"
    macos_dir = contents / "MacOS"
    resources = contents / "Resources"
    macos_dir.mkdir(parents=True)
    resources.mkdir()

    executable = macos_dir / EXECUTABLE_NAME
    executable.write_text(
        _launcher_script(stable_app_command, Path(log_dir).expanduser()),
        encoding="utf-8",
    )
    executable.chmod(0o755)
    _write_plist(contents / "Info.plist", include_icon=build_icon)
    if build_icon:
        _build_icns(icon_source, resources / "cassn.icns")

    backup = install_dir / f".{app_path.name}.previous"
    if backup.exists() or backup.is_symlink():
        raise RuntimeError(f"stale installer backup must be removed first: {backup}")
    try:
        if app_path.exists():
            app_path.rename(backup)
        staged_app.rename(app_path)
    except Exception:
        if not app_path.exists() and backup.exists():
            backup.rename(app_path)
        raise
    else:
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)
    return app_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Install a Dock-ready CA-SSN Field Data Manager.app that launches "
            "the current repository through its tested virtual environment."
        )
    )
    parser.add_argument(
        "--install-dir",
        type=Path,
        default=DEFAULT_INSTALL_DIR,
        help=f"application destination (default: {DEFAULT_INSTALL_DIR})",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        dest="open_after",
        help="open the application after installing it",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if sys.platform != "darwin":
        print("ERROR: this installer is only for macOS", file=sys.stderr)
        return 2
    args = build_parser().parse_args(argv)
    try:
        app_path = install_macos_app(install_dir=args.install_dir)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Installed: {app_path}")
    print(f"Launcher log: {DEFAULT_LOG_DIR / 'launcher.log'}")
    print("To pin it: open the app, right-click its Dock icon, then choose Options > Keep in Dock.")
    if args.open_after:
        subprocess.run(["open", str(app_path)], check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
