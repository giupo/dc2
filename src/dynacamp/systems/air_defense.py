"""Air defence: site states, the IADS, SAM engagements, SEAD and the ADA commander.

* **IADS.** A site within reach of a friendly early-warning radar is
  *networked*: it is cued by the EWR (it sees what the EWR sees), engages more
  effectively, and can keep its own radar dark until the last moment, which
  makes it much harder to suppress.
* **Engagements** (outside player bubbles): every active site with missiles
  fires salvos at the nearest hostile flight it detects within range and
  ceiling. The altitude is the flight's planned profile
  (:func:`dynacamp.air.flight_altitude`, the same the briefing gives and the
  AI flies in the game): short-range systems reach CAS/BAI jets on their
  attack run, helicopters always, strike and SEAD jets at medium altitude
  only if they reach that high.
* **SEAD.** SEAD flights fire anti-radiation missiles at emitting radars in
  reach: the radar is suppressed for a while or destroyed. Networked sites
  often just go dark in time.
* **ADA commander.** Every few hours each faction repositions its mobile
  systems along the roads: short-range ones go with the brigades at the front,
  medium and long-range ones cover depots, airbases and battles from one
  province behind the front. A site under ground threat, or one that was
  targeted by SEAD, moves ("shoot and scoot").
"""

from __future__ import annotations

from ..air import ACTIVE_STATUSES, Flight, FlightStatus, Task, flight_altitude, waypoint_altitudes
from .. import sensors
from ..air_defense import RADAR_ELEMENTS, AirDefenseUnit, SamRole, SamStatus, place_sites
from ..forces import Authority
from ..geo import Vec2
from ..state import CampaignState
from . import air, supply

SALVO = 2
ENGAGE_COOLDOWN_S = 3 * 60.0
NETWORK_RANGE_FACTOR = 0.5  # a site is networked within this share of an EWR's detection range
NETWORK_PK_BONUS = 1.15
SEAD_PK_FACTOR = 0.7  # SEAD aircraft fly defensively (ECM, low, notching)
# The package's SEAD flight jams for everyone near it (its ECM pods stand in for
# a dedicated jammer: DCS has no AI EA-6B or EA-18G).
DEFENSIVE_FACTOR = 0.5  # chaff, ECM and manoeuvring against every engagement
PAIR_COOLDOWN_S = 10 * 60.0  # one site engages the same flight at most this often
HARM_RANGE_M = 60_000.0
HARM_COOLDOWN_S = 15 * 60.0
SUPPRESS_S = 20 * 60.0
GO_DARK_CHANCE = 0.4  # networked site switches off before the HARM arrives
RELOAD_RANGE_M = 60_000.0  # road distance to a depot to reload missiles
REFORM_DELAY_S = 36 * 3600.0  # a destroyed battery is replaced this much later at the main depot
ADA_INTERVAL_S = 2 * 3600.0
# How much better a new position must be before a site packs up and moves:
# long-range systems take long to set up again, short-range ones are nimble.
MOVE_GAIN = {SamRole.LONG: 2.0, SamRole.MEDIUM: 1.6, SamRole.SHORT: 1.25}


def units(state: CampaignState, faction: str | None = None) -> list[AirDefenseUnit]:
    """Air defence units, optionally of one faction."""
    return [u for u in state.air_defense.values() if faction is None or u.faction == faction]


def run(state: CampaignState, dt: float) -> None:
    """Status, reloads, engagements, SEAD, losses; ADA planning every ``ADA_INTERVAL_S``."""
    _reform(state)
    _update_status(state)
    place_sites(state.air_defense.values(), state.theater)
    _reload(state, dt)
    _engage(state)
    _sead(state)
    _remove_destroyed(state)
    if "ada" not in state.timers:
        state.timers["ada"] = state.time + ADA_INTERVAL_S  # respect the scenario's initial deployment
    if state.time >= state.timers["ada"]:
        state.timers["ada"] = state.time + ADA_INTERVAL_S
        for fid in state.factions:
            plan(state, fid)


# ---------------------------------------------------------------- status & IADS


