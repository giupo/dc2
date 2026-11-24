"""Read-only views of the campaign as plain JSON-ready data.

Every position is given twice: ``pos`` = [east, north] metres on the theater
plane (same frame as scenarios) and ``ll`` = [lat, lon] (null when the theater
has no known projection). Times are campaign seconds plus a ``D<day> HH:MM``
label.

These functions only read the state; the HTTP layer calls them under the
engine lock.
"""

from __future__ import annotations

from typing import Any

from .. import geometry
from ..air import FlightStatus, Task
from ..events import clock
from ..forces import Convoy, Division, OrderKind
from ..geo import Vec2
from ..projection import to_latlon
from ..state import CampaignState
from ..world import point_along, polyline_length
from ..systems import ato as ato_sys
from ..systems import supply as supply_sys

API_VERSION = 1


def _pt(state: CampaignState, v: Vec2) -> dict[str, Any]:
    """``pos`` and ``ll`` of a point."""
    ll = to_latlon(state.theater.name, v)
    return {"pos": [round(v.x), round(v.y)], "ll": None if ll is None else [round(ll[0], 6), round(ll[1], 6)]}


def _line(state: CampaignState, pts: list[Vec2]) -> dict[str, Any]:
    """``pos`` and ``ll`` of a polyline."""
    lls = [to_latlon(state.theater.name, p) for p in pts]
    return {
        "pos": [[round(p.x), round(p.y)] for p in pts],
        "ll": None if any(x is None for x in lls) else [[round(a, 6), round(b, 6)] for a, b in lls],
    }


def time_view(t: float) -> dict[str, Any]:
    """A campaign time as ``{t, label}``."""
    return {"t": round(t, 1), "label": clock(t)}


_time = time_view


# ---------------------------------------------------------------- campaign


def players(engine) -> list[dict[str, Any]]:
    """Players the engine currently sees (needs the engine for the bubbles)."""
    state = engine.state
    return [
        {
            "id": p.id,
            "name": p.name,
            "side": p.side,
            "kind": p.kind.value,
            **_pt(state, p.position),
            "altitude": round(p.altitude),
            "flight": state.player_flights.get(p.id),
        }
        for p in engine.bubbles.players.values()
    ]


def campaign(state: CampaignState, live: bool = False, players: list[dict] | None = None) -> dict[str, Any]:
    """Clock, factions with scores and objectives, players.

    Args:
        state: The campaign.
        live: The engine is in live (1:1) mode.
        players: Player views (see :func:`players`).
    """
    t = state.theater
    factions = []
    for f in state.factions.values():
        held = [p for p in t.provinces.values() if p.controller == f.id]
        factions.append(
            {
                "id": f.id,
                "name": f.name,
                "side": f.side,
                "enemies": sorted(f.enemies),
                "provinces": len(held),
                "victory_points": sum(p.victory_points for p in held),
                "divisions": sum(1 for d in state.divisions.values() if d.faction == f.id),
                "aircraft": sum(s.aircraft for s in state.squadrons.values() if s.faction == f.id),
                "objectives": [
                    {
                        "province": o.province,
                        "name": t.provinces[o.province].name,
                        "priority": o.priority,
                        "achieved": t.provinces[o.province].controller == f.id,
                    }
                    for o in f.objectives
                ],
            }
        )
    return {
        "api_version": API_VERSION,
        "name": state.name,
        "theater": t.name,
        "time": _time(state.time),
        "live": live,
        "players": players or [],
        "factions": factions,
        "events": len(state.events),
    }


# ---------------------------------------------------------------- map


def map_view(state: CampaignState) -> dict[str, Any]:
    """Provinces (with territory polygons), links and airbases."""
    t = state.theater
    cells = geometry.cells(t)
    provinces = []
    for pid, p in t.provinces.items():
        poly = [v for v, _ in cells.polygons[pid]]
        provinces.append(
            {
                "id": pid,
                "name": p.name,
                **_pt(state, p.center),
                "terrain": p.terrain.value,
                "infrastructure": p.infrastructure,
                "victory_points": p.victory_points,
                "owner": p.owner,
                "controller": p.controller,
                "depot": p.depot is not None,
                "neighbors": sorted(t.neighbors(pid)),
                "territory": _line(state, poly),
            }
        )
    links = [
        {
            "a": link.a,
            "b": link.b,
            "kind": link.kind.value,
            "river": link.river,
            "length_km": round(link.length / 1000, 1),
            "road": link.route is not None,
            **_line(state, t.link_points(link.a, link.b)),
        }
        for link in t.links()
    ]
    airbases = [
        {"id": b.id, "name": b.name, **_pt(state, b.position), "province": b.province, "owner": b.owner(t)}
        for b in state.airbases.values()
    ]
    return {"theater": t.name, "provinces": provinces, "links": links, "airbases": airbases}


