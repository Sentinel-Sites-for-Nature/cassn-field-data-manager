"""Relaunching requests the existing window through local IPC, without Accessibility."""

import hashlib

from PySide6.QtCore import QObject
from PySide6.QtNetwork import QLocalServer, QLocalSocket


def activation_name(directory):
    digest = hashlib.sha256(str(directory.resolve()).encode("utf-8")).hexdigest()[:20]
    return f"cassn-window-{digest}"


def activate_running_instance(name, timeout_ms=1000):
    """Ask an existing process to show its window; never terminate or restart it."""
    socket = QLocalSocket()
    try:
        socket.connectToServer(name)
        if not socket.waitForConnected(timeout_ms):
            return False
        socket.write(b"activate\n")
        if socket.bytesToWrite() and not socket.waitForBytesWritten(timeout_ms):
            return False
        while not socket.canReadLine():
            if not socket.waitForReadyRead(timeout_ms):
                return False
        return bytes(socket.readLine()) == b"ok\n"
    finally:
        socket.abort()


class ActivationServer(QObject):
    """The existing process handles window activation on its own GUI thread."""

    def __init__(self, name, activate, parent=None):
        super().__init__(parent)
        self._activate = activate
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.UserAccessOption)
        self._server.newConnection.connect(self._accept)
        if not self._server.listen(name):
            raise OSError(self._server.errorString())

    def _accept(self):
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            socket.setReadBufferSize(128)
            socket.readyRead.connect(lambda connection=socket: self._read(connection))
            socket.disconnected.connect(socket.deleteLater)
            self._read(socket)

    def _read(self, socket):
        if not socket.canReadLine():
            return
        command = bytes(socket.readLine())
        if command == b"activate\n":
            self._activate()
            socket.write(b"ok\n")
        else:
            socket.write(b"unsupported\n")
        socket.disconnectFromServer()

    def close(self):
        self._server.close()