def _update_status(state: CampaignState) -> None:
    """Moving -> setting up -> active; suppression expiring."""
    for u in state.air_defense.values():
        if u.movement:
            u.status = SamStatus.MOVING
        elif u.status is SamStatus.MOVING:
            u.status = SamStatus.SETTING_UP
            u.ready_at = state.time + u.system.setup_s
        elif u.status in (SamStatus.SETTING_UP, SamStatus.SUPPRESSED) and state.time >= u.ready_at:
            u.status = SamStatus.ACTIVE
            u.scoot = False


def ewrs(state: CampaignState, faction: str) -> list[AirDefenseUnit]:
    """Operational, active early-warning radars of a faction."""
    return [
        u for u in units(state, faction)
        if u.system.role is SamRole.EWR and u.operational and u.status is SamStatus.ACTIVE
    ]


def network_node(state: CampaignState, u: AirDefenseUnit) -> AirDefenseUnit | None:
    """The EWR this site is networked to, if any."""
    if u.system.role is SamRole.EWR:
        return None
    here = u.position(state.theater)
    for e in ewrs(state, u.faction):
        if e.position(state.theater).distance_to(here) <= e.system.detection_m * NETWORK_RANGE_FACTOR:
            return e
    return None


def detects(state: CampaignState, u: AirDefenseUnit, pos: Vec2, jamming: float = 1.0) -> bool:
    """True if site ``u`` sees ``pos`` with its own radar or through its IADS
    node (against a target jamming by ``jamming``, see :func:`dynacamp.sensors.jam`)."""
    t = state.theater
    if u.position(t).distance_to(pos) <= sensors.reach(u.system.detection_m, jamming):
        return True
    node = network_node(state, u)
    return node is not None and node.position(t).distance_to(pos) <= sensors.reach(node.system.detection_m, jamming)


def covering(state: CampaignState, attacker: str, point: Vec2, roles=(SamRole.LONG, SamRole.MEDIUM)) -> list[AirDefenseUnit]:
    """Enemy SAM sites (of ``attacker``'s enemies) whose engagement range covers ``point``."""
    t = state.theater
    return [
        u for u in state.air_defense.values()
        if state.at_war(attacker, u.faction) and u.system.role in roles and u.operational
        and u.position(t).distance_to(point) <= u.system.range_m
    ]


def _reload(state: CampaignState, dt: float) -> None:
    """Reload missiles of sites near a depot."""
    hours = dt / 3600.0
    reach = {f: supply.depot_distances(state, f) for f in state.factions}
    for u in state.air_defense.values():
        if u.movement or u.missiles >= u.system.missiles:
            continue
        r = reach[u.faction].get(u.location)
        if r and r[0] <= RELOAD_RANGE_M:
            u.missiles = min(float(u.system.missiles), u.missiles + u.system.reload_per_hour * hours)


# ---------------------------------------------------------------- engagements


def _engage(state: CampaignState) -> None:
    """Every ready site fires a salvo at the nearest detected flight in reach."""
    t = state.theater
    flying = [
        f for f in state.flights.values()
        if f.status in ACTIVE_STATUSES and f.simulated_by_campaign and f.ai_count > 0
    ]
    if not flying:
        return
    jams = {f.id: sensors.jam(state, f) for f in flying}
    for u in state.air_defense.values():
        if (
            not u.system.engages or u.status is not SamStatus.ACTIVE or not u.operational
            or u.missiles < 1 or state.time < u.engage_ready or u.authority is not Authority.CAMPAIGN
        ):
            continue
        here = u.position(t)
        targets = [
            f for f in flying
            if state.at_war(u.faction, f.faction) and f.ai_count > 0
            and state.timers.get(f"sam:{u.id}:{f.id}", 0.0) <= state.time
            and f.pos.distance_to(here) <= u.system.range_m
            and flight_altitude(f) <= u.system.ceiling_m
            and detects(state, u, f.pos, jams[f.id])
        ]
        if not targets:
            continue
        f = min(targets, key=lambda x: (x.pos.distance_to(here), x.id))
        networked = network_node(state, u) is not None
        state.timers[f"sam:{u.id}:{f.id}"] = state.time + PAIR_COOLDOWN_S
        pk = (
            u.system.pk * DEFENSIVE_FACTOR
            * (NETWORK_PK_BONUS if networked else 1.0)
            * (SEAD_PK_FACTOR if f.task is Task.SEAD else 1.0)
            * sensors.jam_pk(jams[f.id], f)
        )
        fired = int(min(SALVO, u.missiles))
        kills = min(f.ai_count, sum(1 for _ in range(fired) if state.rng.random() < pk))
        u.missiles -= fired
        u.engage_ready = state.time + ENGAGE_COOLDOWN_S
        state.emit("sam_engagement", sam=u.id, system=u.system.name, faction=u.faction, flight=f.id,
                   callsign=f.callsign, fired=fired, kills=kills, networked=networked)
        # The launch gives the site away to the target's side (fog of war).
        from . import intel

        intel.reveal(state, f.faction, u, f"launch on {f.callsign}")
        air.lose_aircraft(state, f, kills, f"{u.system.name} ({u.id})")
        if kills and not f.task.air_to_air and f.task is not Task.SEAD and not f.attacked and f.status is FlightStatus.AIRBORNE:
            f.status = FlightStatus.RTB
            f.wp_index = len(f.waypoints) - 1
            state.emit("flight_aborted", flight=f.id, callsign=f.callsign, reason="SAM threat")
        if f.ai_count > 0 and f.status in ACTIVE_STATUSES:
            evade(state, f, u)


