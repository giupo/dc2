"""Air Tasking Order: planning packages and seating players in them.

Every ATO cycle each faction turns the situation into mission requests
(CAS over our battles, CAP over the front, BAI on enemy columns and convoys,
STRIKE on depots), ranks them, and builds packages: a main flight plus SEAD
when the target is defended and an escort when enemy fighters can reach it.
Takeoffs are planned at least ``STARTUP_LEAD_S`` ahead so that human pilots
have time to pick a flight and start up.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..air import FlightStatus, Flight, Package, Seat, SeatState, Squadron, Task
from ..events import clock
from ..geo import Vec2
from ..state import CampaignState, Faction
from .. import radio
from . import air, intel

ATO_INTERVAL_S = 3600.0
STARTUP_LEAD_S = 30 * 60.0
MAX_NEW_PACKAGES = 3  # per faction per cycle
MAX_NEW_SUPPORT = 2  # AWACS / tanker / transport packages per faction per cycle
MAX_ACTIVE_PACKAGES = 6  # per faction, CAP excluded
SORTIE_RATE_PER_DAY = 2.5  # aircraft-sorties per airframe per day
BURST_HOURS = 4.0  # sortie tokens accumulate up to this many hours of the daily rate
CAP_STATIONS = 3  # concurrent CAP flights per faction
CAP_STATION_S = 150 * 60.0
IP_DISTANCE_M = 25_000.0
EGRESS_DISTANCE_M = 20_000.0
MIN_ATTACK_LEG_M = 8_000.0  # below this the flight goes straight at the target
EGRESS_TURN_RAD = 0.7  # ~40 degrees off the inbound axis
SEAD_THRESHOLD = 4.0
FRONT_REACH_M = 80_000.0


@dataclass(frozen=True)
class Request:
    """A mission request before allocation.

    Attributes:
        task: Mission type.
        priority: Higher first.
        label: Human-readable target.
        point: Target position.
        target_kind: ``battle``, ``division``, ``convoy``, ``depot``, ``sam`` or ``point``.
        target_id: Target id.
    """
    task: Task
    priority: float
    label: str
    point: Vec2
    target_kind: str
    target_id: str


def run(state: CampaignState, dt: float) -> None:
    """Plan packages for every faction with squadrons, every ``ATO_INTERVAL_S``."""
    if state.time < state.timers.get("ato", 0.0):
        return
    state.timers["ato"] = state.time + ATO_INTERVAL_S
    for faction in state.factions.values():
        if any(s.faction == faction.id for s in state.squadrons.values()):
            plan(state, faction)


# ---------------------------------------------------------------- requests


def requests(state: CampaignState, faction: Faction) -> list[Request]:
    """Mission requests of a faction, highest priority first.

    Args:
        state: The campaign.
        faction: The faction.
    """
    t = state.theater
    fid = faction.id
    front_points = [t.provinces[p].center for p in state.front_provinces(fid)]

    def near_front(pos: Vec2, reach: float = FRONT_REACH_M) -> bool:
        """True if ``pos`` is within ``reach`` of one of the faction's front provinces."""
        return any(pos.distance_to(p) <= reach for p in front_points)

    out: list[Request] = []
    for b in state.battles.values():
        if fid in (b.attacker_faction, b.defender_faction):
            p = t.provinces[b.province]
            out.append(Request(Task.CAS, 10 + p.victory_points, f"enemy forces at {p.name}", p.center, "battle", b.id))

    active_caps = [
        f for f in state.flights.values()
        if f.faction == fid and f.task is Task.CAP and f.status not in (FlightStatus.DONE, FlightStatus.CANCELLED)
    ]
    covered = {f.target_id for f in active_caps}
    stations = sorted(
        (pid for pid in state.front_provinces(fid) if pid not in covered),
        key=lambda pid: -sum(
            d.combat_power() for n in t.neighbors(pid) for d in intel.known_divisions_in(state, fid, n)
        ),
    )
    for pid in stations[: max(0, CAP_STATIONS - len(active_caps))]:
        p = t.provinces[pid]
        station = cap_station(state, fid, p.center)
        if station is not None:
            out.append(Request(Task.CAP, 9, f"CAP over {p.name}", station, "point", pid))

    for c in state.convoys.values():
        pos = intel.located(state, fid, c.id, KNOWN_CONVOY_S)
        if pos is None:
            continue  # nobody has seen it
        if state.at_war(fid, c.faction) and near_front(pos):
            near = t.nearest(pos).name
            out.append(Request(Task.BAI, 7, f"supply convoy near {near}", pos, "convoy", c.id))

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
            out.append(Request(Task.BAI, prio, f"{d.name} near {t.provinces[d.location].name}", pos, "division", d.id))

    for pid, p in t.provinces.items():
        if p.depot is not None and state.at_war(fid, p.controller):
            out.append(Request(Task.STRIKE, 3 + p.depot.stock / 5000, f"supply depot {p.name}", p.center, "depot", pid))

    # SEAD/DEAD on enemy SAM sites that cover the front.
    from ..air_defense import SamRole

    for c in intel.known(state, fid, "sam"):
        u = state.air_defense.get(c.entity_id)
        if u is None or not state.at_war(fid, u.faction) or u.system.role not in (SamRole.LONG, SamRole.MEDIUM):
            continue
        pos = c.position
        if near_front(pos, FRONT_REACH_M + u.system.range_m):
            prio = 8.5 if u.system.role is SamRole.LONG else 7.5
            near = t.nearest(pos).name
            out.append(Request(Task.SEAD, prio, f"{u.system.name} site near {near}", pos, "sam", u.id))
    out += _support_requests(state, faction)
    return sorted(out, key=lambda r: (-r.priority, r.target_id))


