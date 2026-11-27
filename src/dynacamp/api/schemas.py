"""Response models of the HTTP API.

They mirror :mod:`dynacamp.api.views` one to one and exist to give the API a
precise OpenAPI schema (``/openapi.json``, browsable at ``/docs``): the web
front-end can generate its types from it.

Conventions: ``pos`` = [east, north] metres on the theater plane, ``ll`` =
[lat, lon] (null if the theater has no projection); times are ``Time``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..adapters.base import PlayerKind
from ..air import FlightStatus, SeatState, TargetKind, Task
from ..air_defense import SamRole, SamStatus
from ..events import EventKind
from ..forces import Authority, EntityKind, OrderKind
from ..persistence import SaveTrigger
from ..state import BattleKind
from ..structures import StructureKind
from ..systems.briefing import Severity, ThreatKind
from ..systems.intel import SensorKind
from ..units import UnitDomain
from ..world import LinkKind, Terrain

#: What the API shows where the fog of war hides the value.
Unknown = Literal["unknown"]


class Model(BaseModel):
    """Base model: unknown fields are an error."""
    model_config = ConfigDict(extra="forbid")


class Time(Model):
    """A campaign time."""
    t: float = Field(description="campaign seconds since day 1 00:00")
    label: str = Field(description="'D<day> HH:MM'")


class Located(Model):
    """Something with a position."""
    pos: list[int] = Field(description="[east, north] metres on the theater plane")
    ll: list[float] | None = Field(description="[lat, lon] WGS84, null without projection")


class Polyline(Model):
    """A line on the map."""
    pos: list[list[int]]
    ll: list[list[float]] | None


# ---------------------------------------------------------------- campaign


class ObjectiveView(Model):
    """A faction objective and whether it is held."""
    province: str
    name: str
    priority: int
    achieved: bool


class FactionView(Model):
    """A faction with its score."""
    id: str
    name: str
    coalition: str = Field(description="game coalition (DCS coalition.side key); factions of one coalition share intelligence and bullseye")
    army: str = Field(description="catalogue army the faction fields (data/catalog/factions.toml)")
    color: str = Field(description="map colour, #rrggbb")
    quality: float = Field(description="combat quality: multiplier of the effectiveness of its forces (1 = reference)")
    standing: float = Field(description="share of the war it is winning, 0-1, summing to 1 over the factions (dynacamp.standing)")
    enemies: list[str]
    provinces: int
    victory_points: int
    divisions: int
    aircraft: int
    objectives: list[ObjectiveView]
    bullseye: Located = Field(description="fixed reference point of the coalition's bearing/range calls (BULLS)")


class PlayerView(Located):
    """A connected player."""
    id: str
    name: str
    coalition: str
    kind: PlayerKind
    altitude: int
    flight: str | None = Field(description="flight id of the seat the player holds")


class SaveRecordView(Model):
    """A save of the campaign."""
    saved_at: float = Field(description="wall-clock time, Unix seconds")
    time: Time = Field(description="campaign time saved")
    trigger: SaveTrigger = Field(description="autosave, request (POST /api/save) or shutdown")


class SaveView(Model):
    """Saving of the campaign by the server (``dynacamp serve --save``)."""
    enabled: bool = Field(description="the server was started with --save: POST /api/save works")
    file: str | None = Field(None, description="name of the save file")
    autosave_minutes: float | None = Field(None, description="real minutes between autosaves (0 = only on request/exit)")
    last: SaveRecordView | None = Field(None, description="latest save made by this server run")


class CampaignView(Model):
    """Campaign overview: clock, factions, players."""
    save: SaveView | None = Field(None, description="saving (null without a running engine)")
    units: UnitDomain = Field(description="units of the texts for players (default of the UI)")
    control: ControlView | None = Field(None, description="pause and speed (null without a running engine)")
    api_version: int
    name: str
    theater: str
    magnetic_variation_deg: float = Field(
        0.0, description="east positive; BULLS bearings are magnetic = true bearing minus this")
    time: Time
    live: bool = Field(description="a game is simulating part of the campaign: time runs 1:1")
    players: list[PlayerView]
    factions: list[FactionView]
    events: int = Field(description="number of events so far (next seq)")


# ---------------------------------------------------------------- map


class ProvinceView(Located):
    """A province with its territory polygon."""
    id: str
    name: str
    terrain: Terrain
    infrastructure: int
    victory_points: int
    owner: str
    controller: str
    depot: bool
    neighbors: list[str]
    territory: Polyline = Field(description="closed polygon (Voronoi cell of the centre, max 60 km)")


class LinkView(Polyline):
    """A road/track/rail link with its geometry."""
    a: str
    b: str
    kind: LinkKind
    river: bool
    length_km: float
    road: bool = Field(description="geometry follows the real road (else a straight segment)")


class AirbaseView(Located):
    """An airbase and its owner."""
    id: str
    name: str = Field(description="in-game airdrome name")
    province: str | None
    owner: str | None


class MapView(Model):
    """Static map geometry (changes only with control)."""
    theater: str
    provinces: list[ProvinceView]
    links: list[LinkView]
    airbases: list[AirbaseView]


# ---------------------------------------------------------------- fronts & forces


class FrontLineView(Polyline):
    """A front line between two factions."""
    factions: list[str]
    length_km: float
    provinces: list[str]
    battles: list[str]


class SectorView(Model):
    """A front province with local force ratio."""
    faction: str
    province: str
    name: str
    own_power: int
    enemy_power: int
    divisions: list[str]


class BattleView(Located):
    """A battle in progress, located where the forces meet."""
    id: str
    province: str
    name: str
    kind: BattleKind = Field(
        BattleKind.ASSAULT, description="assault on the province's defence line, or meeting engagement on the road (province = nearest)")
    attacker: str
    defender: str
    attackers: list[str]
    defenders: list[str]
    started: Time
    hours: float
    delegated: bool = Field(description="fought by the game (inside a player bubble)")


class FireView(Polyline):
    """A recent artillery fire mission: from the guns to the impact."""

    shooter: str
    target: str
    faction: str
    age_s: int = Field(description="campaign seconds since it was fired")


class FrontsView(Model):
    """Front lines, sectors, battles and recent fire missions."""
    lines: list[FrontLineView]
    sectors: list[SectorView]
    battles: list[BattleView]
    fires: list[FireView]


class ContactInfo(Model):
    """How a faction knows about an enemy entity (fog of war views only)."""
    source: str = Field(description="sensor of the last detection")
    seen: Time = Field(description="time of the last detection")
    age_s: int
    stale: bool = Field(description="old information: shown at its last known position")
    identified: bool = Field(description="type known (else an unidentified radar track)")


class OrderView(Model):
    """A division's order."""
    kind: OrderKind | Unknown
    target: str | None


