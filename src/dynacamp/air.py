"""Air power: aircraft, airbases, squadrons, packages and flights.

Unlike ground units, flights move freely in the plane (not on the province
graph). A flight is a mobile entity like any other, so player bubbles and
handoffs work the same: an AI flight inside a bubble is flown by the game.

Players do not get their own flights: they take *seats* in flights the ATO
planned. A seat replaces one of the squadron's AI-flown aircraft; the rest of
the flight is still flown by the campaign (or by the game inside a bubble).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from .forces import MobileEntity
from .geo import Vec2
from .world import Theater


class Task(StrEnum):
    """Air mission types."""
    CAP = "CAP"  # combat air patrol over a point
    ESCORT = "ESCORT"  # fighter escort of a package
    SEAD = "SEAD"  # suppression of enemy air defences
    CAS = "CAS"  # close air support over a battle
    BAI = "BAI"  # battlefield interdiction: divisions / convoys behind the front
    STRIKE = "STRIKE"  # fixed targets (depots)

    @property
    def air_to_air(self) -> bool:
        """True for fighter tasks (CAP, escort)."""
        return self in (Task.CAP, Task.ESCORT)


@dataclass(frozen=True, slots=True)
class AircraftType:
    """A campaign aircraft type (game-agnostic).

    Attributes:
        id: Type id, e.g. ``"f16c"``.
        name: Display name.
        cruise_kmh: Cruise speed.
        radius_km: Combat radius.
        a2a: Air-to-air quality per aircraft.
        a2g: Air-to-ground effectiveness per aircraft.
        tasks: Tasks it can fly.
        helo: Rotary wing.
    """
    id: str
    name: str
    cruise_kmh: float
    radius_km: float
    a2a: float  # air-to-air quality per aircraft
    a2g: float  # air-to-ground effectiveness per aircraft
    tasks: frozenset[Task]
    helo: bool = False

    @property
    def cruise_alt_m(self) -> float:
        """Typical cruise altitude: 150 m for helicopters, 6000 m for jets."""
        return 150.0 if self.helo else 6000.0


def _t(*names: str) -> frozenset[Task]:
    """Frozen set of tasks from their names."""
    return frozenset(Task(n) for n in names)


AIRCRAFT: dict[str, AircraftType] = {
    a.id: a
    for a in (
        AircraftType("f16c", "F-16C", 800, 550, 1.0, 1.0, _t("CAP", "ESCORT", "SEAD", "STRIKE", "BAI", "CAS")),
        AircraftType("fa18c", "F/A-18C", 800, 550, 0.9, 1.0, _t("CAP", "ESCORT", "SEAD", "STRIKE", "BAI", "CAS")),
        AircraftType("f15c", "F-15C", 850, 700, 1.3, 0.0, _t("CAP", "ESCORT")),
        AircraftType("a10c", "A-10C", 550, 450, 0.2, 1.4, _t("CAS", "BAI")),
        AircraftType("ah64d", "AH-64D", 250, 150, 0.05, 1.2, _t("CAS", "BAI"), helo=True),
        AircraftType("su27", "Su-27", 850, 700, 1.2, 0.0, _t("CAP", "ESCORT")),
        AircraftType("mig29s", "MiG-29S", 850, 450, 1.0, 0.2, _t("CAP", "ESCORT")),
        AircraftType("su25t", "Su-25T", 650, 400, 0.2, 1.2, _t("CAS", "BAI", "SEAD")),
        AircraftType("su24m", "Su-24M", 800, 600, 0.1, 1.1, _t("STRIKE", "BAI")),
        AircraftType("mi24p", "Mi-24P", 250, 160, 0.05, 1.0, _t("CAS", "BAI"), helo=True),
    )
}


@dataclass
class Airbase:
    """An airfield.

    Attributes:
        id: Airbase id.
        name: In-game airdrome name.
        position: Location.
        province: Province it belongs to (its controller owns the base).
        faction: Fixed owner for rear bases outside the province map.
    """
    id: str
    name: str  # in-game name (e.g. the DCS airdrome name)
    position: Vec2
    province: str | None = None  # whoever controls it owns the base
    faction: str | None = None  # for rear bases outside the province map

    def owner(self, theater: Theater) -> str | None:
        """Faction owning the base now.

        Args:
            theater: The map (for the province controller).
        """
        if self.province is not None:
            return theater.provinces[self.province].controller
        return self.faction


@dataclass
class Squadron:
    """An air unit based at an airfield.

    Attributes:
        id: Squadron id.
        name: Display name.
        faction: Owner.
        airbase: Home airbase id.
        aircraft_type: Aircraft type.
        aircraft: Airframes on strength.
        callsign: Callsign stem for its flights (e.g. ``"Viper"``).
        tasks: Tasks it may be given (default: those of the aircraft type).
        committed: Airframes assigned to flights now.
        establishment: Authorised strength replacements fill up to.
        servicing: (ready time, count) of airframes being turned around.
    """
    id: str
    name: str
    faction: str
    airbase: str
    aircraft_type: AircraftType
    aircraft: int  # airframes on strength
    callsign: str
    tasks: frozenset[Task] = frozenset()
    committed: int = 0  # airframes currently assigned to flights
    establishment: int = 0  # authorised strength: replacements fill up to this (0 = initial strength)
    # (ready_time, count): airframes back from a mission, being turned around
    servicing: list[tuple[float, int]] = field(default_factory=list)
    _callsign_counter: int = 0

    def __post_init__(self) -> None:
        """Default tasks and establishment from the aircraft type and initial strength."""
        if not self.tasks:
            self.tasks = self.aircraft_type.tasks
        if not self.establishment:
            self.establishment = self.aircraft

    def available_at(self, now: float) -> int:
        """Airframes free for a new flight.

        Args:
            now: Campaign time.

        Returns:
            Strength minus committed and still-servicing airframes.
        """
        servicing = sum(n for ready, n in self.servicing if ready > now)
        return self.aircraft - self.committed - servicing

    def release(self, count: int, ready_time: float) -> None:
        """Aircraft back from a mission: committed -> servicing until ``ready_time``."""
        self.committed -= count
        self.servicing = [(r, n) for r, n in self.servicing if r > ready_time - 86400] + [(ready_time, count)]

    def next_callsign(self) -> str:
        """Next flight callsign, cycling ``<callsign> 1`` .. ``<callsign> 9``."""
        self._callsign_counter = self._callsign_counter % 9 + 1
        return f"{self.callsign} {self._callsign_counter}"


class FlightStatus(StrEnum):
    """Life cycle of a flight."""
    SCHEDULED = "scheduled"  # on the ground, waiting for takeoff time
    AIRBORNE = "airborne"  # flying towards the target / station
    ON_STATION = "on_station"  # CAP orbiting
    RTB = "rtb"
    DONE = "done"  # landed or lost; aircraft accounted back to the squadron
    CANCELLED = "cancelled"


ACTIVE_STATUSES = (FlightStatus.AIRBORNE, FlightStatus.ON_STATION, FlightStatus.RTB)


class SeatState(StrEnum):
    """State of a player's seat in a flight."""
    ASSIGNED = "assigned"
    AIRBORNE = "airborne"
    LANDED = "landed"
    LOST = "lost"
    LEFT = "left"


