"""Flight operations: takeoff, navigation, abstract attacks and air-to-air.

Only the AI-flown part of flights that the campaign owns is simulated here.
Inside a player bubble the game flies them (and players fly their own seats);
their effects then come back as ordinary losses on the delegated targets.
"""

from __future__ import annotations

import math

from ..air import ACTIVE_STATUSES, Flight, FlightStatus, SeatState, Task
from ..forces import Authority, Convoy, Division
from ..geo import Vec2
from ..state import CampaignState

LATE_JOIN_S = 10 * 60.0
ENGAGE_RANGE_M = 40_000.0
CAP_ENGAGE_RANGE_M = 60_000.0
ENGAGEMENT_COOLDOWN_S = 15 * 60.0
AD_RADIUS_M = 15_000.0
TARGET_SEARCH_M = 20_000.0
PLAYER_FLIGHT_TIMEOUT_S = 3 * 3600.0
TURNAROUND_S = 3 * 3600.0
REPLACEMENT_INTERVAL_S = 8 * 3600.0  # one new airframe per squadron this often, up to establishment
CAP_ORBIT_RADIUS_M = 15_000.0
A2A_KILL = 0.1
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
            state.emit("aircraft_delivered", squadron=sq.id, faction=sq.faction, aircraft=sq.aircraft)


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
    f.size -= n
    sq = state.squadrons[f.squadron]
    sq.aircraft -= n
    sq.committed -= n
    state.emit("aircraft_lost", flight=f.id, faction=f.faction, count=n, cause=cause)
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
    if f.ai_count == 0:
        f.ai_done = True  # flown entirely by players
    state.emit("flight_takeoff", flight=f.id, callsign=f.callsign, faction=f.faction, task=f.task.value, ai=f.ai_count, players=len(f.active_seats))


def _cancel(state: CampaignState, f: Flight, reason: str) -> None:
    """Cancel a flight and release its aircraft and seats."""
    f.status = FlightStatus.CANCELLED
    state.squadrons[f.squadron].committed -= f.size
    for pid, seat in f.players.items():
        if seat.state in (SeatState.ASSIGNED, SeatState.AIRBORNE):
            seat.state = SeatState.LEFT
            state.player_flights.pop(pid, None)
    state.emit("flight_cancelled", flight=f.id, callsign=f.callsign, reason=reason)


def _target_entity(state: CampaignState, f: Flight) -> Division | Convoy | None:
    """The division or convoy a BAI flight is after, if it still exists."""
    if f.target_kind == "division":
        return state.divisions.get(f.target_id)
    if f.target_kind == "convoy":
        return state.convoys.get(f.target_id)
    return None


# ---------------------------------------------------------------- flying


def _fly(state: CampaignState, f: Flight, dt: float) -> None:
    """Move the AI part along its waypoints; attack, orbit or land on arrival."""
    if f.status is FlightStatus.ON_STATION:
        if state.time < f.station_until:
            # Fly the CAP orbit around the station point.
            f.orbit_angle = (f.orbit_angle + f.speed_ms * 0.6 * dt / CAP_ORBIT_RADIUS_M) % (2 * math.pi)
            c = f.target_point
            f.pos = Vec2(c.x + CAP_ORBIT_RADIUS_M * math.sin(f.orbit_angle), c.y + CAP_ORBIT_RADIUS_M * math.cos(f.orbit_angle))
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
        if d > remaining:
            f.pos = f.pos.lerp(wp, remaining / d)
            return
        f.pos = wp
        remaining -= d
        reached = f.wp_index
        f.wp_index += 1
        if reached == f.target_index:
            if f.task is Task.CAP:
                f.status = FlightStatus.ON_STATION
                f.wp_index = reached  # stays on the station point
                return
            if f.task is not Task.ESCORT:
                attack(state, f)
            f.attacked = True
            f.status = FlightStatus.RTB
    if f.wp_index >= len(f.waypoints):
        f.ai_done = True
        state.emit("flight_landed", flight=f.id, callsign=f.callsign, aircraft=f.ai_count)


def _check_complete(state: CampaignState, f: Flight) -> None:
    """Close a flight whose AI part and players are done (or timed out)."""
    if state.time > f.tot + PLAYER_FLIGHT_TIMEOUT_S:
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
    state.emit("flight_complete", flight=f.id, callsign=f.callsign, faction=f.faction, survivors=f.size)


