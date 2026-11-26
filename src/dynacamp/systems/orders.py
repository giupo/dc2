"""Orders of a human planner on the ATO (from the web UI or any API client).

The AI plans packages on its own (:mod:`dynacamp.systems.ato`); these
functions let a planner do what a Falcon/BMS player does in the ATO screen:

* :func:`order_package` - a new package against a target the faction knows
  (fog of war: positions come from :mod:`dynacamp.systems.intel`);
* :func:`set_route` - move, add or remove waypoints of a flight, before or
  during the mission;
* :func:`set_tot` - move the time on target of a package not yet airborne;
* :func:`cancel_package` - cancel the flights of a package still on the ground.

Every refusal raises :class:`OrderError` with a message for the planner, and
nothing changes. A route change marks the flight dirty, so the engine sends
the new route to the game when the game holds the flight.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..air import Flight, FlightStatus, Package, Task
from ..events import clock
from ..geo import Vec2
from ..state import CampaignState
from . import air, ato, intel
from ..events import EventKind

#: Most waypoints a planned route may have.
MAX_WAYPOINTS = 24
#: A TOT may be at most this far in the future, seconds.
MAX_TOT_AHEAD_S = 24 * 3600.0
#: Tolerance on points that must not move (latitude/longitude round trips).
SAME_POINT_M = 50.0
#: The landing point of a new route must be this close to the old one.
LANDING_TOLERANCE_M = 1_000.0

#: Kinds of target each main task can be ordered against.
TARGET_KINDS: dict[Task, tuple[str, ...]] = {
    Task.CAS: ("battle",),
    Task.BAI: ("division", "convoy"),
    Task.STRIKE: ("depot",),
    Task.SEAD: ("sam",),
    Task.DEAD: ("sam",),
    Task.CAP: ("point",),
    Task.AWACS: ("point",),
    Task.TANKER: ("point",),
    Task.TRANSPORT: ("airlift", "resupply"),
}


class OrderError(ValueError):
    """An order was refused; the message says why, for the planner.

    Attributes:
        status: HTTP-like status: 404 for an unknown flight or package, 409 for
            an order the situation does not allow.
    """

    def __init__(self, message: str, status: int = 409) -> None:
        """Record why the order was refused.

        Args:
            message: Why, for the planner.
            status: 404 (unknown object) or 409 (refused).
        """
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Target:
    """What a new package is ordered against.

    Attributes:
        kind: ``battle``, ``division``, ``convoy``, ``depot``, ``sam``,
            ``point``, ``airlift`` or ``resupply``.
        id: Battle, division, convoy, province (depot), SAM site, airbase
            (airlift) or own brigade (resupply) id; unused for ``point``.
        point: Position, for ``point`` targets only.
    """
    kind: str
    id: str = ""
    point: Vec2 | None = None


# ---------------------------------------------------------------- helpers


def _flight(state: CampaignState, flight_id: str, faction: str | None) -> Flight:
    """The flight, checked against the ordering faction.

    Args:
        state: The campaign.
        flight_id: Flight id.
        faction: Ordering faction (None: the umpire, who may order any flight).

    Returns:
        The flight.
    """
    f = state.flights.get(flight_id)
    if f is None or (faction is not None and f.faction != faction):
        raise OrderError(f"no flight {flight_id}", 404)
    return f


def _package(state: CampaignState, package_id: str, faction: str | None) -> Package:
    """The package, checked against the ordering faction.

    Args:
        state: The campaign.
        package_id: Package id.
        faction: Ordering faction (None: the umpire, who may order any package).

    Returns:
        The package.
    """
    p = state.packages.get(package_id)
    if p is None or (faction is not None and p.faction != faction):
        raise OrderError(f"no package {package_id}", 404)
    return p


def _length(points: list[Vec2]) -> float:
    """Length of a polyline in metres.

    Args:
        points: Polyline vertices.

    Returns:
        The length.
    """
    return sum(a.distance_to(b) for a, b in zip(points, points[1:]))


def fuel_limit_m(state: CampaignState, f: Flight) -> float:
    """Longest route a flight may plan: twice the combat radius, stretched by a
    tanker on station for fixed-wing combat aircraft (as the ATO does).

    Args:
        state: The campaign.
        f: The flight.

    Returns:
        The maximum route length in metres.
    """
    stretch = 1.0
    if not f.aircraft_type.helo and not f.task.support and air.tanker_on_station(state, f.faction):
        stretch = ato.TANKER_RADIUS_FACTOR
    return 2.0 * f.aircraft_type.radius_km * 1000.0 * stretch


def _route_length(f: Flight, waypoints: list[Vec2]) -> float:
    """Length of a route as the flight flies it: the legs already flown, then
    from its current position along what is left.

    Args:
        f: The flight.
        waypoints: The proposed route, including the waypoints already flown.

    Returns:
        The length in metres.
    """
    if f.status is FlightStatus.SCHEDULED:
        return _length(waypoints)
    flown = waypoints[: f.wp_index]
    return _length(flown) + _length([flown[-1], f.pos]) + _length([f.pos, *waypoints[f.wp_index:]])


def _to_target(waypoints: list[Vec2], target_index: int) -> float:
    """Distance from takeoff to the target along the route.

    Args:
        waypoints: The route.
        target_index: Index of the target waypoint.

    Returns:
        The distance in metres.
    """
    return _length(waypoints[: target_index + 1])


# ---------------------------------------------------------------- route


def locked_waypoints(f: Flight) -> int:
    """How many leading waypoints of a flight can no longer change: those
    already flown (the takeoff one at least) and, while on station, the station.

    Args:
        f: The flight.

    Returns:
        The number of locked waypoints (at least 1).
    """
    if f.status is FlightStatus.ON_STATION:
        return f.target_index + 1
    return max(1, f.wp_index)


def set_route(
    state: CampaignState, flight_id: str, waypoints: list[Vec2], target_index: int, faction: str | None = None
) -> Flight:
    """Replace the route of a flight.

    ``waypoints`` is the whole route, takeoff to landing. The waypoints already
    flown (see :func:`locked_waypoints`) and the landing one cannot move; the
    target waypoint can move only for orbiting tasks (a new target needs a new
    package). The route must fit the fuel (see :func:`fuel_limit_m`, or the
    current route if that is already longer), and a flight on the ground must
    still be able to take off in time for its TOT.

    Args:
        state: The campaign.
        flight_id: The flight.
        waypoints: New route, takeoff to landing.
        target_index: Index of the target (or station) waypoint in ``waypoints``.
        faction: Ordering faction (None: the umpire, any flight).

    Returns:
        The flight, with the new route.

    Raises:
        OrderError: The order is refused; nothing changed.
    """
    f = _flight(state, flight_id, faction)
    if f.status not in (FlightStatus.SCHEDULED, FlightStatus.AIRBORNE, FlightStatus.ON_STATION, FlightStatus.RTB):
        raise OrderError(f"{f.callsign} is {f.status.value}: its route cannot change")
    if f.ai_done and not f.active_seats:
        raise OrderError(f"{f.callsign} has landed")
    new = list(waypoints)
    if not 3 <= len(new) <= MAX_WAYPOINTS:
        raise OrderError(f"a route has 3 to {MAX_WAYPOINTS} waypoints (takeoff, target, landing at least)")
    locked = locked_waypoints(f)
    if len(new) <= locked:
        raise OrderError("the route must keep the waypoints already flown and add at least the landing")
    for i in range(locked):
        if new[i].distance_to(f.waypoints[i]) > SAME_POINT_M:
            raise OrderError(f"waypoint {i} has already been flown and cannot move")
        new[i] = f.waypoints[i]
    if new[-1].distance_to(f.waypoints[-1]) > LANDING_TOLERANCE_M:
        raise OrderError("the landing waypoint is the home airbase and cannot move")
    new[-1] = f.waypoints[-1]

    if f.attacked or f.status is FlightStatus.ON_STATION:
        # The target is behind (or under) the flight: it keeps its place in the route.
        if target_index != f.target_index:
            raise OrderError("the target waypoint has already been reached")
    elif not locked <= target_index <= len(new) - 2:
        raise OrderError("the target waypoint must come after the waypoints already flown and before landing")
    if not f.task.orbits and not f.attacked and new[target_index].distance_to(f.target_point) > SAME_POINT_M:
        raise OrderError(f"the {f.task.value} target cannot move: order a new package for another target")
    if not f.task.orbits:
        new[target_index] = f.target_point

    limit = max(fuel_limit_m(state, f), _route_length(f, f.waypoints))
    length = _route_length(f, new)
    if length > limit:
        raise OrderError(f"route too long: {length / 1000:.0f} km, fuel for {limit / 1000:.0f} km")

    takeoff = f.takeoff_time
    if f.status is FlightStatus.SCHEDULED:
        takeoff = f.tot - _to_target(new, target_index) / f.speed_ms
        if takeoff < state.time:
            raise OrderError(
                f"{f.callsign} would have to take off {clock(takeoff)}, before now: shorten the route or move the TOT"
            )

    f.waypoints = new
    f.target_index = target_index
    f.takeoff_time = takeoff
    pkg = state.packages.get(f.package)
    if pkg is not None and f.task.orbits and pkg.flights and pkg.flights[0] == f.id:
        pkg.target_point = f.target_point
    state.dirty.add(f.id)
    state.emit(
        EventKind.FLIGHT_REPLANNED, flight=f.id, callsign=f.callsign, faction=f.faction, package=f.package,
        waypoints=len(new), length_km=round(length / 1000), takeoff=clock(takeoff),
    )
    return f


# ---------------------------------------------------------------- time on target


def earliest_tot(state: CampaignState, pkg: Package) -> float:
    """Earliest TOT a package can be moved to. Later is always fine (crews only
    get more time); earlier only as long as every flight still takes off after
    now plus the startup lead given to human pilots.

    Args:
        state: The campaign.
        pkg: The package.

    Returns:
        The earliest TOT, campaign seconds (never later than the current one).
    """
    needed = max(
        (state.time + ato.STARTUP_LEAD_S - f.takeoff_time for f in _live_flights(state, pkg)), default=0.0
    )
    return pkg.tot + min(0.0, needed)


def _live_flights(state: CampaignState, pkg: Package) -> list[Flight]:
    """Flights of a package not done or cancelled.

    Args:
        state: The campaign.
        pkg: The package.

    Returns:
        The live flights.
    """
    return [
        f for f in (state.flights[i] for i in pkg.flights if i in state.flights)
        if f.status not in (FlightStatus.DONE, FlightStatus.CANCELLED)
    ]


def set_tot(state: CampaignState, package_id: str, tot: float, faction: str | None = None) -> Package:
    """Move the time on target of a package whose flights are all on the
    ground; their takeoffs (and station times) move by the same amount.

    Args:
        state: The campaign.
        package_id: The package.
        tot: New time on target (campaign seconds).
        faction: Ordering faction (None: the umpire).

    Returns:
        The package, with the new TOT.

    Raises:
        OrderError: The order is refused; nothing changed.
    """
    pkg = _package(state, package_id, faction)
    flights = _live_flights(state, pkg)
    if not flights:
        raise OrderError(f"package {pkg.number} has no flights left")
    airborne = [f.callsign for f in flights if f.status is not FlightStatus.SCHEDULED]
    if airborne:
        raise OrderError(f"{', '.join(airborne)} already took off: the TOT cannot move")
    earliest = earliest_tot(state, pkg)
    if tot < earliest:
        raise OrderError(f"earliest TOT is {clock(earliest)}: crews need time to start up and fly there")
    if tot == pkg.tot:
        return pkg
    if tot > state.time + MAX_TOT_AHEAD_S:
        raise OrderError("the ATO looks at most 24 hours ahead")
    shift = tot - pkg.tot
    for f in flights:
        f.tot += shift
        f.takeoff_time += shift
        if f.station_until:
            f.station_until += shift
    pkg.tot = tot
    state.emit(EventKind.PACKAGE_RETIMED, package=pkg.id, number=pkg.number, faction=pkg.faction, tot=clock(tot))
    return pkg


# ---------------------------------------------------------------- new and cancelled packages


def _request(state: CampaignState, fid: str, task: Task, target: Target) -> ato.Request:
    """Turn an order into an ATO request, from what the faction knows.

    Args:
        state: The campaign.
        fid: Ordering faction.
        task: The package's main task.
        target: The ordered target.

    Returns:
        The request, with the target's label and position as the faction knows them.
    """
    t = state.theater
    if target.kind not in TARGET_KINDS.get(task, ()):
        allowed = ", ".join(TARGET_KINDS.get(task, ())) or "nothing (it escorts other packages)"
        raise OrderError(f"{task.value} is flown against: {allowed}")
    kind, tid = target.kind, target.id
    if kind == "point":
        if target.point is None:
            raise OrderError("a point target needs a position")
        p = target.point
        near = t.nearest(p).name
        if task is Task.CAP:
            return ato.Request(task, 0, f"CAP near {near}", p, "point", state.new_id("pt"))
        label = "AWACS orbit" if task is Task.AWACS else "Tanker track"
        return ato.Request(task, 0, f"{label} near {near}", p, "point", task.value.lower())
    if kind == "battle":
        b = state.battles.get(tid)
        if b is None or fid not in (b.attacker_faction, b.defender_faction):
            raise OrderError(f"no battle {tid} involving our forces")
        prov = t.provinces[b.province]
        return ato.Request(task, 0, f"enemy forces at {prov.name}", prov.center, "battle", tid)
    if kind == "depot":
        prov = t.provinces.get(tid)
        if prov is None or prov.depot is None or not state.at_war(fid, prov.controller):
            raise OrderError(f"no enemy supply depot in {tid}")
        return ato.Request(task, 0, f"supply depot {prov.name}", prov.center, "depot", tid)
    if kind in ("division", "convoy", "sam"):
        c = intel.knows(state, fid, tid)
        if c is None or c.kind != kind:
            raise OrderError(f"no {kind} contact {tid} in our intelligence picture")
        near = t.nearest(c.position).name
        if kind == "division":
            label = f"{c.detail.get('name', 'enemy brigade')} near {near}"
        elif kind == "convoy":
            label = f"supply convoy near {near}"
        else:
            label = f"{c.detail.get('system_name', 'SAM')} site near {near}"
        return ato.Request(task, 0, label, c.position, kind, tid)
    if kind == "airlift":
        b = state.airbases.get(tid)
        prov = t.provinces.get(b.province) if b and b.province else None
        if b is None or b.owner(t) != fid or prov is None or prov.depot is None:
            raise OrderError(f"no friendly airbase with a depot {tid}")
        return ato.Request(task, 0, f"airlift to {b.name}", b.position, "airlift", tid)
    # resupply
    d = state.divisions.get(tid)
    if d is None or d.faction != fid:
        raise OrderError(f"no friendly brigade {tid}")
    return ato.Request(task, 0, f"resupply {d.name}", d.position(t), "resupply", tid)


def order_package(
    state: CampaignState,
    faction: str,
    task: Task,
    target: Target,
    squadron: str | None = None,
    tot: float | None = None,
) -> Package:
    """Order a new package, as the ATO would plan it (main flight, then SEAD
    and escort when called for), against a target the faction knows.

    Args:
        state: The campaign.
        faction: Ordering faction.
        task: Main task.
        target: What to fly against.
        squadron: Squadron of the main flight (None: the best available).
        tot: Time on target (None: the earliest possible).

    Returns:
        The new package.

    Raises:
        OrderError: The order is refused; nothing changed.
    """
    fac = state.factions.get(faction)
    if fac is None:
        raise OrderError(f"no faction {faction}", 404)
    r = _request(state, faction, task, target)
    if squadron is not None:
        sq = state.squadrons.get(squadron)
        if sq is None or sq.faction != faction:
            raise OrderError(f"no squadron {squadron}")
        flown = Task.STRIKE if task is Task.DEAD else task  # DEAD: the squadron is the strikers'
        if flown not in sq.tasks:
            raise OrderError(f"{sq.name} does not fly {flown.value}")
    if tot is not None and tot > state.time + MAX_TOT_AHEAD_S:
        raise OrderError("the ATO looks at most 24 hours ahead")
    need = ato.package_size(state, faction, r)
    left = ato.sortie_budget_left(state, faction)
    if left < need:
        raise OrderError(f"sortie budget exhausted: {left} sorties left today, the package needs {need}")
    if task is Task.DEAD and ato._allocate(state, faction, Task.SEAD, r.point, 2) is None:
        raise OrderError("no SEAD squadron can keep the site's radars down for the strikers")
    if task in (Task.CAS, Task.BAI, Task.STRIKE) and ato.route_exposed(state, faction, r):
        if ato._allocate(state, faction, Task.SEAD, r.point, 2) is None:
            raise OrderError("the route crosses a known SAM umbrella it cannot fly around and no SEAD squadron can escort the package")
    pkg = ato.build_package(state, fac, r, squadron=squadron, tot=tot)
    if pkg is None:
        raise OrderError(_why_no_package(state, faction, task, r, squadron, tot))
    state.emit(EventKind.PACKAGE_ORDERED, package=pkg.id, number=pkg.number, faction=faction, task=task.value, target=r.label)
    return pkg


def _why_no_package(
    state: CampaignState, fid: str, task: Task, r: ato.Request, squadron: str | None, tot: float | None
) -> str:
    """Explain why :func:`ato.build_package` found nothing.

    Args:
        state: The campaign.
        fid: Ordering faction.
        task: The package's main task.
        r: The ATO request built from the order.
        squadron: The squadron asked for (None: any).
        tot: The TOT asked for (None: the earliest).

    Returns:
        The reason, for the planner.
    """
    size = 4 if task is Task.STRIKE else (1 if task in (Task.AWACS, Task.TANKER) else 2)
    if task is Task.DEAD:
        task = Task.STRIKE  # the strikers (SEAD is checked before building)
    candidates = [
        sq for sq in state.squadrons.values()
        if sq.faction == fid and task in sq.tasks and (squadron is None or sq.id == squadron)
    ]
    if not candidates:
        return f"no squadron flies {task.value}"
    if not any(sq.available_at(state.time) >= size for sq in candidates):
        return f"no squadron has {size} aircraft ready for {task.value}"
    found = ato._allocate(state, fid, task, r.point, size, require=_squadron_filter(state, r, squadron))
    if found is None:
        return "the target is out of range of the squadrons with aircraft ready"
    if tot is not None:
        sq, dist = found
        return f"earliest TOT for {sq.name} is {clock(ato.earliest_tot(sq, dist, state.time))}"
    return "no squadron can fly it"  # pragma: no cover


def _squadron_filter(state: CampaignState, r: ato.Request, squadron: str | None):
    """Squadron filter of an order: the target's requirement and the chosen squadron.

    Args:
        state: The campaign.
        r: The ATO request.
        squadron: The squadron asked for (None: any).

    Returns:
        A predicate on squadrons.
    """
    base = ato._target_requirement(state, r)
    return lambda sq: (squadron is None or sq.id == squadron) and (base is None or base(sq))


def cancel_package(state: CampaignState, package_id: str, faction: str | None = None) -> list[str]:
    """Cancel the flights of a package that are still on the ground (their
    aircraft go back to the squadrons); airborne ones carry on.

    Args:
        state: The campaign.
        package_id: The package.
        faction: Ordering faction (None: the umpire).

    Returns:
        Callsigns of the cancelled flights.

    Raises:
        OrderError: Nothing could be cancelled.
    """
    pkg = _package(state, package_id, faction)
    grounded = [f for f in _live_flights(state, pkg) if f.status is FlightStatus.SCHEDULED]
    if not grounded:
        raise OrderError(f"package {pkg.number} has no flights on the ground to cancel")
    for f in grounded:
        air._cancel(state, f, "cancelled by the planner")
    return [f.callsign for f in grounded]
