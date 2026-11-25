"""Flight operations: takeoff, navigation, fuel, abstract attacks and air-to-air.

Only the AI-flown part of flights that the campaign owns is simulated here.
Inside a player bubble the game flies them (and players fly their own seats);
their effects then come back as ordinary losses on the delegated targets.

**Fuel and weapons.** At takeoff a flight gets its range (``FUEL_FACTOR`` x
the combat radius, more with a tanker on station) and the air-to-air
missiles of its loadout (:func:`dynacamp.loadouts.air_to_air`). Flying burns
range (orbiting at ``ORBIT_FUEL`` of the cruise rate); a flight that can just
make it home with its reserve goes home (*bingo fuel*), and a fighter that
fired all its missiles too (*winchester*).

**Air-to-air.** A fighter (CAP, escort, scramble) that sees an enemy aircraft
in its radar cone (:mod:`dynacamp.sensors`), or is pointed at one by its AWACS
within ``CUE_RANGE_M``, *must* engage it unless it is winchester, bingo or
going home: it leaves its station or route, flies at the target and fires its
radar missiles from their launch range, then heat seekers (or the gun) in the
merge within ``WVR_RANGE_M``, up to ``SALVO_MAX`` missiles a salvo: one pass,
at most a radar salvo and a salvo in the merge, then it breaks off and leaves that
target alone for ``REATTACK_S`` (fights are passes, not duels to the last
missile). Each missile
kills with a probability scaled by the two types' air-to-air quality (and,
for radar missiles, the target's jamming: :mod:`dynacamp.sensors`). A fighter
under fire turns on its attacker; a striker hit or outgunned turns back; a
fighter that lost half its flight goes home. The chase ends when the target is
gone or lost, goes under a known SAM umbrella or drags an orbiting fighter too
far from its station; the fighter then resumes its station or route.
"""

from __future__ import annotations

import math

from ..air import ACTIVE_STATUSES, Flight, FlightStatus, SeatState, Task
from ..forces import Authority, Convoy, Division
from ..geo import Vec2
from ..state import CampaignState
from ..events import EventKind

LATE_JOIN_S = 10 * 60.0
AD_RADIUS_M = 15_000.0
TARGET_SEARCH_M = 20_000.0
PLAYER_FLIGHT_TIMEOUT_S = 3 * 3600.0
TURNAROUND_S = 3 * 3600.0
ARCHIVE_MAX = 2000  # completed packages kept for the event log (see CampaignState.archive)
REPLACEMENT_INTERVAL_S = 8 * 3600.0  # one new airframe per squadron this often, up to establishment
CAP_ORBIT_RADIUS_M = 15_000.0
SUPPORT_ORBIT_RADIUS_M = 30_000.0
AWACS_SUPPORT_RANGE_M = 350_000.0  # fighters under AWACS control fight better
AWACS_A2A_BONUS = 1.15  # missiles fired under AWACS control hit more often
# Air-to-air
CUE_RANGE_M = 50_000.0  # an AWACS sends its fighters at contacts this close to them
WVR_RANGE_M = 10_000.0  # the merge: heat seekers and guns
SHOT_INTERVAL_S = 60.0  # a salvo per flight this often
PK_BVR = 0.07  # kill probability of a radar missile between equal fighters (combat record: 0.1-0.3)
PK_WVR = 0.12  # ...of a heat seeker
PK_GUN = 0.03  # ...of a gun pass (fighters only; unlimited)
GUN_A2A = 0.5  # types this good in air combat have a usable gun
CHASE_LIMIT_M = 100_000.0  # an orbiting fighter gives up a chase this far from its station
REATTACK_S = 30 * 60.0  # after its pass at a target a fighter leaves it alone this long
SALVO_MAX = 2  # missiles a flight fires at one target in one salvo
# Fuel
FUEL_FACTOR = 2.2  # range at takeoff = this x combat radius (the planners use 2x)
ORBIT_FUEL = 0.6  # fuel burnt on station per metre of cruise
BINGO_MARGIN = 1.1  # home is this much further than the straight line...
BINGO_RESERVE_M = 20_000.0  # ...plus this reserve
GROUND_STRENGTH_PER_EFF = 0.006
GROUND_ORG_PER_EFF = 0.03