# ---------------------------------------------------------------- fronts


def fronts(state: CampaignState) -> dict[str, Any]:
    """Front lines, front sectors, battles and recent fire missions."""
    t = state.theater
    lines = []
    for fl in geometry.front_lines(state):
        provs = sorted({p for pair in fl.provinces for p in pair})
        lines.append(
            {
                "factions": list(fl.factions),
                **_line(state, fl.points),
                "length_km": round(sum(a.distance_to(b) for a, b in zip(fl.points, fl.points[1:])) / 1000, 1),
                "provinces": provs,
                "battles": [b.id for b in state.battles.values() if b.province in provs],
            }
        )
    sectors = []
    for f in state.factions.values():
        for pid in state.front_provinces(f.id):
            own = [d for d in state.divisions_in(pid, f.id)]
            enemy = [
                d for n in t.neighbors(pid) if state.hostile_province(f.id, n) for d in state.hostile_divisions_in(f.id, n)
            ]
            sectors.append(
                {
                    "faction": f.id,
                    "province": pid,
                    "name": t.provinces[pid].name,
                    "own_power": round(sum(d.combat_power() for d in own)),
                    "enemy_power": round(sum(d.combat_power() for d in enemy)),
                    "divisions": [d.id for d in own],
                }
            )
    battles = [
        {
            "id": b.id,
            "province": b.province,
            "name": t.provinces[b.province].name,
            **_pt(state, battle_point(state, b)),
            "attacker": b.attacker_faction,
            "defender": b.defender_faction,
            "attackers": list(b.attackers),
            "defenders": list(b.defenders),
            "started": _time(b.started),
            "hours": round((state.time - b.started) / 3600, 1),
            "delegated": any(
                state.divisions[i].authority.value != "campaign" for i in b.attackers + b.defenders if i in state.divisions
            ),
        }
        for b in state.battles.values()
    ]
    fires = [
        {
            "shooter": f.shooter,
            "target": f.target,
            "faction": f.faction,
            "age_s": round(state.time - f.time),
            **_line(state, [Vec2(*f.origin), Vec2(*f.impact)]),
        }
        for f in state.fires
    ]
    return {"lines": lines, "sectors": sectors, "battles": battles, "fires": fires}


def battle_point(state: CampaignState, b) -> Vec2:
    """Where the fighting is: between the attackers' and defenders' positions."""
    t = state.theater

    def centroid(ids: list[str]) -> Vec2 | None:
        """Mean position of some divisions."""
        pts = [state.divisions[i].position(t) for i in ids if i in state.divisions]
        if not pts:
            return None
        return Vec2(sum(p.x for p in pts) / len(pts), sum(p.y for p in pts) / len(pts))

    a, d = centroid(b.attackers), centroid(b.defenders)
    if a and d:
        return a.lerp(d, 0.5)
    return a or d or t.provinces[b.province].center


# ---------------------------------------------------------------- forces


def _route(state: CampaignState, ent) -> dict[str, Any] | None:
    """Remaining route along the roads, from the entity's position."""
    if not ent.movement or not ent.movement.path:
        return None
    t = state.theater
    mv = ent.movement
    here = ent.position(t)
    if mv.back:  # withdrawing: back along the road it came on
        road = t.link_points(ent.location, mv.path[0])
        _, i = point_along(road, mv.progress * polyline_length(road) / t.link(ent.location, mv.path[0]).length)
        return {"provinces": [ent.location], **_line(state, [here, *road[: i + 1][::-1]])}
    pts = [here, *t.link_points_from(ent.location, mv.path[0], mv.progress)[1:]]
    prev = mv.path[0]
    for nxt in mv.path[1:]:
        pts += t.link_points(prev, nxt)[1:]
        prev = nxt
    return {"provinces": list(mv.path), **_line(state, pts)}


