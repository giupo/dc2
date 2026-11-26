"""Append-only campaign event log.

Every meaningful change in the campaign is recorded as an event. The log is the
source for the future web UI (timeline, replay, front-line history) and for
debugging. Events are plain data so they serialise straight to JSON.

:class:`EventKind` is the one catalogue of event types. :data:`NOTABLE` and
:data:`PUBLIC` classify them; nothing else should keep its own list of kinds.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any


class EventKind(StrEnum):
    """Every type of campaign event.

    A ``StrEnum``: members compare equal to their string value and serialise
    to it, so the JSON log, the API and old saves see plain strings.
    """

    # ground war
    AI_ORDER = "ai_order"
    ARTILLERY_FIRE = "artillery_fire"
    ATTACK_HALTED = "attack_halted"
    BATTLE_STARTED = "battle_started"
    BATTLE_ENDED = "battle_ended"
    CONTACT_BROKEN = "contact_broken"
    DIVISION_DESTROYED = "division_destroyed"
    DIVISION_REFORMED = "division_reformed"
    DIVISION_REFORMING = "division_reforming"
    DIVISION_REMOVED = "division_removed"
    DIVISION_RETREATED = "division_retreated"
    OBJECTIVE_CHANGED = "objective_changed"
    ORDER_CANCELLED = "order_cancelled"
    ORDER_COMPLETED = "order_completed"
    PROVINCE_CAPTURED = "province_captured"
    WITHDRAWAL_COMPLETED = "withdrawal_completed"

    # logistics
    CONVOY_DELIVERED = "convoy_delivered"
    CONVOY_DESTROYED = "convoy_destroyed"
    CONVOY_DISPATCHED = "convoy_dispatched"
    CONVOY_RETURNED = "convoy_returned"

    # air
    AIR_ENGAGEMENT = "air_engagement"
    AIR_STRIKE = "air_strike"
    AIRCRAFT_DELIVERED = "aircraft_delivered"
    AIRCRAFT_LOST = "aircraft_lost"
    AIRLIFT_DELIVERED = "airlift_delivered"
    FLIGHT_ABORTED = "flight_aborted"
    FLIGHT_CANCELLED = "flight_cancelled"
    FLIGHT_COMPLETE = "flight_complete"
    FLIGHT_LANDED = "flight_landed"
    FLIGHT_REPLANNED = "flight_replanned"
    FLIGHT_REROUTED = "flight_rerouted"
    FLIGHT_RTB = "flight_rtb"
    FLIGHT_TAKEOFF = "flight_takeoff"
    PACKAGE_COMPLETE = "package_complete"
    PACKAGE_ORDERED = "package_ordered"
    PACKAGE_PLANNED = "package_planned"
    PACKAGE_RETIMED = "package_retimed"
    SCRAMBLE = "scramble"

    # air defence
    HARM_STRIKE = "harm_strike"
    SAM_DESTROYED = "sam_destroyed"
    SAM_ENGAGEMENT = "sam_engagement"
    SAM_RELOCATING = "sam_relocating"
    SAM_REPLACED = "sam_replaced"
    SAM_STRUCK = "sam_struck"

    # players and the live game
    GAME_DISCONNECTED = "game_disconnected"
    GAME_LOSSES = "game_losses"
    HANDOFF = "handoff"
    HANDOFF_FAILED = "handoff_failed"
    HANDOFF_TIMEOUT = "handoff_timeout"
    PLAYER_JOINED = "player_joined"
    PLAYER_LANDED = "player_landed"
    PLAYER_LEFT = "player_left"
    PLAYER_TAKEOFF = "player_takeoff"
    SEAT_RELEASED = "seat_released"
    SEAT_TAKEN = "seat_taken"

    # campaign housekeeping
    CAMPAIGN_RESUMED = "campaign_resumed"
    CAMPAIGN_SAVED = "campaign_saved"
    TIME_CONTROL = "time_control"


NOTABLE: frozenset[EventKind] = frozenset({
    EventKind.BATTLE_STARTED,
    EventKind.BATTLE_ENDED,
    EventKind.CONTACT_BROKEN,
    EventKind.PROVINCE_CAPTURED,
    EventKind.DIVISION_RETREATED,
    EventKind.DIVISION_DESTROYED,
    EventKind.OBJECTIVE_CHANGED,
    EventKind.PLAYER_JOINED,
    EventKind.PLAYER_LEFT,
    EventKind.HANDOFF,
    EventKind.HANDOFF_FAILED,
    EventKind.HANDOFF_TIMEOUT,
    EventKind.GAME_DISCONNECTED,
    EventKind.PACKAGE_PLANNED,
    EventKind.AIR_STRIKE,
    EventKind.AIR_ENGAGEMENT,
    EventKind.AIRCRAFT_LOST,
    EventKind.FLIGHT_ABORTED,
    EventKind.FLIGHT_RTB,
    EventKind.FLIGHT_REROUTED,
    EventKind.SCRAMBLE,
    EventKind.FLIGHT_CANCELLED,
    EventKind.SEAT_TAKEN,
    EventKind.SEAT_RELEASED,
})
"""Kinds worth a line in a non-verbose ``dynacamp simulate`` run."""

PUBLIC: frozenset[EventKind] = frozenset({
    EventKind.BATTLE_STARTED,
    EventKind.BATTLE_ENDED,
    EventKind.PROVINCE_CAPTURED,
    EventKind.AIR_ENGAGEMENT,
    EventKind.SAM_ENGAGEMENT,
    EventKind.AIRCRAFT_LOST,
    EventKind.PLAYER_JOINED,
    EventKind.PLAYER_LEFT,
})
"""Kinds every faction learns about, whoever caused them (fog of war)."""


@dataclass(frozen=True, slots=True)
class Event:
    """One entry of the campaign event log.

    Attributes:
        seq: Position in the log (0-based, gap-free).
        time: Campaign time in seconds when it happened.
        kind: Event type.
        data: Event payload (JSON-serialisable).
    """
    seq: int
    time: float  # campaign seconds since start
    kind: EventKind
    data: dict[str, Any] = field(default_factory=dict)


Subscriber = Callable[[Event], None]


def _upgraded(event: Event) -> Event:
    """``event`` with its kind as an :class:`EventKind` when it names one.

    Args:
        event: An event, possibly unpickled from an older save.

    Returns:
        The same event, or a copy with an :class:`EventKind` kind.
    """
    if isinstance(event.kind, EventKind) or event.kind not in EventKind._value2member_map_:
        return event
    return replace(event, kind=EventKind(event.kind))


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
        """Restore the events with no subscribers.

        Saves made before :class:`EventKind` hold plain strings: those naming a
        known kind become members, any other (a kind since retired) stays a string.

        Args:
            state: The pickled attributes.
        """
        self._events = [_upgraded(e) for e in state["_events"]]
        self._subscribers = []

    def emit(self, time: float, kind: EventKind, **data: Any) -> Event:
        """Append an event and notify subscribers.

        Args:
            time: Campaign time in seconds.
            kind: Event type. A plain string is accepted only if it names a
                member of :class:`EventKind`.
            **data: Payload fields.

        Returns:
            The new event.

        Raises:
            ValueError: If ``kind`` is not a known event type.
        """
        event = Event(seq=len(self._events), time=time, kind=EventKind(kind), data=data)
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

        Returns:
            The events, oldest first.
        """
        return self._events[seq:]

    def of_kind(self, kind: EventKind) -> list[Event]:
        """All events of one type.

        Args:
            kind: Event type.

        Returns:
            The events of that type, oldest first.
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
    """Campaign time as 'D<day> HH:MM' (day 1 starts at 00:00).

    Args:
        seconds: Campaign time in seconds.

    Returns:
        The formatted time, e.g. ``D2 06:30``.
    """
    d, rem = divmod(int(seconds), 86400)
    return f"D{d + 1} {rem // 3600:02d}:{rem % 3600 // 60:02d}"
