"""Read-only views of the campaign as plain JSON-ready data.

Every position is given twice: ``pos`` = [east, north] metres on the theater
plane (same frame as scenarios) and ``ll`` = [lat, lon] (null when the theater
has no known projection). Times are campaign seconds plus a ``D<day> HH:MM``
label.

These functions only read the state; the HTTP layer calls them under the
engine lock.
"""

from __future__ import annotations

import math
from typing import Any

from .. import bullseye, geometry, standing
from ..air import FlightStatus, Task
from ..events import PUBLIC, EventKind, clock
from ..forces import Authority, Convoy, Division, OrderKind
from ..geo import Vec2
from ..projection import to_latlon
from ..state import CampaignState
from ..world import point_along, polyline_length
from ..systems import ato as ato_sys
from ..systems import intel
from ..systems import supply as supply_sys

#: Version of the API views, reported by ``/api`` and in the OpenAPI schema.
API_VERSION = 1
#: What a view shows where the fog of war hides a value (see :data:`schemas.Unknown`).
UNKNOWN = "unknown"

# Every view takes an optional ``viewer`` faction id. Without it the view is the
# umpire's (ground truth). With it, enemy entities appear only if the faction's
# intelligence picture holds them (systems.intel), at their last known position,
# with an ``intel`` block and their internals (orders, routes, supply) hidden.


def _enemy(state: CampaignState, viewer: str | None, faction: str) -> bool:
    """True if ``faction``'s entities are subject to fog of war for ``viewer``.

    Args:
        state: The campaign.
        viewer: Faction whose view is given (None: the umpire's).
        faction: Faction owning the entity.

    Returns:
        Whether the viewer sees the entity only through its contacts.
    """
    return viewer is not None and state.at_war(viewer, faction)


def _contact(state: CampaignState, viewer: str, entity_id: str):
    """The viewer's contact on an entity (None if unknown).

    Args:
        state: The campaign.
        viewer: Faction whose view is given.
        entity_id: The entity.

    Returns:
        The contact, or None.
    """

    return intel.picture(state, viewer).get(entity_id)


def _intel(state: CampaignState, c) -> dict[str, Any]:
    """The ``intel`` block of an enemy entity.

    Args:
        state: The campaign.
        c: The viewer's contact on the entity.

    Returns:
        Source, time seen, age, staleness and identification of the contact.
    """
    return {
        "source": c.source,
        "seen": _time(c.seen),
        "age_s": round(c.age(state.time)),
        "stale": c.stale(state.time),
        "identified": c.identified,
    }


def _pt(state: CampaignState, v: Vec2) -> dict[str, Any]:
    """``pos`` and ``ll`` of a point.

    Args:
        state: The campaign.
        v: The point.

    Returns:
        ``{"pos": [east, north], "ll": [lat, lon] or None}``.
    """
    ll = to_latlon(state.theater.name, v)
    return {"pos": [round(v.x), round(v.y)], "ll": None if ll is None else [round(ll[0], 6), round(ll[1], 6)]}


def _line(state: CampaignState, pts: list[Vec2]) -> dict[str, Any]:
    """``pos`` and ``ll`` of a polyline.

    Args:
        state: The campaign.
        pts: The polyline.

    Returns:
        ``{"pos": [...], "ll": [...] or None}``.
    """
    lls = [to_latlon(state.theater.name, p) for p in pts]
    return {
        "pos": [[round(p.x), round(p.y)] for p in pts],
        "ll": None if any(x is None for x in lls) else [[round(a, 6), round(b, 6)] for a, b in lls],
    }


def time_view(t: float) -> dict[str, Any]:
    """A campaign time as ``{t, label}``.

    Args:
        t: Campaign time in seconds.

    Returns:
        The time and its label.
    """
    return {"t": round(t, 1), "label": clock(t)}


_time = time_view


# ---------------------------------------------------------------- campaign


