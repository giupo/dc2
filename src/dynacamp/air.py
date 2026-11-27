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

from . import catalog
from .forces import EntityKind, MobileEntity
from .geo import Vec2
from .world import Theater

#: Element id of a flight's aircraft in snapshots and game reports.
AIRCRAFT_ELEMENT = "aircraft"


class Task(StrEnum):
    """Air mission types."""
    #: Combat air patrol over a point.
    CAP = "CAP"
    #: Fighter escort of a package.
    ESCORT = "ESCORT"
    #: Suppression of enemy air defences.
    SEAD = "SEAD"
    #: Destruction of enemy air defences: a *package* task only. Its flights are strikers (STRIKE).
    #: on the site, SEAD with ECM keeping the radars down, and fighter escort; no squadron flies
    #: "DEAD" as such.
    DEAD = "DEAD"
    #: Close air support over a battle.
    CAS = "CAS"
    #: Battlefield interdiction: divisions / convoys behind the front.
    BAI = "BAI"
    #: Fixed targets (depots).
    STRIKE = "STRIKE"
    #: Airborne early warning: the main sensor of the air picture.
    AWACS = "AWACS"
    #: Air-to-air refuelling track.
    TANKER = "TANKER"
    #: Airlift of supply.
    TRANSPORT = "TRANSPORT"

    @property
    def air_to_air(self) -> bool:
        """True for fighter tasks (CAP, escort)."""
        return self in (Task.CAP, Task.ESCORT)

    @property
    def support(self) -> bool:
        """True for unarmed support missions (AWACS, tanker, transport)."""
        return self in (Task.AWACS, Task.TANKER, Task.TRANSPORT)

    @property
    def orbits(self) -> bool:
        """True for missions that hold a station (CAP, AWACS, tanker)."""
        return self in (Task.CAP, Task.AWACS, Task.TANKER)


class FlightReason(StrEnum):
    """Why a flight is cancelled, heads home or aborts its mission."""
    #: Its airbase fell before takeoff.
    AIRBASE_LOST = "airbase lost"
    #: The squadron has no aircraft left to fly it.
    NO_CREWS = "no crews"
    #: Cancelled by a planner's order.
    PLANNER = "cancelled by the planner"
    #: Just enough fuel left to get home.
    BINGO_FUEL = "bingo fuel"
    #: Its station time is over.
    END_OF_STATION = "end of station"
    #: Out of weapons.
    WINCHESTER = "winchester"
    #: Too many aircraft lost to go on.
    LOSSES = "losses"
    #: Enemy fighters broke up the attack.
    INTERCEPTED = "intercepted"
    #: An air defence site it cannot avoid.
    SAM_THREAT = "SAM threat"


class TargetKind(StrEnum):
    """What a flight flies against (with ``target_id``, the target itself)."""
    #: A battle (CAS).
    BATTLE = "battle"
    #: A brigade (BAI).
    DIVISION = "division"
    #: A supply convoy (BAI).
    CONVOY = "convoy"
    #: A province's supply depot, struck before structures existed (older saves).
    DEPOT = "depot"
    #: A structure, struck by strike packages: depot, airbase facilities, command post.
    STRUCTURE = "structure"
    #: An air defence site (SEAD, DEAD).
    SAM = "sam"
    #: A station: CAP, AWACS orbit, tanker track.
    POINT = "point"
    #: A friendly airbase to supply (TRANSPORT).
    AIRLIFT = "airlift"
    #: A friendly brigade to supply by helicopter (TRANSPORT).
    RESUPPLY = "resupply"
    #: A hostile aircraft to intercept (scramble).
    FLIGHT = "flight"


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
        cargo: Supply points one aircraft can airlift (transports).
        alt_m: Cruise altitude (0 = default for the kind of aircraft).
        radar_km: Air-search radar range (-1 = derived from the role).
        ground_km: Range at which its sensors (pod, eyes) find ground units
            (-1 = derived from the role).
    """
    id: str
    name: str
    cruise_kmh: float
    radius_km: float
    a2a: float  # air-to-air quality per aircraft
    a2g: float  # air-to-ground effectiveness per aircraft
    tasks: frozenset[Task]
    helo: bool = False
    cargo: float = 0.0
    alt_m: float = 0.0
    radar_km: float = -1.0
    ground_km: float = -1.0

    @property
    def cruise_alt_m(self) -> float:
        """Cruise altitude: the type's own, else 150 m for helicopters and 6000 m for jets."""
        if self.alt_m:
            return self.alt_m
        return 150.0 if self.helo else 6000.0

    @property
    def air_radar_m(self) -> float:
        """Air-search radar range in metres (fighters see far, strikers little)."""
        if self.radar_km >= 0:
            return self.radar_km * 1000
        if self.helo:
            return 0.0
        return 100_000.0 if self.a2a >= 0.9 else (60_000.0 if self.a2a > 0 else 30_000.0)

    @property
    def ground_sensor_m(self) -> float:
        """Range at which it finds ground units (targeting pod, eyes)."""
        if self.ground_km >= 0:
            return self.ground_km * 1000
        if self.helo:
            return 8_000.0
        return 20_000.0 if self.a2g >= 1.0 else 10_000.0