def division_view(state: CampaignState, d: Division) -> dict[str, Any]:
    """JSON view of a division."""
    return {
        "id": d.id,
        "name": d.name,
        "faction": d.faction,
        "template": d.template.id,
        "template_name": d.template.name,
        "location": d.location,
        **_pt(state, d.position(state.theater)),
        "strength": round(d.strength, 3),
        "org": round(d.org, 3),
        "supply": round(d.supply_ratio, 3),
        "elements": {k: round(v, 1) for k, v in d.elements.items()},
        "order": {"kind": d.order.kind.value, "target": d.order.target},
        "route": _route(state, d),
        "battle": d.battle,
        "authority": d.authority.value,
        "moving": d.movement is not None and d.order.kind is not OrderKind.HOLD,
    }


def forces(state: CampaignState) -> dict[str, Any]:
    """All divisions and battles."""
    return {
        "divisions": [division_view(state, d) for d in state.divisions.values()],
        "battles": fronts(state)["battles"],
    }


# ---------------------------------------------------------------- logistics


def convoy_view(state: CampaignState, c: Convoy) -> dict[str, Any]:
    """JSON view of a convoy."""
    return {
        "id": c.id,
        "faction": c.faction,
        **_pt(state, c.position(state.theater)),
        "cargo": round(c.cargo, 1),
        "trucks": round(c.trucks, 1),
        "home": c.home,
        "destination": c.destination,
        "returning": c.returning,
        "heading_to": c.home if c.returning else c.destination,
        "route": _route(state, c),
        "authority": c.authority.value,
    }


def logistics(state: CampaignState) -> dict[str, Any]:
    """Depots, convoys, supply network reach and division supply."""
    t = state.theater
    depots = [
        {
            "province": pid,
            "name": p.name,
            **_pt(state, p.center),
            "controller": p.controller,
            "stock": round(p.depot.stock),
            "capacity": round(p.depot.capacity),
            "production_per_hour": p.depot.production_per_hour,
            "trucks": p.depot.trucks,
        }
        for pid, p in t.provinces.items()
        if p.depot is not None
    ]
    network = {}
    for fid in state.factions:
        reach = supply_sys.depot_distances(state, fid)
        network[fid] = {
            pid: {
                "depot": depot,
                "road_km": round(dist / 1000, 1),
                "direct": dist <= supply_sys.DIRECT_RANGE_M,
            }
            for pid, (dist, depot) in reach.items()
        }
    divisions = [
        {
            "id": d.id,
            "faction": d.faction,
            "location": d.location,
            "supply": round(d.supply_ratio, 3),
            "out_of_supply": d.supply <= 0,
            "direct": network[d.faction].get(d.location, {}).get("direct", False),
            "convoy_incoming": any(
                c.destination == d.location and not c.returning and c.faction == d.faction for c in state.convoys.values()
            ),
        }
        for d in state.divisions.values()
    ]
    return {
        "depots": depots,
        "convoys": [convoy_view(state, c) for c in state.convoys.values()],
        "network": network,
        "divisions": divisions,
        "direct_range_km": supply_sys.DIRECT_RANGE_M / 1000,
    }


# ---------------------------------------------------------------- ATO


def flight_view(state: CampaignState, f) -> dict[str, Any]:
    """JSON view of a flight."""
    sq = state.squadrons[f.squadron]
    return {
        "id": f.id,
        "package": f.package,
        "callsign": f.callsign,
        "faction": f.faction,
        "squadron": sq.id,
        "squadron_name": sq.name,
        "airbase": sq.airbase,
        "aircraft": f.aircraft_type.id,
        "aircraft_name": f.aircraft_type.name,
        "helo": f.aircraft_type.helo,
        "task": f.task.value,
        "status": f.status.value,
        "size": f.size,
        "ai": f.ai_count,
        "seats_free": f.seats_free,
        "players": [{"id": s.player_id, "name": s.name, "state": s.state.value} for s in f.players.values()],
        "takeoff": _time(f.takeoff_time),
        "tot": _time(f.tot),
        **_pt(state, f.position(state.theater)),
        "waypoints": _line(state, f.waypoints),
        "target_index": f.target_index,
        "next_waypoint": f.wp_index,
        "target": {"kind": f.target_kind, "id": f.target_id},
        "authority": f.authority.value,
        "on_station_until": _time(f.station_until) if f.task is Task.CAP else None,
    }


