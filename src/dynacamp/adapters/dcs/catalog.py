"""How campaign elements become DCS units.

A HOI4-sized brigade has hundreds of vehicles: DCS cannot (and need not) spawn
them all. Each element type has a *representation ratio*: one DCS unit stands
for ``ratio`` campaign elements. Losses scale back the same way.

The DCS unit types and ratios come from the ``dcs`` sub-tables of the
campaign catalogue (``dynacamp/data/catalog/*.toml``, see
:mod:`dynacamp.catalog`): add a type there, next to its campaign data. Type
names are DCS unit type strings; check them against your DCS version (Mission
Editor or ``Scripts/Database``).

Everything else DCS names (weapons, the modules players fly, the loadout
recipes) lives in the TOML files of ``adapters/dcs/data/``, read here: when a
DCS update renames something, the fix is in those files, not in the code.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from ... import catalog
from ...air import Task
from ...forces import TRUCK
from ..base import PlayerKind, Side

#: The DCS data files (``weapons.toml``, ``players.toml``, ``recipes.toml``).
DATA_DIR = Path(__file__).parent / "data"

#: The ``dcs`` tables of the ground elements (``ground.toml``), by element id.
_GROUND = catalog.dcs("ground", "elements")

#: DCS unit type of each ground element, by side (from the catalogue's ``[<id>.dcs]`` tables).
UNIT_TYPES: dict[Side, dict[str, str]] = {
    side: {element: d[side] for element, d in _GROUND.items() if side in d} for side in Side
}

#: Campaign elements one DCS unit stands for, by element (default 5).
RATIO: dict[str, float] = {element: float(d["ratio"]) for element, d in _GROUND.items() if "ratio" in d}

#: DCS country of each side (key of ``country.id`` in the DCS scripting environment).
COUNTRY: dict[Side, str] = {Side.BLUE: "USA", Side.RED: "RUSSIA"}
#: DCS type name of each campaign aircraft id (used if no template group is found).
AIRCRAFT_TYPES: dict[str, str] = {aid: d["type"] for aid, d in catalog.dcs("aircraft").items() if "type" in d}
#: DCS group task of a flight, by campaign task.
DCS_MAIN_TASK: dict[Task, str] = {
    Task.CAP: "CAP",
    Task.ESCORT: "CAP",
    Task.SEAD: "SEAD",
    Task.CAS: "CAS",
    Task.BAI: "CAS",
    Task.STRIKE: "Ground Attack",
    Task.AWACS: "AWACS",
    Task.TANKER: "Refueling",
    Task.TRANSPORT: "Transport",
}
#: DCS unit type of each element of each air defence system, by system id (one unit per element).
SAM_TYPES: dict[str, dict[str, str]] = catalog.dcs("air_defense")


@dataclass(frozen=True)
class SpawnPlan:
    """How many DCS units of a type stand for some campaign elements.

    Attributes:
        element: Campaign element id.
        dcs_type: DCS unit type.
        units: DCS units to spawn.
        represents: Campaign elements they stand for.
    """
    element: str
    dcs_type: str
    units: int  # DCS units to spawn
    represents: float  # campaign elements they stand for


def plan_division(side: Side, elements: dict[str, float], max_units: int = 40, include_trucks: bool = False) -> list[SpawnPlan]:
    """Turn element counts into DCS unit counts, capped at ``max_units`` in total.

    Args:
        side: Side picking the DCS unit types.
        elements: Element counts of the division by element id.
        max_units: Cap on the total number of DCS units.
        include_trucks: Also spawn the division's trucks.

    Returns:
        One plan per element with a DCS type, scaled down proportionally if the total exceeds
        ``max_units``.
    """
    plans: list[SpawnPlan] = []
    for element, count in elements.items():
        if count <= 0 or (element == TRUCK and not include_trucks):
            continue
        dcs_type = UNIT_TYPES[side].get(element)
        if dcs_type is None:
            continue
        units = max(1, round(count / RATIO.get(element, 5.0)))
        plans.append(SpawnPlan(element, dcs_type, units, count))
    total = sum(p.units for p in plans)
    if total > max_units:
        scale = max_units / total
        plans = [SpawnPlan(p.element, p.dcs_type, max(1, round(p.units * scale)), p.represents) for p in plans]
    return plans


def plan_convoy(side: Side, trucks: float, max_units: int = 20) -> list[SpawnPlan]:
    """Trucks to spawn for a convoy, capped at ``max_units``.

    Args:
        side: Side picking the truck type.
        trucks: Trucks in the convoy.
        max_units: Cap on the DCS units spawned.

    Returns:
        A single truck plan.
    """
    units = max(1, min(max_units, round(trucks / RATIO[TRUCK])))
    return [SpawnPlan(TRUCK, UNIT_TYPES[side][TRUCK], units, trucks)]


# ---------------------------------------------------------------- aircraft


def _tasks(names: list[str], groups: dict[str, list[str]]) -> frozenset[Task]:
    """Tasks named by an entry of ``players.toml``.

    Args:
        names: Task names or group names.
        groups: The file's ``[groups]``.

    Returns:
        The tasks.
    """
    return frozenset(Task(t) for n in names for t in groups.get(n, [n]))


@cache
def _player_tasks() -> dict[str, frozenset[Task]]:
    """Tasks a player may take a seat for, by DCS aircraft type (``players.toml``).

    Returns:
        The tasks by DCS type.
    """
    d = data("players")
    return {typ: _tasks(names, d["groups"]) for typ, names in d["types"].items()}


def player_tasks(dcs_type: str, kind: PlayerKind) -> frozenset[Task] | None:
    """Tasks a player in ``dcs_type`` may fly. None means no restriction.

    Args:
        dcs_type: DCS type of the player's aircraft.
        kind: What the player flies (unlisted helicopters are limited).

    Returns:
        The allowed tasks, or None for no restriction.
    """
    listed = _player_tasks()
    if dcs_type in listed:
        return listed[dcs_type]
    if kind is PlayerKind.HELO:
        d = data("players")
        return _tasks(d["helicopter"], d["groups"])
    return None


# ---------------------------------------------------------------- air defence



# ---------------------------------------------------------------- data files


@cache
def data(name: str) -> dict[str, Any]:
    """One of the DCS data files.

    Args:
        name: File name without extension, e.g. ``"weapons"``.

    Returns:
        The parsed TOML.
    """
    with open(DATA_DIR / f"{name}.toml", "rb") as fh:
        return tomllib.load(fh)


# ---------------------------------------------------------------- weapons


def classify_store(name: str) -> dict[str, Any]:
    """What the campaign rules need to know about a DCS store (``weapons.toml``).

    Args:
        name: DCS store name, e.g. ``"2 x R-60M"``.

    Returns:
        The fields to write next to the store in ``loadouts.json``: ``kind``
        (a :class:`~dynacamp.loadouts.StoreKind`), plus ``count`` and, for
        radar missiles, ``range_m``; empty for anything else.
    """
    from ...loadouts import StoreKind

    rules = data("weapons")
    if not any(k in name for k in rules["not_live"]):
        found = re.findall(rules["per_pylon"], name)
        count = int(found[-1]) if found else 1
        reach = next((float(r) for k, r in rules["bvr_range_m"].items() if k in name), None)
        if reach is not None:
            return {"kind": StoreKind.BVR.value, "count": count, "range_m": reach}
        if any(k in name for k in rules["wvr"]):
            return {"kind": StoreKind.WVR.value, "count": count}
    if any(k in name.lower() for k in rules["jammer"]):
        return {"kind": StoreKind.ECM.value}
    return {}