def run(state: CampaignState, dt: float) -> None:
    """Advance every flight; air combat; cleanup; airframe replacements."""
    for f in list(state.flights.values()):
        if f.status in (FlightStatus.DONE, FlightStatus.CANCELLED):
            continue
        if f.joinable and state.time > f.takeoff_time + LATE_JOIN_S:
            f.joinable = False
        if f.status is FlightStatus.SCHEDULED:
            if state.time < f.takeoff_time:
                continue
            _takeoff(state, f)
            if f.status is not FlightStatus.AIRBORNE:
                continue
        if f.simulated_by_campaign:
            if f.ai_count > 0:
                _fly(state, f, dt)
            else:
                f.ai_done = True
        _check_complete(state, f)

    _air_combat(state)
    _cleanup(state)
    _replacements(state)


def _replacements(state: CampaignState) -> None:
    """Every ``REPLACEMENT_INTERVAL_S`` give each understrength squadron one airframe."""
    if state.time < state.timers.get("air_replacements", 0.0):
        return
    first = "air_replacements" not in state.timers
    state.timers["air_replacements"] = state.time + REPLACEMENT_INTERVAL_S
    if first:
        return
    for sq in state.squadrons.values():
        base = state.airbases[sq.airbase]
        if sq.aircraft < sq.establishment and base.owner(state.theater) == sq.faction:
            sq.aircraft += 1
            state.emit(EventKind.AIRCRAFT_DELIVERED, squadron=sq.id, faction=sq.faction, aircraft=sq.aircraft)


# ---------------------------------------------------------------- helpers


def air_defense(state: CampaignState, fid: str, point: Vec2, radius: float = AD_RADIUS_M) -> float:
    """Short-range air defence an attacker of ``fid`` meets around ``point``."""
    t = state.theater
    total = 0.0
    for d in state.divisions.values():
        if state.at_war(fid, d.faction) and d.position(t).distance_to(point) <= radius:
            total += d.elements.get("sam_short", 0.0) + 0.5 * d.elements.get("aaa", 0.0)
    return total


def lose_aircraft(state: CampaignState, f: Flight, n: int, cause: str) -> None:
    """Remove AI aircraft from a flight (and from its squadron).

    Args:
        state: The campaign.
        f: The flight.
        n: Aircraft lost (capped to the AI part).
        cause: Reason recorded in the event.
    """
    n = min(n, f.ai_count)
    if n <= 0:
        return
    before = f.ai_count
    if before > 0 and (f.aam_bvr > 0 or f.aam_wvr > 0):  # the missiles go down with the aircraft
        keep = (before - n) / before
        f.aam_bvr, f.aam_wvr = int(round(max(f.aam_bvr, 0) * keep)), int(round(f.aam_wvr * keep))
    f.size -= n
    sq = state.squadrons[f.squadron]
    sq.aircraft -= n
    sq.committed -= n
    state.emit(EventKind.AIRCRAFT_LOST, flight=f.id, faction=f.faction, count=n, cause=cause)
    if f.ai_count == 0:
        f.ai_done = True


def _takeoff(state: CampaignState, f: Flight) -> None:
    """Launch a scheduled flight (or cancel it if its base fell or it has no crews)."""
    sq = state.squadrons[f.squadron]
    base = state.airbases[sq.airbase]
    if base.owner(state.theater) != f.faction:
        _cancel(state, f, "airbase lost")
        return
    if f.ai_count == 0 and not f.active_seats:
        _cancel(state, f, "no crews")
        return
    f.status = FlightStatus.AIRBORNE
    if f.task is Task.TRANSPORT:
        _load_cargo(state, f, base)
    if f.ai_count == 0:
        f.ai_done = True  # flown entirely by players
    state.emit(EventKind.FLIGHT_TAKEOFF, flight=f.id, callsign=f.callsign, faction=f.faction, task=f.task.value, ai=f.ai_count, players=len(f.active_seats))


def _load_cargo(state: CampaignState, f: Flight, base) -> None:
    """Load a transport at its home base: from the base's depot, or freely at a
    rear base outside the map (the strategic supply chain)."""
    want = f.aircraft_type.cargo * f.size
    p = state.theater.provinces.get(base.province) if base.province else None
    if p is None:
        f.cargo = want
    elif p.depot is not None and p.controller == f.faction:
        f.cargo = min(want, p.depot.stock * 0.25)
        p.depot.stock -= f.cargo
    else:
        f.cargo = 0.0