class RouteView(Polyline):
    """Remaining route along the roads."""
    provinces: list[str]


class DivisionView(Located):
    """A brigade."""
    id: str
    name: str
    faction: str
    template: str
    template_name: str
    location: str
    strength: float
    org: float | None
    supply: float | None
    elements: dict[str, float]
    order: OrderView
    route: RouteView | None
    battle: str | None
    authority: Authority
    moving: bool
    intel: ContactInfo | None = Field(None, description="set on enemy brigades in a faction's view")


class ForcesView(Model):
    """All brigades and battles."""
    divisions: list[DivisionView]
    battles: list[BattleView]


# ---------------------------------------------------------------- logistics


class DepotView(Located):
    """A supply depot."""
    province: str
    name: str
    controller: str
    stock: int | None = Field(description="null for enemy depots in a faction's view")
    capacity: int
    production_per_hour: float
    trucks: int | None


class ConvoyView(Located):
    """A supply convoy."""
    id: str
    faction: str
    cargo: float
    trucks: float
    home: str
    destination: str
    returning: bool
    heading_to: str
    route: RouteView | None
    authority: Authority
    intel: ContactInfo | None = None


class NetworkEntry(Model):
    """Supply reach of a province."""
    depot: str
    road_km: float
    direct: bool


class DivisionSupplyView(Model):
    """Supply state of a brigade."""
    id: str
    faction: str
    location: str
    supply: float
    out_of_supply: bool
    direct: bool
    convoy_incoming: bool