SUPPORT_STATION_S = 4 * 3600.0
KNOWN_DIVISION_S = 2 * 3600.0  # BAI only on brigades located this recently
KNOWN_CONVOY_S = 1800.0
AWACS_SETBACK_M = 120_000.0
TANKER_SETBACK_M = 80_000.0
AIRLIFT_THRESHOLD = 0.85  # sustainment airlift to forward depots below this share of capacity
RESUPPLY_THRESHOLD = 0.3  # helicopter resupply of brigades below this supply ratio
TANKER_RADIUS_FACTOR = 1.4
TANKER_CAP_BONUS_S = 3600.0


def _active(state: CampaignState, fid: str, task: Task, target_kind: str | None = None, target_id: str | None = None) -> list[Flight]:
    """Live (not done/cancelled) flights of a faction for a task (and target)."""
    return [
        f for f in state.flights.values()
        if f.faction == fid and f.task is task and f.status not in (FlightStatus.DONE, FlightStatus.CANCELLED)
        and (target_kind is None or f.target_kind == target_kind) and (target_id is None or f.target_id == target_id)
    ]


def _has_squadron(state: CampaignState, fid: str, task: Task) -> bool:
    """True if the faction has a squadron able to fly ``task``."""
    return any(sq.faction == fid and task in sq.tasks and sq.aircraft > 0 for sq in state.squadrons.values())


def support_station(state: CampaignState, fid: str, setback: float) -> Vec2 | None:
    """Orbit point for AWACS/tankers: behind the front towards our airbases, and
    further back until outside (known) enemy SAM coverage."""
    from . import air_defense

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
        if not intel.known_covering(state, fid, point):
            return point
        point = point + unit * 10_000.0
    return None


