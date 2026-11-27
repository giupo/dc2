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
from enum import StrEnum
import logging
import re
import warnings
from pathlib import Path

from ...adapters.base import Side
from ...air import Task
from ...projection import TheaterMap
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

#: pydcs terrain class of each supported theatre.
THEATERS = {TheaterMap.CAUCASUS: Caucasus, TheaterMap.SYRIA: Syria}

# ---------------------------------------------------------------- loadouts
# pydcs only knows default loadouts when a DCS installation is present, so we
# compose them from its per-pylon weapon tables: every pick is valid for that
# pylon by construction. The recipes (weapon name patterns in order of
# preference, number of pylons to fill) are DCS data: adapters/dcs/data/recipes.toml.
# Pylons are filled outside-in.


class Recipe(StrEnum):
    """Kind of loadout composed for a flight."""
    #: Air-to-air missiles.
    A2A = "A2A"
    #: Anti-radiation missiles and a jammer.
    SEAD = "SEAD"
    #: Ground attack.
    MUD = "MUD"
    #: Heavy bombs on fixed targets.
    STRIKE = "STRIKE"
    #: Heavy bombers: every station.
    BOMBER = "BOMBER"
    #: Attack helicopters.
    HELO = "HELO"


#: ``adapters/dcs/data/recipes.toml``.
_RECIPES = catalog.data("recipes")
#: Loadouts composed from DCS' pylon tables, by recipe: (store name pattern, pylons to fill),
#: from ``adapters/dcs/data/recipes.toml``.
RECIPES: dict[Recipe, list[tuple[str, int]]] = {
    Recipe(r): [(_RECIPES["patterns"][pattern], int(n)) for pattern, n in steps]
    for r, steps in _RECIPES["recipes"].items()
}
#: Recipe of each task (the others: :attr:`Recipe.MUD`).
TASK_RECIPE = {Task.CAP: Recipe.A2A, Task.ESCORT: Recipe.A2A, Task.SEAD: Recipe.SEAD, Task.STRIKE: Recipe.STRIKE}
#: Aircraft ids loaded with the :attr:`Recipe.BOMBER` recipe.
BOMBERS = frozenset(_RECIPES["bombers"])
#: pydcs main task of a mission group (template or player slot), by campaign task; it also
#: selects the default loadout.
MAIN_TASK = {
    Task.CAP: dcs.task.CAP,
    Task.ESCORT: dcs.task.Escort,
    Task.SEAD: dcs.task.SEAD,
    Task.CAS: dcs.task.CAS,
    Task.BAI: dcs.task.CAS,
    Task.STRIKE: dcs.task.GroundAttack,
    Task.AWACS: dcs.task.AWACS,
    Task.TANKER: dcs.task.Refueling,
    Task.TRANSPORT: dcs.task.Transport,
}
#: DCS country of each side in the generated missions.
COUNTRY_NAME = {Side.BLUE: "USA", Side.RED: "Russia"}
#: Stores never picked for a loadout (training rounds, smoke...), from ``recipes.toml``.
_EXCLUDE = re.compile(_RECIPES["exclude"], re.IGNORECASE)


def _recipe(task: Task, helo: bool, aircraft: str = "") -> Recipe:
    """Loadout recipe name for an aircraft and task.

    Args:
        task: The flight's task.
        helo: Whether the aircraft is a helicopter.
        aircraft: Campaign aircraft id (bombers get their own recipe).

    Returns:
        The recipe.
    """
    if helo:
        return Recipe.HELO
    if aircraft in BOMBERS:
        return Recipe.BOMBER
    return TASK_RECIPE.get(task, Recipe.MUD)


