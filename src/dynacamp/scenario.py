"""Load a campaign from a TOML scenario file."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from .adapters.base import Side
from .air import AIRCRAFT, Airbase, Squadron, Task
from .air_defense import SYSTEMS, AirDefenseUnit, place_sites
from .forces import DEFAULT_TEMPLATES, Division, DivisionTemplate
from .geo import Vec2
from .projection import from_latlon
from .state import CampaignState, Faction, Objective
from .world import Depot, LinkKind, Province, Terrain, Theater
from . import bullseye, units

#: Theatre of a scenario that names none (no projection: plain plane coordinates).
UNNAMED_THEATER = "unnamed"


class ScenarioError(ValueError):
    """Raised when a scenario file is inconsistent."""
    pass


def load(path: str | Path, with_roads: bool = True) -> CampaignState:
    """Load a scenario; road geometry comes from ``<scenario>.roads.json`` if present.

    Args:
        path: Path of the scenario TOML.
        with_roads: Read the road geometry file (False: straight links).

    Returns:
        The initial campaign state.
    """
    from . import roads

    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    theater = data.get("campaign", {}).get("theater", UNNAMED_THEATER)
    return from_dict(data, roads.load(path, theater) if with_roads else None)


def point(spec: dict[str, Any], theater: str, what: str) -> Vec2:
    """The position a scenario gives for something, on the theatre plane.

    Scenario files give positions as ``ll = [lat, lon]`` (WGS84 degrees),
    converted with the theatre's projection (:mod:`dynacamp.projection`). Only
    a theatre without a projection (test maps) takes ``pos = [east, north]``
    metres on the plane instead.

    Args:
        spec: The table holding ``ll`` or ``pos``.
        theater: The theatre's name.
        what: What it is, for error messages (e.g. ``"province aleppo"``).

    Returns:
        The position on the theatre plane.

    Raises:
        ScenarioError: Missing or malformed coordinates, or ``ll`` on a theatre
            without a projection.
    """
    def pair(key: str) -> tuple[float, float]:
        """The two numbers of ``spec[key]``.

        Args:
            key: ``"ll"`` or ``"pos"``.

        Returns:
            The two numbers as floats.
        """
        v = spec[key]
        if not (isinstance(v, list) and len(v) == 2 and all(isinstance(x, (int, float)) for x in v)):
            raise ScenarioError(f"{what}: {key} must be two numbers, not {v!r}")
        return float(v[0]), float(v[1])

    if "ll" in spec:
        lat, lon = pair("ll")
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ScenarioError(f"{what}: ll = [{lat}, {lon}] is not [lat, lon]")
        v = from_latlon(theater, lat, lon)
        if v is None:
            raise ScenarioError(f"{what}: theater {theater!r} has no projection for lat/lon; give pos = [east, north]")
        return v
    if "pos" in spec:
        return Vec2(*pair("pos"))
    raise ScenarioError(f"{what}: no position (ll = [lat, lon])")


def _side(value: str, faction_id: str) -> Side:
    """A faction's game side, checked.

    Args:
        value: The side named in the scenario (default: the faction id).
        faction_id: The faction, for the error message.

    Returns:
        The side.

    Raises:
        ScenarioError: If it is not a game side.
    """
    if value not in Side:
        raise ScenarioError(f"faction {faction_id}: side {value!r} is not one of {', '.join(Side)}")
    return Side(value)


def _set_bullseyes(specs: list[dict[str, Any]], factions: dict[str, Faction], theater: Theater) -> None:
    """Give every faction its bullseye (see :mod:`dynacamp.bullseye`).

    ``bullseye`` in a ``[[factions]]`` table is a province id (its centre) or
    a point, ``{ ll = [lat, lon] }`` (``{ pos = [east, north] }`` on a theatre
    without projection). Without it a faction takes its side's: the one another faction
    of the side gives, else the centre of the provinces the side holds.

    Args:
        specs: The ``[[factions]]`` tables of the scenario.
        factions: The factions built from them, updated in place.
        theater: The map (province centres).

    Raises:
        ScenarioError: Unknown province, malformed point, or two factions on the
            same side with different bullseyes (the game has one per side).
    """
    given: dict[str, tuple[Vec2, str]] = {}  # side -> (point, faction that set it)
    for spec in specs:
        raw = spec.get("bullseye")

        if raw is None:
            continue

        fid = spec["id"]

        if isinstance(raw, str):
            if raw not in theater.provinces:
                raise ScenarioError(f"faction {fid}: bullseye {raw!r} is not a province")
            where = theater.provinces[raw].center

        elif isinstance(raw, dict):
            where = point(raw, theater.name, f"faction {fid} bullseye")

        else:
            raise ScenarioError(f"faction {fid}: bullseye must be a province id or {{ ll = [lat, lon] }}, not {raw!r}")

        side = factions[fid].side
        if side in given and given[side][0] != where:
            raise ScenarioError(
                f"faction {fid}: bullseye differs from {given[side][1]}'s, on the same side {side!r} (DCS has one per side)"
            )

        given[side] = (where, fid)

    for f in factions.values():
        if f.side in given:
            f.bullseye = given[f.side][0]
            continue

        ids = {o.id for o in factions.values() if o.side == f.side}
        held = [p.center for p in theater.provinces.values() if p.controller in ids]
        f.bullseye = bullseye.centre(held or [p.center for p in theater.provinces.values()] or [Vec2(0.0, 0.0)])


def from_dict(data: dict[str, Any], roads: dict[str, list[Vec2]] | None = None) -> CampaignState:
    """Build a campaign from parsed scenario data.

    Args:
        data: Scenario as a dict (the TOML document).
        roads: Optional road polylines keyed ``"a|b"``.

    Returns:
        The initial campaign state.

    Raises:
        ScenarioError: On unknown references (factions, provinces, templates...).
    """
    meta = data.get("campaign", {})
    theater = Theater(meta.get("theater", UNNAMED_THEATER))

    factions: dict[str, Faction] = {}
    for f in data.get("factions", []):
        faction_id = f["id"]

        factions[faction_id] = Faction(
            id=faction_id,
            name=f.get("name", faction_id),
            side=_side(f.get("side", faction_id), faction_id),
            enemies=set(f.get("enemies", [])),
            objectives=[
                Objective(
                    o["province"],
                    o.get("priority", 1)) for o in f.get("objectives", [])
            ],
            bullseye=Vec2(0.0, 0.0),  # set below, once the provinces are known
        )

    for p in data.get("provinces", []):
        if p["controller"] not in factions:
            raise ScenarioError(f"province {p['id']}: unknown controller {p['controller']!r}")

        depot = p.get("depot")
        theater.add_province(
            Province(
                id=p["id"],
                name=p.get("name", p["id"]),
                center=point(p, theater.name, f"province {p['id']}"),
                terrain=Terrain(p.get("terrain", Terrain.PLAINS)),
                controller=p["controller"],
                owner=p.get("owner", ""),
                infrastructure=p.get("infrastructure", 2),
                victory_points=p.get("victory_points", 0),
                depot=Depot(**depot) if depot else None,
            )
        )

    _set_bullseyes(data.get("factions", []), factions, theater)

    for link in data.get("links", []):
        for end in (link["a"], link["b"]):
            if end not in theater.provinces:
                raise ScenarioError(f"link {link['a']}-{link['b']}: unknown province {end!r}")
        route = None
        if roads:
            route = roads.get(f"{link['a']}|{link['b']}")
            if route is None and f"{link['b']}|{link['a']}" in roads:
                route = roads[f"{link['b']}|{link['a']}"][::-1]
        theater.connect(
            link["a"],
            link["b"],
            kind=LinkKind(link.get("kind", LinkKind.ROAD)),
            river=link.get("river", False),
            length=link.get("length"),
            route=route,
        )

    for f in factions.values():
        for obj in f.objectives:
            if obj.province not in theater.provinces:
                raise ScenarioError(f"faction {f.id}: objective on unknown province {obj.province!r}")

    templates = dict(DEFAULT_TEMPLATES)
    for t in data.get("templates", []):
        templates[t["id"]] = DivisionTemplate(
            id=t["id"],
            name=t.get("name", t["id"]),
            elements=dict(t["elements"]),
            speed_kmh=t.get("speed_kmh", 30.0),
            supply_capacity=t.get("supply_capacity", 72.0),
            supply_use_per_hour=t.get("supply_use_per_hour", 1.0),
        )

    state = CampaignState(
        name=meta.get("name", "campaign"),
        theater=theater,
        factions=factions,
        seed=meta.get("seed", 0),
        time=float(meta.get("start_hour", 6)) * 3600.0,
        units=units.check(meta.get("units", units.DEFAULT_UNITS)),
    )
    for d in data.get("divisions", []):
        if d["template"] not in templates:
            raise ScenarioError(f"division {d['id']}: unknown template {d['template']!r}")
        if d["province"] not in theater.provinces:
            raise ScenarioError(f"division {d['id']}: unknown province {d['province']!r}")
        state.divisions[d["id"]] = Division(
            id=d["id"],
            name=d.get("name", d["id"]),
            faction=d["faction"],
            location=d["province"],
            template=templates[d["template"]],
        )

    for b in data.get("airbases", []):
        if b.get("province") and b["province"] not in theater.provinces:
            raise ScenarioError(f"airbase {b['id']}: unknown province {b['province']!r}")
        if not b.get("province") and b.get("faction") not in factions:
            raise ScenarioError(f"airbase {b['id']}: needs a province or a known faction")
        state.airbases[b["id"]] = Airbase(
            id=b["id"],
            name=b.get("name", b["id"]),
            position=point(b, theater.name, f"airbase {b['id']}"),
            province=b.get("province"),
            faction=b.get("faction"),
        )

    for q in data.get("squadrons", []):
        if q["airbase"] not in state.airbases:
            raise ScenarioError(f"squadron {q['id']}: unknown airbase {q['airbase']!r}")
        if q["aircraft"] not in AIRCRAFT:
            raise ScenarioError(f"squadron {q['id']}: unknown aircraft {q['aircraft']!r} (known: {sorted(AIRCRAFT)})")
        if q["faction"] not in factions:
            raise ScenarioError(f"squadron {q['id']}: unknown faction {q['faction']!r}")
        state.squadrons[q["id"]] = Squadron(
            id=q["id"],
            name=q.get("name", q["id"]),
            faction=q["faction"],
            airbase=q["airbase"],
            aircraft_type=AIRCRAFT[q["aircraft"]],
            aircraft=q.get("count", 12),
            callsign=q.get("callsign", q["id"].capitalize()),
            tasks=frozenset(Task(x) for x in q.get("tasks", [])),
            insignia=q.get("insignia", ""),
        )

    for a in data.get("air_defense", []):
        if a["system"] not in SYSTEMS:
            raise ScenarioError(f"air defence {a['id']}: unknown system {a['system']!r} (known: {sorted(SYSTEMS)})")
        if a["province"] not in theater.provinces:
            raise ScenarioError(f"air defence {a['id']}: unknown province {a['province']!r}")
        if a["faction"] not in factions:
            raise ScenarioError(f"air defence {a['id']}: unknown faction {a['faction']!r}")
        system = SYSTEMS[a["system"]]
        state.air_defense[a["id"]] = AirDefenseUnit(
            id=a["id"],
            name=a.get("name", f"{system.name} {a['id']}"),
            faction=a["faction"],
            location=a["province"],
            system=system,
        )
    place_sites(state.air_defense.values(), theater)
    return state