def _support_requests(state: CampaignState, faction: Faction) -> list[Request]:
    """AWACS orbit, tanker track, airlift to forward depots, helicopter resupply."""
    from . import supply

    t = state.theater
    fid = faction.id
    out: list[Request] = []
    if _has_squadron(state, fid, Task.AWACS) and not _active(state, fid, Task.AWACS):
        station = support_station(state, fid, AWACS_SETBACK_M)
        if station is not None:
            out.append(Request(Task.AWACS, 9.6, "AWACS orbit", station, "point", "awacs"))
    if _has_squadron(state, fid, Task.TANKER) and not _active(state, fid, Task.TANKER):
        station = support_station(state, fid, TANKER_SETBACK_M)
        if station is not None:
            out.append(Request(Task.TANKER, 8.8, "Tanker track", station, "point", "tanker"))
    if _has_squadron(state, fid, Task.TRANSPORT):
        for b in state.airbases.values():
            p = t.provinces.get(b.province) if b.province else None
            if p is None or p.controller != fid or p.depot is None:
                continue
            fill = p.depot.stock / max(1.0, p.depot.capacity)
            if fill < AIRLIFT_THRESHOLD and not _active(state, fid, Task.TRANSPORT, "airlift", b.id):
                # routine sustainment at low priority, urgent when the depot runs dry
                out.append(Request(Task.TRANSPORT, 4.0 + 6.0 * (1.0 - fill), f"airlift to {b.name}", b.position, "airlift", b.id))
        reach = supply.depot_distances(state, fid)
        convoys_to = {c.destination for c in state.convoys.values() if c.faction == fid and not c.returning}
        for d in state.divisions.values():
            if d.faction != fid or d.supply_ratio >= RESUPPLY_THRESHOLD or d.location in convoys_to:
                continue
            r = reach.get(d.location)
            if r is not None and r[0] <= supply.DIRECT_RANGE_M:
                continue
            if not _active(state, fid, Task.TRANSPORT, "resupply", d.id):
                out.append(Request(Task.TRANSPORT, 7.5, f"resupply {d.name}", d.position(t), "resupply", d.id))
    return out


CAP_PULLBACK_STEP_M = 5_000.0
CAP_PULLBACK_MAX_M = 80_000.0


def cap_station(state: CampaignState, fid: str, point: Vec2) -> Vec2 | None:
    """Where to orbit to cover ``point``: pulled back towards our nearest airbase
    until outside enemy medium/long-range SAM coverage (None if impossible)."""
    from . import air_defense

    if not intel.known_covering(state, fid, point):
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
        if not intel.known_covering(state, fid, candidate):
            return candidate
    return None


def _tasked(state: CampaignState, fid: str, r: Request) -> bool:
    """True if a live flight already covers this request's target."""
    return any(
        f.faction == fid
        and f.target_kind == r.target_kind
        and f.target_id == r.target_id
        and f.status not in (FlightStatus.DONE, FlightStatus.CANCELLED)
        for f in state.flights.values()
    )


# ---------------------------------------------------------------- packages


def _daily_sorties(state: CampaignState, fid: str) -> int:
    """Daily aircraft-sortie budget of a faction."""
    fleet = sum(sq.aircraft for sq in state.squadrons.values() if sq.faction == fid)
    return int(SORTIE_RATE_PER_DAY * fleet)


def sortie_budget_left(state: CampaignState, fid: str) -> int:
    """Aircraft-sorties still available in the rolling 24 h window (never negative)."""
    log = [(t, n) for t, n in state.sortie_log.get(fid, []) if t > state.time - 86400]
    state.sortie_log[fid] = log
    return max(0, _daily_sorties(state, fid) - sum(n for _, n in log))


def sortie_tokens(state: CampaignState, fid: str) -> float:
    """Token bucket pacing the day: the daily budget trickles in hour by hour and
    unused share accumulates for up to BURST_HOURS, so a big package can still
    launch while the budget is not burnt in the first hours of the day."""
    rate = _daily_sorties(state, fid) / 24.0
    cap = max(8.0, rate * BURST_HOURS)
    key = f"sortie_t:{fid}"
    tokens = state.sortie_tokens.get(fid, cap)  # start with a full bucket
    if key in state.timers:
        tokens += rate * (state.time - state.timers[key]) / 3600.0
    tokens = min(cap, tokens)
    state.timers[key] = state.time
    state.sortie_tokens[fid] = tokens
    return tokens


