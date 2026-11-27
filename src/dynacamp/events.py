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
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any


class EventKind(StrEnum):
    """Every type of campaign event.

    A ``StrEnum``: members compare equal to their string value and serialise
    to it, so the JSON log, the API and old saves see plain strings.
    """

    # ground war
    #: The theatre AI gave a brigade an order.
    AI_ORDER = "ai_order"
    #: An artillery fire mission.
    ARTILLERY_FIRE = "artillery_fire"
    #: An attacking brigade broke off, disorganised or too weak.
    ATTACK_HALTED = "attack_halted"
    #: A battle began (assault or meeting engagement).
    BATTLE_STARTED = "battle_started"
    #: A battle is over: winner and duration.
    BATTLE_ENDED = "battle_ended"
    #: A brigade on the move broke contact and fell back.
    CONTACT_BROKEN = "contact_broken"
    #: A brigade was destroyed (see :class:`LossReason`).
    DIVISION_DESTROYED = "division_destroyed"
    #: A destroyed brigade is back, at a depot.
    DIVISION_REFORMED = "division_reformed"
    #: A destroyed brigade will be re-formed after a delay.
    DIVISION_REFORMING = "division_reforming"
    #: A destroyed brigade left the campaign.
    DIVISION_REMOVED = "division_removed"
    #: A defeated brigade retreated to another province.
    DIVISION_RETREATED = "division_retreated"
    #: A faction's objective province changed hands.
    OBJECTIVE_CHANGED = "objective_changed"
    #: A brigade's order was cancelled (see :class:`OrderEnd`).
    ORDER_CANCELLED = "order_cancelled"
    #: A brigade reached its destination.
    ORDER_COMPLETED = "order_completed"
    #: A province changed hands.
    PROVINCE_CAPTURED = "province_captured"
    #: A brigade finished falling back.
    WITHDRAWAL_COMPLETED = "withdrawal_completed"

    # logistics
    #: A convoy unloaded its supply.
    CONVOY_DELIVERED = "convoy_delivered"
    #: A convoy was lost (see :class:`LossReason`).
    CONVOY_DESTROYED = "convoy_destroyed"
    #: A convoy left a depot.
    CONVOY_DISPATCHED = "convoy_dispatched"
    #: A convoy is back at its depot.
    CONVOY_RETURNED = "convoy_returned"

    # air
    #: An air-to-air pass between two flights.
    AIR_ENGAGEMENT = "air_engagement"
    #: A flight attacked its ground target.
    AIR_STRIKE = "air_strike"
    #: A squadron received a replacement aircraft.
    AIRCRAFT_DELIVERED = "aircraft_delivered"
    #: Aircraft lost by a flight.
    AIRCRAFT_LOST = "aircraft_lost"
    #: A transport delivered supply to an airbase or a brigade.
    AIRLIFT_DELIVERED = "airlift_delivered"
    #: A flight gave up its mission (see :class:`~dynacamp.air.FlightReason`).
    FLIGHT_ABORTED = "flight_aborted"
    #: A flight will not fly.
    FLIGHT_CANCELLED = "flight_cancelled"
    #: A flight is over: aircraft back to the squadron.
    FLIGHT_COMPLETE = "flight_complete"
    #: The AI part of a flight landed.
    FLIGHT_LANDED = "flight_landed"
    #: A planner changed a flight's route.
    FLIGHT_REPLANNED = "flight_replanned"
    #: A flight changed route around a SAM site that fired.
    FLIGHT_REROUTED = "flight_rerouted"
    #: A flight heads home.
    FLIGHT_RTB = "flight_rtb"
    #: A flight took off.
    FLIGHT_TAKEOFF = "flight_takeoff"
    #: All the flights of a package are over.
    PACKAGE_COMPLETE = "package_complete"
    #: A planner ordered a package.
    PACKAGE_ORDERED = "package_ordered"
    #: The ATO planned a package.
    PACKAGE_PLANNED = "package_planned"
    #: A planner moved a package's time on target.
    PACKAGE_RETIMED = "package_retimed"
    #: Interceptors scrambled against an intruder.
    SCRAMBLE = "scramble"

    # air defence
    #: Anti-radiation missiles on a SAM site (see :class:`StrikeResult`).
    HARM_STRIKE = "harm_strike"
    #: A SAM site was lost.
    SAM_DESTROYED = "sam_destroyed"
    #: A SAM site fired at a flight.
    SAM_ENGAGEMENT = "sam_engagement"
    #: A mobile SAM site moves to a new emplacement.
    SAM_RELOCATING = "sam_relocating"
    #: A replacement SAM battery arrived at a depot.
    SAM_REPLACED = "sam_replaced"
    #: Bombs on a SAM site (see :class:`StrikeResult`).
    SAM_STRUCK = "sam_struck"

    # players and the live game
    #: The game went away: what it held is back to the campaign.
    GAME_DISCONNECTED = "game_disconnected"
    #: The game reported losses of a delegated entity.
    GAME_LOSSES = "game_losses"
    #: An entity passed between campaign and game (see :class:`Simulator`).
    HANDOFF = "handoff"
    #: The game could not spawn an entity.
    HANDOFF_FAILED = "handoff_failed"
    #: The game did not acknowledge a handoff in time.
    HANDOFF_TIMEOUT = "handoff_timeout"
    #: A player appeared in the game.
    PLAYER_JOINED = "player_joined"
    #: A seated player landed.
    PLAYER_LANDED = "player_landed"
    #: A player left (see :class:`PlayerExit`).
    PLAYER_LEFT = "player_left"
    #: A seated player took off.
    PLAYER_TAKEOFF = "player_takeoff"
    #: A player gave up a seat.
    SEAT_RELEASED = "seat_released"
    #: A player took a seat in a flight.
    SEAT_TAKEN = "seat_taken"

    # campaign housekeeping
    #: The campaign was loaded from a save.
    CAMPAIGN_RESUMED = "campaign_resumed"
    #: The campaign was saved (see :class:`~dynacamp.persistence.SaveTrigger`).
    CAMPAIGN_SAVED = "campaign_saved"
    #: Pause or time compression changed.
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


