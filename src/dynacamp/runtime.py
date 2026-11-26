"""Real-time driver for a live campaign.

The campaign normally runs time-compressed. As soon as a game is simulating
part of it (players connected or entities delegated) time drops to 1:1, because
the game cannot be fast-forwarded.

The loop is a fixed-rate game loop: every tick it advances the campaign by the
real time elapsed (times the compression), then sleeps for what is left of the
frame budget. Small ticks (10 Hz by default) keep the world moving smoothly
and the engine lock short; the sleep (never less than ``MIN_SLEEP_S``) leaves
the CPU to the API and the game adapter. A tick that overruns its budget is not
"caught up" with extra steps: the next one simply covers the longer interval
(capped at ``MAX_ELAPSED_S`` so a stall never becomes a time jump).

Pause and speed come from :attr:`Engine.control` and can change at any tick
(the API's ``POST /api/control``). Paused, the loop keeps stepping with
``dt = 0``: the game adapter is still polled and handoffs still happen.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from .engine import Engine

log = logging.getLogger(__name__)

#: Ticks per second of the real-time loop.
DEFAULT_TICK_HZ = 10.0
#: Minimum sleep per tick, even when a tick overruns.
MIN_SLEEP_S = 0.002
#: Real seconds one tick covers at most (after a stall the campaign does not jump ahead).
MAX_ELAPSED_S = 1.0


def run_realtime(
    engine: Engine,
    compression: float = 60.0,
    tick_s: float = 1.0 / DEFAULT_TICK_HZ,
    stop: Callable[[], bool] = lambda: False,
    on_tick: Callable[[Engine], None] | None = None,
) -> None:
    """Run the engine against the wall clock until ``stop()`` returns True.

    Time runs at :attr:`Engine.time_rate`: the compression of
    :attr:`Engine.control`, 0 while paused, 1:1 while a game simulates part of
    the campaign (:attr:`Engine.live`).

    Args:
        engine: The engine.
        compression: Initial time compression with no game involvement.
        tick_s: Real seconds between steps (frame budget).
        stop: Called every tick; return True to stop.
        on_tick: Optional callback after every step.
    """
    engine.control.set(compression=compression)
    last = time.monotonic()
    was = None
    while not stop():
        now = time.monotonic()
        elapsed, last = min(now - last, MAX_ELAPSED_S), now
        rate = engine.time_rate

        mode = "1x (live)" if engine.live else ("paused" if rate == 0 else f"{rate:g}x")

        if mode != was:
            log.info("time: %s", mode)
            was = mode

        engine.step(elapsed * rate)

        if on_tick:
            on_tick(engine)

        spent = time.monotonic() - now

        if spent > tick_s:
            log.debug("tick overran its budget: %.1f ms > %.1f ms", spent * 1000, tick_s * 1000)

        time.sleep(max(MIN_SLEEP_S, tick_s - spent))