def ecm_screened(state: CampaignState, f: Flight) -> bool:
    """True if a friendly SEAD flight is close enough to jam for ``f`` (see :mod:`dynacamp.sensors`)."""
    return sensors.escort_jammer(state, f) is not None


def evade(state: CampaignState, f: Flight, u: AirDefenseUnit) -> None:
    """React to a SAM that just fired on ``f`` (and so is now known): an orbit
    moves out of its umbrella, a transit detours around it on the current leg.

    Once per flight and site. Flights with players seated keep their briefed
    route (the planner can still change it); SEAD flights are there for it.
    """
    from . import ato, routing

    key = f"evade:{f.id}:{u.id}"
    if key in state.timers or f.players or f.task is Task.SEAD or not f.simulated_by_campaign:
        return
    state.timers[key] = state.time + 12 * 3600.0
    if f.task.orbits and f.wp_index <= f.target_index:
        # Keep covering the same point, from outside the umbrellas we now know.
        anchor = f.waypoints[f.target_index]
        radius = air.CAP_ORBIT_RADIUS_M if f.task is Task.CAP else air.SUPPORT_ORBIT_RADIUS_M
        station = ato.cap_station(state, f.faction, anchor, radius, waypoint_altitudes(f)[f.target_index])
        if station is None:
            f.status = FlightStatus.RTB
            f.wp_index = f.target_index + 1
            state.emit("flight_aborted", flight=f.id, callsign=f.callsign, reason="SAM threat")
            return
        if station.distance_to(anchor) < 1.0:
            return
        f.waypoints[f.target_index] = station
        if f.status is FlightStatus.ON_STATION:
            f.status = FlightStatus.AIRBORNE
            f.wp_index = f.target_index
        state.emit("flight_rerouted", flight=f.id, callsign=f.callsign, faction=f.faction, reason=f"{u.system.name} ({u.id})")
        return
    if f.wp_index >= len(f.waypoints):
        return
    detour = routing.leg(f.pos, f.waypoints[f.wp_index], routing.threats(state, f.faction))
    if not detour:
        return
    f.waypoints[f.wp_index:f.wp_index] = detour
    if f.target_index >= f.wp_index:
        f.target_index += len(detour)
    state.emit("flight_rerouted", flight=f.id, callsign=f.callsign, faction=f.faction, reason=f"{u.system.name} ({u.id})")


def _sead(state: CampaignState) -> None:
    """SEAD flights fire anti-radiation missiles at emitting radars in reach."""
    t = state.theater
    for f in state.flights.values():
        if f.task is not Task.SEAD or f.status not in ACTIVE_STATUSES or not f.simulated_by_campaign or f.ai_count <= 0:
            continue
        key = f"harm:{f.id}"
        if state.timers.get(key, 0.0) > state.time:
            continue
        emitters = [
            u for u in state.air_defense.values()
            if state.at_war(f.faction, u.faction) and u.emitting and u.authority is Authority.CAMPAIGN
            and u.position(t).distance_to(f.pos) <= HARM_RANGE_M
        ]
        if not emitters:
            continue
        state.timers[key] = state.time + HARM_COOLDOWN_S
        u = min(emitters, key=lambda x: (x.position(t).distance_to(f.pos), x.id))
        hit_on_radar(state, u, f.ai_count * f.aircraft_type.a2g, by=f.callsign)


