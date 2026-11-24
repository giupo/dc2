"""Append-only campaign event log.

Every meaningful change in the campaign is recorded as an event. The log is the
source for the future web UI (timeline, replay, front-line history) and for
debugging. Events are plain data so they serialise straight to JSON.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class Event:
    """One entry of the campaign event log.

    Attributes:
        seq: Position in the log (0-based, gap-free).
        time: Campaign time in seconds when it happened.
        kind: Event type, e.g. ``"province_captured"``.
        data: Event payload (JSON-serialisable).
    """
    seq: int
    time: float  # campaign seconds since start
    kind: str
    data: dict[str, Any] = field(default_factory=dict)


Subscriber = Callable[[Event], None]


class EventLog:
    """Append-only list of :class:`Event` with synchronous subscribers."""
    def __init__(self) -> None:
        """Create an empty log."""
        self._events: list[Event] = []
        self._subscribers: list[Subscriber] = []

    def __getstate__(self) -> dict[str, Any]:
        """Pickle the events only: subscribers belong to the running process."""
        return {"_events": self._events}

    def __setstate__(self, state: dict[str, Any]) -> None:
        """Restore the events with no subscribers."""
        self._events = state["_events"]
        self._subscribers = []

    def emit(self, time: float, kind: str, **data: Any) -> Event:
        """Append an event and notify subscribers.

        Args:
            time: Campaign time in seconds.
            kind: Event type.
            **data: Payload fields.

        Returns:
            The new event.
        """
        event = Event(seq=len(self._events), time=time, kind=kind, data=data)
        self._events.append(event)
        for sub in self._subscribers:
            sub(event)
        return event

    def subscribe(self, fn: Subscriber) -> None:
        """Register a callback called with every new event.

        Args:
            fn: Callable receiving the :class:`Event`.
        """
        self._subscribers.append(fn)

    def since(self, seq: int) -> list[Event]:
        """Events from sequence number ``seq`` onwards.

        Args:
            seq: First sequence number to return.
        """
        return self._events[seq:]

    def of_kind(self, kind: str) -> list[Event]:
        """All events of one type.

        Args:
            kind: Event type.
        """
        return [e for e in self._events if e.kind == kind]

    def __iter__(self) -> Iterator[Event]:
        """Iterate over all events in order."""
        return iter(self._events)

    def __len__(self) -> int:
        """Number of events so far (also the next sequence number)."""
        return len(self._events)

    def write_jsonl(self, path: Path) -> None:
        """Write the whole log as JSON lines.

        Args:
            path: Output file.
        """
        with path.open("w", encoding="utf-8") as fh:
            for e in self._events:
                fh.write(json.dumps(asdict(e), default=str) + "\n")


def clock(seconds: float) -> str:
    """Campaign time as 'D<day> HH:MM' (day 1 starts at 00:00)."""
    d, rem = divmod(int(seconds), 86400)
    return f"D{d + 1} {rem // 3600:02d}:{rem % 3600 // 60:02d}"
