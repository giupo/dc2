"""Run the FastAPI app with uvicorn in a background thread, next to the engine loop."""

from __future__ import annotations

import logging
import threading
import time

import uvicorn

from ..engine import Engine
from .app import create_app

log = logging.getLogger(__name__)


class ApiServer:
    """Runs the FastAPI app with uvicorn in a background thread."""
    def __init__(
        self,
        engine: Engine,
        host: str = "127.0.0.1",
        port: int = 8080,
        cors_origins: list[str] | None = None,
        log_level: str = "warning",
        web_dir: str | None = None,
        token: str | None = None,
        saver=None,
    ) -> None:
        """Prepare the server (not started yet).

        Args:
            engine: The engine whose campaign is served.
            host: Listen address.
            port: Listen port (0 = any free port).
            cors_origins: Allowed origins (default ``*``).
            log_level: uvicorn log level.
            web_dir: Built front-end to serve at ``/``.
            token: Bearer token required by commands; None leaves them open.
            saver: :class:`~dynacamp.persistence.Saver` behind ``POST /api/save`` (None: disabled).
        """
        self.app = create_app(engine, cors_origins, web_dir, token, saver)
        self.host, self.port = host, port
        self._server = uvicorn.Server(
            uvicorn.Config(self.app, host=host, port=port, log_level=log_level, lifespan="off")
        )
        self._thread: threading.Thread | None = None

    def start(self, timeout: float = 10.0) -> None:
        """Start serving and wait until the socket is open.

        Args:
            timeout: Seconds to wait for the socket to open.

        Raises:
            RuntimeError: If the server does not start in time.
        """
        self._thread = threading.Thread(target=self._server.run, name="api", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + timeout
        while not self._server.started:
            if not self._thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError(f"API server failed to start on {self.host}:{self.port}")
            time.sleep(0.02)
        # With port 0 the OS picks one: read it back.
        sock = self._server.servers[0].sockets[0]
        self.port = sock.getsockname()[1]
        log.info("API listening on http://%s:%s/api (docs at /docs)", self.host, self.port)

    def close(self) -> None:
        """Stop the server."""
        self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)
