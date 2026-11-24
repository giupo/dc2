"""DCS adapter: a TCP server the in-mission Lua bridge connects to.

The engine side is synchronous: ``send``/``poll`` never block. A background
thread accepts the (single) DCS connection and queues incoming JSON lines.
"""

from __future__ import annotations

import json
import logging
import queue
import socket
import threading
from typing import Any

from ..base import Command, GameAdapter, Report
from .protocol import DcsTranslator

log = logging.getLogger(__name__)


class DcsAdapter(GameAdapter):
    """TCP server the DCS Lua bridge connects to (JSON lines).

    A background thread accepts the single DCS connection and queues incoming
    messages; :meth:`send` and :meth:`poll` never block.
    """
    name = "dcs"

    def __init__(self, host: str = "127.0.0.1", port: int = 50555, translator: DcsTranslator | None = None) -> None:
        """Args:
            host: Listen address.
            port: Listen port (0 = any free port).
            translator: Message translator (default :class:`DcsTranslator`).
        """
        self.host, self.port = host, port
        self.translator = translator or DcsTranslator()
        self._inbox: queue.Queue[dict[str, Any]] = queue.Queue()
        self._conn: socket.socket | None = None
        self._lock = threading.Lock()
        self._running = False
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Open the listening socket and start the I/O thread."""
        self._server = socket.create_server((self.host, self.port), reuse_port=False)
        self._server.settimeout(1.0)
        self.port = self._server.getsockname()[1]
        self._running = True
        self._thread = threading.Thread(target=self._serve, name="dcs-adapter", daemon=True)
        self._thread.start()
        log.info("DCS adapter listening on %s:%s", self.host, self.port)

    def close(self) -> None:
        """Close the connection and the server."""
        self._running = False
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None
        if self._server:
            self._server.close()
        if self._thread:
            self._thread.join(timeout=3)

    # ------------------------------------------------------------ port

    @property
    def connected(self) -> bool:
        """True while the DCS bridge is connected."""
        return self._conn is not None

    def send(self, commands: list[Command]) -> None:
        """Encode and send commands (dropped if DCS is not connected)."""
        lines = [json.dumps(self.translator.encode(c), separators=(",", ":")) + "\n" for c in commands]
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.sendall("".join(lines).encode("utf-8"))
            except OSError as exc:
                log.warning("send to DCS failed: %s", exc)
                self._drop_locked(str(exc))

    def poll(self) -> list[Report]:
        """Decode the messages received since the last call."""
        reports: list[Report] = []
        while True:
            try:
                msg = self._inbox.get_nowait()
            except queue.Empty:
                return reports
            try:
                reports += self.translator.decode(msg)
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("bad message from DCS %r: %s", msg, exc)

    # ------------------------------------------------------------ io thread

    def _serve(self) -> None:
        """Accept loop of the I/O thread."""
        assert self._server is not None
        while self._running:
            try:
                conn, addr = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            log.info("DCS connected from %s", addr)
            conn.settimeout(1.0)
            with self._lock:
                if self._conn is not None:
                    self._conn.close()
                self._conn = conn
            self._read(conn)

    def _read(self, conn: socket.socket) -> None:
        """Read JSON lines from a connection until it closes."""
        buf = b""
        while self._running:
            try:
                chunk = conn.recv(65536)
            except TimeoutError:
                continue
            except OSError as exc:
                self._drop(conn, str(exc))
                return
            if not chunk:
                self._drop(conn, "closed by DCS")
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    self._inbox.put(json.loads(line))
                except json.JSONDecodeError:
                    log.warning("malformed line from DCS: %r", line[:200])

    def _drop(self, conn: socket.socket, reason: str) -> None:
        """Drop ``conn`` if it is still the current connection."""
        with self._lock:
            if self._conn is conn:
                self._drop_locked(reason)

    def _drop_locked(self, reason: str) -> None:
        """Close the current connection and report the disconnection (lock held)."""
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
            self._conn = None
            self._inbox.put({"msg": "_disconnected", "reason": reason})
            log.info("DCS disconnected: %s", reason)