def _cancel(state: CampaignState, f: Flight, reason: str) -> None:
    """Cancel a flight and release its aircraft and seats."""
    f.status = FlightStatus.CANCELLED
    state.squadrons[f.squadron].committed -= f.size
    for pid, seat in f.players.items():
        if seat.state in (SeatState.ASSIGNED, SeatState.AIRBORNE):
            seat.state = SeatState.LEFT
            state.player_flights.pop(pid, None)
    state.emit(EventKind.FLIGHT_CANCELLED, flight=f.id, callsign=f.callsign, reason=reason)


def _target_entity(state: CampaignState, f: Flight) -> Division | Convoy | None:
    """The division or convoy a BAI flight is after, if it still exists."""
    if f.target_kind == "division":
        return state.divisions.get(f.target_id)
    if f.target_kind == "convoy":
        return state.convoys.get(f.target_id)
    return None


# ---------------------------------------------------------------- flying


def ready(state: CampaignState, f: Flight) -> None:
    """Fuel and arm a flight that has not been yet (see the module docstring).

    Support aircraft (AWACS, tankers, transports) are not fuel-limited: their
    stations are planned within their endurance.
    """
    if f.fuel_m < 0:
        if f.task.support:
            f.fuel_m = math.inf
        else:
            from .ato import TANKER_RADIUS_FACTOR

            stretch = TANKER_RADIUS_FACTOR if not f.aircraft_type.helo and tanker_on_station(state, f.faction) else 1.0
            f.fuel_m = FUEL_FACTOR * f.aircraft_type.radius_km * 1000.0 * stretch
    if f.aam_bvr < 0:
        from .. import loadouts

        load = loadouts.air_to_air(f.aircraft_type.id, f.task.value)
        n = max(f.ai_count, 0)
        f.aam_bvr, f.aam_wvr, f.aam_range_m = load.bvr * n, load.wvr * n, load.bvr_range_m
        f.aam_loaded = f.aam_bvr + f.aam_wvr


def winchester(f: Flight) -> bool:
    """True if a flight that took off with missiles has fired them all."""
    return f.aam_loaded > 0 and f.aam_bvr <= 0 and f.aam_wvr <= 0


def go_home(state: CampaignState, f: Flight, reason: str) -> None:
    """Break off and fly straight home (bingo fuel, winchester...)."""
    if f.status is FlightStatus.RTB:
        return
    f.status = FlightStatus.RTB
    f.wp_index = len(f.waypoints) - 1
    f.engaging = ""
    if f.task.orbits:
        f.station_until = min(f.station_until, state.time)
    state.emit(EventKind.FLIGHT_RTB, flight=f.id, callsign=f.callsign, faction=f.faction, reason=reason)


def _burn(f: Flight, metres: float) -> None:
    """Fuel for ``metres`` of flight (nothing for untracked flights)."""
    if not math.isinf(f.fuel_m):
        f.fuel_m = max(0.0, f.fuel_m - metres)


def _bingo(state: CampaignState, f: Flight) -> None:
    """Go home when the fuel left only just covers the way back."""
    if f.status is FlightStatus.RTB or math.isinf(f.fuel_m) or not f.waypoints:
        return
    if f.fuel_m <= f.pos.distance_to(f.waypoints[-1]) * BINGO_MARGIN + BINGO_RESERVE_M:
        go_home(state, f, "bingo fuel")


def disengage(state: CampaignState, f: Flight) -> None:
    """End a chase: back to the station (orbiting flights with time left) or
    on along the route."""
    f.engaging = ""
    if f.status is FlightStatus.RTB:
        return
    if f.task.orbits:
        if state.time < f.station_until:
            f.status = FlightStatus.AIRBORNE
            f.wp_index = f.target_index
        else:
            go_home(state, f, "end of station")


