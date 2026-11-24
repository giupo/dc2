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
from collections import Counter
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
