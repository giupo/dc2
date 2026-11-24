"""Load a campaign from a TOML scenario file."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from .air import AIRCRAFT, Airbase, Squadron, Task
from .air_defense import SYSTEMS, AirDefenseUnit
from .forces import DEFAULT_TEMPLATES, Division, DivisionTemplate
from .geo import Vec2
from .state import CampaignState, Faction, Objective
from .world import Depot, LinkKind, Province, Terrain, Theater
from . import units


class ScenarioError(ValueError):
    """Raised when a scenario file is inconsistent."""
    pass


def load(path: str | Path, with_roads: bool = True) -> CampaignState:
    """Load a scenario; road geometry comes from ``<scenario>.roads.json`` if present."""
    from . import roads

    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    return from_dict(data, roads.load(path) if with_roads else None)


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
    theater = Theater(meta.get("theater", "unnamed"))

    factions: dict[str, Faction] = {}
    for f in data.get("factions", []):
        factions[f["id"]] = Faction(
            id=f["id"],
            name=f.get("name", f["id"]),
            side=f.get("side", f["id"]),
            enemies=set(f.get("enemies", [])),
            objectives=[Objective(o["province"], o.get("priority", 1)) for o in f.get("objectives", [])],
        )

    for p in data.get("provinces", []):
        if p["controller"] not in factions:
            raise ScenarioError(f"province {p['id']}: unknown controller {p['controller']!r}")
        depot = p.get("depot")
        theater.add_province(
            Province(
                id=p["id"],
                name=p.get("name", p["id"]),
                center=Vec2(*p["pos"]),
                terrain=Terrain(p.get("terrain", "plains")),
                controller=p["controller"],
                owner=p.get("owner", ""),
                infrastructure=p.get("infrastructure", 2),
                victory_points=p.get("victory_points", 0),
                depot=Depot(**depot) if depot else None,
            )
        )

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
            kind=LinkKind(link.get("kind", "road")),
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
            position=Vec2(*b["pos"]),
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
    return state
