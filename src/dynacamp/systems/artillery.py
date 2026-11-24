"""Artillery fire missions along the front.

Outside set-piece battles the front is never quiet: every division with guns
shells enemy units within range at intervals (harassing and counter-battery
fire). Effects are modest - a little cohesion and some casualties - but they
keep the front alive and wear down units that stay in reach of the enemy's guns.

Units fighting each other in the same battle already include their guns in the
battle's firepower, so they do not also fire missions at each other. Units the
game simulates (inside a player bubble) are left to the game.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..forces import Authority, Division
from ..state import CampaignState
from . import intel

ORGANIC_RANGE_M = 24_000.0  # brigade artillery battalion (M109, 2S19: 24-29 km)
GROUP_RANGE_M = 40_000.0  # dedicated artillery groups with MLRS (BM-21, M270: 20-70 km)
FIRE_INTERVAL_S = 45 * 60.0  # average time between missions of one division
MIN_GUNS = 4.0
STRENGTH_PER_GUN = 0.00005
ORG_PER_GUN = 0.001
TARGET_FRESH_S = 1800.0  # a located target is engaged for this long
FIRE_MEMORY_S = 30 * 60.0  # missions kept for presentation


@dataclass(frozen=True, slots=True)
class FireMission:
    """A fire mission, kept briefly for presentation.

    Attributes:
        time: Campaign time.
        shooter: Firing division id.
        target: Target id.
        faction: Shooter's faction.
        origin: Shooter position (x, y).
        impact: Target position (x, y).
    """
    time: float
    shooter: str
    target: str
    faction: str
    origin: tuple[float, float]
    impact: tuple[float, float]


def guns(d: Division) -> float:
    """Gun-equivalents of a division (MLRS count 1.5)."""
    return d.elements.get("artillery", 0.0) + 1.5 * d.elements.get("mlrs", 0.0)


def fire_range(d: Division) -> float:
    """Fire range of a division: longer with MLRS."""
    return GROUP_RANGE_M if d.elements.get("mlrs", 0.0) >= 1 else ORGANIC_RANGE_M


def run(state: CampaignState, dt: float) -> None:
    """Fire missions of every division with guns at the nearest enemy in range."""
    t = state.theater
    state.fires[:] = [f for f in state.fires if state.time - f.time <= FIRE_MEMORY_S]
    chance = min(1.0, dt / FIRE_INTERVAL_S)
    for shooter in list(state.divisions.values()):
        g = guns(shooter)
        if g < MIN_GUNS or shooter.authority is not Authority.CAMPAIGN or shooter.supply <= 0:
            continue
        if shooter.movement and not shooter.battle:
            continue  # guns are limbered on the march
        if state.rng.random() > chance:
            continue
        here = shooter.position(t)
        reach = fire_range(shooter)
        # Guns only fire at targets someone has located recently (fog of war).
        located = intel.picture(state, shooter.faction)
        fresh = {i for i, c in located.items() if c.age(state.time) <= TARGET_FRESH_S}
        targets = [
            d
            for d in state.divisions.values()
            if d.id in fresh and state.at_war(shooter.faction, d.faction)
            and d.authority is Authority.CAMPAIGN
            and not (shooter.battle and d.battle == shooter.battle)
            and d.position(t).distance_to(here) <= reach
        ]
        # Air defence sites are soft, high-value targets for the guns too.
        targets += [
            u for u in state.air_defense.values()
            if u.id in fresh and state.at_war(shooter.faction, u.faction) and u.authority is Authority.CAMPAIGN
            and u.position(t).distance_to(here) <= reach
        ]
        if not targets:
            continue
        target = min(targets, key=lambda d: d.position(t).distance_to(here))
        effect = g * (0.5 + 0.5 * shooter.org)
        if isinstance(target, Division):
            target.take_losses(STRENGTH_PER_GUN * effect)
            target.org = max(0.0, target.org - ORG_PER_GUN * effect)
        else:  # air defence site: a lucky salvo can knock out a radar or a launcher
            if state.rng.random() < min(0.12, 0.002 * effect):
                k = state.rng.choice(sorted(e for e, n in target.elements.items() if n >= 1) or ["launcher"])
                target.elements[k] = max(0.0, target.elements.get(k, 0.0) - 1)
            target.scoot = True
        to = target.position(t)
        state.fires.append(FireMission(state.time, shooter.id, target.id, shooter.faction, (here.x, here.y), (to.x, to.y)))
        state.emit("artillery_fire", shooter=shooter.id, target=target.id, faction=shooter.faction, km=round(to.distance_to(here) / 1000, 1))