class LogisticsView(Model):
    """Depots, convoys, supply network and brigade supply."""
    depots: list[DepotView]
    convoys: list[ConvoyView]
    network: dict[str, dict[str, NetworkEntry]] = Field(description="faction -> province -> supply reach")
    divisions: list[DivisionSupplyView]
    direct_range_km: float


# ---------------------------------------------------------------- ATO

class SeatView(Model):
    """A player's seat in a flight."""
    id: str
    name: str
    state: SeatState


class TargetRef(Model):
    """Reference to a flight's target."""
    kind: TargetKind | Unknown
    id: str


class FlightView(Located):
    """A flight of the ATO."""
    id: str
    package: str
    package_number: str
    callsign: str
    faction: str
    squadron: str
    squadron_name: str
    squadron_insignia: str = Field("", description="URL path of the squadron's insignia image (\"\" = none)")
    airbase: str
    aircraft: str
    aircraft_name: str
    helo: bool
    task: Task
    status: FlightStatus
    size: int
    ai: int
    seats_free: int
    players: list[SeatView]
    takeoff: Time
    tot: Time
    waypoints: Polyline
    target_index: int
    next_waypoint: int
    target: TargetRef
    locked_waypoints: int = Field(description="leading waypoints that can no longer change (already flown, or the station while on it)")
    fuel_km: int = Field(description="longest route the flight may plan (twice the combat radius, more with a tanker up)")
    authority: Authority
    on_station_until: Time | None
    intel: ContactInfo | None = Field(None, description="set on enemy radar tracks in a faction's view")


class PackageView(Located):
    """An ATO package."""
    id: str
    number: str = Field(description="four-digit package number, as in Falcon's ATO")
    faction: str
    task: Task
    target: str
    tot: Time
    earliest_tot: Time = Field(description="earliest TOT the package can be moved to (startup lead included)")
    created: Time
    archived: bool = Field(False, description="the package is over and kept only for the record")
    flights: list[FlightView]


class SquadronView(Model):
    """A squadron and its availability."""
    id: str
    name: str
    faction: str
    airbase: str
    aircraft: str
    aircraft_name: str
    tasks: list[Task]
    strength: int
    available: int
    committed: int
    servicing: int
    insignia: str = Field("", description="URL path of the insignia image (\"\" = none)")


class BoardFlight(Model):
    """A flight players can join (in-game board)."""
    id: str
    label: str
    task: Task
    seats: int


class BoardPackage(Model):
    """A package on the in-game board."""
    id: str
    label: str
    flights: list[BoardFlight]


class AtoView(Model):
    """The air tasking order."""
    packages: list[PackageView]
    squadrons: list[SquadronView]
    sortie_budget_left: dict[str, int]
    flights_airborne: int
    tracks: list[FlightView] = Field(default_factory=list, description="enemy flights detected (faction view)")
    boards: dict[str, list[BoardPackage]] = Field(description="coalition -> what players see in the in-game menu")


# ---------------------------------------------------------------- briefing


class BriefingWaypoint(Located):
    """A named waypoint of a flight plan."""

    index: int
    name: str = Field(description="TAKEOFF, IP, TARGET, EGRESS, CAP STATION, LAND...")
    eta: Time
    alt_m: int
    speed_kmh: int
    note: str
    bulls: str = Field(description="magnetic bearing/range from the side's bullseye, e.g. 045/32 (NM or km)")


class BriefingTarget(Located):
    """The target of a flight."""

    kind: str
    id: str
    name: str
    description: str
    composition: list[str]
    coordinates: str = Field(description="degrees and decimal minutes")
    from_base: str = Field(description="bearing and range from the home base")
    bulls: str = Field(description="magnetic bearing/range from the side's bullseye")