def hit_on_radar(state: CampaignState, u: AirDefenseUnit, effort: float, by: str) -> None:
    """Anti-radiation attack on an emitting site."""
    if network_node(state, u) is not None and state.rng.random() < GO_DARK_CHANCE:
        _suppress(state, u, SUPPRESS_S / 2)
        state.emit("harm_strike", sam=u.id, system=u.system.name, faction=u.faction, by=by, result="went dark")
        return
    roll = state.rng.random()
    if roll < min(0.6, 0.12 * effort):
        for k in RADAR_ELEMENTS:
            if k in u.elements:
                u.elements[k] = 0.0
        result = "radar destroyed"
    elif roll < 0.75:
        _suppress(state, u, SUPPRESS_S)
        result = "suppressed"
    else:
        result = "missed"
    state.emit("harm_strike", sam=u.id, system=u.system.name, faction=u.faction, by=by, result=result)


def strike_site(state: CampaignState, u: AirDefenseUnit, effort: float, by: str) -> None:
    """Bombs and missiles on a SAM site (DEAD): packed or dark sites are easier to hit."""
    exposed = 1.5 if u.status in (SamStatus.MOVING, SamStatus.SETTING_UP) else 1.0
    if state.rng.random() < min(0.85, 0.2 * effort * exposed):
        for k in u.elements:
            u.elements[k] *= 0.3 if k == "launcher" else 0.0
        state.emit("sam_struck", sam=u.id, system=u.system.name, faction=u.faction, by=by, result="destroyed")
    else:
        _suppress(state, u, SUPPRESS_S)
        state.emit("sam_struck", sam=u.id, system=u.system.name, faction=u.faction, by=by, result="suppressed")


def _suppress(state: CampaignState, u: AirDefenseUnit, seconds: float) -> None:
    """Switch a site's radar off for ``seconds`` and flag it to relocate."""
    if u.status in (SamStatus.ACTIVE, SamStatus.SETTING_UP):
        u.status = SamStatus.SUPPRESSED
        u.ready_at = max(u.ready_at, state.time + seconds)
    u.scoot = True


def _remove_destroyed(state: CampaignState) -> None:
    """Remove sites that lost their radars or launchers and schedule replacements."""
    for u in list(state.air_defense.values()):
        if not u.operational and u.authority is Authority.CAMPAIGN:
            del state.air_defense[u.id]
            state.emit("sam_destroyed", sam=u.id, system=u.system.name, faction=u.faction, province=u.location)
            schedule_replacement(state, u)


def schedule_replacement(state: CampaignState, u: AirDefenseUnit) -> None:
    """Queue a replacement battery for a destroyed site."""
    state.reforming_ad.append((state.time + REFORM_DELAY_S, u.faction, u.system, u.name))


def _reform(state: CampaignState) -> None:
    """Replacement batteries arrive at the faction's best stocked depot."""
    pending = []
    for ready, faction, system, name in state.reforming_ad:
        depots = [p for p in state.theater.provinces.values() if p.depot is not None and p.controller == faction]
        if state.time < ready or not depots:
            pending.append((ready if depots else state.time + 12 * 3600, faction, system, name))
            continue
        home = max(depots, key=lambda p: (p.depot.stock, p.id))
        u = AirDefenseUnit(id=state.new_id("ad"), name=name, faction=faction, location=home.id, system=system,
                           status=SamStatus.SETTING_UP, ready_at=state.time + system.setup_s)
        state.air_defense[u.id] = u
        state.emit("sam_replaced", sam=u.id, system=system.name, faction=faction, province=home.id)
    state.reforming_ad[:] = pending


# ---------------------------------------------------------------- ADA commander


def _asset_weights(state: CampaignState, fid: str) -> tuple[dict[str, float], dict[str, float]]:
    """(manoeuvre units, fixed installations) worth covering, by province."""
    t = state.theater
    units_w: dict[str, float] = {}
    for d in state.divisions.values():
        if d.faction == fid:
            units_w[d.location] = units_w.get(d.location, 0.0) + (5.0 if d.battle else 2.0)
    w: dict[str, float] = {}
    for pid, p in t.provinces.items():
        if p.controller != fid:
            continue
        if p.depot is not None:
            w[pid] = w.get(pid, 0.0) + 3.0
        w[pid] = w.get(pid, 0.0) + p.victory_points / 2
    for b in state.airbases.values():
        if b.province and b.owner(t) == fid and any(s.airbase == b.id and s.aircraft for s in state.squadrons.values()):
            w[b.province] = w.get(b.province, 0.0) + 3.0
    return units_w, w


