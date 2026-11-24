"""Mission packages: briefing, named waypoints, threats and ordnance for a flight.

Everything a pilot needs for an ATO flight, built from the campaign state:

* **mission and situation**: what to do, and the fight around the target;
* **target**: what it is, its composition and coordinates, bearing and range
  from the home base;
* **threats**: enemy SAM sites whose engagement range covers the route
  (sampled along every leg), short-range air defence at the target, enemy
  fighters able to reach it;
* **waypoints**: named (takeoff, IP, target, egress, land / CAP station), with
  times counted back from the time on target, altitude and speed per leg;
* **ordnance**: the loadout from :mod:`dynacamp.loadouts`, the same one the AI
  flights of the mission carry;
* **package** and **comms**.

:func:`briefing` returns plain data (served by the API, shown by the web UI);
:func:`render` turns it into the text sent to players in the game. Numbers
stay in SI (``alt_m``, ``speed_kmh``, ``range_km``); the texts are written in
the requested units (:mod:`dynacamp.units`: knots, feet and nautical miles by
default, or metric).
"""

from __future__ import annotations

import math
from typing import Any

from .. import loadouts
from .. import units as U
from ..units import Units
from ..air import Flight, Task
from ..air_defense import SamRole
from ..events import clock
from ..geo import Vec2
from ..projection import to_latlon
from ..state import CampaignState

ROUTE_SAMPLE_M = 5_000.0
ATTACK_ALT_M = {Task.CAS: 1_500.0, Task.BAI: 1_500.0, Task.STRIKE: 6_000.0, Task.SEAD: 6_000.0, Task.ESCORT: 7_500.0}
CAP_ALT_M = 7_500.0
HELO_ALT_M = 50.0

MISSION_TEXT = {
    Task.CAS: "Close air support: destroy enemy forces engaged with our troops at the target. Expect friendlies within {friendly_range}.",
    Task.BAI: "Battlefield interdiction: destroy the enemy formation before it reaches the front.",
    Task.STRIKE: "Strike: destroy the target installation.",
    Task.SEAD: "Suppress and destroy enemy air defences threatening the package.",
    Task.ESCORT: "Escort: protect the package from enemy fighters from ingress to egress.",
    Task.CAP: "Combat air patrol: deny the area to enemy aircraft for the whole station time.",
    Task.AWACS: "Airborne early warning: hold the orbit, build the air picture and control the fighters.",
    Task.TANKER: "Air refuelling: hold the track and refuel receivers on request.",
    Task.TRANSPORT: "Airlift: deliver the cargo to the landing zone and return.",
}
STATION_NAME = {Task.CAP: "CAP STATION", Task.AWACS: "AWACS ORBIT", Task.TANKER: "TANKER TRACK"}


# ---------------------------------------------------------------- helpers


def ddm(ll: tuple[float, float] | None) -> str:
    """Latitude/longitude as degrees and decimal minutes (``N42°10.5' E042°28.9'``)."""
    if ll is None:
        return "-"
    lat, lon = ll

    def part(v: float, pos: str, neg: str, width: int) -> str:
        """One coordinate as hemisphere, degrees and decimal minutes."""
        d = abs(v)
        deg = int(d)
        return f"{pos if v >= 0 else neg}{deg:0{width}d}°{(d - deg) * 60:04.1f}'"

    return f"{part(lat, 'N', 'S', 2)} {part(lon, 'E', 'W', 3)}"


def bearing_range(a: Vec2, b: Vec2, units: Units = "metric") -> str:
    """True bearing and range from ``a`` to ``b`` (``"087° 64 km"`` / ``"087° 35 NM"``)."""
    return f"{math.degrees(a.heading_to(b)) % 360:03.0f}° {U.distance(a.distance_to(b), units)}"


def _ll(state: CampaignState, p: Vec2) -> list[float] | None:
    """``[lat, lon]`` of a campaign point, or None without projection."""
    ll = to_latlon(state.theater.name, p)
    return None if ll is None else [round(ll[0], 6), round(ll[1], 6)]