def needs_sead(state: CampaignState, fid: str, point: Vec2) -> bool:
    """Short-range air defence around the target, or a SAM site covering it."""
    from . import air_defense

    return air.air_defense(state, fid, point) >= SEAD_THRESHOLD or bool(intel.known_covering(state, fid, point))


def package_size(state: CampaignState, fid: str, r: Request) -> int:
    """Aircraft the package for ``r`` will need (main flight + SEAD/escort if called for)."""
    size = 4 if r.task is Task.STRIKE else 2
    if r.task in (Task.CAS, Task.BAI, Task.STRIKE):
        if needs_sead(state, fid, r.point):
            size += 2
        if enemy_air_threat(state, fid, r.point):
            size += 2
    return size


def plan(state: CampaignState, faction: Faction) -> None:
    """Build up to ``MAX_NEW_PACKAGES`` combat packages (and up to
    ``MAX_NEW_SUPPORT`` support ones, which have their own allowance so routine
    airlift is not starved by combat requests) within the sortie budget."""
    made = made_support = 0
    active = sum(
        1 for p in state.packages.values()
        if p.faction == faction.id and not p.done and p.task is not Task.CAP and not p.task.support
    )
    for r in requests(state, faction):
        if made >= MAX_NEW_PACKAGES and made_support >= MAX_NEW_SUPPORT:
            break
        if (made_support if r.task.support else made) >= (MAX_NEW_SUPPORT if r.task.support else MAX_NEW_PACKAGES):
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
    state: CampaignState, fid: str, task: Task, point: Vec2, size: int, exclude: set[str] = frozenset(),
    require=None,
) -> tuple[Squadron, float] | None:
    """Best squadron for a task in range of a point.

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
        if base.owner(state.theater) != fid:
            continue
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
    """True if enemy fighters can reach ``point``."""
    for sq in state.squadrons.values():
        if not state.at_war(fid, sq.faction) or sq.aircraft <= 0 or not (sq.tasks & {Task.CAP, Task.ESCORT}):
            continue
        base = state.airbases[sq.airbase]
        if base.owner(state.theater) == sq.faction and base.position.distance_to(point) <= sq.aircraft_type.radius_km * 1000:
            return True
    return False


def build_package(state: CampaignState, faction: Faction, r: Request) -> Package | None:
    """Build a package for a request: main flight plus SEAD and escort as needed.

    Returns:
        The package, or None if no squadron is available (or SEAD is required and missing).
    """
    fid = faction.id
    size = 4 if r.task is Task.STRIKE else (1 if r.task in (Task.AWACS, Task.TANKER) else 2)
    if r.task in (Task.CAS, Task.BAI, Task.STRIKE):
        from . import air_defense

        # Never send strikers under a SAM umbrella without SEAD support.
        if intel.known_covering(state, fid, r.point) and _allocate(state, fid, Task.SEAD, r.point, 2) is None:
            return None
    require = None
    if r.target_kind == "resupply":
        require = lambda sq: sq.aircraft_type.helo  # noqa: E731 - land next to the brigades
    elif r.target_kind == "airlift":
        dest = state.airbases[r.target_id]
        require = lambda sq: sq.airbase != dest.id and not sq.aircraft_type.helo  # noqa: E731
    main = _allocate(state, fid, r.task, r.point, size, require=require)
    if main is None:
        return None
    sq, dist = main
    earliest_takeoff = state.time + STARTUP_LEAD_S
    tot = earliest_takeoff + dist / (sq.aircraft_type.cruise_kmh / 3.6)
    tot = math.ceil(tot / 300.0) * 300.0  # round to 5 minutes

    pkg = Package(
        id=state.new_id("pk"),
        faction=fid,
        task=r.task,
        target_label=r.label,
        target_point=r.point,
        tot=tot,
        created=state.time,
    )
    pkg.uhf_mhz = radio.allocate(pkg.id, radio.PACKAGE_BAND, _taken_frequencies(state))
    state.packages[pkg.id] = pkg
    _add_flight(state, pkg, sq, r.task, size, tot, r)

    used = {sq.id}
    if r.task in (Task.CAS, Task.BAI, Task.STRIKE):
        if needs_sead(state, fid, r.point):
            sead = _allocate(state, fid, Task.SEAD, r.point, 2, used)
            if sead:
                used.add(sead[0].id)
                _add_flight(state, pkg, sead[0], Task.SEAD, 2, tot - 120, r)
        if enemy_air_threat(state, fid, r.point):
            esc = _allocate(state, fid, Task.ESCORT, r.point, 2, used)
            if esc:
                _add_flight(state, pkg, esc[0], Task.ESCORT, 2, tot, r)

    state.emit(
        "package_planned",
        package=pkg.id,
        number=pkg.number,
        faction=fid,
        task=r.task.value,
        target=r.label,
        tot=clock(tot),
        flights=[state.flights[f].callsign for f in pkg.flights],
    )
    return pkg


def _taken_frequencies(state: CampaignState) -> list[float]:
    """Frequencies a new package/flight must keep clear of: the theatre's
    airfields and guard, and every live package and flight."""
    taken = list(radio.reserved(state.theater.name))
    taken += [p.uhf_mhz for p in state.packages.values() if not p.done and p.uhf_mhz]
    taken += [
        f.vhf_mhz for f in state.flights.values()
        if f.vhf_mhz and f.status not in (FlightStatus.DONE, FlightStatus.CANCELLED)
    ]
    return taken


def _station_until(state: CampaignState, fid: str, task: Task, tot: float) -> float:
    """End of the station time of an orbiting flight (0 for others)."""
    from . import air

    if task is Task.CAP:
        return tot + CAP_STATION_S + (TANKER_CAP_BONUS_S if air.tanker_on_station(state, fid) else 0.0)
    if task in (Task.AWACS, Task.TANKER):
        return tot + SUPPORT_STATION_S
    return 0.0


def _rotate(v: Vec2, a: float) -> Vec2:
    """Rotate a vector by ``a`` radians."""
    import math

    return Vec2(v.x * math.cos(a) - v.y * math.sin(a), v.x * math.sin(a) + v.y * math.cos(a))


def _add_flight(state: CampaignState, pkg: Package, sq: Squadron, task: Task, size: int, tot: float, r: Request) -> Flight:
    """Create a flight for a package and commit its aircraft."""
    base = state.airbases[sq.airbase]
    home = base.position
    target = r.point
    if task.orbits or task is Task.TRANSPORT:
        waypoints, target_index = [home, target, home], 1
    else:
        leg = home - target
        if leg.length() > MIN_ATTACK_LEG_M:
            unit = leg * (1.0 / leg.length())
            # IP and egress scale down for targets close to the base.
            ip = target + unit * min(IP_DISTANCE_M, leg.length() * 0.5)
            # Egress off the attack axis (not back over the same defences), then home.
            egress = target + _rotate(unit, EGRESS_TURN_RAD) * min(EGRESS_DISTANCE_M, leg.length() * 0.5)
            waypoints, target_index = [home, ip, target, egress, home], 2
        else:
            waypoints, target_index = [home, target, home], 1
    to_target = sum(waypoints[i].distance_to(waypoints[i + 1]) for i in range(target_index))
    speed = sq.aircraft_type.cruise_kmh / 3.6
    flight = Flight(
        id=state.new_id("fl"),
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
    flight.vhf_mhz = radio.allocate(flight.id, radio.FLIGHT_BAND, _taken_frequencies(state))
    sq.committed += size
    state.sortie_log.setdefault(sq.faction, []).append((state.time, size))
    state.sortie_tokens[sq.faction] = state.sortie_tokens.get(sq.faction, 0.0) - size
    state.flights[flight.id] = flight
    pkg.flights.append(flight.id)
    return flight


# ---------------------------------------------------------------- players


def join(
    state: CampaignState, player_id: str, name: str, side: str, flight_id: str, capable: frozenset[Task] | None
) -> tuple[bool, str]:
    """A player asks for a seat.

    Args:
        state: The campaign.
        player_id: Player id.
        name: Player name.
        side: Player's game side.
        flight_id: Requested flight.
        capable: Tasks the player's aircraft can fly (None = no restriction).

    Returns:
        (accepted, message for the player).
    """
    f = state.flights.get(flight_id)
    if f is None or f.status in (FlightStatus.DONE, FlightStatus.CANCELLED):
        return False, "That flight no longer exists."
    if state.factions[f.faction].side != side:
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
    state.emit("seat_taken", player=player_id, flight=f.id, callsign=f.callsign, package=f.package)
    return True, briefing_text(state, f)


def seat_of(state: CampaignState, player_id: str) -> tuple[Flight, Seat] | None:
    """The flight and seat a player holds, or None."""
    fid = state.player_flights.get(player_id)
    f = state.flights.get(fid) if fid else None
    if f is None or player_id not in f.players:
        return None
    return f, f.players[player_id]


def leave(state: CampaignState, player_id: str) -> bool:
    """Release a player's seat. Returns True if they had one."""
    found = seat_of(state, player_id)
    state.player_flights.pop(player_id, None)
    if found is None:
        return False
    f, seat = found
    if seat.state in (SeatState.ASSIGNED, SeatState.AIRBORNE):
        seat.state = SeatState.LEFT
        state.emit("seat_released", player=player_id, flight=f.id)
    return True


