"""Tests for the generated thin macOS application bundle."""

from __future__ import annotations

import os
import plistlib

import pytest
from PIL import Image

from utils.install_macos_app import (
    APP_NAME,
    BUNDLE_IDENTIFIER,
    EXECUTABLE_NAME,
    install_command_link,
    install_macos_app,
)


def test_installer_builds_dock_ready_bundle_without_copying_python(tmp_path):
    repo = tmp_path / "repo"
    (repo / "utils").mkdir(parents=True)
    (repo / "assets").mkdir()
    (repo / "utils" / "cassn-app").write_text("#!/bin/sh\nexit 0\n")
    (repo / "utils" / "cassn-clear-staging").write_text("#!/bin/sh\nexit 0\n")
    (repo / "assets" / "cassn_icon_macos.png").write_bytes(b"test icon")
    app = install_macos_app(
        repo_root=repo,
        install_dir=tmp_path / "Applications",
        log_dir=tmp_path / "Logs",
        build_icon=False,
        install_links=False,
    )

    assert app == tmp_path / "Applications" / f"{APP_NAME}.app"
    executable = app / "Contents" / "MacOS" / EXECUTABLE_NAME
    assert executable.is_file()
    assert os.access(executable, os.X_OK)
    launcher = executable.read_text()
    assert str(repo / "utils" / "cassn-app") in launcher
    assert str(tmp_path / "Logs") in launcher
    assert "/usr/local/bin" in launcher
    assert not (app / "Contents" / "Resources" / "python").exists()

    with (app / "Contents" / "Info.plist").open("rb") as handle:
        plist = plistlib.load(handle)
    assert plist["CFBundleDisplayName"] == APP_NAME
    assert plist["CFBundleIdentifier"] == BUNDLE_IDENTIFIER
    assert plist["CFBundleExecutable"] == EXECUTABLE_NAME
    assert "CFBundleIconFile" not in plist


def test_installer_atomically_replaces_existing_bundle(tmp_path):
    repo = tmp_path / "repo"
    (repo / "utils").mkdir(parents=True)
    (repo / "assets").mkdir()
    (repo / "utils" / "cassn-app").write_text("#!/bin/sh\nexit 0\n")
    (repo / "utils" / "cassn-clear-staging").write_text("#!/bin/sh\nexit 0\n")
    (repo / "assets" / "cassn_icon_macos.png").write_bytes(b"test icon")
    install_dir = tmp_path / "Applications"
    existing = install_dir / f"{APP_NAME}.app"
    existing.mkdir(parents=True)
    (existing / "old").write_text("old")

    app = install_macos_app(
        repo_root=repo,
        install_dir=install_dir,
        log_dir=tmp_path / "Logs",
        build_icon=False,
        install_links=False,
    )

    assert app.is_dir()
    assert not (app / "old").exists()
    assert not (install_dir / f".{APP_NAME}.app.previous").exists()


def test_installer_builds_a_real_icns_resource(tmp_path):
    repo = tmp_path / "repo"
    (repo / "utils").mkdir(parents=True)
    (repo / "assets").mkdir()
    (repo / "utils" / "cassn-app").write_text("#!/bin/sh\nexit 0\n")
    (repo / "utils" / "cassn-clear-staging").write_text("#!/bin/sh\nexit 0\n")
    Image.new("RGBA", (1024, 1024), "green").save(
        repo / "assets" / "cassn_icon_macos.png"
    )

    app = install_macos_app(
        repo_root=repo,
        install_dir=tmp_path / "Applications",
        log_dir=tmp_path / "Logs",
        build_icon=True,
        install_links=False,
    )

    assert (app / "Contents" / "Resources" / "cassn.icns").is_file()


def test_command_link_refuses_to_replace_regular_file(tmp_path):
    target = tmp_path / "target"
    target.write_text("target")
    link = tmp_path / "command"
    link.write_text("user file")

    with pytest.raises(RuntimeError, match="non-symlink"):
        install_command_link(link, target)

    assert link.read_text() == "user file"