def players(engine) -> list[dict[str, Any]]:
    """Players the engine currently sees (needs the engine for the bubbles).

    Args:
        engine: The engine.

    Returns:
        One entry per player, with position and seat.
    """
    state = engine.state
    return [
        {
            "id": p.id,
            "name": p.name,
            "coalition": p.coalition,
            "kind": p.kind.value,
            **_pt(state, p.position),
            "altitude": round(p.altitude),
            "flight": state.player_flights.get(p.id),
        }
        for p in engine.bubbles.players.values()
    ]


def campaign(
    state: CampaignState, live: bool = False, players: list[dict] | None = None, clock: dict | None = None
) -> dict[str, Any]:
    """Clock, factions with scores and objectives, players.

    Args:
        state: The campaign.
        live: The engine is in live (1:1) mode.
        players: Player views (see :func:`players`).
        clock: Pause and speed (see :func:`control`).

    Returns:
        The campaign view.
    """
    t = state.theater
    shares = standing.shares(state)
    factions = []
    for f in state.factions.values():
        held = [p for p in t.provinces.values() if p.controller == f.id]
        factions.append(
            {
                "id": f.id,
                "name": f.name,
                "coalition": f.coalition,
                "army": f.army,
                "color": f.color,
                "quality": f.quality,
                "standing": round(shares.get(f.id, 0.0), 4),
                "enemies": sorted(f.enemies),
                "provinces": len(held),
                "victory_points": sum(p.victory_points for p in held),
                "divisions": sum(1 for d in state.divisions.values() if d.faction == f.id),
                "aircraft": sum(s.aircraft for s in state.squadrons.values() if s.faction == f.id),
                "bullseye": _pt(state, f.bullseye),
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
        "units": state.units,
        "control": clock,
        "api_version": API_VERSION,
        "name": state.name,
        "theater": t.name,
        "magnetic_variation_deg": bullseye.MAGNETIC_VARIATION_DEG.get(t.name.lower(), 0.0),
        "time": _time(state.time),
        "live": live,
        "players": players or [],
        "factions": factions,
        "events": len(state.events),
    }


# ---------------------------------------------------------------- map


def map_view(state: CampaignState) -> dict[str, Any]:
    """Provinces (with territory polygons), links and airbases.

    Args:
        state: The campaign.

    Returns:
        The map view.
    """
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


def fronts(state: CampaignState, viewer: str | None = None) -> dict[str, Any]:
    """Front lines, front sectors, battles and recent fire missions. In a
    faction's view: only its sectors (enemy power as estimated by its
    intelligence) and fire missions it fired or received.

    Args:
        state: The campaign.
        viewer: Faction whose view to give (None: the umpire's).

    Returns:
        The fronts view.
    """
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
        if _enemy(state, viewer, f.id):
            continue
        for pid in state.front_provinces(f.id):
            own = [d for d in state.divisions_in(pid, f.id)]
            enemy = [
                d for n in t.neighbors(pid) if state.hostile_province(f.id, n)
                for d in (intel.known_divisions_in(state, f.id, n) if viewer else state.hostile_divisions_in(f.id, n))
            ]
            sectors.append(
                {
                    "faction": f.id,
                    "province": pid,
                    "name": t.provinces[pid].name,
                    "own_power": round(sum(d.combat_power(state.quality(d.faction)) for d in own)),
                    "enemy_power": round(sum(d.combat_power(state.quality(d.faction)) for d in enemy)),
                    "divisions": [d.id for d in own],
                }
            )
    battles = [
        {
            "id": b.id,
            "province": b.province,
            "name": t.provinces[b.province].name,
            "kind": b.kind,
            **_pt(state, battle_point(state, b)),
            "attacker": b.attacker_faction,
            "defender": b.defender_faction,
            "attackers": list(b.attackers),
            "defenders": list(b.defenders),
            "started": _time(b.started),
            "hours": round((state.time - b.started) / 3600, 1),
            "delegated": any(
                state.divisions[i].authority is not Authority.CAMPAIGN for i in b.attackers + b.defenders if i in state.divisions
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
        if viewer is None or not _enemy(state, viewer, f.faction) or _target_faction(state, f.target) == viewer
    ]
    return {"lines": lines, "sectors": sectors, "battles": battles, "fires": fires}


def _target_faction(state: CampaignState, entity_id: str) -> str | None:
    """Faction of a (possibly gone) entity.

    Args:
        state: The campaign.
        entity_id: The entity.

    Returns:
        Its faction, or None if it no longer exists.
    """
    e = state.entity(entity_id)
    return None if e is None else e.faction


def battle_point(state: CampaignState, b) -> Vec2:
    """Where the fighting is (see :func:`dynacamp.systems.combat.battle_point`).

    Args:
        state: The campaign.
        b: The battle.

    Returns:
        The point of the fighting.
    """
    from ..systems import combat

    return combat.battle_point(state, b)


# ---------------------------------------------------------------- forces


def _route(state: CampaignState, ent) -> dict[str, Any] | None:
    """Remaining route along the roads, from the entity's position.

    Args:
        state: The campaign.
        ent: The moving entity.

    Returns:
        The route, or None when it is not moving.
    """
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


def division_view(state: CampaignState, d: Division, contact=None) -> dict[str, Any]:
    """JSON view of a division (as seen through ``contact`` if given).

    Args:
        state: The campaign.
        d: The division.
        contact: The viewer's contact on it (None: the truth).

    Returns:
        The division view.
    """
    if contact is not None:
        det = contact.detail
        return {
            "id": d.id,
            "name": det.get("name", d.name),
            "faction": d.faction,
            "template": det.get("template", d.template.id),
            "template_name": det.get("template_name", d.template.name),
            "location": det.get("province", d.location),
            **_pt(state, contact.position),
            "strength": det.get("strength", 1.0),
            "org": None,
            "supply": None,
            "elements": {},
            "order": {"kind": UNKNOWN, "target": None},
            "route": None,
            "battle": d.battle if d.battle and not contact.stale(state.time) else None,
            "authority": d.authority.value,
            "moving": bool(det.get("moving")),
            "intel": _intel(state, contact),
        }
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


def forces(state: CampaignState, viewer: str | None = None) -> dict[str, Any]:
    """Divisions and battles (known enemies only in a faction's view).

    Args:
        state: The campaign.
        viewer: Faction whose view to give (None: the umpire's).

    Returns:
        The forces view.
    """
    divs = []
    for d in state.divisions.values():
        if not _enemy(state, viewer, d.faction):
            divs.append(division_view(state, d))
        elif (c := _contact(state, viewer, d.id)) is not None:
            divs.append(division_view(state, d, c))
    return {"divisions": divs, "battles": fronts(state, viewer)["battles"]}


# ---------------------------------------------------------------- logistics


def convoy_view(state: CampaignState, c: Convoy, contact=None) -> dict[str, Any]:
    """JSON view of a convoy (as seen through ``contact`` if given).

    Args:
        state: The campaign.
        c: The convoy.
        contact: The viewer's contact on it (None: the truth).

    Returns:
        The convoy view.
    """
    if contact is not None:
        return {
            "id": c.id,
            "faction": c.faction,
            **_pt(state, contact.position),
            "cargo": 0.0,
            "trucks": contact.detail.get("trucks", 0),
            "home": c.home,
            "destination": c.destination,
            "returning": c.returning,
            "heading_to": c.home if c.returning else c.destination,
            "route": None,
            "authority": c.authority.value,
            "intel": _intel(state, contact),
        }
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


def logistics(state: CampaignState, viewer: str | None = None) -> dict[str, Any]:
    """Depots, convoys, supply network reach and division supply (in a
    faction's view: enemy depots without stock, known enemy convoys only, and
    only its own network and brigades).

    Args:
        state: The campaign.
        viewer: Faction whose view to give (None: the umpire's).

    Returns:
        The logistics view.
    """
    t = state.theater
    mine = lambda fid: not _enemy(state, viewer, fid)  # noqa: E731
    depots = [
        {
            "province": pid,
            "name": p.name,
            **_pt(state, p.center),
            "controller": p.controller,
            "stock": round(p.depot.stock) if mine(p.controller) else None,
            "capacity": round(p.depot.capacity),
            "production_per_hour": p.depot.production_per_hour,
            "trucks": p.depot.trucks if mine(p.controller) else None,
        }
        for pid, p in t.provinces.items()
        if p.depot is not None
    ]
    network = {}
    for fid in state.factions:
        if not mine(fid):
            continue
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
        if mine(d.faction)
    ]
    convoys = []
    for c in state.convoys.values():
        if mine(c.faction):
            convoys.append(convoy_view(state, c))
        elif (k := _contact(state, viewer, c.id)) is not None:
            convoys.append(convoy_view(state, c, k))
    return {
        "depots": depots,
        "convoys": convoys,
        "network": network,
        "divisions": divisions,
        "direct_range_km": supply_sys.DIRECT_RANGE_M / 1000,
    }


# ---------------------------------------------------------------- ATO


def track_view(state: CampaignState, f, c) -> dict[str, Any]:
    """An enemy flight as a radar track: identified or not, at its last known position.

    Args:
        state: The campaign.
        f: The enemy flight.
        c: The viewer's contact on it.

    Returns:
        The flight view as a track (type only if identified).
    """
    det = c.detail
    ident = c.identified
    return {
        "id": f.id,
        "package": "",
        "package_number": "",
        "callsign": f.callsign if ident else f"TRACK {f.id[-4:].upper()}",
        "faction": f.faction,
        "squadron": "",
        "squadron_name": "",
        "airbase": "",
        "aircraft": det.get("aircraft", UNKNOWN),
        "aircraft_name": det.get("aircraft_name", "Unknown " + ("helicopter" if det.get("helo") else "aircraft")),
        "helo": bool(det.get("helo")),
        "task": f.task.value if ident else Task.CAP.value,
        "status": FlightStatus.AIRBORNE.value,
        "size": det.get("size", intel.CLASS_SIZE.get(det.get("size_class"), intel.CLASS_SIZE[intel.SizeClass.GROUP])),
        "ai": 0,
        "seats_free": 0,
        "players": [],
        "takeoff": _time(c.first_seen),
        "tot": _time(c.seen),
        **_pt(state, c.position),
        "waypoints": _line(state, [c.position]),
        "target_index": 0,
        "next_waypoint": 0,
        "target": {"kind": UNKNOWN, "id": ""},
        "locked_waypoints": 0,
        "fuel_km": 0,
        "authority": f.authority.value,
        "on_station_until": None,
        "intel": _intel(state, c),
    }


def insignia_url(sq) -> str:
    """URL path of a squadron's insignia, served with the web UI ("" = none).

    Args:
        sq: The squadron.

    Returns:
        The URL path, or ``""``.
    """
    return f"/insignia/{sq.insignia}" if sq.insignia else ""


def flight_view(state: CampaignState, f) -> dict[str, Any]:
    """JSON view of a flight.

    Args:
        state: The campaign.
        f: The flight (live or archived).

    Returns:
        The flight view.
    """
    from ..systems import orders

    sq = state.squadrons[f.squadron]
    return {
        "id": f.id,
        "package": f.package,
        "package_number": p.number if (p := state.packages.get(f.package) or state.archive.get(f.package)) else "",
        "callsign": f.callsign,
        "faction": f.faction,
        "squadron": sq.id,
        "squadron_name": sq.name,
        "squadron_insignia": insignia_url(sq),
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
        "locked_waypoints": orders.locked_waypoints(f),
        "fuel_km": round(orders.fuel_limit_m(state, f) / 1000),
        "authority": f.authority.value,
        "on_station_until": _time(f.station_until) if f.task.orbits else None,
    }


def package_view(state: CampaignState, p) -> dict[str, Any]:
    """JSON view of a package and its flights (live or archived).

    Args:
        state: The campaign.
        p: The package (live or archived).

    Returns:
        The package view.
    """
    from ..systems import orders

    archived = p.id not in state.packages
    flights = state.archived_flights if archived else state.flights
    return {
        "id": p.id,
        "number": p.number,
        "faction": p.faction,
        "task": p.task.value,
        "target": p.target_label,
        **_pt(state, p.target_point),
        "tot": _time(p.tot),
        "earliest_tot": _time(p.tot if archived else orders.earliest_tot(state, p)),
        "created": _time(p.created),
        "archived": archived,
        "flights": [flight_view(state, flights[i]) for i in p.flights if i in flights],
    }


def find_package(state: CampaignState, package_id: str = "", flight_id: str = "", viewer: str | None = None):
    """A live or archived package, by its id or one of its flights' (None if
    unknown or, in ``viewer``'s view, an enemy's).

    Args:
        state: The campaign.
        package_id: The package id ("" when looking up by flight).
        flight_id: Id of a flight of the package ("" when looking up by package).
        viewer: Faction whose view is given (None: the umpire's).

    Returns:
        The package, or None.
    """
    if flight_id:
        f = state.flights.get(flight_id) or state.archived_flights.get(flight_id)
        package_id = f.package if f is not None else ""
    p = state.packages.get(package_id) or state.archive.get(package_id)
    if p is None or _enemy(state, viewer, p.faction):
        return None
    return p


def ato(state: CampaignState, viewer: str | None = None) -> dict[str, Any]:
    """Packages, squadrons, sortie budget and in-game join boards. In a
    faction's view: only its own (and allies') air tasking, plus the enemy
    flights it has detected as ``tracks``.

    Args:
        state: The campaign.
        viewer: Faction whose view to give (None: the umpire's).

    Returns:
        The ATO view.
    """
    mine = lambda fid: not _enemy(state, viewer, fid)  # noqa: E731
    packages = [
        package_view(state, p) for p in sorted(state.packages.values(), key=lambda p: (p.tot, p.id)) if mine(p.faction)
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
            "insignia": insignia_url(s),
            "servicing": sum(n for ready, n in s.servicing if ready > state.time),
        }
        for s in state.squadrons.values()
        if mine(s.faction)
    ]
    budget = {f: ato_sys.sortie_budget_left(state, f) for f in state.factions if mine(f)}
    tracks = []
    if viewer is not None:
        for f in state.flights.values():
            if _enemy(state, viewer, f.faction) and (c := _contact(state, viewer, f.id)) is not None:
                tracks.append(track_view(state, f, c))
    airborne = sum(
        1 for f in state.flights.values()
        if mine(f.faction) and f.status not in (FlightStatus.SCHEDULED, FlightStatus.DONE, FlightStatus.CANCELLED)
    )
    coalitions = sorted({f.coalition for f in state.factions.values() if mine(f.id)})
    return {
        "packages": packages,
        "squadrons": squadrons,
        "sortie_budget_left": budget,
        "flights_airborne": airborne,
        "boards": {c: ato_sys.ato_board(state, c) for c in coalitions},
        "tracks": tracks,
    }


# ---------------------------------------------------------------- air defence


def air_defense(state: CampaignState, viewer: str | None = None) -> dict[str, Any]:
    """SAM sites and EWRs with their state, and IADS links (known enemy sites
    only, without their internals, in a faction's view).

    Args:
        state: The campaign.
        viewer: Faction whose view to give (None: the umpire's).

    Returns:
        The air defence view.
    """
    from ..systems import air_defense as ada

    t = state.theater
    sites, links = [], []
    for u in state.air_defense.values():
        if _enemy(state, viewer, u.faction):
            c = _contact(state, viewer, u.id)
            if c is None:
                continue
            det = c.detail
            sites.append(
                {
                    "id": u.id,
                    "name": det.get("name", u.name),
                    "faction": u.faction,
                    "system": u.system.id,
                    "system_name": u.system.name,
                    "role": u.system.role.value,
                    "status": det.get("status", u.status.value),
                    **_pt(state, c.position),
                    "location": det.get("province", u.location),
                    "range_km": round(u.system.range_m / 1000, 1),
                    "detection_km": round(u.system.detection_m / 1000, 1),
                    "ceiling_m": round(u.system.ceiling_m),
                    "missiles": None,
                    "missiles_max": u.system.missiles,
                    "elements": {},
                    "emitting": u.emitting and c.age(state.time) <= 120,
                    "networked_to": None,
                    "route": None,
                    "authority": u.authority.value,
                    "intel": _intel(state, c),
                }
            )
            continue
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


def visible_event(state: CampaignState, viewer: str | None, e) -> bool:
    """True if ``viewer`` would know about event ``e``: its own and allies'
    events and the public ones (battles, captures, engagements, losses).

    Args:
        state: The campaign.
        viewer: Faction whose view is given (None: the umpire's, who sees everything).
        e: The event.

    Returns:
        Whether the event is visible.
    """
    if viewer is None or e.kind in PUBLIC:
        return True
    owner = e.data.get("faction")
    return owner is None or not state.at_war(viewer, owner)


def events(
    state: CampaignState, since: int = 0, kinds: set[EventKind] | None = None, limit: int = 500, viewer: str | None = None
) -> dict[str, Any]:
    """A page of events.

    Args:
        state: The campaign.
        since: First sequence number.
        kinds: Keep only these kinds.
        limit: Page size.
        viewer: Faction whose view to give (None = umpire).

    Returns:
        The events and the ``next`` sequence number to ask for.
    """
    out = []
    for e in state.events.since(max(0, since)):
        if kinds and e.kind not in kinds:
            continue
        if not visible_event(state, viewer, e):
            continue
        out.append({"seq": e.seq, "time": _time(e.time), "kind": e.kind, "data": e.data})
        if len(out) >= limit:
            break
    next_seq = out[-1]["seq"] + 1 if out and len(out) >= limit else len(state.events)
    return {"events": out, "next": next_seq}


def snapshot(
    state: CampaignState, live: bool = False, players: list[dict] | None = None, viewer: str | None = None,
    clock: dict | None = None,
) -> dict[str, Any]:
    """All views in one document (a faction's view if ``viewer`` is given).

    Args:
        state: The campaign.
        live: Whether a game is connected.
        players: The :func:`players` entries (None: none).
        viewer: Faction whose view to give (None: the umpire's).
        clock: The :func:`control` entry (None: not given).

    Returns:
        Campaign, map, fronts, forces, logistics, ATO, air defence and events.
    """
    return {
        "campaign": campaign(state, live, players, clock),
        "map": map_view(state),
        "fronts": fronts(state, viewer),
        "forces": forces(state, viewer),
        "logistics": logistics(state, viewer),
        "ato": ato(state, viewer),
        "air_defense": air_defense(state, viewer),
    }


def save_view(saver) -> dict[str, Any]:
    """Saving status of a :class:`~dynacamp.persistence.Saver` (None = saving disabled).

    Args:
        saver: The saver, or None.

    Returns:
        Whether saving is on, the file, the autosave interval and the last save.
    """
    if saver is None:
        return {"enabled": False, "file": None, "autosave_minutes": None, "last": None}
    last = saver.last
    return {
        "enabled": True,
        "file": saver.path.name,
        "autosave_minutes": saver.autosave_s / 60,
        "last": None if last is None else {
            "saved_at": last.saved_at, "time": time_view(last.campaign_time), "trigger": last.trigger,
        },
    }


def control(engine) -> dict[str, Any]:
    """Pause and speed of an engine's clock.

    Args:
        engine: The engine.

    Returns:
        Pause, compression and the allowed compression range.
    """
    from ..engine import MAX_COMPRESSION, MIN_COMPRESSION

    c = engine.control
    return {
        "paused": c.paused,
        "compression": c.compression,
        "rate": engine.time_rate,
        "min_compression": MIN_COMPRESSION,
        "max_compression": MAX_COMPRESSION,
    }


def positions(state: CampaignState, viewer: str | None = None) -> dict[str, Any]:
    """Compact positions of everything that moves, for smooth map animation.

    ``{"t": time, "d": {...}, "c": {...}, "f": {...}, "s": {...}}``: brigades,
    convoys, flights (airborne, own, and enemy tracks in a faction's view) and
    air defence sites, each ``id -> [lat, lon]``. Enemy entities in a faction's
    view are at their last known position, and only if known.

    Args:
        state: The campaign.
        viewer: Faction whose view to give (None: the umpire's).

    Returns:
        The positions.
    """
    from ..air import ACTIVE_STATUSES

    t = state.theater
    pic = {} if viewer is None else _picture(state, viewer)

    def ll(p: Vec2) -> list[float] | None:
        """``[lat, lon]`` rounded to ~1 m.

        Args:
            p: The point.

        Returns:
            Latitude and longitude, or None.
        """
        v = to_latlon(t.name, p)
        return None if v is None else [round(v[0], 5), round(v[1], 5)]

    def put(out: dict, ent, pos_of) -> None:
        """Add ``ent`` with its (true or known) position.

        Args:
            out: The id -> position table to fill.
            ent: The entity.
            pos_of: Gives an entity's true position.
        """
        if _enemy(state, viewer, ent.faction):
            c = pic.get(ent.id)
            if c is None:
                return
            p = c.position
        else:
            p = pos_of(ent)
        v = ll(p)
        if v is not None:
            out[ent.id] = v

    out: dict[str, Any] = {"t": round(state.time, 1), "d": {}, "c": {}, "f": {}, "s": {}}
    for d in state.divisions.values():
        put(out["d"], d, lambda e: e.position(t))
    for c in state.convoys.values():
        put(out["c"], c, lambda e: e.position(t))
    for f in state.flights.values():
        if f.status in ACTIVE_STATUSES:
            put(out["f"], f, lambda e: e.position(t))
    for u in state.air_defense.values():
        put(out["s"], u, lambda e: e.position(t))
    return out


def _picture(state: CampaignState, viewer: str) -> dict:
    """The viewer's intelligence picture (entity id -> contact).

    Args:
        state: The campaign.
        viewer: Faction id.

    Returns:
        The picture.
    """

    return intel.picture(state, viewer)


def intel_view(state: CampaignState, viewer: str) -> dict[str, Any]:
    """A faction's intelligence picture and its air-search sensors.

    Args:
        state: The campaign.
        viewer: Faction id.

    Returns:
        The faction's contacts, counts by kind and its sensors.
    """
    from collections import Counter


    contacts = [
        {
            "id": c.entity_id,
            "kind": c.kind,
            "faction": c.faction,
            **_pt(state, c.position),
            "source": c.source,
            "seen": _time(c.seen),
            "first_seen": _time(c.first_seen),
            "age_s": round(c.age(state.time)),
            "stale": c.stale(state.time),
            "identified": c.identified,
            "detail": c.detail,
        }
        for c in sorted(intel.picture(state, viewer).values(), key=lambda c: (c.kind, c.entity_id))
    ]
    from .. import sensors as radar

    sensors = [
        {
            "kind": kind, "name": name, **_pt(state, pos), "range_km": round(rng / 1000, 1),
            "heading_deg": round(math.degrees(radar.track(carrier)) % 360) if carrier else None,
            "half_angle_deg": round(math.degrees(radar.SCAN_HALF_ANGLE_RAD)) if carrier else None,
        }
        for pos, rng, kind, name, _ident, carrier in intel.air_sensors(state, viewer)
    ]
    return {"faction": viewer, "contacts": contacts, "sensors": sensors, "counts": dict(Counter(c["kind"] for c in contacts))}
