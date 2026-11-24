"""Real-time driver for a live campaign.

The campaign normally runs time-compressed. As soon as a game is simulating
part of it (players connected or entities delegated) time drops to 1:1, because
the game cannot be fast-forwarded.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from .engine import Engine

log = logging.getLogger(__name__)


def run_realtime(
    engine: Engine,
    compression: float = 60.0,
    tick_s: float = 1.0,
    stop: Callable[[], bool] = lambda: False,
    on_tick: Callable[[Engine], None] | None = None,
) -> None:
    """Run the engine against the wall clock until ``stop()`` returns True.

    Time runs at ``compression`` times real time, or 1:1 while a game simulates
    part of the campaign (:attr:`Engine.live`).

    Args:
        engine: The engine.
        compression: Time compression with no game involvement.
        tick_s: Real seconds between steps.
        stop: Called every tick; return True to stop.
        on_tick: Optional callback after every step.
    """
    last = time.monotonic()
    was_live = False
    while not stop():
        now = time.monotonic()
        elapsed, last = now - last, now
        live = engine.live
        if live != was_live:
            log.info("time compression %s", "1x (live)" if live else f"{compression:g}x")
            was_live = live
        engine.step(elapsed * (1.0 if live else compression))
        if on_tick:
            on_tick(engine)
        time.sleep(max(0.0, tick_s - (time.monotonic() - now)))
