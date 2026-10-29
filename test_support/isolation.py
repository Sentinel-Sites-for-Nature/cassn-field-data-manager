"""Early accidental-side-effect guards for Python tests and their children.

This is test infrastructure, not an OS security sandbox. Native code and third
party plugins still require inspection; live integrations do not belong here.
"""
from __future__ import annotations

import atexit
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import threading

REPOSITORY = Path(__file__).resolve().parents[1]
STARTUP = REPOSITORY / "test_support" / "startup"
_POLICY = None
_INSTALLED = False
_LOCAL = threading.local()
_ORIGINAL_STAT = os.stat
_ORIGINAL_LSTAT = os.lstat
_ORIGINAL_OPEN = os.open
_TEMPORARIES = []


class TestIsolationError(RuntimeError):
    """An ordinary test attempted a real external effect."""
    __test__ = False


def _within(path, root):
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _canonical(path):
    if not isinstance(path, (str, bytes, os.PathLike)):
        return None
    previous = getattr(_LOCAL, "checking", False)
    _LOCAL.checking = True
    try:
        return os.path.normcase(os.path.realpath(os.fsdecode(path)))
    finally:
        _LOCAL.checking = previous


class Policy:
    def __init__(self, root):
        self.root = _canonical(root)
        self.read_roots = [_canonical(REPOSITORY), _canonical(sys.prefix), _canonical(sys.base_prefix)]
        # OS runtime, shared libraries, fonts and certificate/timezone data.
        self.read_roots += [_canonical(p) for p in
                            ('/usr', '/lib', '/lib64', '/System', '/Library', '/opt/homebrew',
                             '/etc/ssl', '/etc/localtime', '/etc/ld.so.cache', '/private/var/db/timezone', '/private/etc/zoneinfo', os.devnull)]
        self.private_repo = [_canonical(REPOSITORY / name) for name in
                             ('local_data', 'output', 'outputs', 'dist', 'build', '.aws', '.codex', '.agents')]
        self.python = _canonical(sys.executable)
        self.flac = _canonical(shutil.which('flac')) if shutil.which('flac') else None

    def lexical(self, value, *, write=False, metadata=False):
        if not isinstance(value, (str, bytes, os.PathLike)):
            return
        path = os.path.normcase(os.path.abspath(os.fsdecode(value)))
        if sys.platform == 'darwin':
            for alias in ('/tmp', '/var', '/etc'):
                if _within(path, alias):
                    path = '/private' + path
                    break
        roots = [self.root, *self.read_roots]
        if path == os.devnull or _within(path, self.root):
            return
        if not write and metadata and any(_within(root, path) for root in roots):
            return
        if not write and any(_within(path, root) for root in self.read_roots):
            if not any(_within(path, root) for root in self.private_repo):
                return
        raise TestIsolationError(f'Test isolation: production/unrelated path or write outside temporary session: {path}')

    def path(self, value, *, write=False, metadata=False):
        if getattr(_LOCAL, 'checking', False):
            return
        self.lexical(value, write=write, metadata=metadata)
        path = _canonical(value)
        if path is None:
            return  # Already-open descriptors (stdout, pipes, etc.).
        if path == _canonical(os.devnull):
            return
        if _within(path, self.root):
            return
        if write:
            raise TestIsolationError('Test isolation: writes must stay inside the test session directory')
        if metadata and any(_within(root, path) for root in [self.root, *self.read_roots]):
            return  # Resolving code/runtime ancestors does not enumerate them.
        if any(_within(path, root) for root in self.private_repo):
            raise TestIsolationError(f'Test isolation: private repository data is unavailable: {path}')
        if any(_within(path, root) for root in self.read_roots):
            return
        raise TestIsolationError(f'Test isolation: production or unrelated filesystem path is unavailable: {path}')

    def entry(self, value):
        # unlink/rename operate on the directory entry, not a symlink's target.
        # Validate its real parent, preserving safe cleanup of test symlinks.
        self.lexical(value, write=True)
        if _canonical(value) == self.root:
            return  # Removing this session's own registered temporary root.
        parent = os.path.dirname(os.path.abspath(os.fsdecode(value)))
        self.path(parent, write=True)

    def process(self, executable, argv, env):
        executable = os.fsdecode(executable)
        resolved = shutil.which(executable) if not os.path.isabs(executable) else executable
        path = _canonical(resolved) if resolved else None
        if path == self.flac and self.flac:
            # Existing lossless-audio tests, no arbitrary input/output locations.
            for arg in argv[1:]:
                arg = os.fsdecode(arg)
                if arg.startswith('--output-name='):
                    self.path(arg.split('=', 1)[1], write=True)
                elif not arg.startswith('-'):
                    self.path(arg, write=True)
                elif arg not in ('--silent', '--verify', '--keep-foreign-metadata', '--output-name',
                                 '-d', '-f', '-o', '--', *('-' + str(n) for n in range(9))):
                    raise TestIsolationError('Test isolation: unsupported native FLAC option')
            return
        python = path == self.python
        fake = bool(path and _within(path, self.root))
        if fake:
            self.path(path)
            with open(path, 'rb') as stream:
                shebang = stream.readline().decode('utf-8').strip()
            fake = shebang == '#!' + sys.executable
        if not python and not fake:
            raise TestIsolationError('Test isolation: real external commands are disabled')
        if python:
            # Permit the interpreter forms used by fixtures. Reject unknown or
            # combined flags that could suppress sitecustomize/PYTHONPATH.
            options = iter(argv[1:])
            for arg in options:
                if arg in ('-c', '-m', '--') or not arg.startswith('-'):
                    break
                if arg == '-W':
                    next(options, None)
                elif arg.startswith('-W') or arg in ('-B', '-u', '-s'):
                    continue
                else:
                    raise TestIsolationError('Test isolation: child Python must load the test guards')
        environment = os.environ if env is None else env
        if (environment.get('CASSN_TEST_ROOT') != self.root
                or environment.get('PYTHONPATH', '').split(os.pathsep)[0] != str(STARTUP)):
            raise TestIsolationError('Test isolation: child process lost its startup guards')