def _chase(state: CampaignState, f: Flight, dt: float) -> bool:
    """Fly at the flight's air-to-air target. False if there is nothing to chase."""
    tgt = state.flights.get(f.engaging)
    if tgt is None or tgt.status not in ACTIVE_STATUSES or tgt.ai_count + len(tgt.active_seats) <= 0:
        disengage(state, f)
        return False
    if f.status is FlightStatus.ON_STATION:
        f.status = FlightStatus.AIRBORNE  # leaves the orbit
    where = tgt.position(state.theater)
    d = f.pos.distance_to(where)
    step = f.speed_ms * dt
    if d > 1.0:
        f.track = f.pos.heading_to(where)
        f.pos = f.pos.lerp(where, min(1.0, step / d))
    _burn(f, min(step, d))
    return True


def _fly(state: CampaignState, f: Flight, dt: float) -> None:
    """Move the AI part along its waypoints; attack, orbit or land on arrival."""
    if f.fuel_m < 0 or f.aam_bvr < 0:
        ready(state, f)
    if f.target_kind == "flight":  # scrambled interceptors chase their intruder
        from . import scramble

        scramble.pursue(state, f)
    if f.engaging and f.status is not FlightStatus.RTB and _chase(state, f, dt):
        _bingo(state, f)
        return
    if f.status is FlightStatus.ON_STATION:
        if state.time < f.station_until:
            # Fly the orbit around the station point (wider for AWACS and tankers).
            radius = CAP_ORBIT_RADIUS_M if f.task is Task.CAP else SUPPORT_ORBIT_RADIUS_M
            f.orbit_angle = (f.orbit_angle + f.speed_ms * 0.6 * dt / radius) % (2 * math.pi)
            c = f.target_point
            f.pos = Vec2(c.x + radius * math.sin(f.orbit_angle), c.y + radius * math.cos(f.orbit_angle))
            _burn(f, f.speed_ms * dt * ORBIT_FUEL)
            _bingo(state, f)
            return
        f.status = FlightStatus.RTB
        f.wp_index = f.target_index + 1
    remaining = f.speed_ms * dt
    while remaining > 0 and f.wp_index < len(f.waypoints):
        if f.wp_index == f.target_index:
            tgt = _target_entity(state, f)
            if tgt is not None:  # armed reconnaissance: follow the column
                f.waypoints[f.target_index] = tgt.position(state.theater)
        wp = f.waypoints[f.wp_index]
        d = f.pos.distance_to(wp)
        if d > 0.5:
            f.track = f.pos.heading_to(wp)
        if d > remaining:
            f.pos = f.pos.lerp(wp, remaining / d)
            _burn(f, f.speed_ms * dt)
            _bingo(state, f)
            return
        f.pos = wp
        remaining -= d
        reached = f.wp_index
        f.wp_index += 1
        if reached == f.target_index:
            if f.task.orbits:
                f.status = FlightStatus.ON_STATION
                f.wp_index = reached  # stays on the station point
                return
            if f.task is not Task.ESCORT:
                attack(state, f)
            f.attacked = True
            f.status = FlightStatus.RTB
    _burn(f, f.speed_ms * dt - remaining)
    if f.wp_index >= len(f.waypoints):
        f.ai_done = True
        state.emit(EventKind.FLIGHT_LANDED, flight=f.id, callsign=f.callsign, aircraft=f.ai_count)
    else:
        _bingo(state, f)


def _check_complete(state: CampaignState, f: Flight) -> None:
    """Close a flight whose AI part and players are done (or timed out)."""
    if state.time > max(f.tot, f.station_until) + PLAYER_FLIGHT_TIMEOUT_S:
        for pid, seat in f.players.items():
            if seat.state is SeatState.ASSIGNED:
                seat.state = SeatState.LEFT
                state.player_flights.pop(pid, None)
            elif seat.state is SeatState.AIRBORNE:
                seat.state = SeatState.LANDED
                state.player_flights.pop(pid, None)
        if f.status not in (FlightStatus.DONE, FlightStatus.CANCELLED):
            f.ai_done = True
    if not f.ai_done or f.active_seats or not f.simulated_by_campaign:
        return
    f.status = FlightStatus.DONE
    state.squadrons[f.squadron].release(f.size, state.time + TURNAROUND_S)
    state.emit(EventKind.FLIGHT_COMPLETE, flight=f.id, callsign=f.callsign, faction=f.faction, survivors=f.size)


# ---------------------------------------------------------------- attacks


