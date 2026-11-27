"""Structures: strikes, repairs and what their health is worth (see :mod:`dynacamp.structures`)."""

from __future__ import annotations

import math

from ..events import EventKind
from ..state import CampaignState
from ..structures import Structure, StructureKind

# -- Strikes -----------------------------------------------------------------

#: Chance per unit of attack effectiveness that a building is destroyed...
KILL_PER_EFF = 0.1
#: ...at most this per building and strike.
KILL_MAX = 0.6
#: Share of a depot's stock burnt with all its buildings (in proportion for fewer).
STOCK_BURNT = 0.6

# -- Effects -----------------------------------------------------------------

#: Airbase facilities this damaged still turn aircraft around (slowly); below, nobody takes off.
MIN_AIRBASE_HEALTH = 0.2


def run(state: CampaignState, dt: float) -> None:
    """Rebuild the buildings whose repair time has come.

    Args:
        state: The campaign.
        dt: Campaign seconds elapsed (unused: repairs are timed).
    """
    for s in state.structures.values():
        done = [i for i, ready in s.destroyed.items() if ready <= state.time]
        for i in done:
            del s.destroyed[i]
        if done:
            state.emit(EventKind.STRUCTURE_REPAIRED, structure=s.id, name=s.name, faction=s.owner(state),
                       health=round(s.health, 2))


def _destroy(state: CampaignState, s: Structure, indices: list[int]) -> None:
    """Mark buildings destroyed (until rebuilt) and apply the immediate losses.

    Args:
        state: The campaign.
        s: The structure.
        indices: Buildings destroyed now (standing ones).
    """
    for i in indices:
        s.destroyed[i] = state.time + s.type.repair_h * 3600.0
    if s.kind is StructureKind.DEPOT:
        depot = state.theater.provinces[s.province].depot
        if depot is not None:
            depot.stock *= 1.0 - STOCK_BURNT * len(indices) / s.type.elements


def strike(state: CampaignState, s: Structure, eff: float, by: str) -> int:
    """Bombs on a structure (abstract strike, outside the players' bubbles).

    Args:
        state: The campaign.
        s: The structure.
        eff: Attack effectiveness (aircraft x ``a2g`` x quality).
        by: Callsign of the attacking flight.

    Returns:
        Buildings destroyed.
    """
    p = min(KILL_MAX, KILL_PER_EFF * eff)
    hit = [i for i in s.standing() if state.rng.random() < p]
    _destroy(state, s, hit)
    state.emit(EventKind.STRUCTURE_HIT, structure=s.id, name=s.name, faction=s.owner(state), by=by,
               destroyed=len(hit), health=round(s.health, 2))
    return len(hit)


def building_lost(state: CampaignState, s: Structure, i: int) -> None:
    """The game reports a building destroyed there (players or AI in a bubble).

    Args:
        state: The campaign.
        s: The structure.
        i: Building index.
    """
    if i in s.destroyed or not 0 <= i < s.type.elements:
        return
    _destroy(state, s, [i])
    state.emit(EventKind.STRUCTURE_HIT, structure=s.id, name=s.name, faction=s.owner(state), by="game",
               destroyed=1, health=round(s.health, 2))


# ---------------------------------------------------------------- what health is worth


def _health(state: CampaignState, kind: StructureKind, match) -> float:
    """Health of the structures of one kind that ``match``, averaged (1 without any).

    Args:
        state: The campaign.
        kind: The kind.
        match: Predicate on the structure.

    Returns:
        The average health.
    """
    found = [s.health for s in state.structures.values() if s.kind is kind and match(s)]
    return sum(found) / len(found) if found else 1.0


def depot_factor(state: CampaignState, province: str) -> float:
    """Share of its production a province's depot still makes.

    Args:
        state: The campaign.
        province: The depot's province.

    Returns:
        0-1.
    """
    return _health(state, StructureKind.DEPOT, lambda s: s.province == province)


def airbase_factor(state: CampaignState, airbase: str) -> float:
    """Health of an airbase's facilities (0: nobody takes off).

    Args:
        state: The campaign.
        airbase: Airbase id.

    Returns:
        0-1; 0 below ``MIN_AIRBASE_HEALTH``.
    """
    h = _health(state, StructureKind.AIRBASE, lambda s: s.airbase == airbase)
    return h if h >= MIN_AIRBASE_HEALTH else 0.0


def command_factor(state: CampaignState, faction: str) -> float:
    """Health of a faction's command posts (1 if it has none).

    Args:
        state: The campaign.
        faction: Faction id.

    Returns:
        0-1.
    """
    return _health(state, StructureKind.COMMAND, lambda s: s.owner(state) == faction)


def packages_per_cycle(state: CampaignState, faction: str, nominal: int) -> int:
    """Combat packages a faction can plan in one cycle with its command posts as they are.

    Args:
        state: The campaign.
        faction: Faction id.
        nominal: The number with intact command posts.

    Returns:
        At least 1 while a command post stands (or none exists), else 0.
    """
    h = command_factor(state, faction)
    return max(1, math.ceil(nominal * h)) if h > 0 else 0