def _route_samples(points: list[Vec2]) -> list[Vec2]:
    """Points every ``ROUTE_SAMPLE_M`` along a route."""
    out: list[Vec2] = []
    for a, b in zip(points, points[1:]):
        n = max(1, int(a.distance_to(b) // ROUTE_SAMPLE_M))
        out += [a.lerp(b, i / n) for i in range(n)]
    return out + points[-1:]


def frequencies(f: Flight) -> dict[str, str]:
    """Package (UHF) and flight (VHF) frequencies, stable for the package/flight."""
    pkg_n = int("".join(c for c in f.package if c.isdigit()) or 0)
    fl_n = int("".join(c for c in f.id if c.isdigit()) or 0)
    return {"package": f"UHF {250.0 + (pkg_n % 40) * 0.5:.3f} AM", "flight": f"VHF {130.0 + (fl_n % 60) * 0.25:.3f} AM"}


# ---------------------------------------------------------------- waypoints


def waypoint_names(f: Flight) -> list[str]:
    """Names of a flight's waypoints."""
    n = len(f.waypoints)
    names = ["WP"] * n
    names[0], names[-1] = "TAKEOFF", "LAND"
    if f.task.orbits:
        names[f.target_index] = STATION_NAME[f.task]
    elif f.task is Task.TRANSPORT:
        names[f.target_index] = "DELIVERY"
    else:
        names[f.target_index] = "TARGET"
        if f.target_index >= 2:
            names[f.target_index - 1] = "IP"
        if f.target_index + 1 < n - 1:
            names[f.target_index + 1] = "EGRESS"
    return names


def waypoint_plan(state: CampaignState, f: Flight) -> list[dict[str, Any]]:
    """Named waypoints with ETA (counted from the TOT), altitude and speed.

    Returns:
        ``[{index, name, pos, ll, eta, alt_m, speed_kmh, note}, ...]``.
    """
    ac = f.aircraft_type
    speed = ac.cruise_kmh / 3.6
    wps = f.waypoints
    names = waypoint_names(f)
    ti = f.target_index

    def dist(i: int, j: int) -> float:
        """Route distance between waypoints ``i`` and ``j``."""
        lo, hi = min(i, j), max(i, j)
        return sum(wps[k].distance_to(wps[k + 1]) for k in range(lo, hi))

    station_s = max(0.0, f.station_until - f.tot) if f.task.orbits else 0.0
    attack_alt = HELO_ALT_M if ac.helo else ATTACK_ALT_M.get(f.task, ac.cruise_alt_m)
    plan = []
    for i, p in enumerate(wps):
        if i <= ti:
            eta = f.tot - dist(i, ti) / speed
        else:
            eta = f.tot + station_s + dist(ti, i) / speed
        name = names[i]
        if name == "TAKEOFF" or name == "LAND":
            alt = 0.0
        elif ac.helo:
            alt = HELO_ALT_M
        elif name == "CAP STATION":
            alt = CAP_ALT_M
        elif name in ("AWACS ORBIT", "TANKER TRACK", "DELIVERY"):
            alt = ac.cruise_alt_m if name != "DELIVERY" else (HELO_ALT_M if ac.helo else 0.0)
        elif name in ("TARGET", "EGRESS") or (name == "IP" and f.task in (Task.CAS, Task.BAI)):
            alt = attack_alt
        else:
            alt = ac.cruise_alt_m
        note = {
            "TAKEOFF": "start up, take off and join up",
            "IP": "push: weapons hot, descend for the attack" if f.task in (Task.CAS, Task.BAI) else "push to target",
            "TARGET": "time on target" if f.task is not Task.SEAD else "suppress emitting radars",
            "EGRESS": "off target, check out",
            "CAP STATION": f"hold until {clock(f.station_until)}",
            "AWACS ORBIT": f"hold until {clock(f.station_until)}",
            "TANKER TRACK": f"hold until {clock(f.station_until)}",
            "DELIVERY": "land, unload, take off",
            "LAND": "recover at home base",
        }.get(name, "")
        plan.append(
            {
                "index": i,
                "name": name,
                "pos": [round(p.x), round(p.y)],
                "ll": _ll(state, p),
                "eta": {"t": round(eta, 1), "label": clock(eta)},
                "alt_m": round(alt),
                "speed_kmh": round(ac.cruise_kmh * (0.9 if name in ("IP", "TARGET") and not ac.helo else 1.0)),
                "note": note,
            }
        )
    return plan


def waypoint_marks(state: CampaignState, f: Flight, units: Units | None = None) -> list[tuple[str, Vec2]]:
    """Map marks for the game: ``(label, position)`` for every waypoint after takeoff
    (altitudes in ``units``, default the campaign's)."""
    units = units or state.units
    out = []
    for wp in waypoint_plan(state, f)[1:]:
        alt = f" {U.altitude(wp['alt_m'], units)}" if wp["alt_m"] else ""
        label = f"{f.callsign} WP{wp['index']} {wp['name']} {wp['eta']['label'][3:]}{alt}"
        out.append((label, Vec2(*wp["pos"])))
    return out


# ---------------------------------------------------------------- target, threats


def target_info(state: CampaignState, f: Flight, units: Units = "metric") -> dict[str, Any]:
    """What the flight is sent against."""
    t = state.theater
    point = f.target_point
    base = state.airbases[state.squadrons[f.squadron].airbase]
    info: dict[str, Any] = {
        "kind": f.target_kind,
        "id": f.target_id,
        "name": state.packages[f.package].target_label if f.package in state.packages else f.target_id,
        "description": "",
        "composition": [],
        "pos": [round(point.x), round(point.y)],
        "ll": _ll(state, point),
        "coordinates": ddm(to_latlon(t.name, point)),
        "from_base": f"{bearing_range(base.position, point, units)} from {base.name}",
    }
    if f.target_kind == "battle" and f.target_id in state.battles:
        b = state.battles[f.target_id]
        enemy_ids = b.defenders if b.attacker_faction == f.faction else b.attackers
        enemies = [state.divisions[i] for i in enemy_ids if i in state.divisions]
        info["description"] = f"Battle for {t.provinces[b.province].name}: {len(enemies)} enemy brigade(s) in contact."
        info["composition"] = [f"{d.name} ({d.template.name}, {d.strength:.0%} strength)" for d in enemies]
    elif f.target_kind == "division" and f.target_id in state.divisions:
        d = state.divisions[f.target_id]
        info["description"] = f"{d.name}, {d.template.name}, {d.strength:.0%} strength, " + (
            f"moving towards {t.provinces[d.movement.path[-1]].name}" if d.movement and d.movement.path else f"at {t.provinces[d.location].name}"
        )
        info["composition"] = [f"{k}: {round(n)}" for k, n in d.elements.items() if n >= 1]
    elif f.target_kind == "convoy" and f.target_id in state.convoys:
        c = state.convoys[f.target_id]
        dest = c.home if c.returning else c.destination
        info["description"] = f"Supply convoy, {round(c.trucks)} trucks, heading to {t.provinces[dest].name}. Expect it to have moved: search along the road."
        info["composition"] = [f"trucks: {round(c.trucks)}", f"cargo: {round(c.cargo)} supply"]
    elif f.target_kind == "depot" and f.target_id in t.provinces:
        p = t.provinces[f.target_id]
        info["description"] = f"Supply depot at {p.name}" + (f", {round(p.depot.stock)} supply in stock." if p.depot else ".")
    elif f.target_kind == "sam" and f.target_id in state.air_defense:
        u = state.air_defense[f.target_id]
        info["description"] = f"{u.system.name} site ({u.name}), {u.status.value.replace('_', ' ')}, range {U.distance(u.system.range_m, units)}."
        info["composition"] = [f"{k.replace('_', ' ')}: {round(n)}" for k, n in u.elements.items() if n >= 1]
    elif f.target_kind == "airlift" and f.target_id in state.airbases:
        b = state.airbases[f.target_id]
        info["description"] = f"Airlift {round(f.cargo or f.aircraft_type.cargo * f.size)} supply to {b.name}."
    elif f.target_kind == "resupply" and f.target_id in state.divisions:
        d = state.divisions[f.target_id]
        info["description"] = f"Resupply {d.name} ({d.supply_ratio:.0%} supply) in the field: land near the brigade."
    elif f.task in (Task.AWACS, Task.TANKER):
        info["description"] = f"{'Orbit' if f.task is Task.AWACS else 'Refuelling track'} behind the front, clear of known enemy SAMs."
    elif f.task is Task.CAP:
        info["description"] = f"CAP station over {t.provinces[f.target_id].name if f.target_id in t.provinces else 'the front'}."
    return info


def threats(state: CampaignState, f: Flight, units: Units = "metric") -> list[dict[str, Any]]:
    """Enemy air defences along the route and fighters able to reach the target.

    SAM sites are those the faction *knows about* (systems.intel), at their last
    known position: a site nobody has located is not in the briefing."""
    from . import air, air_defense, ato, intel

    t = state.theater
    route = f.waypoints
    samples = _route_samples(route)
    out: list[dict[str, Any]] = []
    for c in intel.known(state, f.faction, "sam"):
        u = state.air_defense.get(c.entity_id)
        if u is None or not state.at_war(f.faction, u.faction) or not u.system.engages:
            continue
        pos = c.position
        reach = u.system.range_m
        if u.system.role is SamRole.SHORT and pos.distance_to(f.target_point) > reach + 5_000:
            continue  # short-range systems only matter near the target (low altitude)
        closest = min(p.distance_to(pos) for p in samples)
        if closest > reach:
            continue
        near = t.nearest(pos).name
        out.append(
            {
                "kind": "sam",
                "name": u.system.name,
                "detail": f"{u.name} near {near}: range {U.distance(reach, units)}, route passes {U.distance(closest, units)} from it"
                + (", IADS-networked" if air_defense.network_node(state, u) else "")
                + ("" if c.detail.get("status", "active") == "active" else f" ({c.detail['status'].replace('_', ' ')})")
                + (f", last located {c.age(state.time) / 3600:.0f} h ago" if c.stale(state.time) else ""),
                "range_km": round(reach / 1000, 1),
                "pos": [round(pos.x), round(pos.y)],
                "ll": _ll(state, pos),
                "severity": "high" if u.system.role is SamRole.LONG or closest < reach * 0.5 else "medium",
            }
        )
    shorad = air.air_defense(state, f.faction, f.target_point)
    if shorad >= 1:
        out.append(
            {
                "kind": "shorad",
                "name": "SHORAD / AAA at target",
                "detail": f"{'heavy' if shorad >= ato.SEAD_THRESHOLD else 'light'} short-range air defence with the enemy brigades",
                "range_km": 5.0,
                "pos": [round(f.target_point.x), round(f.target_point.y)],
                "ll": _ll(state, f.target_point),
                "severity": "medium" if shorad >= ato.SEAD_THRESHOLD else "low",
            }
        )
    for sq in state.squadrons.values():
        if not state.at_war(f.faction, sq.faction) or sq.aircraft <= 0 or not (sq.tasks & {Task.CAP, Task.ESCORT}):
            continue
        base = state.airbases[sq.airbase]
        if base.owner(t) != sq.faction:
            continue
        d = base.position.distance_to(f.target_point)
        if d <= sq.aircraft_type.radius_km * 1000:
            out.append(
                {
                    "kind": "fighters",
                    "name": sq.aircraft_type.name,
                    "detail": f"{sq.name}, {sq.aircraft} x {sq.aircraft_type.name} at {base.name}, {U.distance(d, units)} from the target",
                    "range_km": round(sq.aircraft_type.radius_km, 0),
                    "pos": [round(base.position.x), round(base.position.y)],
                    "ll": _ll(state, base.position),
                    "severity": "high" if sq.aircraft_type.a2a >= 1.0 else "medium",
                }
            )
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(out, key=lambda x: (order[x["severity"]], x["kind"], x["name"]))


# ---------------------------------------------------------------- briefing


def briefing(state: CampaignState, f: Flight, units: Units | None = None) -> dict[str, Any]:
    """The complete mission package of a flight, as plain data.

    Args:
        state: The campaign.
        f: The flight.
        units: Units of the texts (default: the campaign's, see :mod:`dynacamp.units`).
    """
    units = units or state.units
    t = state.theater
    pkg = state.packages.get(f.package)
    sq = state.squadrons[f.squadron]
    base = state.airbases[sq.airbase]
    stores = loadouts.summary(f.aircraft_type.id, f.task.value)
    situation = []
    near = [b for b in state.battles.values() if t.provinces[b.province].center.distance_to(f.target_point) <= 40_000]
    for b in near:
        situation.append(f"Battle in progress for {t.provinces[b.province].name}: {len(b.attackers)} attacking vs {len(b.defenders)} defending brigades.")
    enemies = sorted(
        (d for d in state.divisions.values() if state.at_war(f.faction, d.faction) and d.position(t).distance_to(f.target_point) <= 30_000),
        key=lambda d: d.position(t).distance_to(f.target_point),
    )
    if enemies:
        situation.append(f"Enemy forces within {U.distance(30_000, units)} of the target: " + ", ".join(f"{d.name} ({d.strength:.0%})" for d in enemies[:4]) + ".")
    friends = [d for d in state.divisions.values() if d.faction == f.faction and d.position(t).distance_to(f.target_point) <= 15_000]
    if friends:
        situation.append(f"FRIENDLY forces within {U.distance(15_000, units)} of the target: " + ", ".join(d.name for d in friends[:4]) + ". Positive identification required.")
    flights = [state.flights[i] for i in (pkg.flights if pkg else []) if i in state.flights]
    return {
        "units": units,
        "package_number": pkg.number if pkg else "",
        "support": support(state, f, units),
        "flight_id": f.id,
        "callsign": f.callsign,
        "title": f"{'PKG ' + pkg.number if pkg else ''} {f.task.value} - {pkg.target_label if pkg else ''}".strip(),
        "task": f.task.value,
        "aircraft": f.aircraft_type.name,
        "size": f.size,
        "squadron": sq.name,
        "base": base.name,
        "takeoff": {"t": round(f.takeoff_time, 1), "label": clock(f.takeoff_time)},
        "tot": {"t": round(f.tot, 1), "label": clock(f.tot)},
        "mission": MISSION_TEXT.get(f.task, "").format(friendly_range=U.distance(3_000, units)),
        "situation": situation or ["No contact reported near the target."],
        "target": target_info(state, f, units),
        "threats": threats(state, f, units),
        "loadout": [{"store": name, "count": n} for name, n in stores],
        "loadout_note": "" if stores else (
            "Unarmed." if f.task.support else "Ordnance as set in the aircraft template (mission editor)."
        ),
        "comms": frequencies(f),
        "waypoints": waypoint_plan(state, f),
        "package": [
            {
                "callsign": o.callsign,
                "aircraft": o.aircraft_type.name,
                "task": o.task.value,
                "size": o.size,
                "base": state.airbases[state.squadrons[o.squadron].airbase].name,
                "takeoff": {"t": round(o.takeoff_time, 1), "label": clock(o.takeoff_time)},
                "you": o.id == f.id,
            }
            for o in flights
        ],
    }


def support(state: CampaignState, f: Flight, units: Units = "metric") -> list[dict[str, Any]]:
    """Friendly AWACS and tankers airborne or planned around the flight's time."""
    out = []
    for o in state.flights.values():
        if o.faction != f.faction or o.id == f.id or o.task not in (Task.AWACS, Task.TANKER):
            continue
        if o.status.value in ("done", "cancelled") or o.station_until < f.takeoff_time or o.tot > f.tot + 3600:
            continue
        p = o.target_point
        out.append(
            {
                "task": o.task.value,
                "callsign": o.callsign,
                "aircraft": o.aircraft_type.name,
                "status": o.status.value,
                "position": ddm(to_latlon(state.theater.name, p)),
                "from_target": bearing_range(f.target_point, p, units),
                "frequency": frequencies(o)["flight"],
                "until": {"t": round(o.station_until, 1), "label": clock(o.station_until)},
                "pos": [round(p.x), round(p.y)],
                "ll": _ll(state, p),
            }
        )
    return out


def render(b: dict[str, Any], now: float | None = None) -> str:
    """The briefing as text for in-game messages (in the briefing's units)."""
    units: Units = b.get("units", U.DEFAULT_UNITS)

    def rel(t: float) -> str:
        """Relative time label, empty when ``now`` is not given."""
        if now is None:
            return ""
        m = round((t - now) / 60)
        return f" (in {m} min)" if m >= 0 else f" ({-m} min ago)"

    lines = [
        f"=== {b['title']} ===",
        f"{b['callsign']}: {b['size']}x {b['aircraft']} ({b['squadron']}) from {b['base']}, task {b['task']}",
        f"Takeoff {b['takeoff']['label']}{rel(b['takeoff']['t'])} - TOT {b['tot']['label']}{rel(b['tot']['t'])}",
        f"MISSION: {b['mission']}",
        f"TARGET: {b['target']['name']} at {b['target']['coordinates']} ({b['target']['from_base']})",
    ]
    if b["target"]["description"]:
        lines.append(f"  {b['target']['description']}")
    lines += [f"SITUATION: {s}" for s in b["situation"]]
    if b["threats"]:
        lines.append("THREATS: " + "; ".join(f"{x['name']} ({x['severity']})" for x in b["threats"][:6]))
    else:
        lines.append("THREATS: none known")
    if b["loadout"]:
        lines.append("ORDNANCE: " + ", ".join(f"{s['store']} x{s['count']}" for s in b["loadout"]))
    elif b["loadout_note"]:
        lines.append("ORDNANCE: " + b["loadout_note"])
    lines.append(f"COMMS: package {b['comms']['package']}, flight {b['comms']['flight']}")
    for s in b.get("support", []):
        lines.append(f"SUPPORT: {s['task']} {s['callsign']} ({s['aircraft']}) at {s['position']}, {s['frequency']}, until {s['until']['label'][3:]}")
    lines.append("WAYPOINTS (ETA / alt / speed):")
    for wp in b["waypoints"]:
        alt = U.altitude(wp["alt_m"], units) if wp["alt_m"] else "-"
        lines.append(f"  WP{wp['index']} {wp['name']:<11} {wp['eta']['label'][3:]}  {alt:>8}  {U.speed(wp['speed_kmh'], units)}")
    others = [p for p in b["package"] if not p["you"]]
    if others:
        lines.append("PACKAGE: " + "; ".join(f"{p['callsign']} {p['size']}x {p['aircraft']} {p['task']}" for p in others))
    lines.append("Waypoints are marked on the F10 map.")
    return "\n".join(lines)
