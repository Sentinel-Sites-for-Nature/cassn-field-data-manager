"""Protect the installed app when a build cannot safely replace it."""
from pathlib import Path
from unittest.mock import Mock

import pytest

from utils import build_macos


@pytest.fixture
def bundles(tmp_path, monkeypatch):
    source = tmp_path / 'build' / 'CA-SSN Data Manager.app'
    source.mkdir(parents=True)
    (source / 'version').write_text('new')
    destination = tmp_path / 'Applications' / source.name
    destination.mkdir(parents=True)
    (destination / 'version').write_text('old')
    monkeypatch.setattr(build_macos.subprocess, 'check_output', lambda *a, **k: '')
    monkeypatch.setattr(build_macos.subprocess, 'run', Mock())
    return source, destination


def test_install_reuses_the_same_path(bundles):
    source, destination = bundles
    assert build_macos.install_built_app(source, destination.parent) == destination
    assert (destination / 'version').read_text() == 'new'
    assert (source / 'version').read_text() == 'new'


def test_running_installed_app_is_preserved(bundles, monkeypatch):
    source, destination = bundles
    command = str(destination / 'Contents' / 'MacOS' / destination.stem)
    monkeypatch.setattr(build_macos.subprocess, 'check_output', lambda *a, **k: command + '\n')
    with pytest.raises(RuntimeError, match='Quit'):
        build_macos.install_built_app(source, destination.parent)
    assert (destination / 'version').read_text() == 'old'


def test_failed_replacement_restores_old_app(bundles, monkeypatch):
    source, destination = bundles
    rename = Path.rename

    def fail_candidate(path, target):
        if path.parent.name.startswith('.cassn-install-'):
            raise OSError('simulated replacement failure')
        return rename(path, target)

    monkeypatch.setattr(Path, 'rename', fail_candidate)
    with pytest.raises(OSError, match='simulated'):
        build_macos.install_built_app(source, destination.parent)
    assert (destination / 'version').read_text() == 'old'