def ato(state: CampaignState) -> dict[str, Any]:
    """Packages, squadrons, sortie budget and in-game join boards."""
    packages = [
        {
            "id": p.id,
            "faction": p.faction,
            "task": p.task.value,
            "target": p.target_label,
            **_pt(state, p.target_point),
            "tot": _time(p.tot),
            "created": _time(p.created),
            "flights": [flight_view(state, state.flights[i]) for i in p.flights if i in state.flights],
        }
        for p in sorted(state.packages.values(), key=lambda p: (p.tot, p.id))
    ]
    squadrons = [
        {
            "id": s.id,
            "name": s.name,
            "faction": s.faction,
            "airbase": s.airbase,
            "aircraft": s.aircraft_type.id,
            "aircraft_name": s.aircraft_type.name,
            "tasks": sorted(t.value for t in s.tasks),
            "strength": s.aircraft,
            "available": s.available_at(state.time),
            "committed": s.committed,
            "servicing": sum(n for ready, n in s.servicing if ready > state.time),
        }
        for s in state.squadrons.values()
    ]
    budget = {f: ato_sys.sortie_budget_left(state, f) for f in state.factions}
    airborne = sum(1 for f in state.flights.values() if f.status not in (FlightStatus.SCHEDULED, FlightStatus.DONE, FlightStatus.CANCELLED))
    return {
        "packages": packages,
        "squadrons": squadrons,
        "sortie_budget_left": budget,
        "flights_airborne": airborne,
        "boards": {side: ato_sys.ato_board(state, side) for side in sorted({f.side for f in state.factions.values()})},
    }


# ---------------------------------------------------------------- air defence


def air_defense(state: CampaignState) -> dict[str, Any]:
    """SAM sites and EWRs with their state, and IADS links."""
    from ..systems import air_defense as ada

    t = state.theater
    sites, links = [], []
    for u in state.air_defense.values():
        node = ada.network_node(state, u)
        sites.append(
            {
                "id": u.id,
                "name": u.name,
                "faction": u.faction,
                "system": u.system.id,
                "system_name": u.system.name,
                "role": u.system.role.value,
                "status": u.status.value,
                **_pt(state, u.position(t)),
                "location": u.location,
                "range_km": round(u.system.range_m / 1000, 1),
                "detection_km": round(u.system.detection_m / 1000, 1),
                "ceiling_m": round(u.system.ceiling_m),
                "missiles": round(u.missiles, 1),
                "missiles_max": u.system.missiles,
                "elements": {k: round(v, 1) for k, v in u.elements.items()},
                "emitting": u.emitting,
                "networked_to": node.id if node else None,
                "route": _route(state, u),
                "authority": u.authority.value,
            }
        )
        if node is not None:
            links.append({"ewr": node.id, "site": u.id, "faction": u.faction, **_line(state, [node.position(t), u.position(t)])})
    return {"sites": sites, "links": links}


# ---------------------------------------------------------------- events


def events(state: CampaignState, since: int = 0, kinds: set[str] | None = None, limit: int = 500) -> dict[str, Any]:
    """A page of events.

    Args:
        state: The campaign.
        since: First sequence number.
        kinds: Keep only these kinds.
        limit: Page size.
    """
    out = []
    for e in state.events.since(max(0, since)):
        if kinds and e.kind not in kinds:
            continue
        out.append({"seq": e.seq, "time": _time(e.time), "kind": e.kind, "data": e.data})
        if len(out) >= limit:
            break
    next_seq = out[-1]["seq"] + 1 if out and len(out) >= limit else len(state.events)
    return {"events": out, "next": next_seq}


def snapshot(state: CampaignState, live: bool = False, players: list[dict] | None = None) -> dict[str, Any]:
    """All views in one document."""
    return {
        "campaign": campaign(state, live, players),
        "map": map_view(state),
        "fronts": fronts(state),
        "forces": forces(state),
        "logistics": logistics(state),
        "ato": ato(state),
        "air_defense": air_defense(state),
    }