def deliver(state: CampaignState, f: Flight) -> None:
    """A transport flight unloads its cargo: into a depot (airlift) or to the
    brigades near the landing zone (helicopter resupply)."""
    t = state.theater
    cargo = f.cargo * (f.ai_count / f.size if f.size else 0.0)
    f.cargo = 0.0
    delivered = 0.0
    if f.target_kind == "airlift" and f.target_id in state.airbases:
        base = state.airbases[f.target_id]
        p = t.provinces.get(base.province) if base.province else None
        if p is not None and p.depot is not None and p.controller == f.faction:
            delivered = min(cargo, p.depot.capacity - p.depot.stock)
            p.depot.stock += delivered
    elif f.target_kind == "resupply":
        divs = [d for d in state.divisions.values() if d.faction == f.faction and d.position(t).distance_to(f.target_point) <= 15_000]
        needs = {d.id: d.template.supply_capacity - d.supply for d in divs}
        total = sum(needs.values())
        if total > 0:
            share = min(1.0, cargo / total)
            for d in divs:
                d.supply += needs[d.id] * share
            delivered = min(cargo, total)
    state.emit(EventKind.AIRLIFT_DELIVERED, flight=f.id, callsign=f.callsign, faction=f.faction, delivery=f.target_kind, target=f.target_id, amount=round(delivered, 1))


def awacs_on_station(state: CampaignState, faction: str, near: Vec2 | None = None, reach: float = AWACS_SUPPORT_RANGE_M) -> Flight | None:
    """An AWACS of ``faction`` holding its orbit (within ``reach`` of ``near`` if given)."""
    for f in state.flights.values():
        if f.faction == faction and f.task is Task.AWACS and f.status is FlightStatus.ON_STATION and f.ai_count + len(f.active_seats) > 0:
            if near is None or f.pos.distance_to(near) <= reach:
                return f
    return None


def tanker_on_station(state: CampaignState, faction: str) -> Flight | None:
    """A tanker of ``faction`` on its track."""
    for f in state.flights.values():
        if f.faction == faction and f.task is Task.TANKER and f.status is FlightStatus.ON_STATION:
            return f
    return None


