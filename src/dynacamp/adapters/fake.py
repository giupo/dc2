"""An in-memory pretend game, for tests and offline experiments.

It acknowledges handoffs, drives materialised entities along their waypoints
in a straight line, and lets a test script players and casualties.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..geo import Vec2
from .base import (
    BriefingRequest,
    Command,
    DematerializeAck,
    Dematerialize,
    EntityState,
    GameAdapter,
    GameDisconnected,
    JoinRequest,
    LeaveRequest,
    Materialize,
    MaterializeAck,
    NotifyPlayer,
    PlayerEvent,
    PlayerEventKind,
    PlayerKind,
    PlayerLeft,
    PlayerSeen,
    Report,
    UpdateRoute,
)


@dataclass
class FakeEntity:
    """An entity held by the fake game: position, elements and route."""
    id: str
    position: Vec2
    elements: dict[str, float]
    waypoints: list[Vec2] = field(default_factory=list)
    speed_ms: float = 8.0


class FakeGame(GameAdapter):
    """In-memory game for tests: acknowledges handoffs, moves entities in straight
    lines and lets tests script players, joins and losses.
    """
    name = "fake"

    def __init__(self, auto_ack: bool = True) -> None:
        """Start a game with no entities.

        Args:
            auto_ack: Acknowledge materialise/dematerialise commands immediately.
        """
        self.auto_ack = auto_ack
        self.entities: dict[str, FakeEntity] = {}
        self.received: list[Command] = []
        self._reports: list[Report] = []
        self._connected = True

    # ------------------------------------------------------------ port

    @property
    def connected(self) -> bool:
        """True until :meth:`disconnect`."""
        return self._connected

    def send(self, commands: list[Command]) -> None:
        """Record commands and apply them to the fake world.

        Args:
            commands: Commands sent by the engine, in order.
        """
        for cmd in commands:
            self.received.append(cmd)
            match cmd:
                case Materialize(entity=snap):
                    self.entities[snap.id] = FakeEntity(
                        snap.id, snap.position, dict(snap.elements), list(snap.waypoints), snap.speed_kmh / 3.6
                    )
                    if self.auto_ack:
                        self._reports.append(MaterializeAck(snap.id))
                case Dematerialize(entity_id=eid):
                    ent = self.entities.pop(eid, None)
                    if self.auto_ack:
                        self._reports.append(
                            DematerializeAck(eid, ent.position if ent else None, dict(ent.elements) if ent else {})
                        )
                case UpdateRoute(entity_id=eid, waypoints=wps, speed_kmh=speed):
                    if eid in self.entities:
                        self.entities[eid].waypoints = list(wps)
                        self.entities[eid].speed_ms = speed / 3.6

    def poll(self) -> list[Report]:
        """Return and clear the queued reports.

        Returns:
            The reports queued since the last call.
        """
        out, self._reports = self._reports, []
        return out

    # ------------------------------------------------------------ scripting

    def player(self, pid: str, pos: Vec2, side: str = "blue", kind: PlayerKind = PlayerKind.AIR) -> None:
        """Report a player at ``pos``.

        Args:
            pid: Player id (also used as the name).
            pos: Where the player is.
            side: The player's coalition side.
            kind: Air or ground player.
        """
        self._reports.append(PlayerSeen(pid, pid, side, kind, pos))

    def join(self, pid: str, flight_id: str, tasks: frozenset[str] | None = None) -> None:
        """A player asks to join a flight.

        Args:
            pid: Player id.
            flight_id: The flight to join.
            tasks: Tasks the player's aircraft may fly (None = any).
        """
        self._reports.append(JoinRequest(pid, flight_id, tasks))

    def leave(self, pid: str) -> None:
        """A player leaves their flight.

        Args:
            pid: Player id.
        """
        self._reports.append(LeaveRequest(pid))

    def ask_briefing(self, pid: str) -> None:
        """A player asks for their briefing.

        Args:
            pid: Player id.
        """
        self._reports.append(BriefingRequest(pid))

    def player_event(self, pid: str, event: str) -> None:
        """Report ``takeoff``, ``landed`` or ``lost`` for a player.

        Args:
            pid: Player id.
            event: A :class:`PlayerEventKind` value.
        """
        self._reports.append(PlayerEvent(pid, PlayerEventKind(event)))

    def messages_for(self, pid: str) -> list[NotifyPlayer]:
        """Messages sent to a player so far.

        Args:
            pid: Player id.

        Returns:
            The :class:`NotifyPlayer` commands addressed to the player, oldest first.
        """
        return [c for c in self.received if isinstance(c, NotifyPlayer) and c.player_id == pid]

    def player_leaves(self, pid: str) -> None:
        """A player leaves the game.

        Args:
            pid: Player id.
        """
        self._reports.append(PlayerLeft(pid))

    def kill(self, eid: str, element: str, count: float) -> None:
        """Destroy ``count`` elements of a held entity.

        Args:
            eid: Id of an entity the fake game holds.
            element: The element to destroy.
            count: How many (the count never goes below zero).
        """
        ent = self.entities[eid]
        ent.elements[element] = max(0.0, ent.elements.get(element, 0.0) - count)

    def disconnect(self) -> None:
        """Simulate the game going away."""
        self._connected = False
        self.entities.clear()
        self._reports.append(GameDisconnected("test"))

    def advance(self, dt: float) -> None:
        """Move entities along their waypoints and report their state.

        Args:
            dt: Game seconds to simulate.
        """
        for ent in self.entities.values():
            budget = ent.speed_ms * dt
            while budget > 0 and ent.waypoints:
                target = ent.waypoints[0]
                d = ent.position.distance_to(target)
                if d <= budget:
                    ent.position = target
                    ent.waypoints.pop(0)
                    budget -= d
                else:
                    ent.position = ent.position.lerp(target, budget / d)
                    budget = 0
            self._reports.append(EntityState(ent.id, ent.position, dict(ent.elements)))