# ---------------------------------------------------------------- attacks


def attack(state: CampaignState, f: Flight) -> None:
    """Abstract attack run by the AI part of ``f`` at its target point."""
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
        state.emit("air_strike", flight=f.id, faction=f.faction, task=f.task.value, **result)
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
            if site.emitting:
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
                state.emit("convoy_destroyed", convoy=convoy.id, faction=convoy.faction, reason="air")
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
    state.emit("air_strike", flight=f.id, faction=f.faction, task=f.task.value, **result)


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


def _air_combat(state: CampaignState) -> None:
    """Hostile flights that come close fight. A flight fights one engagement at a
    time; fighter-vs-fighter pairs are resolved first, so escorts tie up the
    interceptors and the strikers get through."""
    flying = [
        f for f in state.flights.values()
        if f.status in ACTIVE_STATUSES and f.simulated_by_campaign and f.ai_count > 0
    ]
    pairs = []
    for i, a in enumerate(flying):
        for b in flying[i + 1 :]:
            if not state.at_war(a.faction, b.faction):
                continue
            if not (a.task.air_to_air or b.task.air_to_air):
                continue  # two strike flights do not hunt each other
            both_fighters = a.task.air_to_air and b.task.air_to_air
            # A CAP reaches out further for intruders; fighters meet at shorter range.
            on_station = FlightStatus.ON_STATION in (a.status, b.status)
            reach = CAP_ENGAGE_RANGE_M if on_station and not both_fighters else ENGAGE_RANGE_M
            d = a.pos.distance_to(b.pos)
            if d <= reach:
                pairs.append((not both_fighters, d, a, b))
    pairs.sort(key=lambda p: (p[0], p[1], p[2].id, p[3].id))

    def busy(f: Flight) -> bool:
        """True if the flight is still in an engagement."""
        return state.timers.get(f"aa:{f.id}", 0.0) > state.time

    for _, _, a, b in pairs:
        if busy(a) or busy(b) or a.ai_count <= 0 or b.ai_count <= 0:
            continue
        state.timers[f"aa:{a.id}"] = state.timers[f"aa:{b.id}"] = state.time + ENGAGEMENT_COOLDOWN_S
        pa, pb = a.ai_count * a.aircraft_type.a2a, b.ai_count * b.aircraft_type.a2a
        la = sum(1 for _ in range(a.ai_count) if state.rng.random() < A2A_KILL * pb / (pa + pb))
        lb = sum(1 for _ in range(b.ai_count) if state.rng.random() < A2A_KILL * pa / (pa + pb))
        state.emit("air_engagement", flights=[a.id, b.id], callsigns=[a.callsign, b.callsign], losses=[la, lb])
        lose_aircraft(state, a, la, f"air combat with {b.callsign}")
        lose_aircraft(state, b, lb, f"air combat with {a.callsign}")
        for x, lost, own, other in ((a, la, pa, pb), (b, lb, pb, pa)):
            if x.ai_count <= 0 or x.status is FlightStatus.RTB:
                continue
            striker_turned_back = not x.task.air_to_air and not x.attacked and (lost or other > own)
            fighter_mauled = x.task.air_to_air and lost * 2 >= lost + x.ai_count
            if striker_turned_back or fighter_mauled:
                x.status = FlightStatus.RTB
                x.wp_index = len(x.waypoints) - 1
                state.emit("flight_aborted", flight=x.id, callsign=x.callsign, reason="intercepted" if striker_turned_back else "losses")


def _cleanup(state: CampaignState) -> None:
    """Drop expired cooldowns and completed packages."""
    for key in [k for k, v in state.timers.items() if k.startswith(("aa:", "sam:", "harm:")) and v <= state.time]:
        del state.timers[key]
    for pkg in list(state.packages.values()):
        flights = [state.flights[i] for i in pkg.flights if i in state.flights]
        if all(f.status in (FlightStatus.DONE, FlightStatus.CANCELLED) for f in flights):
            for f in flights:
                if f.authority is not Authority.CAMPAIGN:
                    break  # still held by the game: wait for the handoff
            else:
                for f in flights:
                    del state.flights[f.id]
                del state.packages[pkg.id]
                state.emit("package_complete", package=pkg.id, faction=pkg.faction, task=pkg.task.value, target=pkg.target_label)
