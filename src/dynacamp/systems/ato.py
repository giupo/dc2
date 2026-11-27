"""Air Tasking Order: planning packages and seating players in them.

Every ATO cycle each faction turns the situation into mission requests
(CAS over our battles, CAP over the front, BAI on enemy columns and convoys,
STRIKE on depots, DEAD on SAM sites), ranks them, and builds packages: a main
flight plus SEAD when the target is defended and an escort when enemy fighters
can reach it.

Routes go around the SAM umbrellas the faction knows when a reasonable detour
exists (:mod:`dynacamp.systems.routing`). What cannot be avoided (the target's
own cover) calls for SEAD, whose ECM also screens the package; without SEAD
the strikers do not go. DEAD packages put strikers on the SAM sites
themselves, two minutes behind the SEAD that keeps their radars down.
Takeoffs are planned at least ``STARTUP_LEAD_S`` ahead so that human pilots
have time to pick a flight and start up.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from ..air import CAP_ALT_M, FlightStatus, Flight, Package, Seat, SeatState, Squadron, TargetKind, Task
from ..forces import EntityKind
from ..events import clock
from ..geo import Vec2
from ..state import BattleKind, CampaignState, Faction, IdPrefix, Timer
from .. import radio
from . import air, intel, routing, structures
from ..structures import StructureKind
from ..events import EventKind, LossReason

# -- Planning cycle --------------------------------------------------------

#: Label of the station of each support task, for the planner and the briefing.
SUPPORT_STATION = {
    Task.AWACS: "AWACS orbit",
    Task.TANKER: "Tanker track"
}

#: Seconds between two ATO planning cycles of a faction.
ATO_INTERVAL_S = 3600.0
#: Minimum time between planning and takeoff, so that human pilots can pick
#: a flight and start up.
STARTUP_LEAD_S = 30 * 60.0
#: Combat packages a faction may plan in one cycle.
MAX_NEW_PACKAGES = 3
#: Support packages (AWACS, tanker, transport) a faction may plan in one
#: cycle, on top of :data:`MAX_NEW_PACKAGES`.
MAX_NEW_SUPPORT = 2
#: Live combat packages a faction may have at once (CAP and support excluded).
MAX_ACTIVE_PACKAGES = 6

# -- Sortie budget -----------------------------------------------------------

#: Aircraft-sorties per airframe per day.
SORTIE_RATE_PER_DAY = 2.5
#: Sortie tokens accumulate up to this many hours of the daily rate.
BURST_HOURS = 4.0

# -- CAP ---------------------------------------------------------------------

#: Concurrent CAP stations per faction (scrambles not counted).
CAP_STATIONS = 3
#: Time on a CAP station, seconds.
CAP_STATION_S = 150 * 60.0
#: Extra time on a CAP station while a friendly tanker is on its track.
TANKER_CAP_BONUS_S = 3600.0
#: Share of the spare fuel a CAP spends on station (the rest is kept for a fight).
CAP_FUEL_FOR_FIGHT = 0.75
#: Step by which a CAP station is pulled back out of known SAM coverage.
CAP_PULLBACK_STEP_M = 5_000.0
#: Furthest a CAP station is pulled back before giving up.
CAP_PULLBACK_MAX_M = 80_000.0

# -- Support: AWACS, tankers, transports -------------------------------------

#: Time on station of AWACS and tankers, seconds.
SUPPORT_STATION_S = 4 * 3600.0
#: Distance behind the front where the AWACS orbit is first tried.
AWACS_SETBACK_M = 120_000.0
#: Distance behind the front where the tanker track is first tried.
TANKER_SETBACK_M = 80_000.0
#: Combat radius multiplier of fixed-wing combat aircraft while a tanker is on station.
TANKER_RADIUS_FACTOR = 1.4
#: Sustainment airlift goes to forward depots below this share of their capacity.
AIRLIFT_THRESHOLD = 0.85
#: Helicopter resupply goes to brigades below this supply ratio.
RESUPPLY_THRESHOLD = 0.3

# -- Targets -----------------------------------------------------------------

#: BAI only on enemy brigades located this recently, seconds.
KNOWN_DIVISION_S = 2 * 3600.0
#: BAI only on enemy convoys located this recently, seconds.
KNOWN_CONVOY_S = 1800.0
#: Mission requests consider targets within this distance of a front province.
FRONT_REACH_M = 80_000.0
#: Short-range air defence around a target (SAMs + half the AAA, see
#: :func:`dynacamp.systems.air.air_defense`) from which a package needs SEAD.
SEAD_THRESHOLD = 4.0

# -- Route geometry ----------------------------------------------------------

#: Distance of the initial point (IP) before the target.
IP_DISTANCE_M = 25_000.0
#: Distance of the egress point after the target.
EGRESS_DISTANCE_M = 20_000.0
#: Below this distance from home the flight goes straight at the target (no IP, no egress).
MIN_ATTACK_LEG_M = 8_000.0
#: Egress turn off the inbound axis, radians (~40 degrees).
EGRESS_TURN_RAD = 0.7
#: Bearings of the IP tried off the direct inbound axis, radians.
ATTACK_AXES_RAD = (0.0, 0.5, -0.5, 1.0, -1.0, 1.5, -1.5)
#: Route cost of a metre inside a known SAM umbrella, in metres of detour.
EXPOSURE_WEIGHT = 5.0


@dataclass(frozen=True)
class Request:
    """A mission request before allocation.

    Attributes:
        task: Mission type.
        priority: Higher first.
        label: Human-readable target.
        point: Target position.
        target_kind: What it is against.
        target_id: Target id.
    """
    task: Task
    priority: float
    label: str
    point: Vec2
    target_kind: TargetKind
    target_id: str


def run(state: CampaignState, dt: float) -> None:
    """Plan packages for every faction with squadrons, every ``ATO_INTERVAL_S``.

    Args:
        state: The campaign.
        dt: Campaign seconds elapsed (unused: planning runs on its own timer).
    """
    if state.time < state.timers.get(Timer.ATO, 0.0):
        return

    state.timers[Timer.ATO] = state.time + ATO_INTERVAL_S
    for faction in state.factions.values():
        if any(s.faction == faction.id for s in state.squadrons.values()):
            plan(state, faction)


# ---------------------------------------------------------------- requests


def requests(state: CampaignState, faction: Faction) -> list[Request]:
    """Mission requests of a faction, highest priority first.

    Args:
        state: The campaign.
        faction: The faction.

    Returns:
        The requests, sorted by decreasing priority.
    """
    t = state.theater
    fid = faction.id
    front_points = [t.provinces[p].center for p in state.front_provinces(fid)]

    def near_front(pos: Vec2, reach: float = FRONT_REACH_M) -> bool:
        """True if ``pos`` is within ``reach`` of one of the faction's front provinces.

        Args:
            pos: The point.
            reach: Maximum distance in metres.

        Returns:
            Whether the point is near the front.
        """
        return any(pos.distance_to(p) <= reach for p in front_points)

    out: list[Request] = []
    for b in state.battles.values():
        if fid in (b.attacker_faction, b.defender_faction):
            p = t.provinces[b.province]
            if b.kind is BattleKind.MEETING:  # on the road, where the forces met
                from .combat import battle_point

                out.append(Request(Task.CAS, 9 + p.victory_points, f"enemy column near {p.name}", battle_point(state, b), TargetKind.BATTLE, b.id))
            else:
                out.append(Request(Task.CAS, 10 + p.victory_points, f"enemy forces at {p.name}", p.center, TargetKind.BATTLE, b.id))

    active_caps = [
        f for f in state.flights.values()
        if f.faction == fid and f.task is Task.CAP and f.target_kind != TargetKind.FLIGHT  # scrambles are extra
        and f.status not in (FlightStatus.DONE, FlightStatus.CANCELLED)
    ]
    covered = {f.target_id for f in active_caps}
    stations = sorted(
        (pid for pid in state.front_provinces(fid) if pid not in covered),
        key=lambda pid: -sum(
            d.combat_power(state.quality(d.faction)) for n in t.neighbors(pid) for d in intel.known_divisions_in(state, fid, n)
        ),
    )
    for pid in stations[: max(0, CAP_STATIONS - len(active_caps))]:
        p = t.provinces[pid]
        station = cap_station(state, fid, p.center)
        if station is not None:
            out.append(Request(Task.CAP, 9, f"CAP over {p.name}", station, TargetKind.POINT, pid))

    for c in state.convoys.values():
        pos = intel.located(state, fid, c.id, KNOWN_CONVOY_S)
        if pos is None:
            continue  # nobody has seen it
        if state.at_war(fid, c.faction) and near_front(pos):
            near = t.nearest(pos).name
            out.append(Request(Task.BAI, 7, f"supply convoy near {near}", pos, TargetKind.CONVOY, c.id))

    for d in state.divisions.values():
        if not state.at_war(fid, d.faction) or d.battle or d.strength < 0.3:
            continue
        contact = intel.knows(state, fid, d.id, KNOWN_DIVISION_S)
        if contact is None:
            continue
        pos = contact.position
        on_the_move = bool(contact.detail.get("moving"))
        at_front = any(n in state.front_provinces(fid) for n in t.neighbors(d.location))
        if (on_the_move or at_front) and near_front(pos, 60_000):
            prio = 6 if on_the_move else 4
            out.append(Request(Task.BAI, prio, f"{d.name} near {t.provinces[d.location].name}", pos, TargetKind.DIVISION, d.id))

    # STRIKE on the enemy's structures (fixed and known): depots by their stock,
    # airbases by the squadrons they keep flying, command posts first.
    for s in state.structures.values():
        owner = s.owner(state)
        if not state.at_war(fid, owner) or s.health <= 0:
            continue
        if s.kind is StructureKind.DEPOT:
            depot = t.provinces[s.province].depot
            prio = 3 + (depot.stock / 5000 if depot is not None else 0)
        elif s.kind is StructureKind.AIRBASE:
            prio = 3 + 0.5 * sum(1 for q in state.squadrons.values() if q.airbase == s.airbase and q.faction == owner)
        else:
            prio = 5.0
        out.append(Request(Task.STRIKE, prio * s.health, s.name, s.position, TargetKind.STRUCTURE, s.id))

    # DEAD on enemy SAM sites that cover the front: strikers on the site under
    # SEAD cover (see build_package; SEAD alone if nobody can strike it).
    from ..air_defense import SamRole

    for c in intel.known(state, fid, EntityKind.SAM):
        u = state.air_defense.get(c.entity_id)
        if u is None or not state.at_war(fid, u.faction) or u.system.role not in (SamRole.LONG, SamRole.MEDIUM):
            continue
        pos = c.position
        if near_front(pos, FRONT_REACH_M + u.system.range_m):
            prio = 8.5 if u.system.role is SamRole.LONG else 7.5
            near = t.nearest(pos).name
            out.append(Request(Task.DEAD, prio, f"{u.system.name} site near {near}", pos, TargetKind.SAM, u.id))
    out += _support_requests(state, faction)
    return sorted(out, key=lambda r: (-r.priority, r.target_id))


def _active(state: CampaignState, fid: str, task: Task, target_kind: TargetKind | None = None, target_id: str | None = None) -> list[Flight]:
    """Live (not done/cancelled) flights of a faction for a task (and target).

    Args:
        state: The campaign.
        fid: Faction id.
        task: The task.
        target_kind: Keep only flights against this kind of target (None = any).
        target_id: Keep only flights against this target (None = any).

    Returns:
        The live flights.
    """
    return [
        f for f in state.flights.values()
        if f.faction == fid and f.task is task and f.status not in (FlightStatus.DONE, FlightStatus.CANCELLED)
        and (target_kind is None or f.target_kind == target_kind) and (target_id is None or f.target_id == target_id)
    ]


def _has_squadron(state: CampaignState, fid: str, task: Task) -> bool:
    """True if the faction has a squadron able to fly ``task``.

    Args:
        state: The campaign.
        fid: Faction id.
        task: The task.

    Returns:
        Whether one of its squadrons with aircraft lists the task.
    """
    return any(sq.faction == fid and task in sq.tasks and sq.aircraft > 0 for sq in state.squadrons.values())


def support_station(state: CampaignState, fid: str, setback: float) -> Vec2 | None:
    """Orbit point for AWACS/tankers: behind the front towards our airbases, and
    further back until outside (known) enemy SAM coverage.

    Args:
        state: The campaign.
        fid: Faction id.
        setback: Distance in metres behind the front to start from.

    Returns:
        The orbit point, or None without front or airbases (or if no clear point is found).
    """
    t = state.theater

    fronts = [t.provinces[p].center for p in state.front_provinces(fid)] or [
        p.center for p in t.provinces.values() if p.controller == fid
    ]

    bases = [b.position for b in state.airbases.values() if b.owner(t) == fid]

    if not fronts or not bases:
        return None

    front = Vec2(sum(p.x for p in fronts) / len(fronts), sum(p.y for p in fronts) / len(fronts))
    home = Vec2(sum(p.x for p in bases) / len(bases), sum(p.y for p in bases) / len(bases))
    leg = home - front

    if leg.length() < 1.0:
        return home

    unit = leg * (1.0 / leg.length())
    point = front + unit * setback
    for _ in range(12):
        if orbit_clear(state, fid, point, air.SUPPORT_ORBIT_RADIUS_M):
            return point
        point = point + unit * 10_000.0

    return None


def orbit_clear(
    state: CampaignState,
    fid: str,
    point: Vec2,
    orbit_radius: float,
    altitude: float = routing.DEFAULT_ALTITUDE_M
) -> bool:
    """True if a whole orbit of ``orbit_radius`` around ``point``, flown at
    ``altitude``, stays out of every known enemy SAM umbrella (with the routing margin).

    Args:
        state: The campaign.
        fid: Planning faction.
        point: Centre of the orbit.
        orbit_radius: Radius of the orbit, metres.
        altitude: Altitude of the orbit, metres (picks the threats that reach it).

    Returns:
        Whether the orbit is clear.
    """
    return all(t.center.distance_to(point) >= t.radius + orbit_radius for t in routing.threats(state, fid, altitude))


def _support_requests(state: CampaignState, faction: Faction) -> list[Request]:
    """AWACS orbit, tanker track, airlift to forward depots, helicopter resupply.

    Args:
        state: The campaign.
        faction: The faction.

    Returns:
        The support requests.
    """
    from . import supply

    t = state.theater
    fid = faction.id
    out: list[Request] = []
    if _has_squadron(state, fid, Task.AWACS) and not _active(state, fid, Task.AWACS):
        station = support_station(state, fid, AWACS_SETBACK_M)
        if station is not None:
            out.append(Request(Task.AWACS, 9.6, SUPPORT_STATION[Task.AWACS], station, TargetKind.POINT, Task.AWACS.lower()))
    if _has_squadron(state, fid, Task.TANKER) and not _active(state, fid, Task.TANKER):
        station = support_station(state, fid, TANKER_SETBACK_M)
        if station is not None:
            out.append(Request(Task.TANKER, 8.8, SUPPORT_STATION[Task.TANKER], station, TargetKind.POINT, Task.TANKER.lower()))
    if _has_squadron(state, fid, Task.TRANSPORT):
        for b in state.airbases.values():
            p = t.provinces.get(b.province) if b.province else None
            if p is None or p.controller != fid or p.depot is None:
                continue
            fill = p.depot.stock / max(1.0, p.depot.capacity)
            if fill < AIRLIFT_THRESHOLD and not _active(state, fid, Task.TRANSPORT, TargetKind.AIRLIFT, b.id):
                # routine sustainment at low priority, urgent when the depot runs dry
                out.append(Request(Task.TRANSPORT, 4.0 + 6.0 * (1.0 - fill), f"airlift to {b.name}", b.position, TargetKind.AIRLIFT, b.id))
        reach = supply.depot_distances(state, fid)
        convoys_to = {c.destination for c in state.convoys.values() if c.faction == fid and not c.returning}
        for d in state.divisions.values():
            if d.faction != fid or d.supply_ratio >= RESUPPLY_THRESHOLD or d.location in convoys_to:
                continue
            r = reach.get(d.location)
            if r is not None and r[0] <= supply.DIRECT_RANGE_M:
                continue
            if not _active(state, fid, Task.TRANSPORT, TargetKind.RESUPPLY, d.id):
                out.append(Request(Task.TRANSPORT, 7.5, f"resupply {d.name}", d.position(t), TargetKind.RESUPPLY, d.id))
    return out


def cap_station(
    state: CampaignState, fid: str, point: Vec2, orbit_radius: float | None = None, altitude: float = CAP_ALT_M
) -> Vec2 | None:
    """Where to orbit to cover ``point``: pulled back towards our nearest airbase
    until the whole orbit is outside the known enemy SAM coverage at the
    orbit's altitude (None if impossible).

    Args:
        state: The campaign.
        fid: Planning faction.
        point: What the orbit should cover.
        orbit_radius: Radius of the orbit (None: a CAP's).
        altitude: Altitude of the orbit (default: a CAP's).

    Returns:
        The orbit point, or None.
    """
    radius = air.CAP_ORBIT_RADIUS_M if orbit_radius is None else orbit_radius
    if orbit_clear(state, fid, point, radius, altitude):
        return point
    bases = [b.position for b in state.airbases.values() if b.owner(state.theater) == fid]
    if not bases:
        return None
    home = min(bases, key=lambda b: b.distance_to(point))
    leg = home - point
    if leg.length() == 0:
        return None
    step = leg * (1.0 / leg.length())
    moved = 0.0
    while moved < min(CAP_PULLBACK_MAX_M, leg.length()):
        moved += CAP_PULLBACK_STEP_M
        candidate = point + step * moved
        if orbit_clear(state, fid, candidate, radius, altitude):
            return candidate
    return None


def _tasked(state: CampaignState, fid: str, r: Request) -> bool:
    """True if a live flight already covers this request's target.

    Args:
        state: The campaign.
        fid: Faction id.
        r: The request.

    Returns:
        Whether the target is already covered.
    """
    return any(
        f.faction == fid
        and f.target_kind == r.target_kind
        and f.target_id == r.target_id
        and f.status not in (FlightStatus.DONE, FlightStatus.CANCELLED)
        for f in state.flights.values()
    )


# ---------------------------------------------------------------- packages


def _daily_sorties(state: CampaignState, fid: str) -> int:
    """Daily aircraft-sortie budget of a faction.

    Args:
        state: The campaign.
        fid: Faction id.

    Returns:
        ``SORTIE_RATE_PER_DAY`` times the faction's aircraft.
    """
    fleet = sum(sq.aircraft for sq in state.squadrons.values() if sq.faction == fid)
    return int(SORTIE_RATE_PER_DAY * fleet)


def sortie_budget_left(state: CampaignState, fid: str) -> int:
    """Aircraft-sorties still available in the rolling 24 h window (never negative).

    Args:
        state: The campaign.
        fid: Faction id.

    Returns:
        The sorties left (old log entries are dropped as a side effect).
    """
    log = [(t, n) for t, n in state.sortie_log.get(fid, []) if t > state.time - 86400]
    state.sortie_log[fid] = log
    return max(0, _daily_sorties(state, fid) - sum(n for _, n in log))


def sortie_tokens(state: CampaignState, fid: str) -> float:
    """Token bucket pacing the day: the daily budget trickles in hour by hour and
    unused share accumulates for up to BURST_HOURS, so a big package can still
    launch while the budget is not burnt in the first hours of the day.

    Args:
        state: The campaign.
        fid: Faction id.

    Returns:
        The tokens available now (the bucket is refilled and stored as a side effect).
    """
    rate = _daily_sorties(state, fid) / 24.0
    cap = max(8.0, rate * BURST_HOURS)
    key = Timer.SORTIE_TOKENS.key(fid)
    tokens = state.sortie_tokens.get(fid, cap)  # start with a full bucket
    if key in state.timers:
        tokens += rate * (state.time - state.timers[key]) / 3600.0
    tokens = min(cap, tokens)
    state.timers[key] = state.time
    state.sortie_tokens[fid] = tokens
    return tokens


def needs_sead(state: CampaignState, fid: str, point: Vec2) -> bool:
    """Short-range air defence around the target, or a SAM site covering it.

    Args:
        state: The campaign.
        fid: Planning faction.
        point: The target point.

    Returns:
        Whether SEAD is needed at the target.
    """

    return air.air_defense(state, fid, point) >= SEAD_THRESHOLD or bool(intel.known_covering(state, fid, point))


def _likely_home(state: CampaignState, fid: str, task: Task, point: Vec2) -> Vec2 | None:
    """Nearest own airbase with a squadron for ``task``: where the package will
    most likely come from, to estimate its route before allocating it.

    Args:
        state: The campaign.
        fid: Planning faction.
        task: The task.
        point: The target point.

    Returns:
        The position of the nearest suitable airbase, or None.
    """
    bases = [
        state.airbases[sq.airbase] for sq in state.squadrons.values()
        if sq.faction == fid and task in sq.tasks and state.airbases[sq.airbase].owner(state.theater) == fid
    ]
    return min((b.position for b in bases), key=lambda p: (p.distance_to(point), p.x, p.y), default=None)


def sead_required(state: CampaignState, fid: str, r: Request, home: Vec2 | None = None) -> bool:
    """True if a package against ``r`` needs SEAD support: air defence at the
    target, or a known SAM umbrella its route cannot fly around.

    Args:
        state: The campaign.
        fid: Planning faction.
        r: The request.
        home: Where the main flight takes off (None: the likeliest airbase).

    Returns:
        Whether SEAD must go with the package.
    """
    return needs_sead(state, fid, r.point) or route_exposed(state, fid, r, home)


def route_exposed(state: CampaignState, fid: str, r: Request, home: Vec2 | None = None) -> bool:
    """True if the best route to ``r`` still enters a known SAM umbrella (the
    target's own cover, or one too costly to fly around).

    Args:
        state: The campaign.
        fid: Planning faction.
        r: The request.
        home: Where the main flight takes off (None: the likeliest airbase).

    Returns:
        Whether the route is exposed (False if the faction has no base for the task).
    """
    home = home or _likely_home(state, fid, r.task, r.point)

    if home is None:
        return False

    waypoints, _ = plan_route(state, fid, r.task, home, r.point)
    return bool(routing.exposure(waypoints, routing.threats(state, fid)))


def package_size(state: CampaignState, fid: str, r: Request) -> int:
    """Aircraft the package for ``r`` will need (main flight + SEAD/escort if called for).

    Args:
        state: The campaign.
        fid: Planning faction.
        r: The request.

    Returns:
        The number of aircraft.
    """
    if r.task is Task.DEAD:
        return 4 + (2 if enemy_air_threat(state, fid, r.point) else 0)
    size = 4 if r.task is Task.STRIKE else 2
    if r.task in (Task.CAS, Task.BAI, Task.STRIKE):
        if sead_required(state, fid, r):
            size += 2
        if enemy_air_threat(state, fid, r.point):
            size += 2
    return size


def plan(state: CampaignState, faction: Faction) -> None:
    """Build up to ``MAX_NEW_PACKAGES`` combat packages (and up to
    ``MAX_NEW_SUPPORT`` support ones, which have their own allowance so routine
    airlift is not starved by combat requests) within the sortie budget.

    Args:
        state: The campaign.
        faction: The planning faction.
    """
    made = made_support = 0
    max_new = structures.packages_per_cycle(state, faction.id, MAX_NEW_PACKAGES)  # command posts hit: fewer
    active = sum(
        1 for p in state.packages.values()
        if p.faction == faction.id and not p.done and p.task is not Task.CAP and not p.task.support
    )
    for r in requests(state, faction):
        if made >= max_new and made_support >= MAX_NEW_SUPPORT:
            break
        if (made_support if r.task.support else made) >= (MAX_NEW_SUPPORT if r.task.support else max_new):
            continue
        if r.task is not Task.CAP and not r.task.support and active >= MAX_ACTIVE_PACKAGES:
            continue
        if _tasked(state, faction.id, r):
            continue
        need = package_size(state, faction.id, r)
        if min(sortie_budget_left(state, faction.id), sortie_tokens(state, faction.id)) < need:
            continue
        if build_package(state, faction, r):
            if r.task.support:
                made_support += 1
            else:
                made += 1
            active += r.task is not Task.CAP and not r.task.support


def _allocate(
    state: CampaignState,
    fid: str,
    task: Task,
    point: Vec2,
    size: int,
    exclude: set[str] = frozenset(),
    require=None,
) -> tuple[Squadron, float] | None:
    """Best squadron for a task in range of a point.

    Args:
        state: The campaign.
        fid: Planning faction.
        task: The task to fly.
        point: The target point.
        size: Aircraft needed.
        exclude: Squadron ids not to use.
        require: Extra filter on squadrons (None = any).

    Returns:
        (squadron, distance) or None.
    """
    from . import air

    # A tanker on station stretches the combat radius of strike and fighter aircraft.
    stretch = TANKER_RADIUS_FACTOR if not task.support and air.tanker_on_station(state, fid) else 1.0
    best: tuple[float, Squadron, float] | None = None
    for sq in state.squadrons.values():
        if sq.faction != fid or sq.id in exclude or task not in sq.tasks or sq.available_at(state.time) < size:
            continue

        if require is not None and not require(sq):
            continue

        base = state.airbases[sq.airbase]

        if base.owner(state.theater) != fid or structures.airbase_factor(state, sq.airbase) <= 0:
            continue  # lost, or its facilities in ruins

        dist = base.position.distance_to(point)

        if dist > sq.aircraft_type.radius_km * 1000 * (1.0 if sq.aircraft_type.helo else stretch):
            continue

        if task.support:
            quality = 1.0 + sq.aircraft_type.cargo / 100.0

        else:
            quality = sq.aircraft_type.a2a if task.air_to_air else sq.aircraft_type.a2g

        score = quality / (1.0 + dist / 200_000.0)

        if best is None or score > best[0]:
            best = (score, sq, dist)

    return None if best is None else (best[1], best[2])


def enemy_air_threat(state: CampaignState, fid: str, point: Vec2) -> bool:
    """True if enemy fighters can reach ``point``.

    Args:
        state: The campaign.
        fid: Planning faction.
        point: The target point.

    Returns:
        Whether an enemy CAP or escort squadron has the point within its combat radius.
    """
    for sq in state.squadrons.values():
        if not state.at_war(fid, sq.faction) or sq.aircraft <= 0 or not (sq.tasks & {Task.CAP, Task.ESCORT}):
            continue
        base = state.airbases[sq.airbase]
        if base.owner(state.theater) == sq.faction and base.position.distance_to(point) <= sq.aircraft_type.radius_km * 1000:
            return True
    return False


def build_package(
    state: CampaignState,
    faction: Faction,
    r: Request,
    squadron: str | None = None,
    tot: float | None = None
) -> Package | None:
    """Build a package for a request: main flight plus SEAD and escort as needed.

    Args:
        state: The campaign.
        faction: The planning faction.
        r: The mission request.
        squadron: Squadron of the main flight (None: the best one available).
        tot: Time on target (None: the earliest possible, rounded to 5 minutes).
            Earlier than the earliest possible is refused.

    Returns:
        The package, or None if no squadron is available (or SEAD is required
        and missing, or ``tot`` is too early).
    """
    fid = faction.id

    if r.task is Task.DEAD:
        return _build_dead(state, faction, r, squadron, tot)

    size = 4 if r.task is Task.STRIKE else (1 if r.task in (Task.AWACS, Task.TANKER) else 2)
    require = _target_requirement(state, r)

    if squadron is not None:
        base_require = require
        require = lambda sq: sq.id == squadron and (base_require is None or base_require(sq))  # noqa: E731

    main = _allocate(state, fid, r.task, r.point, size, require=require)

    if main is None:
        return None

    sq, _ = main

    # Route first: around the umbrellas it can avoid. What is left (the target's
    # own cover, or a detour too long) calls for SEAD, without which strikers stay home.
    main_route = plan_route(state, fid, r.task, state.airbases[sq.airbase].position, r.point, sq)
    sead = None
    if r.task in (Task.CAS, Task.BAI, Task.STRIKE) and sead_required(state, fid, r, state.airbases[sq.airbase].position):
        sead = _allocate(state, fid, Task.SEAD, r.point, 2, {sq.id})

        if sead is None and routing.exposure(main_route[0], routing.threats(state, fid)):
            return None

    earliest = earliest_tot(sq, routing.length(main_route[0][: main_route[1] + 1]), state.time)
    if sead is not None:
        earliest = max(earliest, earliest_tot(sead[0], sead[1], state.time) + 120)

    if tot is None:
        tot = math.ceil(earliest / 300.0) * 300.0  # round to 5 minutes

    elif tot < earliest:
        return None

    pkg = _new_package(state, fid, r, tot)
    lead = _add_flight(state, pkg, sq, r.task, size, tot, r, main_route)

    used = {sq.id}
    if r.task in (Task.CAS, Task.BAI, Task.STRIKE):
        if sead is not None:
            used.add(sead[0].id)
            _add_flight(state, pkg, sead[0], Task.SEAD, 2, tot - 120, r)

        _add_escort(state, pkg, lead, r, used)

    _announce(state, pkg)

    return pkg


def _add_escort(state: CampaignState, pkg: Package, lead: Flight, r: Request, used: set[str]) -> None:
    """Fighter escort for a package if enemy fighters can reach its target.

    The escort sweeps ahead of the package and holds at the edge of the SAM
    umbrella its route enters (the SEAD flight and its ECM work inside):
    fighters are no use against SAMs, and enemy fighters keep out of their
    own SAMs' way too. With no umbrella on the route it goes to the target.

    Args:
        state: The campaign.
        pkg: The package.
        lead: Its main flight.
        r: The package's request.
        used: Squadron ids already in the package (not used again).
    """
    if not enemy_air_threat(state, pkg.faction, r.point):
        return
    esc = _allocate(state, pkg.faction, Task.ESCORT, r.point, 2, used)
    if esc is None:
        return
    umbrellas = routing.threats(state, pkg.faction, esc[0].aircraft_type.cruise_alt_m)
    screen = routing.screen_point(lead.waypoints, lead.target_index, umbrellas)
    _add_flight(state, pkg, esc[0], Task.ESCORT, 2, pkg.tot, replace(r, point=screen) if screen else r)


def _new_package(state: CampaignState, fid: str, r: Request, tot: float, task: Task | None = None) -> Package:
    """Register an empty package for ``r`` with its common frequency.

    Args:
        state: The campaign.
        fid: Planning faction.
        r: The request.
        tot: Time on target.
        task: Task of the package (None: the request's).

    Returns:
        The new package.
    """
    pkg = Package(
        id=state.new_id(IdPrefix.PACKAGE),
        faction=fid,
        task=task or r.task,
        target_label=r.label,
        target_point=r.point,
        tot=tot,
        created=state.time,
    )
    pkg.uhf_mhz = radio.allocate(pkg.id, radio.PACKAGE_BAND, _taken_frequencies(state))
    state.packages[pkg.id] = pkg
    return pkg


def _build_dead(
    state: CampaignState, faction: Faction, r: Request, squadron: str | None, tot: float | None
) -> Package | None:
    """DEAD package on a SAM site: SEAD (with ECM) two minutes ahead keeps the
    radars down, strikers bomb the site, fighters escort if enemy fighters can
    reach it. Without strikers it is flown as SEAD alone; without SEAD it is
    not flown at all (bombers into a live umbrella).

    Args:
        state: The campaign.
        faction: Planning faction.
        r: DEAD request on a ``sam`` target.
        squadron: Squadron of the strikers (None: the best available).
        tot: Time on target of the strikers (None: the earliest possible).

    Returns:
        The package, or None if it cannot be flown.
    """
    fid = faction.id
    if squadron is not None:
        strike = _allocate(state, fid, Task.STRIKE, r.point, 2, require=lambda sq: sq.id == squadron)
        if strike is None:
            return None
    else:
        strike = _allocate(state, fid, Task.STRIKE, r.point, 2)
    sead = _allocate(state, fid, Task.SEAD, r.point, 2, {strike[0].id} if strike else set())
    if sead is None:
        return None
    lead = 120.0 if strike else 0.0
    earliest = earliest_tot(sead[0], sead[1], state.time) + lead
    if strike:
        earliest = max(earliest, earliest_tot(strike[0], strike[1], state.time))
    if tot is None:
        tot = math.ceil(earliest / 300.0) * 300.0
    elif tot < earliest:
        return None

    pkg = _new_package(state, fid, r, tot, Task.DEAD if strike else Task.SEAD)
    first = _add_flight(state, pkg, sead[0], Task.SEAD, 2, tot - lead, r)
    used = {sead[0].id}
    if strike:
        first = _add_flight(state, pkg, strike[0], Task.STRIKE, 2, tot, r)
        used.add(strike[0].id)
    _add_escort(state, pkg, first, r, used)
    _announce(state, pkg)
    return pkg


def _announce(state: CampaignState, pkg: Package) -> None:
    """Emit ``package_planned`` for a package just built.

    Args:
        state: The campaign.
        pkg: The package.
    """
    state.emit(
        EventKind.PACKAGE_PLANNED,
        package=pkg.id,
        number=pkg.number,
        faction=pkg.faction,
        task=pkg.task.value,
        target=pkg.target_label,
        tot=clock(pkg.tot),
        flights=[state.flights[f].callsign for f in pkg.flights],
    )


def _target_requirement(state: CampaignState, r: Request):
    """Squadron filter a request's target imposes (None: any squadron able to fly the task).

    Args:
        state: The campaign.
        r: The request.

    Returns:
        A predicate on squadrons, or None.
    """
    if r.target_kind == TargetKind.RESUPPLY:
        return lambda sq: sq.aircraft_type.helo  # land next to the brigades
    if r.target_kind == TargetKind.AIRLIFT:
        dest = state.airbases[r.target_id]
        return lambda sq: sq.airbase != dest.id and not sq.aircraft_type.helo
    return None


def earliest_tot(sq: Squadron, dist: float, now: float) -> float:
    """Earliest time on target for a squadron ``dist`` metres from the target:
    startup lead for human pilots, then the transit at cruise speed.

    Args:
        sq: The squadron.
        dist: Distance in metres from its base to the target.
        now: Current campaign time.

    Returns:
        The earliest TOT, campaign seconds.
    """
    return now + STARTUP_LEAD_S + dist / (sq.aircraft_type.cruise_kmh / 3.6)


def _taken_frequencies(state: CampaignState) -> list[float]:
    """Frequencies a new package/flight must keep clear of: the theatre's
    airfields and guard, and every live package and flight.

    Args:
        state: The campaign.

    Returns:
        The frequencies in MHz.
    """
    taken = list(radio.reserved(state.theater.name))
    taken += [p.uhf_mhz for p in state.packages.values() if not p.done and p.uhf_mhz]
    taken += [
        f.vhf_mhz for f in state.flights.values()
        if f.vhf_mhz and f.status not in (FlightStatus.DONE, FlightStatus.CANCELLED)
    ]
    return taken


def _station_until(state: CampaignState, fid: str, task: Task, tot: float) -> float:
    """End of the station time of an orbiting flight (0 for others).

    Args:
        state: The campaign.
        fid: Faction id.
        task: The flight's task.
        tot: Its time on station.

    Returns:
        Campaign time when the station ends.
    """
    from . import air

    if task is Task.CAP:
        return tot + CAP_STATION_S + (TANKER_CAP_BONUS_S if air.tanker_on_station(state, fid) else 0.0)
    if task in (Task.AWACS, Task.TANKER):
        return tot + SUPPORT_STATION_S
    return 0.0


def _rotate(v: Vec2, a: float) -> Vec2:
    """Rotate a vector by ``a`` radians.

    Args:
        v: The vector.
        a: Angle in radians (counter-clockwise in x/y).

    Returns:
        The rotated vector.
    """
    import math

    return Vec2(v.x * math.cos(a) - v.y * math.sin(a), v.x * math.sin(a) + v.y * math.cos(a))


def _key_points(home: Vec2, target: Vec2, task: Task, axis: float = 0.0, egress_side: float = 1.0) -> tuple[list[Vec2], int]:
    """Key waypoints of a sortie before any detour: home, IP, target, egress, home
    (orbits, transports and short hops just go there and back).

    Args:
        home: Takeoff and landing point.
        target: Target or station.
        task: Flight task.
        axis: Bearing of the IP off the direct inbound axis, radians.
        egress_side: +1 or -1: which side of the attack axis to leave by.

    Returns:
        (points, index of the target).
    """
    leg = home - target
    if task.orbits or task is Task.TRANSPORT or leg.length() <= MIN_ATTACK_LEG_M:
        return [home, target, home], 1
    unit = _rotate(leg * (1.0 / leg.length()), axis)
    # IP and egress scale down for targets close to the base.
    ip = target + unit * min(IP_DISTANCE_M, leg.length() * 0.5)
    # Egress off the attack axis (not back over the same defences), then home.
    egress = target + _rotate(unit, egress_side * EGRESS_TURN_RAD) * min(EGRESS_DISTANCE_M, leg.length() * 0.5)
    return [home, ip, target, egress, home], 2


def plan_route(
    state: CampaignState, fid: str, task: Task, home: Vec2, target: Vec2, sq: Squadron | None = None
) -> tuple[list[Vec2], int]:
    """Route of a sortie around the enemy SAM umbrellas the faction knows.

    Attack sorties try several attack axes and both egress sides, and keep the
    one with the least exposure (metres inside umbrellas, weighted) for its
    length; every leg detours around the umbrellas it can (see
    :mod:`dynacamp.systems.routing`). Umbrellas over the target itself cannot be
    avoided: they are what the package's SEAD is for.

    Args:
        state: The campaign.
        fid: Planning faction.
        task: Flight task.
        home: Takeoff and landing point.
        target: Target or station.
        sq: Squadron flying it, to keep the route within its fuel (None: no check).

    Returns:
        (waypoints, index of the target waypoint).
    """
    umbrellas = routing.threats(state, fid, sq.aircraft_type.cruise_alt_m if sq else routing.DEFAULT_ALTITUDE_M)
    plain = _key_points(home, target, task)
    if not umbrellas:
        return plain
    fuel = math.inf
    if sq is not None:
        stretch = TANKER_RADIUS_FACTOR if not task.support and not sq.aircraft_type.helo and air.tanker_on_station(state, fid) else 1.0
        fuel = 2.0 * sq.aircraft_type.radius_km * 1000.0 * stretch
    axes = [(0.0, 1.0)] if plain[1] == 1 else [(a, s) for a in ATTACK_AXES_RAD for s in (1.0, -1.0)]
    best: tuple[float, list[Vec2], int] | None = None
    for axis, side in axes:
        points, ti = _key_points(home, target, task, axis, side)
        waypoints, index = routing.route(points, umbrellas)
        total = routing.length(waypoints)
        if total > fuel:
            continue
        cost = total + EXPOSURE_WEIGHT * sum(routing.exposure(waypoints, umbrellas).values())
        if best is None or cost < best[0]:
            best = (cost, waypoints, index[ti])
    if best is None:
        return plain
    return best[1], best[2]


def _add_flight(
    state: CampaignState, pkg: Package, sq: Squadron, task: Task, size: int, tot: float, r: Request,
    planned: tuple[list[Vec2], int] | None = None,
) -> Flight:
    """Create a flight for a package and commit its aircraft.

    Args:
        state: The campaign.
        pkg: Its package.
        sq: Squadron that flies it.
        task: Flight task.
        size: Aircraft.
        tot: Time on target (or on station).
        r: The package's request.
        planned: Route already planned with :func:`plan_route` (None: plan it here).

    Returns:
        The new flight.
    """
    base = state.airbases[sq.airbase]
    home = base.position
    waypoints, target_index = planned or plan_route(state, sq.faction, task, home, r.point, sq)
    to_target = sum(waypoints[i].distance_to(waypoints[i + 1]) for i in range(target_index))
    speed = sq.aircraft_type.cruise_kmh / 3.6
    flight = Flight(
        id=state.new_id(IdPrefix.FLIGHT),
        faction=sq.faction,
        location=base.province or "",
        package=pkg.id,
        squadron=sq.id,
        aircraft_type=sq.aircraft_type,
        task=task,
        callsign=sq.next_callsign(),
        size=size,
        takeoff_time=tot - to_target / speed,
        tot=tot,
        waypoints=waypoints,
        target_index=target_index,
        pos=home,
        target_kind=r.target_kind,
        target_id=r.target_id,
        station_until=_station_until(state, sq.faction, task, tot),
    )
    air.ready(state, flight)  # fuel and missiles
    if task is Task.CAP and not math.isinf(flight.fuel_m):
        # Station time within the fuel: there and back, the reserve, and some for a fight.
        back = waypoints[target_index].distance_to(home) * air.BINGO_MARGIN + air.BINGO_RESERVE_M
        spare = (flight.fuel_m - to_target - back) * CAP_FUEL_FOR_FIGHT
        flight.station_until = min(flight.station_until, tot + max(0.0, spare) / (air.ORBIT_FUEL * speed))
    flight.vhf_mhz = radio.allocate(flight.id, radio.FLIGHT_BAND, _taken_frequencies(state))
    sq.committed += size
    state.sortie_log.setdefault(sq.faction, []).append((state.time, size))
    state.sortie_tokens[sq.faction] = state.sortie_tokens.get(sq.faction, 0.0) - size
    state.flights[flight.id] = flight
    pkg.flights.append(flight.id)
    return flight


# ---------------------------------------------------------------- players


def join(
    state: CampaignState, player_id: str, name: str, coalition: str, flight_id: str, capable: frozenset[Task] | None
) -> tuple[bool, str]:
    """A player asks for a seat.

    Args:
        state: The campaign.
        player_id: Player id.
        name: Player name.
        coalition: Player's game coalition.
        flight_id: Requested flight.
        capable: Tasks the player's aircraft can fly (None = no restriction).

    Returns:
        (accepted, message for the player).
    """
    f = state.flights.get(flight_id)
    if f is None or f.status in (FlightStatus.DONE, FlightStatus.CANCELLED):
        return False, "That flight no longer exists."
    if state.factions[f.faction].coalition != coalition:
        return False, "That flight belongs to the other side."
    if player_id in f.players and f.players[player_id].state in (SeatState.ASSIGNED, SeatState.AIRBORNE):
        return True, briefing_text(state, f)
    if not f.joinable or f.ai_count <= 0:
        return False, f"{f.callsign} has no free seats."
    if capable is not None and f.task not in capable:
        return False, f"Your aircraft cannot fly {f.task.value}."
    leave(state, player_id)
    f.players[player_id] = Seat(player_id, name)
    state.player_flights[player_id] = f.id
    state.emit(EventKind.SEAT_TAKEN, player=player_id, flight=f.id, callsign=f.callsign, package=f.package)
    return True, briefing_text(state, f)


def seat_of(state: CampaignState, player_id: str) -> tuple[Flight, Seat] | None:
    """The flight and seat a player holds, or None.

    Args:
        state: The campaign.
        player_id: Player id.

    Returns:
        The flight and the seat.
    """
    fid = state.player_flights.get(player_id)
    f = state.flights.get(fid) if fid else None
    if f is None or player_id not in f.players:
        return None
    return f, f.players[player_id]


def leave(state: CampaignState, player_id: str) -> bool:
    """Release a player's seat. Returns True if they had one.

    Args:
        state: The campaign.
        player_id: Player id.

    Returns:
        Whether the player had a seat.
    """
    found = seat_of(state, player_id)
    state.player_flights.pop(player_id, None)
    if found is None:
        return False
    f, seat = found
    if seat.state in (SeatState.ASSIGNED, SeatState.AIRBORNE):
        seat.state = SeatState.LEFT
        state.emit(EventKind.SEAT_RELEASED, player=player_id, flight=f.id)
    return True


def player_took_off(state: CampaignState, player_id: str) -> None:
    """Mark a player's seat airborne.

    Args:
        state: The campaign.
        player_id: Player id.
    """
    found = seat_of(state, player_id)
    if found and found[1].state is SeatState.ASSIGNED:
        found[1].state = SeatState.AIRBORNE
        state.emit(EventKind.PLAYER_TAKEOFF, player=player_id, flight=found[0].id)


def player_landed(state: CampaignState, player_id: str) -> None:
    """Mark a player's seat landed and release the assignment.

    Args:
        state: The campaign.
        player_id: Player id.
    """
    found = seat_of(state, player_id)
    if found and found[1].state is SeatState.AIRBORNE:
        found[1].state = SeatState.LANDED
        state.player_flights.pop(player_id, None)
        state.emit(EventKind.PLAYER_LANDED, player=player_id, flight=found[0].id)


def player_lost(state: CampaignState, player_id: str) -> None:
    """A player's aircraft was lost: count it against the squadron.

    Args:
        state: The campaign.
        player_id: Player id.
    """
    found = seat_of(state, player_id)
    if not found or found[1].state not in (SeatState.ASSIGNED, SeatState.AIRBORNE):
        return
    f, seat = found
    seat.state = SeatState.LOST
    state.player_flights.pop(player_id, None)
    f.size -= 1
    sq = state.squadrons[f.squadron]
    sq.aircraft -= 1
    sq.committed -= 1
    state.emit(EventKind.AIRCRAFT_LOST, flight=f.id, faction=f.faction, count=1, cause=LossReason.PLAYER, player=player_id)


def player_disconnected(state: CampaignState, player_id: str) -> None:
    """A pilot vanished: unflown seats are freed, airborne ones are assumed home.

    Args:
        state: The campaign.
        player_id: Player id.
    """
    found = seat_of(state, player_id)
    if not found:
        return
    if found[1].state is SeatState.AIRBORNE:
        player_landed(state, player_id)
    else:
        leave(state, player_id)


# ---------------------------------------------------------------- presentation


def briefing_text(state: CampaignState, f: Flight) -> str:
    """Briefing of a flight for its players (see :mod:`dynacamp.systems.briefing`).

    Args:
        state: The campaign.
        f: The flight.

    Returns:
        The rendered briefing.
    """
    from . import briefing

    return briefing.render(briefing.briefing(state, f), now=state.time)


def waypoint_marks(state: CampaignState, f: Flight) -> list[tuple[str, Vec2]]:
    """Map marks for a flight's named waypoints: (label, position).

    Args:
        state: The campaign.
        f: The flight.

    Returns:
        The marks.
    """
    from . import briefing

    return briefing.waypoint_marks(state, f)


def ato_board(state: CampaignState, coalition: str, limit: int = 8) -> list[dict]:
    """What players of ``coalition`` can join, for in-game menus.

    Args:
        state: The campaign.
        coalition: Game coalition.
        limit: Maximum number of packages.

    Returns:
        Packages (id, label and flights with free seats), soonest TOT first.
    """
    board = []
    for pkg in sorted(state.packages.values(), key=lambda p: (p.tot, p.id)):
        if pkg.done or state.factions[pkg.faction].coalition != coalition:
            continue
        flights = [
            {
                "id": f.id,
                "label": f"{f.callsign} {f.aircraft_type.name} {f.task.value} ({f.seats_free} free)",
                "task": f.task.value,
                "seats": f.seats_free,
            }
            for f in (state.flights[i] for i in pkg.flights if i in state.flights)
            if f.seats_free > 0
        ]
        if flights:
            board.append(
                {"id": pkg.id, "label": f"PKG {pkg.number} {pkg.task.value} {pkg.target_label} TOT {clock(pkg.tot)}", "flights": flights}
            )
    return board[:limit]