def _at(path, directory_fd=None):
    """Resolve relative audit paths against their actual directory descriptor."""
    if directory_fd in (None, -1) or not isinstance(path, (str, bytes, os.PathLike)) or os.path.isabs(path):
        return path
    if sys.platform == 'darwin':
        import fcntl
        parent = fcntl.fcntl(directory_fd, fcntl.F_GETPATH, b'\0' * 1024).split(b'\0', 1)[0]
        parent = os.fsdecode(parent)
    else:
        parent = os.readlink(f'/proc/self/fd/{directory_fd}')
    return os.path.join(parent, os.fsdecode(path))


def _audit(event, args):
    policy = _POLICY
    if policy is None or getattr(_LOCAL, 'checking', False):
        return
    if event == 'open':
        flags = args[2] or 0
        policy.path(_at(args[0], getattr(_LOCAL, 'open_directory_fd', None)), write=bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)))
    elif event in ('os.listdir', 'os.scandir'):
        policy.path(args[0])
    elif event in ('os.remove', 'os.rmdir', 'os.mkdir', 'os.chmod', 'os.chown', 'os.utime', 'os.truncate'):
        index = {'os.remove': 1, 'os.rmdir': 1, 'os.mkdir': 2, 'os.chmod': 2, 'os.chown': 3, 'os.utime': 3}.get(event)
        directory_fd = args[index] if index is not None and len(args) > index else None
        target = _at(args[0], directory_fd)
        if event in ('os.remove', 'os.rmdir'):
            policy.entry(target)
        else:
            policy.path(target, write=True)
    elif event in ('os.rename', 'os.link', 'os.symlink'):
        source_fd = args[2] if event in ('os.rename', 'os.link') and len(args) > 2 else None
        target_fd = args[3] if event in ('os.rename', 'os.link') and len(args) > 3 else (args[2] if len(args) > 2 else None)
        if event == 'os.rename':
            policy.entry(_at(args[0], source_fd))
        else:
            policy.path(_at(args[0], source_fd), write=event != 'os.symlink')
        policy.entry(_at(args[1], target_fd))
    elif event in ('os.system', 'os.posix_spawn', 'os.exec', 'os.fork', 'os.forkpty'):
        raise TestIsolationError('Test isolation: unguarded process creation is disabled')
    elif event == 'subprocess.Popen':
        policy.process(args[0], args[1], args[3])
    elif event in ('socket.connect', 'socket.bind', 'socket.sendto', 'socket.sendmsg'):
        sock, address = args[0], args[-1]
        if sock.family == getattr(socket, 'AF_UNIX', None) and isinstance(address, (str, bytes)) and address:
            policy.path(address, write=True)
        else:
            raise TestIsolationError('Test isolation: real network access is disabled')
    elif event in ('socket.getaddrinfo', 'socket.gethostbyname', 'socket.gethostbyaddr', 'socket.getnameinfo'):
        raise TestIsolationError('Test isolation: real network lookup is disabled')


def _deny(*args, **kwargs):
    raise TestIsolationError('Test isolation: real credential store or native network access is disabled')