def _tasks(value: object) -> frozenset[Task]:
    """Catalogue converter: a list of task names to a frozen set of :class:`Task`.

    Args:
        value: The TOML list, e.g. ``["CAP", "ESCORT"]``.

    Returns:
        The tasks.

    Raises:
        TypeError: If it is not a list of strings.
        ValueError: On an unknown task name.
    """
    if not isinstance(value, list) or not all(isinstance(n, str) for n in value):
        raise TypeError(f"expected a list of task names, got {value!r}")
    return frozenset(Task(n) for n in value)


AIRCRAFT: dict[str, AircraftType] = catalog.build("aircraft", AircraftType, convert={"tasks": _tasks})
"""Every aircraft type, from ``data/catalog/aircraft.toml``."""


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

        Returns:
            The owning faction id, or None for a neutral base.
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
        insignia: File of the unit's insignia in the web UI's ``insignia/``
            folder (credits in ``CREDITS.md`` there), "" if it has none.
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
    insignia: str = ""  # file in web/public/insignia/ ("" = none)
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
        """Aircraft back from a mission: committed -> servicing until ``ready_time``.

        Args:
            count: Aircraft returning.
            ready_time: Campaign time when they are mission-ready again.
        """
        self.committed -= count
        self.servicing = [(r, n) for r, n in self.servicing if r > ready_time - 86400] + [(ready_time, count)]

    def next_callsign(self) -> str:
        """Next flight callsign, cycling ``<callsign> 1`` .. ``<callsign> 9``.

        Returns:
            The callsign, e.g. ``Lobo 3``.
        """
        self._callsign_counter = self._callsign_counter % 9 + 1
        return f"{self.callsign} {self._callsign_counter}"


class FlightStatus(StrEnum):
    """Life cycle of a flight."""
    #: On the ground, waiting for takeoff time.
    SCHEDULED = "scheduled"
    #: Flying towards the target / station.
    AIRBORNE = "airborne"
    #: Holding its station (CAP, AWACS orbit, tanker track).
    ON_STATION = "on_station"
    #: Returning to base.
    RTB = "rtb"
    #: Landed or lost; aircraft accounted back to the squadron.
    DONE = "done"
    #: Will not fly; its aircraft went back to the squadron.
    CANCELLED = "cancelled"


#: Statuses of a flight in the air.
ACTIVE_STATUSES = (FlightStatus.AIRBORNE, FlightStatus.ON_STATION, FlightStatus.RTB)


