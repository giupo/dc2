"""How campaign elements become DCS units.

A HOI4-sized brigade has hundreds of vehicles: DCS cannot (and need not) spawn
them all. Each element type has a *representation ratio*: one DCS unit stands
for ``ratio`` campaign elements. Losses scale back the same way.

Type names are DCS unit type strings; check them against your DCS version
(Mission Editor or ``Scripts/Database``) and extend the tables freely.
"""

from __future__ import annotations

from dataclasses import dataclass

# side -> element -> DCS type
UNIT_TYPES: dict[str, dict[str, str]] = {
    "blue": {
        "infantry": "Soldier M4",
        "mbt": "M-1 Abrams",
        "ifv": "M-2 Bradley",
        "apc": "M-113",
        "artillery": "M-109",
        "mlrs": "MLRS",
        "sam_short": "M1097 Avenger",
        "aaa": "Vulcan",
        "truck": "M 818",
    },
    "red": {
        "infantry": "Infantry AK",
        "mbt": "T-72B",
        "ifv": "BMP-2",
        "apc": "BTR-80",
        "artillery": "SAU Msta",
        "mlrs": "Grad-URAL",
        "sam_short": "Strela-10M3",
        "aaa": "ZSU-23-4 Shilka",
        "truck": "Ural-375",
    },
}

# campaign elements represented by one DCS unit
RATIO: dict[str, float] = {
    "infantry": 10.0,
    "mbt": 6.0,
    "ifv": 8.0,
    "apc": 8.0,
    "artillery": 6.0,
    "mlrs": 6.0,
    "sam_short": 4.0,
    "aaa": 4.0,
    "truck": 3.0,
}

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
AIRCRAFT_TYPES: dict[str, str] = {
    "f16c": "F-16C_50",
    "fa18c": "FA-18C_hornet",
    "f15c": "F-15C",
    "a10c": "A-10C_2",
    "ah64d": "AH-64D_BLK_II",
    "su27": "Su-27",
    "mig29s": "MiG-29S",
    "su25t": "Su-25T",
    "su24m": "Su-24M",
    "mi24p": "Mi-24P",
}

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
    "F-14A-135-GR": _FIGHTER | {"STRIKE", "BAI"},
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
    "UH-1H": _MUD,
    "Mi-8MT": _MUD,
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
}


# ---------------------------------------------------------------- air defence

# campaign SAM system -> site element -> DCS unit type (one unit per element)
SAM_TYPES: dict[str, dict[str, str]] = {
    "patriot": {"radar": "Patriot str", "launcher": "Patriot ln", "cp": "Patriot ECS", "power": "Patriot EPP"},
    "s300ps": {"search_radar": "S-300PS 64H6E sr", "radar": "S-300PS 40B6M tr", "cp": "S-300PS 54K6 cp", "launcher": "S-300PS 5P85C ln"},
    "hawk": {"search_radar": "Hawk sr", "radar": "Hawk tr", "cp": "Hawk pcp", "launcher": "Hawk ln"},
    "nasams": {"radar": "NASAMS_Radar_MPQ64F1", "cp": "NASAMS_Command_Post", "launcher": "NASAMS_LN_C"},
    "buk": {"search_radar": "SA-11 Buk SR 9S18M1", "cp": "SA-11 Buk CC 9S470M1", "launcher": "SA-11 Buk LN 9A310M1"},
    "kub": {"radar": "Kub 1S91 str", "launcher": "Kub 2P25 ln"},
    "tor": {"launcher": "Tor 9A331"},
    "osa": {"launcher": "Osa 9A33 ln"},
    "avenger": {"launcher": "M1097 Avenger"},
    "ewr_1l13": {"radar": "1L13 EWR"},
    "ewr_55g6": {"radar": "55G6 EWR"},
    "ewr_fps117": {"radar": "FPS-117", "dome": "FPS-117 Dome", "cp": "FPS-117 ECS"},
}
