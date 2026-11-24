"""The port between the campaign core and a game.

The core speaks only in these messages. An adapter translates them into its
game's world (spawning groups, routing them, reading positions and losses) and
translates the game's observations back. Element counts are always in *campaign*
units: scaling to in-game objects is the adapter's business.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..geo import Vec2

# ------------------------------------------------------------------ core -> game


@dataclass(frozen=True, slots=True)
class EntitySnapshot:
    """Everything a game needs to create an entity.

    Attributes:
        id: Entity id.
        kind: ``division``, ``convoy``, ``flight`` or ``sam``.
        name: Display name.
        faction: Owning faction.
        side: Game side.
        position: Current position.
        heading: Radians, 0 = north.
        elements: Element counts in campaign units.
        waypoints: Remaining route.
        speed_kmh: Speed.
        altitude: Metres (flights).
        attrs: Kind-specific details (aircraft, task, SAM system, road behind...).
    """
    id: str
    kind: str  # "division" | "convoy"
    name: str
    faction: str
    side: str  # game-facing side ("blue"/"red")
    position: Vec2
    heading: float  # radians, 0 = north
    elements: dict[str, float]
    waypoints: tuple[Vec2, ...] = ()
    speed_kmh: float = 30.0
    altitude: float = 0.0  # metres, for flights
    # Kind-specific details (for flights: aircraft, task, target, callsign, helo...).
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Materialize:
    """Create this entity in the game; from now on the game simulates it."""

    entity: EntitySnapshot


@dataclass(frozen=True, slots=True)
class Dematerialize:
    """Remove the entity from the game and report its final state."""

    entity_id: str


@dataclass(frozen=True, slots=True)
class UpdateRoute:
    """The campaign gave a delegated entity new orders."""

    entity_id: str
    waypoints: tuple[Vec2, ...]
    speed_kmh: float


@dataclass(frozen=True, slots=True)
class PublishAto:
    """The flights players of ``side`` can join (see systems.ato.ato_board)."""

    side: str
    packages: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class NotifyPlayer:
    """Show a message (and optionally map marks) to one player."""

    player_id: str
    text: str
    marks: tuple[tuple[str, Vec2], ...] = ()


Command = Materialize | Dematerialize | UpdateRoute | PublishAto | NotifyPlayer

# ------------------------------------------------------------------ game -> core


class PlayerKind(StrEnum):
    """What a player is flying or driving."""
    AIR = "air"
    HELO = "helo"
    GROUND = "ground"


@dataclass(frozen=True, slots=True)
class PlayerSeen:
    """A player is present (sent periodically).

    Attributes:
        player_id: Player id.
        name: Display name.
        side: Game side.
        kind: Aircraft, helicopter or ground.
        position: Position.
        altitude: Metres.
        aircraft_type: Game type name (informational).
    """
    player_id: str
    name: str
    side: str
    kind: PlayerKind
    position: Vec2
    altitude: float = 0.0
    aircraft_type: str = ""  # game type name, informational


@dataclass(frozen=True, slots=True)
class PlayerLeft:
    """A player left the game."""
    player_id: str


@dataclass(frozen=True, slots=True)
class MaterializeAck:
    """The game created (or failed to create) an entity.

    Attributes:
        entity_id: Entity id.
        ok: Success.
        error: Failure reason.
    """
    entity_id: str
    ok: bool = True
    error: str = ""


@dataclass(frozen=True, slots=True)
class EntityState:
    """Periodic observation of a delegated entity."""

    entity_id: str
    position: Vec2 | None
    elements: dict[str, float] = field(default_factory=dict)  # alive, campaign units


@dataclass(frozen=True, slots=True)
class DematerializeAck:
    """The game removed an entity; its final state.

    Attributes:
        entity_id: Entity id.
        position: Last position (None if nothing survived).
        elements: Alive elements in campaign units.
    """
    entity_id: str
    position: Vec2 | None
    elements: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GameDisconnected:
    """The game connection was lost: the engine takes everything back."""
    reason: str = ""


class PlayerEventKind(StrEnum):
    """Player flight events."""
    TAKEOFF = "takeoff"
    LANDED = "landed"
    LOST = "lost"  # crashed, ejected, killed


@dataclass(frozen=True, slots=True)
class PlayerEvent:
    """A player took off, landed or was lost."""
    player_id: str
    event: PlayerEventKind


@dataclass(frozen=True, slots=True)
class JoinRequest:
    """A player asks for a seat in a flight from the ATO board."""

    player_id: str
    flight_id: str
    capable_tasks: frozenset[str] | None = None  # None = no restriction known


@dataclass(frozen=True, slots=True)
class LeaveRequest:
    """A player gives up their seat."""
    player_id: str


@dataclass(frozen=True, slots=True)
class BriefingRequest:
    """A player asks for their briefing again."""
    player_id: str


Report = (
    PlayerSeen
    | PlayerLeft
    | MaterializeAck
    | EntityState
    | DematerializeAck
    | GameDisconnected
    | PlayerEvent
    | JoinRequest
    | LeaveRequest
    | BriefingRequest
)


class GameAdapter(ABC):
    """Port between the engine and a game.

    Implementations translate :data:`Command` objects into game actions and game
    observations into :data:`Report` objects. Both calls must not block.
    """
    name: str = "game"

    @property
    @abstractmethod
    def connected(self) -> bool:
        """True while the game is connected."""
        ...

    @abstractmethod
    def send(self, commands: list[Command]) -> None:
        """Deliver commands to the game (non-blocking)."""

    @abstractmethod
    def poll(self) -> list[Report]:
        """Return the reports received since the last call (non-blocking)."""

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release resources (optional)."""
        pass