def attack(state: CampaignState, f: Flight) -> None:
    """Abstract attack run by the AI part of ``f`` at its target point."""
    if f.task is Task.TRANSPORT:
        deliver(state, f)
        return
    if f.task.support:
        return
    t = state.theater
    point = f.target_point
    pkg = state.packages.get(f.package)
    sead_cover = pkg is not None and any(
        o.task is Task.SEAD and o.attacked and o.ai_count > 0
        for o in (state.flights[i] for i in pkg.flights if i in state.flights and i != f.id)
    )
    ad = air_defense(state, f.faction, point)
    p_loss = (0.01 + ad * 0.004) * (0.4 if sead_cover else 1.0) * (1.5 if f.aircraft_type.helo else 1.0)
    if f.task is Task.SEAD:
        p_loss *= 0.5
    p_loss = min(0.5, p_loss)
    lost = sum(1 for _ in range(f.ai_count) if state.rng.random() < p_loss)
    lose_aircraft(state, f, lost, "air defence")
    eff = f.ai_count * f.aircraft_type.a2g
    result: dict[str, object] = {"lost": lost}
    if eff <= 0:
        state.emit(EventKind.AIR_STRIKE, flight=f.id, faction=f.faction, task=f.task.value, **result)
        return

    def campaign_owned(e) -> bool:
        """True if the campaign (not the game) simulates ``e``."""
        return e.authority is Authority.CAMPAIGN

    if f.target_kind == "sam":
        from . import air_defense as ada

        site = state.air_defense.get(f.target_id)
        if site is None or site.position(t).distance_to(point) > TARGET_SEARCH_M:
            near = [u for u in state.air_defense.values() if state.at_war(f.faction, u.faction) and u.position(t).distance_to(point) <= TARGET_SEARCH_M]
            site = min(near, key=lambda u: u.position(t).distance_to(point)) if near else None
        if site is not None and site.authority is Authority.CAMPAIGN:
            # SEAD shoots anti-radiation missiles at a live radar; strikers (DEAD)
            # bomb the site, best once the SEAD ahead of them has put it down.
            if site.emitting and f.task is Task.SEAD:
                ada.hit_on_radar(state, site, eff, by=f.callsign)
            else:
                ada.strike_site(state, site, eff, by=f.callsign)
            result["sam"] = site.id
    elif f.task is Task.SEAD:
        # SEAD escort: anti-radiation missiles at SAM radars covering the package...
        from . import air_defense as ada

        for site in [u for u in state.air_defense.values() if state.at_war(f.faction, u.faction) and u.emitting
                     and u.authority is Authority.CAMPAIGN and u.position(t).distance_to(point) <= u.system.range_m][:2]:
            ada.hit_on_radar(state, site, eff / 2, by=f.callsign)
        # ...and suppression of the brigades' short-range air defence.
        hit = 0
        for d in state.divisions.values():
            if state.at_war(f.faction, d.faction) and campaign_owned(d) and d.position(t).distance_to(point) <= AD_RADIUS_M:
                for k in ("sam_short", "aaa"):
                    if k in d.elements:
                        d.elements[k] *= 1.0 - min(0.8, 0.15 * eff)
                hit += 1
        result["suppressed"] = hit
    elif f.target_kind == "depot":
        p = t.provinces.get(f.target_id)
        if p is not None and p.depot is not None and state.at_war(f.faction, p.controller):
            p.depot.stock = max(0.0, p.depot.stock - 150.0 * eff)
            p.depot.trucks = max(0, p.depot.trucks - int(2 * eff))
            result["depot"] = p.id
    elif f.target_kind == "convoy" or (f.target_kind == "division" and f.target_id in state.convoys):
        convoy = _nearest_enemy(state, f, state.convoys.values(), f.target_id)
        if convoy is not None and campaign_owned(convoy):
            killed = convoy.trucks * min(0.9, 0.3 * eff)
            convoy.cargo *= (convoy.trucks - killed) / convoy.trucks
            convoy.trucks -= killed
            result["convoy"] = convoy.id
            result["trucks_destroyed"] = round(killed, 1)
            if convoy.trucks < 0.5:
                del state.convoys[convoy.id]
                state.emit(EventKind.CONVOY_DESTROYED, convoy=convoy.id, faction=convoy.faction, reason="air")
    else:
        if f.target_kind == "battle" and f.target_id in state.battles:
            b = state.battles[f.target_id]
            ids = b.defenders if b.attacker_faction == f.faction else b.attackers
            targets = [state.divisions[i] for i in ids if i in state.divisions]
        else:
            tgt = _nearest_enemy(state, f, state.divisions.values(), f.target_id)
            targets = [tgt] if tgt else []
        targets = [d for d in targets if campaign_owned(d)]
        for d in targets:
            share = eff / len(targets)
            d.take_losses(GROUND_STRENGTH_PER_EFF * share)
            d.org = max(0.0, d.org - GROUND_ORG_PER_EFF * share)
        result["divisions"] = [d.id for d in targets]
    state.emit(EventKind.AIR_STRIKE, flight=f.id, faction=f.faction, task=f.task.value, **result)


def _nearest_enemy(state: CampaignState, f: Flight, pool, preferred: str):
    """The preferred target if close to the target point, else the nearest enemy.

    Args:
        state: The campaign.
        f: The attacking flight.
        pool: Candidate entities.
        preferred: Id of the planned target.
    """
    t = state.theater
    candidates = [e for e in pool if state.at_war(f.faction, e.faction)]
    for e in candidates:
        if e.id == preferred and e.position(t).distance_to(f.target_point) <= TARGET_SEARCH_M:
            return e
    near = [e for e in candidates if e.position(t).distance_to(f.target_point) <= TARGET_SEARCH_M]
    return min(near, key=lambda e: e.position(t).distance_to(f.target_point)) if near else None


# ---------------------------------------------------------------- air to air


def _hunter(f: Flight) -> bool:
    """A fighter that must engage what it sees: air-to-air task, AI-flown by the
    campaign, airborne and not going home, with missiles (or only a gun)."""
    return (
        f.task.air_to_air
        and f.status in (FlightStatus.AIRBORNE, FlightStatus.ON_STATION)
        and f.simulated_by_campaign
        and f.ai_count > 0
        and not winchester(f)
    )


def _quality(shooter: Flight, target: Flight) -> float:
    """Kill probability multiplier from the two types' air-to-air quality (1 between equals)."""
    a, b = shooter.aircraft_type.a2a + 0.2, target.aircraft_type.a2a + 0.2
    return min(1.6, max(0.4, 2.0 * a / (a + b)))