# Vocabularies of the payloads: closed sets of values, readable in the log.


class LossReason(StrEnum):
    """Why something was destroyed or lost (``reason`` / ``cause`` of the loss events)."""
    #: An air attack.
    AIR = "air"
    #: Short-range air defence over the target.
    AIR_DEFENCE = "air defence"
    #: Lost in the game (inside a player bubble).
    GAME = "game"
    #: A player's aircraft.
    PLAYER = "player"
    #: Its province fell.
    CAPTURED = "captured"
    #: No way home.
    CUT_OFF = "cut off"
    #: Nowhere to retreat.
    ENCIRCLED = "encircled"
    #: Too weak to go on.
    SHATTERED = "shattered"
    #: Enemy brigades reached it.
    OVERRUN = "overrun"


class StrikeResult(StrEnum):
    """Outcome of an attack on a SAM site (``harm_strike``, ``sam_struck``)."""
    #: The radars switched off in time.
    WENT_DARK = "went dark"
    #: The site's radars were hit.
    RADAR_DESTROYED = "radar destroyed"
    #: The site shut down for a while.
    SUPPRESSED = "suppressed"
    #: No effect.
    MISSED = "missed"
    #: The site was destroyed (launchers partly survive).
    DESTROYED = "destroyed"


class OrderEnd(StrEnum):
    """Why a division's order was cancelled (``order_cancelled``)."""
    #: No way to the destination any more.
    ROUTE_BLOCKED = "route blocked"
    #: The enemy holds the road.
    ROAD_CONTESTED = "road contested"


class PlayerExit(StrEnum):
    """How a player left (``player_left``)."""
    #: The game reported that the player left.
    LEFT = "left"
    #: Not seen for too long.
    TIMEOUT = "timeout"


class Simulator(StrEnum):
    """Who an entity is handed to (``handoff``)."""
    #: The campaign engine.
    CAMPAIGN = "campaign"
    #: The game (inside a player bubble).
    GAME = "game"


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
            **data: Payload fields; enum members are stored as their value.

        Returns:
            The new event.

        Raises:
            ValueError: If ``kind`` is not a known event type.
        """
        data = {k: v.value if isinstance(v, Enum) else v for k, v in data.items()}
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