def player_took_off(state: CampaignState, player_id: str) -> None:
    """Mark a player's seat airborne."""
    found = seat_of(state, player_id)
    if found and found[1].state is SeatState.ASSIGNED:
        found[1].state = SeatState.AIRBORNE
        state.emit("player_takeoff", player=player_id, flight=found[0].id)


def player_landed(state: CampaignState, player_id: str) -> None:
    """Mark a player's seat landed and release the assignment."""
    found = seat_of(state, player_id)
    if found and found[1].state is SeatState.AIRBORNE:
        found[1].state = SeatState.LANDED
        state.player_flights.pop(player_id, None)
        state.emit("player_landed", player=player_id, flight=found[0].id)


def player_lost(state: CampaignState, player_id: str) -> None:
    """A player's aircraft was lost: count it against the squadron."""
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
    state.emit("aircraft_lost", flight=f.id, faction=f.faction, count=1, cause="player", player=player_id)


def player_disconnected(state: CampaignState, player_id: str) -> None:
    """A pilot vanished: unflown seats are freed, airborne ones are assumed home."""
    found = seat_of(state, player_id)
    if not found:
        return
    if found[1].state is SeatState.AIRBORNE:
        player_landed(state, player_id)
    else:
        leave(state, player_id)


# ---------------------------------------------------------------- presentation


def briefing_text(state: CampaignState, f: Flight) -> str:
    """Briefing of a flight for its players (see :mod:`dynacamp.systems.briefing`)."""
    from . import briefing

    return briefing.render(briefing.briefing(state, f), now=state.time)


def waypoint_marks(state: CampaignState, f: Flight) -> list[tuple[str, Vec2]]:
    """Map marks for a flight's named waypoints: (label, position)."""
    from . import briefing

    return briefing.waypoint_marks(state, f)


def ato_board(state: CampaignState, side: str, limit: int = 8) -> list[dict]:
    """What players of ``side`` can join, for in-game menus."""
    board = []
    for pkg in sorted(state.packages.values(), key=lambda p: (p.tot, p.id)):
        if pkg.done or state.factions[pkg.faction].side != side:
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