def compose_loadout(unit_type, recipe: Recipe) -> list[tuple[int, dict]]:
    """Pick weapons for every pylon from pydcs' pylon tables, following a recipe.

    Args:
        unit_type: pydcs aircraft type.
        recipe: The kind of loadout.

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


def apply_loadout(fg, unit_type, task: Task, helo: bool, aircraft: str = "") -> int:
    """Load a flight group with the loadout of :mod:`dynacamp.loadouts` (the one
    players are briefed with), or compose one if the file has none.

    Args:
        fg: The pydcs flight group to arm.
        unit_type: pydcs type of the aircraft, used when a loadout has to be composed.
        task: The flight's task.
        helo: Whether the aircraft is a helicopter.
        aircraft: Campaign aircraft id whose briefed loadout to use ("" = compose one).

    Returns:
        The number of stores loaded.
    """
    from ... import loadouts

    briefed = loadouts.stores(aircraft, task) if aircraft else []
    if briefed:
        picks = [(s["pylon"], {"clsid": s["clsid"], "name": s["name"], "weight": s.get("weight", 0.0)}) for s in briefed]
    else:
        picks = compose_loadout(unit_type, _recipe(task, helo, aircraft))
    for weapon in picks:
        fg.load_pylon(weapon)
    return len(picks)


def generate_radio_data(path: Path | None = None) -> Path:
    """Write the ATC frequencies of every airfield of every theatre used by the
    scenarios, read by :mod:`dynacamp.radio`.

    Args:
        path: Output file (default: the package's ``data/radio.json``).

    Returns:
        The written path.
    """
    import json

    from ... import radio

    out: dict[str, list[dict]] = {}
    for name, terrain_cls in THEATERS.items():
        terrain = terrain_cls()
        bases = []
        for ap in sorted(terrain.airports.values(), key=lambda a: a.name):
            r = ap.atc_radio
            if r is None:
                continue
            mhz = sorted({round(hz / 1e6, 3) for hz in (r.hf_hz, r.vhf_low_hz, r.vhf_high_hz, r.uhf_hz) if hz})
            bases.append({"name": ap.name, "mhz": mhz})
        out[name] = bases
    path = path or radio.DATA
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"source": "pydcs airport atc_radio", "theaters": out}, indent=1) + "\n", encoding="utf-8")
    return path


def safe_group_frequency(theater: str, mhz: float) -> float:
    """A group radio frequency near ``mhz`` (same band) clear of the theatre's
    airfields and guard: ``mhz`` itself if already clear, else the next clear
    channel above it.

    Args:
        theater: Theatre name, lower case, as keyed in ``data/radio.json``.
        mhz: The wanted frequency in MHz.

    Returns:
        The clear frequency in MHz (``mhz`` unchanged if no clear channel is found within 400
        steps).
    """
    from ... import radio

    taken = radio.reserved(theater)
    f = round(mhz, 3)
    for _ in range(400):
        if radio.clear(f, taken):
            return f
        f = round(f + radio.STEP_MHZ, 3)
    return mhz


def generate_loadouts(path: Path | None = None) -> Path:
    """Compose every aircraft/task loadout from DCS' pylon tables and write the
    loadouts file read by :mod:`dynacamp.loadouts`.

    Args:
        path: Output file (default: the package's ``data/loadouts.json``).

    Returns:
        The written path.
    """
    import json

    from ... import loadouts
    from ...air import AIRCRAFT

    out: dict[str, dict[str, list[dict]]] = {}
    for ac in AIRCRAFT.values():
        unit_type = _unit_type(catalog.AIRCRAFT_TYPES[ac.id])
        if unit_type is None:
            continue
        for task in sorted(ac.tasks, key=lambda t: t.value):
            if task.support:
                continue  # AWACS, tankers and transports carry no ordnance
            picks = compose_loadout(unit_type, _recipe(task, ac.helo, ac.id))
            out.setdefault(ac.id, {})[task.value] = [
                {
                    "pylon": p,
                    "name": w["name"],
                    "clsid": w["clsid"],
                    "weight": round(float(w.get("weight") or 0.0), 1),
                    **catalog.classify_store(w["name"]),
                }
                for p, w in sorted(picks, key=lambda x: x[0])
            ]
    path = path or loadouts.DATA
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"source": "DCS pylon tables via pydcs", "aircraft": out}, indent=1) + "\n", encoding="utf-8")
    loadouts._data.cache_clear()
    return path


def _unit_type(dcs_id: str):
    """pydcs aircraft or helicopter type by DCS type name.

    Args:
        dcs_id: DCS type name of the aircraft.

    Returns:
        The pydcs plane or helicopter type, or None if pydcs does not know it.
    """
    return dcs.planes.plane_map.get(dcs_id) or dcs.helicopters.helicopter_map.get(dcs_id)


def build_mission(
    state: CampaignState,
    port: int = 50555,
    date: dt.date = dt.date(2024, 6, 1),
    clients_per_squadron: int = 4,
) -> tuple[Mission, list[str]]:
    """Return the mission and a list of warnings (things that could not be placed).

    Args:
        state: The campaign to export (theatre, sides, airbases, squadrons).
        port: TCP port the embedded bridge connects to.
        date: Calendar date of the mission start (the campaign clock adds the time of day).
        clients_per_squadron: Player slots created for each squadron.

    Returns:
        The pydcs mission and the warnings collected while building it.
    """
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
        if countries[side].name.upper() != catalog.COUNTRY[side]:
            notes.append(f"country mismatch for {side}: {countries[side].name} vs catalog {catalog.COUNTRY[side]}")

    def point(v) -> Point:
        """Campaign point -> pydcs point.

        Args:
            v: Point on the campaign plane.

        Returns:
            The same point on the DCS terrain.
        """
        return Point(v.y, v.x, terrain)  # campaign (east, north) -> DCS (x north, y east)

    # -------------------------------------------------- bullseyes (one per coalition: the side's)
    for f in state.factions.values():
        if f.side in m.coalition:
            m.coalition[f.side].bullseye = {"x": f.bullseye.y, "y": f.bullseye.x}  # DCS x north, y east

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
            # pydcs' default group frequency can sit on an airfield's ATC
            fg.frequency = safe_group_frequency(state.theater.name, fg.frequency)
            if not (task or tasks[0]).support and not apply_loadout(fg, unit_type, task or tasks[0], ac.helo, ac.id):
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
        fg.frequency = safe_group_frequency(state.theater.name, fg.frequency)
        first = sorted(sq.tasks, key=lambda t: t.value)[0]
        if not first.support:
            apply_loadout(fg, unit_type, first, sq.aircraft_type.helo, sq.aircraft_type.id)

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
    for side, setter in ((Side.BLUE, m.set_description_bluetask_text), (Side.RED, m.set_description_redtask_text)):
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