@dataclass
class Seat:
    """A player's seat in a flight.

    Attributes:
        player_id: Player id.
        name: Player name.
        state: Assigned, airborne, landed, lost or left.
    """
    player_id: str
    name: str
    state: SeatState = SeatState.ASSIGNED


@dataclass(eq=False, kw_only=True)
class Flight(MobileEntity):
    """A flight of an ATO package, partly AI-flown and partly flown by players.

    Attributes:
        package: Package id.
        squadron: Squadron id.
        aircraft_type: Aircraft type.
        task: Mission.
        callsign: Flight callsign.
        size: Aircraft in the flight (AI + players), decreasing with losses.
        takeoff_time: Planned takeoff (campaign seconds).
        tot: Time on target.
        waypoints: Route (home, IP, target/station, home).
        target_index: Waypoint index of the target or CAP station.
        pos: Current position of the AI part.
        status: Life-cycle status.
        wp_index: Next waypoint.
        target_kind: ``battle``, ``division``, ``convoy``, ``depot``, ``sam`` or ``point``.
        target_id: Target id.
        station_until: End of the CAP station time.
        attacked: The attack has been made.
        ai_done: The AI part has landed or been shot down.
        players: Seats by player id.
        joinable: Players may still take seats.
        orbit_angle: Position on the CAP orbit.
    """
    package: str
    squadron: str
    aircraft_type: AircraftType
    task: Task
    callsign: str
    size: int  # aircraft in the flight (AI + players), decreases with losses
    takeoff_time: float
    tot: float  # time on target
    waypoints: list[Vec2]
    target_index: int  # waypoint index of the target / station
    pos: Vec2
    status: FlightStatus = FlightStatus.SCHEDULED
    wp_index: int = 1  # next waypoint to fly to
    target_kind: str = ""  # "battle" | "division" | "convoy" | "depot" | "point"
    target_id: str = ""
    station_until: float = 0.0
    attacked: bool = False
    ai_done: bool = False  # the AI part has landed or been shot down
    players: dict[str, Seat] = field(default_factory=dict)
    joinable: bool = True  # players may still take seats (until shortly after takeoff)
    orbit_angle: float = 0.0  # position on the CAP orbit, radians
    kind: str = "flight"

    @property
    def target_point(self) -> Vec2:
        """The target or station waypoint."""
        return self.waypoints[self.target_index]

    @property
    def active_seats(self) -> list[Seat]:
        """Seats whose player has not landed, been lost or left."""
        return [s for s in self.players.values() if s.state in (SeatState.ASSIGNED, SeatState.AIRBORNE)]

    @property
    def player_aircraft(self) -> int:
        """Aircraft of this flight that players fly (lost ones are already out of ``size``)."""
        return sum(1 for s in self.players.values() if s.state in (SeatState.ASSIGNED, SeatState.AIRBORNE, SeatState.LANDED))

    @property
    def ai_count(self) -> int:
        """Aircraft flown by the AI (size minus player aircraft)."""
        return 0 if self.ai_done else max(0, self.size - self.player_aircraft)

    @property
    def seats_free(self) -> int:
        """Seats players can still take."""
        return self.ai_count if self.joinable else 0

    @property
    def airborne(self) -> bool:
        """True between takeoff and landing."""
        return self.status in ACTIVE_STATUSES

    @property
    def materializable(self) -> bool:
        """The game can take the AI part over: airborne with AI aircraft."""
        return self.airborne and self.ai_count > 0

    @property
    def speed_ms(self) -> float:
        """Cruise speed in m/s."""
        return self.aircraft_type.cruise_kmh / 3.6

    def position(self, theater: Theater) -> Vec2:
        """Current position (reported by the game while delegated).

        Args:
            theater: The map (unused, for the :class:`MobileEntity` interface).
        """
        if self.game_position is not None and not self.simulated_by_campaign:
            return self.game_position
        return self.pos

    def heading(self, theater: Theater) -> float:
        """Heading towards the next waypoint (radians)."""
        if self.wp_index < len(self.waypoints):
            return self.pos.heading_to(self.waypoints[self.wp_index])
        return 0.0


@dataclass
class Package:
    """A set of flights against one target with a common time on target.

    Attributes:
        id: Package id.
        faction: Owner.
        task: Main task.
        target_label: Human-readable target.
        target_point: Target position.
        tot: Time on target.
        flights: Flight ids.
        created: Planning time.
        done: All flights complete.
    """
    id: str
    faction: str
    task: Task  # the main task
    target_label: str
    target_point: Vec2
    tot: float
    flights: list[str] = field(default_factory=list)
    created: float = 0.0
    done: bool = False
