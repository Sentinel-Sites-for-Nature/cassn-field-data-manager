"""Negative controls: rejected effects must never reach real files/services."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

import pytest

from test_support.isolation import REPOSITORY, TestIsolationError


def test_config_cache_and_temporary_files_are_isolated_before_import(test_session_root):
    from cassn import config
    assert config.BASE_CONFIG_DIR == test_session_root / 'config'
    assert config.LOCAL_DATA_DIR.is_relative_to(test_session_root)
    assert Path(tempfile.gettempdir()) == test_session_root / 'tmp'
    assert Path(os.environ['XDG_CACHE_HOME']).is_relative_to(test_session_root)
    assert Path(os.environ['AWS_SHARED_CREDENTIALS_FILE']).is_relative_to(test_session_root)
    assert os.environ['QT_QPA_PLATFORM'] == 'offscreen'
    assert sys.dont_write_bytecode
    # HOME is unchanged; access control, not a fake home, protects the workstation.
    assert Path.home() != test_session_root


@pytest.mark.parametrize('relative', ['.cassn_config/config.json', '.aws/credentials',
                                     'Library/CloudStorage/Box-Box/CASSN/data/media.jpg',
                                     'Desktop/CASSN_field_data_staging/session.json'])
def test_real_user_paths_cannot_be_read_or_probed(relative):
    path = Path.home() / relative
    for action in (lambda: path.read_bytes(), lambda: path.exists(), lambda: list(path.parent.iterdir())):
        with pytest.raises(TestIsolationError):
            action()


@pytest.mark.parametrize('path', ['/Volumes/CASSN-test-prohibited/media.jpg', '/mnt/cassn-test-prohibited/media.jpg',
                                 str(REPOSITORY / 'local_data/future_work/private.md'),
                                 str(REPOSITORY / 'output/test_test_isolation_must_not_be_collected.py')])
def test_production_and_ignored_inputs_are_unavailable(path):
    with pytest.raises(TestIsolationError):
        Path(path).read_bytes()


def test_writes_cannot_touch_the_checkout_or_unrelated_disk():
    for path in (REPOSITORY / 'must-not-be-written.txt', Path('/tmp/cassn-outside-test-session.txt')):
        with pytest.raises(TestIsolationError):
            path.write_text('this must never be written')


def test_symlink_cannot_escape_to_private_input(tmp_path):
    # Creation of a link to an unrelated source is itself rejected.
    with pytest.raises(TestIsolationError):
        (tmp_path / 'private-link').symlink_to(Path.home() / '.aws/credentials')


def test_write_symlink_to_readable_source_still_cannot_modify_source(tmp_path):
    link = tmp_path / 'README-link'
    link.symlink_to(REPOSITORY / 'README.md')
    with pytest.raises(TestIsolationError):
        link.write_text('must not replace README')


def test_network_connections_listeners_and_dns_are_disabled():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(TestIsolationError):
            sock.connect(('127.0.0.1', 1))
        with pytest.raises(TestIsolationError):
            sock.bind(('127.0.0.1', 0))
    with pytest.raises(TestIsolationError):
        socket.getaddrinfo('example.invalid', 443)


def test_native_network_browser_and_credentials_are_disabled():
    import keyring
    from PySide6 import QtGui, QtNetwork
    with pytest.raises(TestIsolationError):
        keyring.get_password('test-only-service', 'test-only-account')
    with pytest.raises(TestIsolationError):
        QtGui.QDesktopServices.openUrl(None)
    with pytest.raises(TestIsolationError):
        QtNetwork.QTcpSocket.connectToHost(None, '127.0.0.1', 1)


@pytest.mark.parametrize('command', [['aws', 's3', 'ls'], ['security', 'find-generic-password'],
                                   ['curl', 'https://example.invalid'], ['/bin/sh', '-c', 'true']])
def test_real_commands_cannot_start(command):
    with pytest.raises(TestIsolationError):
        subprocess.run(command, check=True)


def test_python_child_inherits_guards_before_application_import(monkeypatch, test_session_root):
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'synthetic-must-not-reach-child')
    script = '''
import json, os, socket, sys
assert 'cassn.config' not in sys.modules
from cassn import config
from test_support.isolation import TestIsolationError
assert 'AWS_ACCESS_KEY_ID' not in os.environ
try:
    socket.getaddrinfo('example.invalid', 443)
except TestIsolationError:
    pass
else:
    raise AssertionError('DNS escaped guard')
print(json.dumps(str(config.BASE_CONFIG_DIR)))
'''
    result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == str(test_session_root / 'config')


@pytest.mark.parametrize('flag', ['-I', '-E', '-S', '-IS', '-ES', '--isolated', '--ignore-environment'])
def test_python_child_cannot_skip_startup_guards(flag):
    with pytest.raises(TestIsolationError):
        subprocess.run([sys.executable, flag, '-c', 'pass'], check=True)


def test_child_without_startup_environment_is_rejected():
    environment = dict(os.environ)
    environment.pop('PYTHONPATH')
    with pytest.raises(TestIsolationError):
        subprocess.run([sys.executable, '-c', 'pass'], env=environment)


def test_failed_child_guard_startup_stops_before_user_code():
    environment = dict(os.environ, CASSN_CONFIG_ROOT='/not-a-test-session/config')
    result = subprocess.run([sys.executable, '-c', 'print("UNSAFE USER CODE RAN")'], env=environment,
                            text=True, capture_output=True)
    assert result.returncode == 97
    assert not result.stdout
    assert 'isolation startup failed' in result.stderr


def test_directory_descriptor_operations_remain_inside_real_temporary_directory(tmp_path):
    child = tmp_path / 'child'
    child.mkdir()
    descriptor = os.open(tmp_path, os.O_RDONLY)
    try:
        os.rmdir('child', dir_fd=descriptor)
    finally:
        os.close(descriptor)
    assert not child.exists()


def test_fixture_cleanup_removes_link_without_touching_readonly_target(tmp_path):
    import shutil
    directory = tmp_path / 'links'
    directory.mkdir()
    source = REPOSITORY / 'README.md'
    before = source.read_bytes()
    (directory / 'link').symlink_to(source)
    shutil.rmtree(directory)
    assert source.read_bytes() == before
    assert not directory.exists()


def test_session_cleanup_can_remove_only_its_owned_root(test_session_root):
    # The parent directory is not writable test space; only the registered root
    # gets this exception for TemporaryDirectory's final rmdir.
    from test_support import isolation
    isolation._POLICY.entry(test_session_root)
    with pytest.raises(TestIsolationError):
        isolation._POLICY.entry(test_session_root.parent / 'not-the-session')


def test_native_audio_options_cannot_smuggle_an_external_file(test_session_root):
    from test_support import isolation
    policy = isolation._POLICY
    if policy.flac is None:
        pytest.skip('FLAC is not installed; no native audio exception')
    with pytest.raises(TestIsolationError, match='FLAC option'):
        subprocess.run([policy.flac, '--tag-from-file=COMMENT=/not-a-test-session/private.txt'])
