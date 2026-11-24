"""Generate a ready-to-fly DCS mission (.miz) for a scenario.

The mission contains only what the live campaign needs from the mission editor:

* the Lua bridge, embedded and started by a MISSION START trigger;
* late-activated *template* groups ``DC_TPL_<aircraft>[_<TASK>]`` with default
  pydcs loadouts for each task, which the bridge copies when it spawns AI flights;
* client slots (cold start) for every flyable squadron, at its home airbase;
* a trigger zone per province (``DC <province id>``), handy as map reference.

Ground forces, convoys and AI flights are *not* placed: the engine spawns them
around the players at run time.

Requires the optional dependency ``pydcs`` (``uv sync --extra miz``).
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import warnings
from pathlib import Path

from ...air import Task
from ...state import CampaignState
from . import LUA_BRIDGE, catalog

with warnings.catch_warnings():
    warnings.simplefilter("ignore", SyntaxWarning)  # pydcs' generated weapon tables
    import dcs
    from dcs import action, triggers
    from dcs.mapping import Point
    from dcs.mission import Mission, StartType
    from dcs.terrain import Caucasus, Syria

logging.getLogger("dcs").setLevel(logging.ERROR)  # "no DCS installation found" noise

THEATERS = {"caucasus": Caucasus, "syria": Syria}

# ---------------------------------------------------------------- loadouts
# pydcs only knows default loadouts when a DCS installation is present, so we
# compose them from its per-pylon weapon tables: every pick is valid for that
# pylon by construction. A recipe is a list of (weapon name patterns in order of
# preference, number of pylons to fill). Pylons are filled outside-in.

_A2A_LONG = "AIM-120C|AIM-120B|R-77|R-27ER|R-27R|AIM-7M"
_A2A_SHORT = "AIM-9X|AIM-9M|R-73|R-60"
_TANK = "Fuel tank|Fuel Tank|PTB"
_POD = "LITENING|Sniper|AN/AAQ-28|Shkval|Kopyo"

RECIPES: dict[str, list[tuple[str, int]]] = {
    "A2A": [(_A2A_LONG, 4), (_A2A_SHORT, 2), (_TANK, 1)],
    "SEAD": [("AGM-88C|Kh-58U|Kh-25MPU", 2), (_A2A_LONG, 2), (_A2A_SHORT, 2), ("ALQ-184|ALQ-131", 1), (_TANK, 1)],
    "MUD": [
        ("AGM-65D|AGM-65H|AGM-65K|Kh-25ML|Kh-29L|S-25L", 2),
        ("CBU-97|CBU-87|Mk-20|RBK-500|RBK-250", 2),
        ("GBU-12|Mk-82|FAB-250|FAB-500", 2),
        (_A2A_SHORT, 2),
        ("ALQ-131|ALQ-184", 1),
        (_POD, 1),
        (_TANK, 1),
    ],
    "STRIKE": [("GBU-31|GBU-38|GBU-10|KAB-500|KAB-1500|Mk-84|FAB-1500|FAB-500", 4), (_A2A_SHORT, 2), (_POD, 1), (_TANK, 1)],
    "HELO": [("AGM-114K|AGM-114L|9M114|9M120|Vikhr", 4), ("S-8KOM|S-8OFP2|M151|S-5KO|Hydra", 4)],
}
# practice/inert stores, marker and illumination rockets, empty racks
_EXCLUDE = re.compile(r"practice|training|inert|dummy|smoke|empty|TsM|M257|M274|M156|illum", re.IGNORECASE)


def _recipe(task: Task, helo: bool) -> str:
    """Loadout recipe name for a task."""
    if helo:
        return "HELO"
    return {Task.CAP: "A2A", Task.ESCORT: "A2A", Task.SEAD: "SEAD", Task.STRIKE: "STRIKE"}.get(task, "MUD")


def compose_loadout(unit_type, recipe: str) -> list[tuple[int, dict]]:
    """Pick weapons for every pylon from pydcs' pylon tables, following a recipe.

    Args:
        unit_type: pydcs aircraft type.
        recipe: Key of ``RECIPES``.

    Returns:
        (pylon, weapon) pairs, valid by construction.
    """
    pylons = sorted(getattr(unit_type, "pylons", []) or [])
    if not pylons:
        return []
    center = (pylons[0] + pylons[-1]) / 2
    order = sorted(pylons, key=lambda n: (-abs(n - center), n))
    options = {}
    for n in pylons:
        cls = getattr(unit_type, f"Pylon{n}", None)
        options[n] = [v for k, v in vars(cls).items() if not k.startswith("_")] if cls else []
    used: set[int] = set()
    picks: list[tuple[int, dict]] = []
    for patterns, count in RECIPES[recipe]:
        taken = 0
        for pat in patterns.split("|"):
            for n in order:
                if taken >= count:
                    break
                if n in used:
                    continue
                for pylon, weapon in options[n]:
                    name = weapon.get("name", "")
                    if pat.lower() in name.lower() and not _EXCLUDE.search(name):
                        picks.append((pylon, weapon))
                        used.add(n)
                        taken += 1
                        break
            if taken >= count:
                break
    return picks


def apply_loadout(fg, unit_type, task: Task, helo: bool) -> int:
    """Load a composed loadout on a flight group; returns the number of stores."""
    picks = compose_loadout(unit_type, _recipe(task, helo))
    for weapon in picks:
        fg.load_pylon(weapon)
    return len(picks)

# campaign task -> pydcs main task (also selects the default loadout)
MAIN_TASK = {
    Task.CAP: dcs.task.CAP,
    Task.ESCORT: dcs.task.Escort,
    Task.SEAD: dcs.task.SEAD,
    Task.CAS: dcs.task.CAS,
    Task.BAI: dcs.task.CAS,
    Task.STRIKE: dcs.task.GroundAttack,
}

COUNTRY_NAME = {"blue": "USA", "red": "Russia"}


def _unit_type(dcs_id: str):
    """pydcs aircraft or helicopter type by DCS type name."""
    return dcs.planes.plane_map.get(dcs_id) or dcs.helicopters.helicopter_map.get(dcs_id)


def build_mission(
    state: CampaignState,
    port: int = 50555,
    date: dt.date = dt.date(2024, 6, 1),
    clients_per_squadron: int = 4,
) -> tuple[Mission, list[str]]:
    """Return the mission and a list of warnings (things that could not be placed)."""
    theater = state.theater.name.lower()
    if theater not in THEATERS:
        raise ValueError(f"no DCS terrain for theater {state.theater.name!r} (known: {sorted(THEATERS)})")
    terrain = THEATERS[theater]()
    m = Mission(terrain)
    notes: list[str] = []

    start = dt.datetime.combine(date, dt.time()) + dt.timedelta(seconds=state.time)
    m.start_time = start

    countries = {side: m.country(name) for side, name in COUNTRY_NAME.items()}
    for side, name in COUNTRY_NAME.items():
        if countries[side].id != catalog.COUNTRY[side]:
            notes.append(f"country id mismatch for {side}: {countries[side].id} vs catalog {catalog.COUNTRY[side]}")

    def point(v) -> Point:
        """Campaign point -> pydcs point."""
        return Point(v.y, v.x, terrain)  # campaign (east, north) -> DCS (x north, y east)

    # -------------------------------------------------- province reference zones
    for p in state.theater.provinces.values():
        m.triggers.add_triggerzone(point(p.center), radius=3000, hidden=False, name=f"DC {p.id}")

    # -------------------------------------------------- templates
    done: set[str] = set()
    for sq in state.squadrons.values():
        side = state.factions[sq.faction].side
        ac = sq.aircraft_type
        unit_type = _unit_type(catalog.AIRCRAFT_TYPES[ac.id])
        if unit_type is None:
            notes.append(f"{ac.id}: DCS type {catalog.AIRCRAFT_TYPES[ac.id]!r} unknown to pydcs")
            continue
        base = state.airbases[sq.airbase]
        tasks = sorted(sq.tasks, key=lambda t: t.value)
        for task in [None, *tasks]:
            name = f"DC_TPL_{ac.id}" + (f"_{task.value}" if task else "")
            if name in done:
                continue
            done.add(name)
            main = MAIN_TASK[task or tasks[0]]
            fg = m.flight_group_inflight(
                countries[side], name, unit_type, point(base.position), altitude=int(ac.cruise_alt_m),
                maintask=main, group_size=4,
            )
            fg.late_activation = True
            if not apply_loadout(fg, unit_type, task or tasks[0], ac.helo):
                notes.append(f"{name}: could not compose a loadout, set one in the mission editor")

    # -------------------------------------------------- client slots
    for sq in state.squadrons.values():
        side = state.factions[sq.faction].side
        unit_type = _unit_type(catalog.AIRCRAFT_TYPES[sq.aircraft_type.id])
        if unit_type is None or not unit_type.flyable or clients_per_squadron <= 0:
            continue
        base = state.airbases[sq.airbase]
        airport = terrain.airports.get(base.name)
        if airport is None:
            notes.append(f"{sq.name}: airbase {base.name!r} not found on {terrain.name}")
            continue
        main = MAIN_TASK[sorted(sq.tasks, key=lambda t: t.value)[0]]
        size = clients_per_squadron
        while size > 0:
            try:
                fg = m.flight_group_from_airport(
                    countries[side], f"{sq.callsign} Clients", unit_type, airport,
                    maintask=main, start_type=StartType.Cold, group_size=size,
                )
                break
            except Exception:  # noqa: BLE001 - pydcs raises plain errors when parking is full
                size -= 1
        else:
            notes.append(f"{sq.name}: no parking for client slots at {base.name}")
            continue
        if size < clients_per_squadron:
            notes.append(f"{sq.name}: only {size} client slots fit at {base.name}")
        for u in fg.units:
            u.set_client()
        apply_loadout(fg, unit_type, sorted(sq.tasks, key=lambda t: t.value)[0], sq.aircraft_type.helo)

    # -------------------------------------------------- bridge
    trig = triggers.TriggerStart(comment="DynaCamp bridge")
    trig.add_action(action.DoScript(m.string(f"DYNACAMP_HOST = '127.0.0.1'\nDYNACAMP_PORT = {port}\n")))
    trig.add_action(action.DoScriptFile(m.map_resource.add_resource_file(str(LUA_BRIDGE))))
    m.triggerrules.triggers.append(trig)

    m.set_description_text(
        f"{state.name} - DynaCamp live campaign.\n\n"
        "The campaign runs in the DynaCamp engine: start `dynacamp serve` before the mission.\n"
        "Requires a desanitised MissionScripting.lua (LuaSocket).\n\n"
        "Pick a slot, then F10 > Other > DynaCamp ATO to join a flight."
    )
    for side, setter in (("blue", m.set_description_bluetask_text), ("red", m.set_description_redtask_text)):
        f = state.faction_for_side(side)
        if f is not None:
            objectives = ", ".join(state.theater.provinces[o.province].name for o in f.objectives)
            setter(f"{f.name}. Objectives: {objectives or 'hold the line'}.")
    return m, notes


def write_mission(state: CampaignState, path: Path, **kw) -> list[str]:
    """Build and save the mission of a scenario.

    Args:
        state: The campaign.
        path: Output ``.miz``.
        **kw: Passed to :func:`build_mission`.

    Returns:
        Notes about things that could not be placed.
    """
    m, notes = build_mission(state, **kw)
    path.parent.mkdir(parents=True, exist_ok=True)
    m.save(str(path))
    return notes
