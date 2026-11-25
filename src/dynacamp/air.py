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
    # Destruction of enemy air defences: a *package* task only. Its flights are
    # strikers (STRIKE) on the site, SEAD with ECM keeping the radars down, and
    # fighter escort; no squadron flies "DEAD" as such.
    DEAD = "DEAD"
    CAS = "CAS"  # close air support over a battle
    BAI = "BAI"  # battlefield interdiction: divisions / convoys behind the front
    STRIKE = "STRIKE"  # fixed targets (depots)
    AWACS = "AWACS"  # airborne early warning: the main sensor of the air picture
    TANKER = "TANKER"  # air-to-air refuelling track
    TRANSPORT = "TRANSPORT"  # airlift of supply
    SCRAMBLE = "SCRAMBLE"
    
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
        # ---- 1980s-2000s, western / blue
        AircraftType("f14a", "F-14A", 850, 700, 1.1, 0.3, _t("CAP", "ESCORT")),
        AircraftType("f14b", "F-14B", 850, 750, 1.2, 0.6, _t("CAP", "ESCORT", "STRIKE", "BAI")),
        AircraftType("f15e", "F-15E", 850, 900, 1.1, 1.3, _t("CAP", "ESCORT", "STRIKE", "BAI", "CAS")),
        AircraftType("f16a", "F-16A", 800, 500, 0.9, 0.8, _t("CAP", "ESCORT", "BAI", "CAS")),
        AircraftType("f4e", "F-4E", 800, 500, 0.7, 0.8, _t("CAP", "ESCORT", "STRIKE", "BAI", "CAS")),
        AircraftType("a10a", "A-10A", 550, 400, 0.2, 1.2, _t("CAS", "BAI")),
        AircraftType("av8b", "AV-8B", 650, 350, 0.4, 1.1, _t("CAS", "BAI", "STRIKE")),
        AircraftType("f117", "F-117A", 800, 1000, 0.0, 1.3, _t("STRIKE")),
        AircraftType("b1b", "B-1B", 900, 3000, 0.0, 3.0, _t("STRIKE", "BAI")),
        AircraftType("b52h", "B-52H", 800, 3500, 0.0, 3.0, _t("STRIKE")),
        AircraftType("tornado_gr4", "Tornado GR4", 800, 700, 0.2, 1.2, _t("STRIKE", "BAI", "SEAD")),
        AircraftType("tornado_ids", "Tornado IDS", 800, 700, 0.2, 1.1, _t("STRIKE", "BAI", "SEAD")),
        AircraftType("m2000c", "Mirage 2000C", 850, 450, 1.1, 0.5, _t("CAP", "ESCORT", "BAI")),
        AircraftType("m2000_5", "Mirage 2000-5", 850, 500, 1.2, 0.2, _t("CAP", "ESCORT")),
        AircraftType("f5e", "F-5E", 750, 300, 0.6, 0.5, _t("CAP", "ESCORT", "CAS")),
        AircraftType("jf17", "JF-17", 800, 500, 0.9, 1.0, _t("CAP", "ESCORT", "SEAD", "CAS", "BAI", "STRIKE")),
        AircraftType("mirage_f1", "Mirage F1EE", 800, 400, 0.7, 0.6, _t("CAP", "ESCORT", "CAS", "BAI")),
        AircraftType("ajs37", "AJS-37 Viggen", 800, 400, 0.4, 1.0, _t("BAI", "STRIKE", "CAS")),
        AircraftType("ah1w", "AH-1W", 230, 150, 0.05, 1.0, _t("CAS", "BAI"), helo=True),
        AircraftType("oh58d", "OH-58D", 200, 120, 0.0, 0.6, _t("CAS", "BAI"), helo=True),
        AircraftType("sa342", "SA342 Gazelle", 250, 140, 0.0, 0.6, _t("CAS", "BAI"), helo=True),
        # ---- 1980s-2000s, eastern / red
        AircraftType("mig29a", "MiG-29A", 850, 400, 0.9, 0.2, _t("CAP", "ESCORT")),
        AircraftType("mig29g", "MiG-29G", 850, 400, 0.9, 0.2, _t("CAP", "ESCORT")),
        AircraftType("mig31", "MiG-31", 1000, 700, 1.1, 0.0, _t("CAP", "ESCORT")),
        AircraftType("mig25pd", "MiG-25PD", 1000, 600, 0.8, 0.0, _t("CAP")),
        AircraftType("mig23mld", "MiG-23MLD", 850, 450, 0.7, 0.5, _t("CAP", "ESCORT", "CAS", "BAI")),
        AircraftType("mig27k", "MiG-27K", 800, 450, 0.2, 1.0, _t("CAS", "BAI", "STRIKE", "SEAD")),
        AircraftType("su33", "Su-33", 850, 700, 1.2, 0.4, _t("CAP", "ESCORT")),
        AircraftType("su30", "Su-30", 850, 800, 1.3, 1.0, _t("CAP", "ESCORT", "SEAD", "STRIKE", "BAI")),
        AircraftType("su34", "Su-34", 850, 900, 0.6, 1.4, _t("STRIKE", "BAI", "CAS", "SEAD")),
        AircraftType("su25", "Su-25", 650, 400, 0.2, 1.1, _t("CAS", "BAI")),
        AircraftType("su25tm", "Su-25TM", 650, 400, 0.2, 1.3, _t("CAS", "BAI", "SEAD")),
        AircraftType("su17m4", "Su-17M4", 800, 400, 0.2, 0.9, _t("CAS", "BAI", "STRIKE")),
        AircraftType("tu22m3", "Tu-22M3", 900, 2000, 0.0, 2.5, _t("STRIKE")),
        AircraftType("tu95ms", "Tu-95MS", 750, 4000, 0.0, 2.5, _t("STRIKE")),
        # (Tu-160 left out: DCS' data has no weapons for its internal bays.)
        # ---- support: AWACS, tankers, transports
        AircraftType("e3a", "E-3A Sentry", 750, 1500, 0.0, 0.0, _t("AWACS"), alt_m=9000, radar_km=400, ground_km=0),
        AircraftType("e2c", "E-2C Hawkeye", 550, 1200, 0.0, 0.0, _t("AWACS"), alt_m=8000, radar_km=350, ground_km=0),
        AircraftType("a50", "A-50 Mainstay", 750, 1500, 0.0, 0.0, _t("AWACS"), alt_m=9000, radar_km=300, ground_km=0),
        AircraftType("kc135", "KC-135", 800, 2500, 0.0, 0.0, _t("TANKER"), alt_m=7000, radar_km=0, ground_km=0),
        AircraftType("kc130", "KC-130", 550, 2000, 0.0, 0.0, _t("TANKER"), alt_m=5500, radar_km=0, ground_km=0),
        AircraftType("il78m", "IL-78M", 750, 2500, 0.0, 0.0, _t("TANKER"), alt_m=7000, radar_km=0, ground_km=0),
        AircraftType("c130", "C-130", 550, 2000, 0.0, 0.0, _t("TRANSPORT"), cargo=150, alt_m=6000, radar_km=0, ground_km=0),
        AircraftType("c17", "C-17A", 750, 3000, 0.0, 0.0, _t("TRANSPORT"), cargo=400, alt_m=8000, radar_km=0, ground_km=0),
        AircraftType("il76", "IL-76MD", 750, 2500, 0.0, 0.0, _t("TRANSPORT"), cargo=300, alt_m=8000, radar_km=0, ground_km=0),
        AircraftType("an26", "An-26B", 430, 1000, 0.0, 0.0, _t("TRANSPORT"), cargo=60, alt_m=5000, radar_km=0, ground_km=0),
        AircraftType("uh60a", "UH-60A", 250, 400, 0.0, 0.0, _t("TRANSPORT"), helo=True, cargo=25, radar_km=0),
        AircraftType("ch47d", "CH-47D", 250, 400, 0.0, 0.0, _t("TRANSPORT"), helo=True, cargo=60, radar_km=0),
        AircraftType("mi8mt", "Mi-8MT", 230, 400, 0.0, 0.0, _t("TRANSPORT"), helo=True, cargo=30, radar_km=0),
        AircraftType("mi26", "Mi-26", 250, 500, 0.0, 0.0, _t("TRANSPORT"), helo=True, cargo=100, radar_km=0),
        AircraftType("j11a", "J-11A", 850, 700, 1.2, 0.3, _t("CAP", "ESCORT")),
        AircraftType("mig21bis", "MiG-21bis", 800, 300, 0.6, 0.4, _t("CAP", "ESCORT", "CAS")),
        AircraftType("ka50", "Ka-50", 270, 160, 0.05, 1.2, _t("CAS", "BAI"), helo=True),
        AircraftType("mi28n", "Mi-28N", 260, 160, 0.05, 1.2, _t("CAS", "BAI"), helo=True),
        AircraftType("mi24v", "Mi-24V", 250, 160, 0.05, 1.0, _t("CAS", "BAI"), helo=True),
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
        vhf_mhz: Intra-flight frequency, clear of the airfields (see :mod:`dynacamp.radio`).
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
    cargo: float = 0.0  # supply carried (transports), for the whole flight
    vhf_mhz: float = 0.0  # intra-flight frequency (see dynacamp.radio), 0 = not assigned
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


# ---------------------------------------------------------------- altitude profile
#
# One profile per flight, used by everyone: the players' briefing, the AI's
# route in the game, and the SAM model of the campaign (who can reach whom).
# The altitude of a waypoint is the one to fly *towards* it, as DCS AI does:
# CAS/BAI reach the IP at cruise and go down for the attack run to the
# target, stay low to the egress point and climb back home.

ATTACK_ALT_M = {Task.CAS: 1_500.0, Task.BAI: 1_500.0, Task.STRIKE: 6_000.0, Task.SEAD: 6_000.0, Task.ESCORT: 7_500.0}
CAP_ALT_M = 7_500.0


def waypoint_altitudes(f: Flight) -> list[float]:
    """Planned altitude at each waypoint of a flight, in metres.

    Cruise everywhere, except the CAP station (``CAP_ALT_M``) and, for attack
    sorties, the target and egress points (``ATTACK_ALT_M`` of the task).
    Helicopters stay at their (low) cruise altitude. Takeoff and landing
    points carry the cruise altitude too: the briefing shows them as 0.
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
    of the waypoint it is flying to (see :func:`waypoint_altitudes`)."""
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