class SeatState(StrEnum):
    """State of a player's seat in a flight."""
    #: Taken, not yet airborne.
    ASSIGNED = "assigned"
    #: The player is flying.
    AIRBORNE = "airborne"
    #: The player landed.
    LANDED = "landed"
    #: The player's aircraft was lost.
    LOST = "lost"
    #: The player gave the seat up or disconnected.
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
        target_kind: What it flies against.
        target_id: Target id.
        station_until: End of the CAP station time.
        attacked: The attack has been made.
        ai_done: The AI part has landed or been shot down.
        players: Seats by player id.
        joinable: Players may still take seats.
        orbit_angle: Position on the CAP orbit.
        vhf_mhz: Intra-flight frequency, clear of the airfields (see :mod:`dynacamp.radio`).
        fuel_m: Distance the AI part can still fly, metres (-1: not fuelled yet;
            ``inf``: not tracked, e.g. support aircraft).
        aam_bvr: Radar-guided air-to-air missiles left on the AI aircraft (-1: not loaded yet).
        aam_wvr: Heat-seeking air-to-air missiles left on the AI aircraft.
        aam_range_m: Launch range of its radar-guided missiles.
        aam_loaded: Missiles it took off with (0: guns only).
        arm: Anti-radiation missiles left on the AI aircraft (SEAD).
        arm_range_m: Their launch range.
        track: Direction it last flew, radians (0 = north): where its radar looks.
        engaging: Hostile flight it is committed against ("" = none).
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
    target_kind: TargetKind = TargetKind.POINT
    target_id: str = ""
    station_until: float = 0.0
    attacked: bool = False
    ai_done: bool = False  # the AI part has landed or been shot down
    players: dict[str, Seat] = field(default_factory=dict)
    joinable: bool = True  # players may still take seats (until shortly after takeoff)
    cargo: float = 0.0  # supply carried (transports), for the whole flight
    vhf_mhz: float = 0.0  # intra-flight frequency (see dynacamp.radio), 0 = not assigned
    orbit_angle: float = 0.0  # position on the CAP orbit, radians
    fuel_m: float = -1.0  # range left for the AI part (-1: not fuelled yet, inf: not tracked)
    aam_bvr: int = -1  # radar missiles left on the AI aircraft (-1: not loaded yet)
    aam_wvr: int = 0  # heat seekers left
    aam_range_m: float = 0.0  # launch range of the radar missiles
    aam_loaded: int = 0  # missiles at takeoff (0: guns only)
    arm: int = 0  # anti-radiation missiles left on the AI aircraft
    arm_range_m: float = 0.0  # their launch range
    track: float = 0.0  # direction last flown, radians (0 = north): where the radar looks
    engaging: str = ""  # hostile flight it is committed against
    kind: EntityKind = EntityKind.FLIGHT

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

        Returns:
            The flight's position on the campaign plane.
        """
        if self.game_position is not None and not self.simulated_by_campaign:
            return self.game_position
        return self.pos

    def heading(self, theater: Theater) -> float:
        """Heading towards the next waypoint (radians).

        Args:
            theater: The map (unused, for the :class:`MobileEntity` interface).

        Returns:
            The heading in radians, 0 once past the last waypoint.
        """
        if self.wp_index < len(self.waypoints):
            return self.pos.heading_to(self.waypoints[self.wp_index])
        return 0.0


# ---------------------------------------------------------------- altitude profile
#
# One profile per flight, used by everyone: the players' briefing, the AI's
# route in the game, and the SAM model of the campaign (who can reach whom).
# The altitude of a waypoint is the one to fly *towards* it, as DCS AI does:
# CAS/BAI reach the IP at cruise and go down for the attack run to the
# target, stay low to the egress point and climb back home.

#: Altitude of the attack run, by task (the target and egress waypoints).
ATTACK_ALT_M = {Task.CAS: 1_500.0, Task.BAI: 1_500.0, Task.STRIKE: 6_000.0, Task.SEAD: 6_000.0, Task.ESCORT: 7_500.0}
#: Altitude of a CAP station.
CAP_ALT_M = 7_500.0


def waypoint_altitudes(f: Flight) -> list[float]:
    """Planned altitude at each waypoint of a flight, in metres.

    Cruise everywhere, except the CAP station (``CAP_ALT_M``) and, for attack
    sorties, the target and egress points (``ATTACK_ALT_M`` of the task).
    Helicopters stay at their (low) cruise altitude. Takeoff and landing
    points carry the cruise altitude too: the briefing shows them as 0.

    Args:
        f: The flight.

    Returns:
        One altitude per waypoint, in metres.
    """
    ac = f.aircraft_type
    n = len(f.waypoints)
    alts = [ac.cruise_alt_m] * n
    if ac.helo or n == 0:
        return alts
    ti = f.target_index
    if f.task is Task.CAP:
        alts[ti] = CAP_ALT_M
    elif not f.task.orbits and f.task is not Task.TRANSPORT:
        attack = ATTACK_ALT_M.get(f.task, ac.cruise_alt_m)
        alts[ti] = attack
        if ti + 1 < n - 1:  # the egress point (not the landing one)
            alts[ti + 1] = attack
    return alts


def flight_altitude(f: Flight) -> float:
    """Altitude the flight is at now: its station's while on station, else that
    of the waypoint it is flying to (see :func:`waypoint_altitudes`).

    Args:
        f: The flight.

    Returns:
        The altitude in metres.
    """
    alts = waypoint_altitudes(f)
    if not alts:
        return f.aircraft_type.cruise_alt_m
    if f.status is FlightStatus.ON_STATION:
        return alts[f.target_index]
    return alts[min(f.wp_index, len(alts) - 1)]



def package_number(package_id: str) -> str:
    """Four-digit package number (1000-9999) from a package id.

    Looks random but is deterministic and unique for the first 9000 packages:
    the id's counter is multiplied by a prime coprime with 9000.

    Args:
        package_id: Package id ending with a counter, e.g. ``pk42``.

    Returns:
        The package number as a string.
    """
    n = int("".join(c for c in package_id if c.isdigit()) or 0)
    return str((n * 7919) % 9000 + 1000)

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
        number: Four-digit package number, as in Falcon's ATO (see
            :func:`package_number`).
        uhf_mhz: Package common frequency, clear of the airfields (see
            :mod:`dynacamp.radio`).
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
    number: str = ""
    uhf_mhz: float = 0.0  # package common frequency (see dynacamp.radio), 0 = not assigned

    def __post_init__(self) -> None:
        """Derive the package number from the id when not given."""
        if not self.number:
            self.number = package_number(self.id)
