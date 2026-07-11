"""Single-instance coordination for the native desktop application."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QByteArray, QObject, Slot
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from app_meta import APP_BUNDLE_IDENTIFIER


ACTIVATE_MESSAGE = b"activate\n"
DEFAULT_CONNECT_TIMEOUT_MS = 750


def default_server_name() -> str:
    return f"{APP_BUNDLE_IDENTIFIER}.native"


class SingleInstanceCoordinator(QObject):
    """Own a local server or notify the already-running application."""

    def __init__(
        self,
        on_activate: Callable[[], None],
        parent: QObject | None = None,
        *,
        server_name: str | None = None,
    ) -> None:
        super().__init__(parent)
        self._on_activate = on_activate
        self._server_name = server_name or default_server_name()
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._accept_connections)
        self._sockets: set[QLocalSocket] = set()

    @property
    def server_name(self) -> str:
        return self._server_name

    def claim_or_notify(self, timeout_ms: int = DEFAULT_CONNECT_TIMEOUT_MS) -> bool:
        """Return True for the primary instance, False after notifying it."""
        if self._notify_existing(timeout_ms):
            return False
        if self._server.listen(self._server_name):
            return True

        # A crashed process can leave a stale endpoint behind. A second
        # connection check avoids removing a live server during a startup race.
        if self._notify_existing(timeout_ms):
            return False
        QLocalServer.removeServer(self._server_name)
        return self._server.listen(self._server_name)

    def close(self) -> None:
        try:
            self._server.newConnection.disconnect(self._accept_connections)
        except (RuntimeError, TypeError):
            pass
        for socket in tuple(self._sockets):
            self._dispose_socket(socket, abort=True)
        if self._server.isListening():
            self._server.close()

    def _notify_existing(self, timeout_ms: int) -> bool:
        socket = QLocalSocket(self)
        self._track_socket(socket)
        socket.connectToServer(self._server_name)
        if not socket.waitForConnected(max(0, int(timeout_ms))):
            self._dispose_socket(socket, abort=True)
            return False
        socket.write(QByteArray(ACTIVATE_MESSAGE))
        socket.flush()
        socket.waitForBytesWritten(max(0, int(timeout_ms)))
        socket.disconnectFromServer()
        self._dispose_socket(socket, abort=True)
        return True

    def _accept_connections(self) -> None:
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if socket is None:
                continue
            self._track_socket(socket)
            self._read_socket(socket)

    def _track_socket(self, socket: QLocalSocket) -> None:
        self._sockets.add(socket)
        socket.readyRead.connect(self._read_sender_socket)
        socket.disconnected.connect(self._drop_sender_socket)

    @Slot()
    def _read_sender_socket(self) -> None:
        socket = self.sender()
        if isinstance(socket, QLocalSocket) and socket in self._sockets:
            self._read_socket(socket)

    @Slot()
    def _drop_sender_socket(self) -> None:
        socket = self.sender()
        if isinstance(socket, QLocalSocket):
            self._dispose_socket(socket)

    def _read_socket(self, socket: QLocalSocket) -> None:
        if ACTIVATE_MESSAGE.strip() in bytes(socket.readAll()).splitlines():
            self._on_activate()

    def _dispose_socket(self, socket: QLocalSocket, *, abort: bool = False) -> None:
        """Schedule one deletion even when ``abort`` emits ``disconnected`` inline."""

        if socket not in self._sockets:
            return
        self._sockets.remove(socket)
        try:
            socket.readyRead.disconnect(self._read_sender_socket)
        except (RuntimeError, TypeError):
            pass
        try:
            socket.disconnected.disconnect(self._drop_sender_socket)
        except (RuntimeError, TypeError):
            pass
        if abort:
            socket.abort()
        socket.deleteLater()