class BriefingThreat(Located):
    """A threat to the flight."""

    kind: ThreatKind
    name: str
    detail: str
    range_km: float
    severity: Severity
    bulls: str = Field(description="magnetic bearing/range from the side's bullseye")


class BriefingStore(Model):
    """Ordnance item and number of pylons carrying it."""

    store: str
    count: int


class BriefingFlight(Model):
    """A flight of the package."""

    callsign: str
    aircraft: str
    task: Task
    size: int
    base: str
    takeoff: Time
    you: bool


class BriefingSupport(Located):
    """A friendly AWACS or tanker available during the mission."""

    task: Literal[Task.AWACS, Task.TANKER]
    callsign: str
    aircraft: str
    status: str
    position: str
    from_target: str
    frequency: str
    until: Time


class BriefingBullseye(Located):
    """The side's bullseye, reference of every ``bulls`` bearing/range."""

    coordinates: str = Field(description="degrees and decimal minutes")


class BriefingView(Model):
    """The mission package of a flight. Numbers are SI (``alt_m``, ``speed_kmh``,
    ``range_km``); texts are written in ``units``."""
    units: UnitDomain
    package_number: str
    bullseye: BriefingBullseye

    flight_id: str
    callsign: str
    radio_callsign: str = Field(
        "", description="DCS radio callsign (what the AI calls the flight); differs from `callsign` when "
        "the squadron's own name is not in DCS' list for the type; empty for armies without named callsigns")
    title: str
    task: Task
    aircraft: str
    size: int
    squadron: str
    squadron_insignia: str = Field("", description="URL path of the squadron's insignia image (\"\" = none)")
    base: str
    takeoff: Time
    tot: Time
    mission: str
    situation: list[str]
    target: BriefingTarget
    threats: list[BriefingThreat]
    loadout: list[BriefingStore]
    loadout_note: str
    comms: dict[str, str]
    support: list[BriefingSupport]
    waypoints: list[BriefingWaypoint]
    package: list[BriefingFlight]
    text: str = Field(description="the briefing as sent to players in the game")


# ---------------------------------------------------------------- air defence


class SamSiteView(Located):
    """A SAM site or early-warning radar."""
    id: str
    name: str
    faction: str
    system: str
    system_name: str
    role: SamRole
    status: SamStatus
    location: str
    range_km: float = Field(description="engagement range (0 for early-warning radars)")
    detection_km: float
    ceiling_m: int
    missiles: float | None
    missiles_max: int
    elements: dict[str, float] = Field(description="site components still alive (radar, launcher, cp...)")
    emitting: bool = Field(description="radar on: visible to anti-radiation missiles")
    networked_to: str | None = Field(description="early-warning radar cueing this site (IADS)")
    route: RouteView | None
    authority: Authority
    intel: ContactInfo | None = None


class IadsLinkView(Polyline):
    """IADS link between an EWR and a site it cues."""
    ewr: str
    site: str
    faction: str


class AirDefenseView(Model):
    """Air defence sites and the IADS network."""
    sites: list[SamSiteView]
    links: list[IadsLinkView]


class StructureView(Located):
    """A fixed installation: depot, airbase facilities, command post."""
    id: str
    name: str
    kind: StructureKind
    faction: str = Field(description="owner: the province's controller, or the airbase's owner")
    province: str
    airbase: str = Field(description="airbase served (airbase facilities), else empty")
    health: float = Field(description="share of its buildings standing, 0-1")
    buildings: int = Field(description="buildings it is made of")
    standing: int = Field(description="buildings standing")
    repaired_at: Time | None = Field(description="when the next destroyed building is rebuilt (null: none destroyed)")


class StructuresView(Model):
    """The structures that can be struck (fixed, so every faction knows them)."""
    structures: list[StructureView]


# ---------------------------------------------------------------- events


class EventView(Model):
    """A campaign event."""
    seq: int
    time: Time
    kind: EventKind
    data: dict[str, Any]