def _guard_native_services():
    import keyring
    for name in ('get_password', 'set_password', 'delete_password', 'get_keyring'):
        setattr(keyring, name, _deny)
    if sys.platform == 'darwin':
        from keyring.backends import macOS
        for name in ('get_password', 'set_password', 'delete_password'):
            setattr(macOS.Keyring, name, _deny)
        for name in ('SecItemCopyMatching', 'SecItemAdd', 'SecItemUpdate', 'SecItemDelete'):
            if hasattr(macOS.api, name):
                setattr(macOS.api, name, _deny)
    from PySide6 import QtNetwork, QtGui
    QtGui.QDesktopServices.openUrl = _deny
    for cls, names in (
        (QtNetwork.QNetworkAccessManager, ('get', 'post', 'put', 'deleteResource', 'sendCustomRequest', 'connectToHost', 'connectToHostEncrypted')),
        (QtNetwork.QTcpSocket, ('connectToHost',)),
        (QtNetwork.QSslSocket, ('connectToHostEncrypted',)),
        (QtNetwork.QTcpServer, ('listen',)),
        (QtNetwork.QUdpSocket, ('bind', 'connectToHost', 'writeDatagram')),
    ):
        for name in names:
            setattr(cls, name, _deny)


def install(root):
    global _POLICY, _INSTALLED
    _POLICY = Policy(root)
    if not _INSTALLED:
        _guard_native_services()
        sys.addaudithook(_audit)
        def stat(path, *args, **kwargs):
            _POLICY.path(_at(path, kwargs.get("dir_fd")), metadata=True)
            return _ORIGINAL_STAT(path, *args, **kwargs)
        def lstat(path, *args, **kwargs):
            if getattr(_LOCAL, 'checking', False):
                _POLICY.lexical(_at(path, kwargs.get("dir_fd")), metadata=True)
            else:
                _POLICY.path(_at(path, kwargs.get("dir_fd")), metadata=True)
            return _ORIGINAL_LSTAT(path, *args, **kwargs)
        def open_file(path, *args, **kwargs):
            previous = getattr(_LOCAL, 'open_directory_fd', None)
            _LOCAL.open_directory_fd = kwargs.get('dir_fd')
            try:
                return _ORIGINAL_OPEN(path, *args, **kwargs)
            finally:
                _LOCAL.open_directory_fd = previous
        os.stat, os.lstat, os.open = stat, lstat, open_file
        _INSTALLED = True


def _environment(root, *, child=False):
    os.environ['CASSN_TEST_ROOT'] = str(root)
    if not child:
        os.environ['CASSN_CONFIG_ROOT'] = str(root / 'config')
        os.environ['CASSN_PROFILE'] = 'legacy'
    elif not _within(os.path.normcase(os.path.abspath(os.environ.get('CASSN_CONFIG_ROOT', ''))), str(root)):
        raise TestIsolationError('Test isolation: child configuration is outside the session')
    for name in tuple(os.environ):
        if name.startswith(('AWS_', 'BOTO_', 'BOX_', 'GOOGLE_', 'AZURE_')) or name.upper() in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'):
            os.environ.pop(name, None)
    os.environ.update(
        PATH=os.pathsep.join((str(Path(sys.executable).parent), '/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', '/bin')),
        AWS_CONFIG_FILE=str(root / 'aws-config'), AWS_SHARED_CREDENTIALS_FILE=str(root / 'aws-credentials'),
        AWS_EC2_METADATA_DISABLED='true', XDG_CACHE_HOME=str(root / 'cache'),
        XDG_CONFIG_HOME=str(root / 'xdg-config'), XDG_DATA_HOME=str(root / 'xdg-data'),
        TMPDIR=str(root / 'tmp'), TEMP=str(root / 'tmp'), TMP=str(root / 'tmp'),
        QT_QPA_PLATFORM='offscreen', PYTHONDONTWRITEBYTECODE='1',
        PYTHONPATH=os.pathsep.join((str(STARTUP), str(REPOSITORY))),
    )
    sys.dont_write_bytecode = True
    tempfile.tempdir = str(root / 'tmp')


def start_session():
    # Before project imports; ignore production config/profile overrides.
    temporary = tempfile.TemporaryDirectory(prefix='cassn-tests-', dir='/tmp' if os.name != 'nt' else None)
    root = Path(temporary.name).resolve()
    (root / 'tmp').mkdir()
    _TEMPORARIES.append(temporary)
    _environment(root)
    install(root)
    # Cleanup runs while the guard still permits this root; child processes exit first.
    atexit.register(temporary.cleanup)
    return root


def start_child():
    root = Path(os.environ['CASSN_TEST_ROOT'])
    _environment(root, child=True)
    install(root)