def _threatened(state: CampaignState, u: AirDefenseUnit) -> bool:
    """Enemy troops in or attacking the site's province."""
    loc = u.location
    if any(b.province == loc for b in state.battles.values()):
        return True
    return any(
        state.at_war(u.faction, d.faction) and d.order.target == loc and d.movement
        for d in state.divisions.values()
    )


def plan(state: CampaignState, fid: str) -> None:
    """ADA commander of a faction: reposition mobile systems along the roads.

    Args:
        state: The campaign.
        fid: Faction id.
    """
    t = state.theater
    units_w, fixed_w = _asset_weights(state, fid)

    def role_weights(role: SamRole) -> dict[str, float]:
        # Short-range systems go with the manoeuvre units; the others cover installations as well.
        """Asset weights as seen by systems of one role."""
        k_units, k_fixed = (2.0, 0.5) if role is SamRole.SHORT else (1.0, 1.0)
        out = {q: k_fixed * w for q, w in fixed_w.items()}
        for q, w in units_w.items():
            out[q] = out.get(q, 0.0) + k_units * w
        return out

    weights_by_role = {r: role_weights(r) for r in SamRole}
    fronts = set(state.front_provinces(fid))
    hot_front = {p for p in fronts if any(state.hostile_divisions_in(fid, n) for n in t.neighbors(p) if state.hostile_province(fid, n))}
    covered: dict[tuple[str, str], float] = {}  # (role, province) -> share already covered

    def value(u: AirDefenseUnit, pid: str) -> float:
        """Coverage value of putting site ``u`` in province ``pid``."""
        here = t.provinces[pid].center
        reach = max(u.system.range_m * 0.8, 8_000.0)
        total = 0.0
        for q, w in weights_by_role[u.system.role].items():
            if t.provinces[q].center.distance_to(here) <= reach:
                total += w * (1.0 - covered.get((u.system.role.value, q), 0.0))
        if u.system.role in (SamRole.LONG, SamRole.MEDIUM) and pid in hot_front:
            total *= 0.3  # too exposed: cover the front from behind it
        return total

    order = {SamRole.LONG: 0, SamRole.MEDIUM: 1, SamRole.SHORT: 2}
    mobile = sorted(
        (u for u in units(state, fid) if u.system.role is not SamRole.EWR and u.authority in (Authority.CAMPAIGN, Authority.DELEGATED)),
        key=lambda u: (order[u.system.role], u.id),
    )
    def relocating(role: SamRole) -> bool:
        """True if a site of that role is already on the road."""
        return any(o.movement and o.system.role is role for o in mobile)

    for u in mobile:
        if u.movement:
            continue
        # Keep coverage up: only one long/medium site of each kind on the road at a time.
        busy_role = u.system.role in (SamRole.LONG, SamRole.MEDIUM) and relocating(u.system.role)
        current = value(u, u.location)
        if _threatened(state, u):
            current, busy_role = 0.0, False  # get out regardless
        if getattr(u, "scoot", False):
            current *= 0.5
        best, best_pid = current, u.location
        for pid, p in t.provinces.items():
            if p.controller != fid or pid == u.location:
                continue
            if t.provinces[pid].center.distance_to(t.provinces[u.location].center) > 150_000:
                continue
            v = value(u, pid)
            if v > best * MOVE_GAIN[u.system.role] + 0.5:
                best, best_pid = v, pid
        if best_pid != u.location and not busy_role and state.route_entity(u, best_pid):
            u.status = SamStatus.MOVING
            state.emit("sam_relocating", sam=u.id, system=u.system.name, faction=fid, origin=u.location, destination=best_pid)
            target_pid = best_pid
        else:
            target_pid = u.location
        # mark what this site will cover
        here = t.provinces[target_pid].center
        reach = max(u.system.range_m * 0.8, 8_000.0)
        for q in weights_by_role[u.system.role]:
            if t.provinces[q].center.distance_to(here) <= reach:
                k = (u.system.role.value, q)
                covered[k] = min(1.0, covered.get(k, 0.0) + 0.6)
