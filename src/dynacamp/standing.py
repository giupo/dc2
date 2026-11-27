"""Who is winning: one number per faction, the shares summing to 1.

Every measure in :data:`WEIGHTS` counts as the faction's share of all the
factions' total, weighted: victory points (cities, depots) most, then the
faction's own objectives (share of their priority achieved), then the forces
left; provinces least (an empty desert province is cheap). Measures nobody has
are skipped and the weights of the others renormalised.

The web UI shows it as the header bar (``standing`` in ``/api/campaign``) and
the players read it in their situation report (:mod:`dynacamp.systems.sitrep`).
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .state import CampaignState, Faction


class Measure(StrEnum):
    """What the standing weighs."""
    #: Victory points of the provinces held.
    VICTORY_POINTS = "victory_points"
    #: Share of the faction's own objectives (by priority) held.
    OBJECTIVES = "objectives"
    #: Brigades left.
    DIVISIONS = "divisions"
    #: Aircraft left.
    AIRCRAFT = "aircraft"
    #: Provinces held.
    PROVINCES = "provinces"


#: Weight of each measure in the standing.
WEIGHTS: dict[Measure, float] = {
    Measure.VICTORY_POINTS: 0.35,
    Measure.OBJECTIVES: 0.20,
    Measure.DIVISIONS: 0.20,
    Measure.AIRCRAFT: 0.15,
    Measure.PROVINCES: 0.10,
}


def measures(state: CampaignState, f: Faction) -> dict[Measure, float]:
    """The faction's value of every measure in :data:`WEIGHTS`.

    Args:
        state: The campaign.
        f: The faction.

    Returns:
        Measure -> value.
    """
    provinces = state.theater.provinces
    held = [p for p in provinces.values() if p.controller == f.id]
    priority = sum(o.priority for o in f.objectives)
    achieved = sum(o.priority for o in f.objectives if provinces[o.province].controller == f.id)
    return {
        Measure.VICTORY_POINTS: float(sum(p.victory_points for p in held)),
        Measure.OBJECTIVES: achieved / priority if priority else 0.0,
        Measure.DIVISIONS: float(sum(1 for d in state.divisions.values() if d.faction == f.id)),
        Measure.AIRCRAFT: float(sum(s.aircraft for s in state.squadrons.values() if s.faction == f.id)),
        Measure.PROVINCES: float(len(held)),
    }


def shares(state: CampaignState) -> dict[str, float]:
    """Share of the war each faction is winning.

    Args:
        state: The campaign.

    Returns:
        Faction id -> share, summing to 1 (even shares if nobody has anything).
    """
    factions = list(state.factions.values())
    if not factions:
        return {}
    values = {f.id: measures(state, f) for f in factions}
    score = {f.id: 0.0 for f in factions}
    weight = 0.0
    for m, w in WEIGHTS.items():
        total = sum(v[m] for v in values.values())
        if not total:
            continue
        weight += w
        for fid, v in values.items():
            score[fid] += w * v[m] / total
    return {fid: (s / weight if weight else 1.0 / len(factions)) for fid, s in score.items()}