class EventsPage(Model):
    """A page of events."""
    events: list[EventView]
    next: int = Field(description="pass as `since` to get the following page")


class ContactView(Located):
    """One contact of a faction's intelligence picture."""
    id: str
    kind: EntityKind
    faction: str
    source: str
    seen: Time
    first_seen: Time
    age_s: int
    stale: bool
    identified: bool
    detail: dict[str, Any]


class SensorView(Located):
    """A friendly sensor and its reach."""
    kind: SensorKind
    name: str
    range_km: float
    heading_deg: float | None = Field(None, description="aircraft radars: direction of the cone (null: all round)")
    half_angle_deg: float | None = Field(None, description="aircraft radars: half width of the cone")


class IntelView(Model):
    """A faction's intelligence picture and its air-search sensors."""
    faction: str
    contacts: list[ContactView]
    sensors: list[SensorView]
    counts: dict[str, int] = Field(description="contacts per kind")


class ControlView(Model):
    """Pause and speed of the campaign clock."""
    paused: bool
    compression: float = Field(description="campaign seconds per real second when not live")
    rate: float = Field(description="effective rate now: 0 paused, 1 live, else the compression")
    min_compression: float
    max_compression: float


class AuthView(Model):
    """Whether commands need a token, and whether the one sent is valid."""
    required: bool = Field(description="commands (POST) need `Authorization: Bearer <token>`")
    authenticated: bool = Field(description="the request carried a valid token (always true when none is required)")


class PointIn(Model):
    """A position: ``ll`` = [lat, lon] or ``pos`` = [east, north] metres (``ll`` wins)."""
    ll: list[float] | None = Field(None, min_length=2, max_length=2)
    pos: list[float] | None = Field(None, min_length=2, max_length=2)


class RouteRequest(Model):
    """New route of a flight, takeoff to landing."""
    waypoints: list[PointIn] = Field(min_length=3, max_length=24)
    target_index: int = Field(ge=1, description="index of the target / station waypoint in `waypoints`")
    faction: str | None = Field(None, description="ordering faction (omit: the umpire)")


class TargetIn(Model):
    """What a new package flies against."""
    kind: TargetKind
    id: str = Field("", description="battle, brigade, convoy, structure, SAM site, airbase (airlift) or own brigade (resupply)")
    point: PointIn | None = Field(None, description="for `point` targets (CAP station, AWACS orbit, tanker track)")


class PackageRequest(Model):
    """Order a new package."""
    faction: str
    task: Task
    target: TargetIn
    squadron: str | None = Field(None, description="squadron of the main flight (omit: the best available)")
    tot: float | None = Field(None, description="time on target in campaign seconds (omit: the earliest possible)")


class TotRequest(Model):
    """Move the time on target of a package."""
    tot: float = Field(description="campaign seconds")
    faction: str | None = None


class CancelRequest(Model):
    """Cancel a package's flights still on the ground."""
    faction: str | None = None


class CancelView(Model):
    """Result of a cancellation."""
    cancelled: list[str] = Field(description="callsigns of the cancelled flights")


class ControlRequest(Model):
    """Change pause and/or speed (fields left out are unchanged)."""
    paused: bool | None = None
    compression: float | None = Field(None, description="campaign seconds per real second")


class PositionsView(Model):
    """Compact positions of what moves (``id -> [lat, lon]``), for animation."""
    t: float = Field(description="campaign time of the sample")
    d: dict[str, list[float]] = Field(description="brigades")
    c: dict[str, list[float]] = Field(description="convoys")
    f: dict[str, list[float]] = Field(description="airborne flights (and enemy tracks in a faction's view)")
    s: dict[str, list[float]] = Field(description="air defence sites")


class SnapshotView(Model):
    """Every view in one document."""
    campaign: CampaignView
    map: MapView
    fronts: FrontsView
    forces: ForcesView
    logistics: LogisticsView
    ato: AtoView
    air_defense: AirDefenseView


class TickView(Model):
    """Clock message of the event stream."""
    time: Time
    live: bool