def _air_combat(state: CampaignState) -> None:
    """Fighters commit on what they see and fire at it (see the module docstring)."""
    from .. import sensors
    from . import routing

    t = state.theater
    flying = [
        f for f in state.flights.values()
        if f.status in ACTIVE_STATUSES and f.simulated_by_campaign and f.ai_count > 0
    ]
    if not flying:
        return
    pos = {f.id: f.position(t) for f in flying}
    awacs = [
        a for a in state.flights.values()
        if a.task is Task.AWACS and a.status is FlightStatus.ON_STATION and a.size > 0
    ]
    umbrellas: dict[str, list] = {}
    jams = {f.id: sensors.jam(state, f) for f in flying}

    def seen_by(f: Flight, tgt: Flight) -> bool:
        """``f`` has ``tgt`` on its radar, or its AWACS points it out within ``CUE_RANGE_M``."""
        if sensors.radar_sees(f, pos[f.id], tgt, pos[tgt.id], track_only=True, jamming=jams[tgt.id]):
            return True
        return pos[f.id].distance_to(pos[tgt.id]) <= CUE_RANGE_M and any(
            a.faction == f.faction and sensors.radar_sees(a, a.position(t), tgt, pos[tgt.id], jamming=jams[tgt.id])
            for a in awacs
        )

    def covered(f: Flight, where: Vec2) -> bool:
        """``where`` is under an enemy SAM umbrella ``f``'s side knows about."""
        if f.faction not in umbrellas:
            umbrellas[f.faction] = routing.threats(state, f.faction, sensors.flight_altitude(f))
        return any(u.contains(where) for u in umbrellas[f.faction])

    # 1. commit: every free fighter takes on the nearest enemy it sees
    for f in flying:
        if f.engaging or not _hunter(f):
            continue
        seen = [
            x for x in flying
            if state.at_war(f.faction, x.faction) and seen_by(f, x) and not covered(f, pos[x.id])
            and state.timers.get(f"aa_pass:{f.id}:{x.id}", 0.0) <= state.time
        ]
        if seen:
            f.engaging = min(seen, key=lambda x: (pos[f.id].distance_to(pos[x.id]), x.id)).id

    # 2. chases that end, and shots
    for f in sorted(flying, key=lambda x: x.id):
        if not f.engaging or f.ai_count <= 0:
            continue
        tgt = state.flights.get(f.engaging)
        if tgt is None or tgt.id not in pos or tgt.ai_count <= 0 or not _hunter(f):
            disengage(state, f)
            continue
        d = pos[f.id].distance_to(pos[tgt.id])
        lost = d > sensors.reach(f.aircraft_type.air_radar_m, jams[tgt.id]) * 1.2 or covered(f, pos[tgt.id])
        if lost or (f.task.orbits and f.pos.distance_to(f.target_point) > CHASE_LIMIT_M):
            disengage(state, f)
            continue
        if state.timers.get(f"aa:{f.id}", 0.0) > state.time:
            continue
        _salvo(state, f, tgt, d)

    for f in flying:
        if f.task.air_to_air and winchester(f) and f.status is not FlightStatus.RTB and f.ai_count > 0:
            go_home(state, f, "winchester")


