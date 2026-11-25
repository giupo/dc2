"""How campaign elements become DCS units.

A HOI4-sized brigade has hundreds of vehicles: DCS cannot (and need not) spawn
them all. Each element type has a *representation ratio*: one DCS unit stands
for ``ratio`` campaign elements. Losses scale back the same way.

The DCS unit types and ratios come from the ``dcs`` sub-tables of the
campaign catalogue (``dynacamp/data/catalog/*.toml``, see
:mod:`dynacamp.catalog`): add a type there, next to its campaign data. Type
names are DCS unit type strings; check them against your DCS version (Mission
Editor or ``Scripts/Database``).
"""

from __future__ import annotations

from dataclasses import dataclass

from ... import catalog

SIDES = ("blue", "red")

_GROUND = catalog.dcs("ground", "elements")

# side -> element -> DCS type
UNIT_TYPES: dict[str, dict[str, str]] = {
    side: {element: d[side] for element, d in _GROUND.items() if side in d} for side in SIDES
}

# campaign elements represented by one DCS unit
RATIO: dict[str, float] = {element: float(d["ratio"]) for element, d in _GROUND.items() if "ratio" in d}

# DCS country ids (country.id.*) used per side
COUNTRY: dict[str, int] = {"blue": 2, "red": 0}  # USA, RUSSIA


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


def plan_division(side: str, elements: dict[str, float], max_units: int = 40, include_trucks: bool = False) -> list[SpawnPlan]:
    """Turn element counts into DCS unit counts, capped at ``max_units`` in total."""
    plans: list[SpawnPlan] = []
    for element, count in elements.items():
        if count <= 0 or (element == "truck" and not include_trucks):
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


def plan_convoy(side: str, trucks: float, max_units: int = 20) -> list[SpawnPlan]:
    """Trucks to spawn for a convoy, capped at ``max_units``."""
    units = max(1, min(max_units, round(trucks / RATIO["truck"])))
    return [SpawnPlan("truck", UNIT_TYPES[side]["truck"], units, trucks)]


# ---------------------------------------------------------------- aircraft

# campaign aircraft id -> DCS type (used if no template group is found)
AIRCRAFT_TYPES: dict[str, str] = {aid: d["type"] for aid, d in catalog.dcs("aircraft").items() if "type" in d}

_ALL = frozenset({"CAP", "ESCORT", "SEAD", "CAS", "BAI", "STRIKE"})
_FIGHTER = frozenset({"CAP", "ESCORT"})
_MUD = frozenset({"CAS", "BAI"})

# DCS player aircraft type -> tasks it may take a seat for.
# Unknown aeroplanes may join anything; unknown helicopters only CAS/BAI.
PLAYER_TASKS: dict[str, frozenset[str]] = {
    "F-16C_50": _ALL,
    "FA-18C_hornet": _ALL,
    "F-15ESE": _ALL,
    "JF-17": _ALL,
    "F-15C": _FIGHTER,
    "Su-27": _FIGHTER,
    "Su-33": _FIGHTER,
    "J-11A": _FIGHTER,
    "MiG-29A": _FIGHTER,
    "MiG-29S": _FIGHTER,
    "MiG-29G": _FIGHTER,
    "F-14A-135-GR": _FIGHTER,
    "F-4E-45MC": _FIGHTER | _MUD | {"STRIKE"},
    "AJS37": _MUD | {"STRIKE"},
    "C-101CC": _FIGHTER | _MUD,
    "L-39ZA": _MUD,
    "F-14B": _FIGHTER | {"STRIKE", "BAI"},
    "M-2000C": _FIGHTER | {"BAI"},
    "Mirage-F1EE": _FIGHTER | {"BAI", "CAS"},
    "MiG-21Bis": _FIGHTER | {"BAI"},
    "F-5E-3": _FIGHTER | _MUD,
    "AV8BNA": _MUD | {"STRIKE"},
    "A-10C": _MUD,
    "A-10C_2": _MUD,
    "Su-25": _MUD,
    "Su-25T": _MUD | {"SEAD"},
    "AH-64D_BLK_II": _MUD,
    "Ka-50": _MUD,
    "Ka-50_3": _MUD,
    "Mi-24P": _MUD,
    "SA342M": _MUD,
    "OH58D": _MUD,
    "Mi-8MT": _MUD | {"TRANSPORT"},
    "UH-1H": _MUD | {"TRANSPORT"},
}


def player_tasks(dcs_type: str, category: int) -> frozenset[str] | None:
    """Tasks a player in ``dcs_type`` may fly. None means no restriction."""
    if dcs_type in PLAYER_TASKS:
        return PLAYER_TASKS[dcs_type]
    return _MUD if category == 1 else None


# campaign task -> DCS group main task
DCS_MAIN_TASK: dict[str, str] = {
    "CAP": "CAP",
    "ESCORT": "CAP",
    "SEAD": "SEAD",
    "CAS": "CAS",
    "BAI": "CAS",
    "STRIKE": "Ground Attack",
    "AWACS": "AWACS",
    "TANKER": "Refueling",
    "TRANSPORT": "Transport",
}


# ---------------------------------------------------------------- air defence

# campaign SAM system -> site element -> DCS unit type (one unit per element)
SAM_TYPES: dict[str, dict[str, str]] = catalog.dcs("air_defense")
