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

Authority = Literal["campaign", "materializing", "delegated", "dematerializing"]


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
    side: str
    enemies: list[str]
    provinces: int
    victory_points: int
    divisions: int
    aircraft: int
    objectives: list[ObjectiveView]


class PlayerView(Located):
    """A connected player."""
    id: str
    name: str
    side: str
    kind: Literal["air", "helo", "ground"]
    altitude: int
    flight: str | None = Field(description="flight id of the seat the player holds")


class CampaignView(Model):
    """Campaign overview: clock, factions, players."""
    api_version: int
    name: str
    theater: str
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
    terrain: Literal["plains", "forest", "hills", "mountain", "urban", "marsh"]
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
    kind: Literal["road", "track", "rail"]
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


class OrderView(Model):
    """A division's order."""
    kind: Literal["hold", "move", "attack"]
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
    org: float
    supply: float
    elements: dict[str, float]
    order: OrderView
    route: RouteView | None
    battle: str | None
    authority: Authority
    moving: bool


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
    stock: int
    capacity: int
    production_per_hour: float
    trucks: int


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

Task = Literal["CAP", "ESCORT", "SEAD", "CAS", "BAI", "STRIKE"]


class SeatView(Model):
    """A player's seat in a flight."""
    id: str
    name: str
    state: Literal["assigned", "airborne", "landed", "lost", "left"]


class TargetRef(Model):
    """Reference to a flight's target."""
    kind: str
    id: str


class FlightView(Located):
    """A flight of the ATO."""
    id: str
    package: str
    callsign: str
    faction: str
    squadron: str
    squadron_name: str
    airbase: str
    aircraft: str
    aircraft_name: str
    helo: bool
    task: Task
    status: Literal["scheduled", "airborne", "on_station", "rtb", "done", "cancelled"]
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
    authority: Authority
    on_station_until: Time | None


class PackageView(Located):
    """An ATO package."""
    id: str
    faction: str
    task: Task
    target: str
    tot: Time
    created: Time
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
    boards: dict[str, list[BoardPackage]] = Field(description="side -> what players see in the in-game menu")


# ---------------------------------------------------------------- air defence


class SamSiteView(Located):
    """A SAM site or early-warning radar."""
    id: str
    name: str
    faction: str
    system: str
    system_name: str
    role: Literal["long", "medium", "short", "ewr"]
    status: Literal["active", "setting_up", "moving", "suppressed"]
    location: str
    range_km: float = Field(description="engagement range (0 for early-warning radars)")
    detection_km: float
    ceiling_m: int
    missiles: float
    missiles_max: int
    elements: dict[str, float] = Field(description="site components still alive (radar, launcher, cp...)")
    emitting: bool = Field(description="radar on: visible to anti-radiation missiles")
    networked_to: str | None = Field(description="early-warning radar cueing this site (IADS)")
    route: RouteView | None
    authority: Authority


class IadsLinkView(Polyline):
    """IADS link between an EWR and a site it cues."""
    ewr: str
    site: str
    faction: str


class AirDefenseView(Model):
    """Air defence sites and the IADS network."""
    sites: list[SamSiteView]
    links: list[IadsLinkView]


# ---------------------------------------------------------------- events


class EventView(Model):
    """A campaign event."""
    seq: int
    time: Time
    kind: str
    data: dict[str, Any]


class EventsPage(Model):
    """A page of events."""
    events: list[EventView]
    next: int = Field(description="pass as `since` to get the following page")


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
