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
    "f14a": "F-14A-135-GR",
    "f14b": "F-14B",
    "f15e": "F-15ESE",
    "f16a": "F-16A",
    "f4e": "F-4E-45MC",
    "a10a": "A-10A",
    "av8b": "AV8BNA",
    "f117": "F-117A",
    "b1b": "B-1B",
    "b52h": "B-52H",
    "tornado_gr4": "Tornado GR4",
    "tornado_ids": "Tornado IDS",
    "m2000c": "M-2000C",
    "m2000_5": "Mirage 2000-5",
    "f5e": "F-5E-3",
    "jf17": "JF-17",
    "mirage_f1": "Mirage-F1EE",
    "ajs37": "AJS37",
    "ah1w": "AH-1W",
    "oh58d": "OH58D",
    "sa342": "SA342M",
    "mig29a": "MiG-29A",
    "mig29g": "MiG-29G",
    "mig31": "MiG-31",
    "mig25pd": "MiG-25PD",
    "mig23mld": "MiG-23MLD",
    "mig27k": "MiG-27K",
    "su33": "Su-33",
    "su30": "Su-30",
    "su34": "Su-34",
    "su25": "Su-25",
    "su25tm": "Su-25TM",
    "su17m4": "Su-17M4",
    "tu22m3": "Tu-22M3",
    "tu95ms": "Tu-95MS",
    "j11a": "J-11A",
    "mig21bis": "MiG-21Bis",
    "ka50": "Ka-50_3",
    "mi28n": "Mi-28N",
    "mi24v": "Mi-24V",
    "e3a": "E-3A",
    "e2c": "E-2C",
    "a50": "A-50",
    "kc135": "KC-135",
    "kc130": "KC130",
    "il78m": "IL-78M",
    "c130": "C-130",
    "c17": "C-17A",
    "il76": "IL-76MD",
    "an26": "An-26B",
    "uh60a": "UH-60A",
    "ch47d": "CH-47D",
    "mi8mt": "Mi-8MT",
    "mi26": "Mi-26",
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