def _salvo(state: CampaignState, f: Flight, tgt: Flight, d: float) -> None:
    """``f`` fires at ``tgt`` if in range: radar missiles out to their launch
    range, heat seekers or the gun in the merge. In the merge a target with
    heat seekers fires back."""
    if d <= WVR_RANGE_M:
        phase = "WVR"
    elif f.aam_bvr > 0 and d <= f.aam_range_m and state.timers.get(f"aa_pass:{f.id}:{tgt.id}:bvr", 0.0) <= state.time:
        phase = "BVR"
        state.timers[f"aa_pass:{f.id}:{tgt.id}:bvr"] = state.time + REATTACK_S  # one radar salvo per pass
    else:
        return  # still closing in
    state.timers[f"aa:{f.id}"] = state.time + SHOT_INTERVAL_S
    bonus = AWACS_A2A_BONUS if awacs_on_station(state, f.faction, f.pos) else 1.0
    kills, shots = _fire(state, f, tgt, phase, bonus)
    if phase == "WVR":  # end of the pass: the merge
        state.timers[f"aa_pass:{f.id}:{tgt.id}"] = state.time + REATTACK_S
        f.engaging = "" if f.engaging == tgt.id else f.engaging
    back_kills = back_shots = 0
    if phase == "WVR" and tgt.ai_count > 0 and not tgt.task.air_to_air and tgt.aam_wvr > 0:
        back_kills, back_shots = _fire(state, tgt, f, "WVR", 1.0)  # a striker defends itself
    if not shots and not back_shots:
        return
    state.emit(
        EventKind.AIR_ENGAGEMENT, flights=[f.id, tgt.id], callsigns=[f.callsign, tgt.callsign],
        losses=[back_kills, kills], phase=phase, shots=[shots, back_shots],
    )
    lose_aircraft(state, tgt, kills, f"air combat with {f.callsign}")
    lose_aircraft(state, f, back_kills, f"air combat with {tgt.callsign}")
    # the target reacts: a fighter turns on its attacker, a striker hit or outgunned turns back
    if tgt.ai_count > 0 and not tgt.engaging and _hunter(tgt):
        tgt.engaging = f.id
    for x, lost, own, other in ((tgt, kills, tgt.ai_count, f.ai_count), (f, back_kills, f.ai_count, tgt.ai_count)):
        if x.ai_count <= 0 or x.status is FlightStatus.RTB:
            continue
        if not x.task.air_to_air and not x.attacked and (lost or other > own):
            x.status = FlightStatus.RTB
            x.wp_index = len(x.waypoints) - 1
            state.emit(EventKind.FLIGHT_ABORTED, flight=x.id, callsign=x.callsign, reason="intercepted")
        elif x.task.air_to_air and lost * 2 >= lost + x.ai_count:
            go_home(state, x, "losses")
            state.emit(EventKind.FLIGHT_ABORTED, flight=x.id, callsign=x.callsign, reason="losses")
    if tgt.ai_count <= 0 or not f.engaging:
        disengage(state, f)


def _fire(state: CampaignState, f: Flight, tgt: Flight, phase: str, bonus: float) -> tuple[int, int]:
    """One salvo: up to ``SALVO_MAX`` missiles (a gun pass per aircraft).
    Returns (kills, rounds fired)."""
    n = min(f.ai_count, SALVO_MAX)
    if phase == "BVR":
        from .. import sensors

        shots = min(n, f.aam_bvr)
        f.aam_bvr -= shots
        pk = PK_BVR * sensors.jam_pk(sensors.jam(state, tgt), tgt)  # radar missiles hate jammers
    elif f.aam_wvr > 0:
        shots = min(n, f.aam_wvr)
        f.aam_wvr -= shots
        pk = PK_WVR
    elif f.aircraft_type.a2a >= GUN_A2A:
        shots, pk = f.ai_count, PK_GUN
    else:
        return 0, 0
    pk = min(0.9, pk * _quality(f, tgt) * bonus)
    kills = sum(1 for _ in range(shots) if state.rng.random() < pk)
    return min(kills, tgt.ai_count), shots


def _cleanup(state: CampaignState) -> None:
    """Drop expired cooldowns and completed packages."""
    for key in [k for k, v in state.timers.items() if k.startswith(("aa:", "aa_pass:", "sam:", "harm:", "evade:", "scramble:", "contested:", "meet:")) and v <= state.time]:
        del state.timers[key]
    for pkg in list(state.packages.values()):
        flights = [state.flights[i] for i in pkg.flights if i in state.flights]
        if all(f.status in (FlightStatus.DONE, FlightStatus.CANCELLED) for f in flights):
            for f in flights:
                if f.authority is not Authority.CAMPAIGN:
                    break  # still held by the game: wait for the handoff
            else:
                for f in flights:
                    state.archived_flights[f.id] = state.flights.pop(f.id)
                state.archive[pkg.id] = state.packages.pop(pkg.id)
                while len(state.archive) > ARCHIVE_MAX:
                    old = state.archive.pop(next(iter(state.archive)))
                    for fid in old.flights:
                        state.archived_flights.pop(fid, None)
                state.emit(
                    EventKind.PACKAGE_COMPLETE,
                    package=pkg.id,
                    number=pkg.number,
                    faction=pkg.faction,
                    task=pkg.task.value,
                    target=pkg.target_label
                )
