"""Ordnance carried by each aircraft type for each task.

The loadouts live in ``dynacamp/data/loadouts.json``: for every aircraft and
task, the stores on each pylon (with the game's weapon ids). The file is
generated from DCS' own pylon tables by ``dynacamp loadouts`` (needs pydcs), so
every store is valid for its pylon, and it is the single source of truth for:

* the briefing players receive (what to carry and use);
* the mission templates (:mod:`dynacamp.adapters.dcs.miz`), so AI flights fly
  with exactly the briefed ordnance.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from functools import cache
from pathlib import Path

DATA = Path(__file__).parent / "data" / "loadouts.json"


@cache
def _data() -> dict:
    """The loadouts file (empty if it has not been generated)."""
    if not DATA.exists():
        return {}
    return json.loads(DATA.read_text(encoding="utf-8"))["aircraft"]


def stores(aircraft: str, task: str) -> list[dict]:
    """Stores on each pylon for an aircraft and task.

    Args:
        aircraft: Campaign aircraft id, e.g. ``"f16c"``.
        task: Task name, e.g. ``"SEAD"``.

    Returns:
        ``[{"pylon": n, "name": ..., "clsid": ..., "weight": kg}, ...]``, empty if unknown.
    """
    return list(_data().get(aircraft, {}).get(task, []))


def summary(aircraft: str, task: str) -> list[tuple[str, int]]:
    """Stores grouped by name: ``[(store name, number of pylons), ...]``, heaviest use first."""
    counts = Counter(s["name"] for s in stores(aircraft, task))
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def weight_kg(aircraft: str, task: str) -> float:
    """Total weight of the stores, kg."""
    return sum(float(s.get("weight", 0.0)) for s in stores(aircraft, task))


# ---------------------------------------------------------------- air-to-air missiles

# Radar-guided (beyond visual range) missiles and how far they are fired from
# (effective launch range against a fighter-sized target, not the brochure figure).
BVR_RANGE_M = {
    "AIM-120": 50_000.0, "AIM-54": 90_000.0, "AIM-7": 30_000.0, "R-77": 50_000.0, "R-27ER": 50_000.0,
    "R-27R": 35_000.0, "R-33": 90_000.0, "R-40": 30_000.0, "R-24R": 25_000.0, "Super 530": 30_000.0,
    "SD-10": 50_000.0, "PL-12": 50_000.0,
}
# Heat seekers for the merge (within visual range).
WVR_KEYS = ("AIM-9", "R-60", "R-73", "R-27T", "R-27ET", "R-24T", "RB-74", "Magic", "PL-5", "PL-8", "Python")
NOT_LIVE = ("Captive",)  # training rounds


@dataclass(frozen=True)
class AirToAir:
    """Air-to-air missiles of one aircraft for a task.

    Attributes:
        bvr: Radar-guided missiles.
        bvr_range_m: Launch range of the longest-reaching of them (0 without).
        wvr: Heat seekers.
    """
    bvr: int = 0
    bvr_range_m: float = 0.0
    wvr: int = 0


def _per_pylon(name: str) -> int:
    """Missiles on one pylon: ``"2 x R-60M"`` carriers hold several."""
    found = re.findall(r"(\d+)\s*x\s", name)
    return int(found[-1]) if found else 1


@cache
def air_to_air(aircraft: str, task: str) -> AirToAir:
    """Air-to-air missiles one aircraft carries for a task (from its loadout).

    Args:
        aircraft: Campaign aircraft id.
        task: Task name; escorts carry the CAP load when they have none of their own.
    """
    loads = stores(aircraft, task) or (stores(aircraft, "CAP") if task == "ESCORT" else [])
    bvr = wvr = 0
    reach = 0.0
    for s in loads:
        name = s["name"]
        if any(k in name for k in NOT_LIVE):
            continue
        n = _per_pylon(name)
        hit = next((r for k, r in BVR_RANGE_M.items() if k in name), None)
        if hit is not None:
            bvr += n
            reach = max(reach, hit)
        elif any(k in name for k in WVR_KEYS):
            wvr += n
    return AirToAir(bvr, reach, wvr)


@cache
def has_ecm_pod(aircraft: str, task: str) -> bool:
    """True if the loadout carries a self-protection jamming pod (ALQ-131/184, SPS-141...).

    Args:
        aircraft: Campaign aircraft id.
        task: Task name.
    """
    return any("ECM" in s["name"] or "jamming" in s["name"].lower() for s in stores(aircraft, task))
