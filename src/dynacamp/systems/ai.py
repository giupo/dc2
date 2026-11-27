"""Theatre commander AI (one per faction).

Every AI cycle, in this order:

1. **Security (rear and flanks).** From every enemy division we flood-fill the
   map for a few hops, stopping at provinces we hold with troops: that is where
   the enemy could walk in unopposed. Our empty provinces reached this way are
   *exposed*, weighted by importance (depots, victory points, enemy objectives,
   airbases) and by how close the enemy is. Divisions are moved, one at a time,
   where they remove the most exposure. Because a garrison also screens what
   lies behind it, this naturally finds chokepoints (holding a crossing protects
   the cities beyond it).
2. **Offence.** Attack hostile provinces next to the front where we have local
   superiority (more needed against hard terrain), preferring objectives.
3. **Reserves.** Idle divisions in the rear go to the front sectors that are
   weakest relative to the threat - unless leaving would expose something.
"""

from __future__ import annotations

from logging import getLogger
from collections import deque
from enum import StrEnum

from ..forces import Authority, Division, Order, OrderKind
from ..state import CampaignState, Faction, Timer
from ..world import TERRAIN
from . import intel
from .combat import ENTRENCHMENT, RIVER_PENALTY
from ..events import EventKind


log = getLogger(__name__)


class AiOrder(StrEnum):
    """What the theatre AI orders a brigade to do (``ai_order``)."""
    #: Move to plug a hole in the front line.
    SECURE = "secure"
    #: Redeploy to another province.
    MOVE = "move"
    #: Attack an enemy province.
    ATTACK = "attack"


#: Seconds between two planning rounds of the theatre AI.
AI_INTERVAL_S = 3600.0
#: An attack goes in with at least this force ratio over the known defenders (terrain-adjusted).
ATTACK_RATIO = 1.1
#: Minimum organisation of a brigade sent to attack.
READY_ORG = 0.6
#: Minimum strength of a brigade sent to attack.
READY_STRENGTH = 0.5
#: Minimum supply ratio of a brigade sent to attack.
READY_SUPPLY = 0.3

#: Provinces this many hops (or fewer) from an enemy brigade are exposed.
EXPOSURE_HOPS = 3
#: Weight of an exposed province by its hops from the nearest enemy brigade.
EXPOSURE_DECAY = {0: 1.5, 1: 1.0, 2: 0.6, 3: 0.3}
#: Exposure points a garrison move must remove to be worth it.
MIN_SECURITY_GAIN = 1.0
#: Brigades weaker than this neither hold a province nor threaten one.
GARRISON_MIN_STRENGTH = 0.3
#: An enemy province is known to be empty only if a friendly observer is this
#: close to its centre (defenders deploy near it) and reports nothing.
WATCH_M = 12_000.0


def run(state: CampaignState, _: float) -> None:
    """Plan for every faction every ``AI_INTERVAL_S``.

    Args:
        state: The campaign.
        dt: Campaign seconds elapsed (unused: planning runs on its own timer).
    """

    # there's no time for AI, bail out
    if state.time < state.timers.get(Timer.AI, 0.0):
        return

    state.timers[Timer.AI] = state.time + AI_INTERVAL_S
    for faction in state.factions.values():
        plan(state, faction)


def _power(state: CampaignState, divs: list[Division]) -> float:
    """Sum of the divisions' :meth:`~dynacamp.forces.Division.combat_power`.

    Args:
        state: The campaign (the factions' quality).
        divs: The divisions.

    Returns:
        Their total combat power.
    """
    return sum(d.combat_power(state.quality(d.faction)) for d in divs)


def _expected_defence(state: CampaignState, fid: str, target: str, divs: list[Division]) -> float:
    """Defensive power the faction should expect to meet in ``target``.

    The brigades the intelligence picture shows there; if it shows none, the
    province is empty only when a friendly observer watches its centre.
    Otherwise nobody has looked: the planners assume a typical garrison (the
    median power of their own brigades), not an open door.

    Args:
        state: The campaign.
        fid: Faction id.
        target: Enemy province.
        divs: The faction's own divisions (for the typical garrison).

    Returns:
        The expected defending combat power (0 = known to be empty).
    """
    known = _defence_power(state, intel.known_divisions_in(state, fid, target))

    if known > 0:
        return known

    centre = state.theater.provinces[target].center

    if any(pos.distance_to(centre) <= min(rng, WATCH_M) for pos, rng, _ in intel.ground_observers(state, fid)):
        return 0.0

    powers = sorted(_defence_power(state, [d]) for d in divs if d.strength >= GARRISON_MIN_STRENGTH)
    return powers[len(powers) // 2] if powers else 0.0


def _readiness(state: CampaignState, d: Division) -> float:
    """Organisation and quality scaling of a brigade's power, as in :meth:`Division.combat_power`.

    Args:
        state: The campaign (the factions' quality).
        d: The division.

    Returns:
        The multiplier: 0.4 (no organisation) to 1.0, times the faction's quality.
    """
    return (0.4 + 0.6 * d.org) * state.quality(d.faction)


def _attack_power(state: CampaignState, divs: list[Division]) -> float:
    """What the brigades are worth in an assault: firepower plus the attacks
    they can absorb while attacking (``breakthrough``), as the combat system
    counts them. Infantry is worth much less here than in defence.

    Args:
        state: The campaign (the factions' quality).
        divs: The attackers.

    Returns:
        Their total assault power.
    """
    return sum((d.firepower(0.3) + d.breakthrough()) * _readiness(state, d) for d in divs)


def _defence_power(state: CampaignState, divs: list[Division]) -> float:
    """What the brigades are worth holding a province: firepower plus the
    attacks they absorb dug in (``defense`` times the entrenchment bonus).

    Args:
        state: The campaign (the factions' quality).
        divs: The defenders.

    Returns:
        Their total defensive power.
    """
    return sum((d.firepower(0.3) + d.defense() * ENTRENCHMENT) * _readiness(state, d) for d in divs)


def _ready(d: Division) -> bool:
    """Fit to attack: enough organisation, strength and supply.

    Args:
        d: The division.

    Returns:
        Whether organisation, strength and supply ratio are all at the ``READY_*`` thresholds.
    """
    return d.org >= READY_ORG and d.strength >= READY_STRENGTH and d.supply_ratio >= READY_SUPPLY


def _objective_value(state: CampaignState, faction: Faction, pid: str) -> float:
    """How much ``faction`` wants province ``pid`` (VP + objectives and closeness to them).

    Args:
        state: The campaign.
        faction: The faction.
        pid: Province id.

    Returns:
        The value (0 for a province with no VP far from every objective still to take).
    """
    t = state.theater
    here = t.provinces[pid]
    value = float(here.victory_points)
    for obj in faction.objectives:
        if t.provinces[obj.province].controller == faction.id:
            continue
        if obj.province == pid:
            value += 10.0 * obj.priority
        else:
            km = here.center.distance_to(t.provinces[obj.province].center) / 1000.0
            value += 5.0 * obj.priority / (1.0 + km / 30.0)
    return value


# ---------------------------------------------------------------- exposure


def importance(state: CampaignState, fid: str, pid: str) -> float:
    """How much we care about losing ``pid``.

    Args:
        state: The campaign.
        fid: Faction id.
        pid: Province id.

    Returns:
        The value, from 1 upwards (VP, depot, enemy objectives, active airbase).
    """
    p = state.theater.provinces[pid]
    value = 1.0 + p.victory_points
    if p.depot is not None:
        value += 5.0
    for f in state.factions.values():
        if state.at_war(fid, f.id):
            value += sum(3.0 * o.priority for o in f.objectives if o.province == pid)
    for base in getattr(state, "airbases", {}).values():
        if base.province == pid and any(
            s.airbase == base.id and s.aircraft > 0 for s in state.squadrons.values() if s.faction == fid
        ):
            value += 4.0
    return value


def blockers(state: CampaignState, fid: str) -> dict[str, int]:
    """Provinces our troops hold (or are about to hold) -> number of divisions.

    Args:
        state: The campaign.
        fid: Faction id.

    Returns:
        The number of divisions holding each province.
    """
    out: dict[str, int] = {}
    for d in state.divisions.values():
        if d.faction != fid or d.strength < GARRISON_MIN_STRENGTH:
            continue

        if d.movement and d.order.kind is OrderKind.MOVE and d.order.target:
            pid = d.order.target  # on its way there

        elif d.order.kind is OrderKind.ATTACK and d.movement and not d.movement.back:
            pid = d.location  # attacking out of it: still screens it

        elif d.movement and d.movement.progress > 0:
            continue  # on the road, holds nothing

        else:
            pid = d.location

        out[pid] = out.get(pid, 0) + 1
    return out


def enemy_reach(state: CampaignState, fid: str, held: set[str]) -> dict[str, int]:
    """Hops from the nearest enemy division, not passing through ``held`` provinces.

    Args:
        state: The campaign.
        fid: Faction id.
        held: Provinces held with troops (the flood-fill stops there).

    Returns:
        Hops from the nearest enemy division, for provinces within ``EXPOSURE_HOPS``.
    """
    t = state.theater
    dist: dict[str, int] = {}
    queue: deque[str] = deque()

    for d in state.divisions.values():
        if state.at_war(fid, d.faction) and d.strength >= GARRISON_MIN_STRENGTH and d.location not in dist:
            dist[d.location] = 0
            queue.append(d.location)

    while queue:
        pid = queue.popleft()
        if dist[pid] >= EXPOSURE_HOPS or (pid in held and dist[pid] > 0):
            continue

        for n in t.neighbors(pid):
            if n not in dist:
                dist[n] = dist[pid] + 1
                queue.append(n)

    return dist


def exposure(state: CampaignState, fid: str, held: set[str], weights: dict[str, float]) -> float:
    """Total exposure of ``fid``'s provinces given the provinces it holds.

    Args:
        state: The campaign.
        fid: Faction id.
        held: Provinces held with troops (they block the enemy flood-fill).
        weights: Importance of each province.

    Returns:
        The importance-weighted exposure (lower is safer).
    """
    reach = enemy_reach(state, fid, held)
    return sum(
        weights[pid] * EXPOSURE_DECAY[hops]
        for pid, hops in reach.items()
        if pid not in held and state.theater.provinces[pid].controller == fid
    )


def _secure(
    state: CampaignState,
    faction: Faction,
    divs: list[Division],
    busy: set[str]
) -> set[str]:
    """Greedy garrisoning of exposed provinces. Returns the ids it gave orders to.

    Args:
        state: The campaign.
        faction: The faction.
        divs: Its divisions available for orders.
        busy: Ids of divisions already given an order this round (not moved again).

    Returns:
        The ids of the divisions sent to garrison a province.
    """
    t = state.theater
    fid = faction.id
    weights = {pid: importance(state, fid, pid) for pid in t.provinces}
    counts = blockers(state, fid)
    moved: set[str] = set()

    for _ in range(len(divs)):
        held = set(counts)
        base = exposure(state, fid, held, weights)
        if base < MIN_SECURITY_GAIN:
            break

        reach = enemy_reach(state, fid, held)
        holes = [pid for pid in reach if pid not in held and t.provinces[pid].controller == fid]
        best: tuple[float, Division, str] | None = None

        for d in divs:
            if d.id in busy or d.id in moved or d.battle or d.order.kind is OrderKind.ATTACK:
                continue

            if d.strength < GARRISON_MIN_STRENGTH or (d.movement and d.movement.progress > 0):
                continue

            here = d.order.target if (d.movement and d.order.target) else d.location

            for hole in holes:
                path = t.shortest_path(d.location, hole, passable=lambda pid: state.friendly_province(fid, pid))
                if path is None:
                    continue

                after = set(held)

                if counts.get(here, 0) <= 1:
                    after.discard(here)

                after.add(hole)
                gain = base - exposure(state, fid, after, weights)
                hours = t.path_length(d.location, path) / 1000.0 / d.template.speed_kmh
                score = gain / (1.0 + hours / 12.0)

                if gain >= MIN_SECURITY_GAIN and (best is None or score > best[0]):
                    best = (score, d, hole)

        if best is None:
            break

        _, d, hole = best
        here = d.order.target if (d.movement and d.order.target) else d.location
        if not state.set_order(d, Order(OrderKind.MOVE, hole)):
            break

        counts[here] = counts.get(here, 1) - 1
        if counts[here] <= 0:
            del counts[here]

        counts[hole] = counts.get(hole, 0) + 1
        moved.add(d.id)
        state.emit(EventKind.AI_ORDER, faction=fid, division=d.id, order=AiOrder.SECURE, target=hole)

    return moved


def _keepers(state: CampaignState, fid: str, candidates: list[Division]) -> set[str]:
    """Divisions whose departure would leave something exposed.

    Args:
        state: The campaign.
        fid: Faction id.
        candidates: Divisions that might be sent elsewhere.

    Returns:
        The ids of those that should stay.
    """
    t = state.theater
    weights = {pid: importance(state, fid, pid) for pid in t.provinces}
    counts = blockers(state, fid)
    held = set(counts)
    base = exposure(state, fid, held, weights)
    keep: set[str] = set()
    for d in candidates:
        if counts.get(d.location, 0) > 1:
            continue
        if exposure(state, fid, held - {d.location}, weights) - base >= MIN_SECURITY_GAIN:
            keep.add(d.id)
    return keep


# ---------------------------------------------------------------- planning


def plan(state: CampaignState, faction: Faction) -> None:
    """One AI cycle for a faction: rear/flank security, front deployment, offence,
    reserves.

    Args:
        state: The campaign.
        faction: The faction.
    """
    t = state.theater
    fid = faction.id
    divs = [
        d
        for d in state.divisions.values()
        if d.faction == fid and d.authority in (Authority.CAMPAIGN, Authority.DELEGATED)
    ]
    fronts = set(state.front_provinces(fid))
    busy = {d.id for d in divs if d.battle or (d.order.kind is OrderKind.ATTACK and d.movement)}
    busy |= {d.id for d in divs if d.movement and d.movement.progress > 0}

    # ---- 1. rear and flank security
    busy |= _secure(state, faction, divs, busy)
    if not fronts:
        return
    _man_the_front(state, fid, divs, fronts, busy)

    # ---- 2. offence
    targets: set[str] = {n for f in fronts for n in t.neighbors(f) if state.hostile_province(fid, n)}
    scored = sorted(targets, key=lambda pid: (-_objective_value(state, faction, pid), pid))
    committed: set[str] = set()
    contested = state.road_penalty(fid)  # roads where we just ran into the enemy
    for target in scored:
        enemy = _expected_defence(state, fid, target, divs)
        candidates = [
            d
            for d in divs
            if d.id not in busy
            and d.id not in committed
            and d.location in fronts
            and d.location in t.neighbors(target)
            and _ready(d)
            and contested(d.location, target) == 1.0
        ]
        candidates = [d for d in candidates if _may_leave(state, d, target, committed)]
        if not candidates:
            continue
        def assault(d: Division) -> float:
            """One candidate's assault power on ``target``, across a river if its road crosses one.

            Args:
                d: The candidate attacker.

            Returns:
                Its assault power.
            """
            river = t.link(d.location, target).river
            return _attack_power(state, [d]) * (RIVER_PENALTY if river else 1.0)

        candidates.sort(key=lambda d: (-assault(d), d.id))
        if enemy == 0:
            group = candidates[:1]
        elif sum(assault(d) for d in candidates) >= ATTACK_RATIO * enemy / TERRAIN[t.provinces[target].terrain].attack_factor:
            group = candidates
        else:
            continue
        for d in group:
            if state.set_order(d, Order(OrderKind.ATTACK, target)):
                committed.add(d.id)
                state.emit(EventKind.AI_ORDER, faction=fid, division=d.id, order=AiOrder.ATTACK, target=target)

    # ---- 3. reserves to the front
    threat = {
        f: _power(state, [e for n in t.neighbors(f) for e in intel.known_divisions_in(state, fid, n)]) for f in fronts
    }
    assigned = {f: _power(state, [d for d in divs if d.location == f and not d.movement]) for f in fronts}
    for d in divs:
        if d.order.kind is OrderKind.MOVE and d.order.target in assigned:
            assigned[d.order.target] += d.combat_power(state.quality(d.faction))

    reserves = [
        d
        for d in divs
        if d.id not in busy
        and d.id not in committed
        and d.location not in fronts
        and d.order.kind is OrderKind.HOLD
    ]
    keep = _keepers(state, fid, reserves)
    for d in sorted(reserves, key=lambda d: d.id):
        if d.id in keep:
            continue
        best: tuple[float, str] | None = None
        for f in fronts:
            path = t.shortest_path(d.location, f, passable=lambda pid: state.friendly_province(fid, pid))
            if path is None:
                continue
            km = t.path_length(d.location, path) / 1000.0
            deficit = threat[f] * 1.2 - assigned[f] + _objective_value(state, faction, f)
            score = deficit / (1.0 + km / 50.0)
            if best is None or score > best[0]:
                best = (score, f)
        if best is None:
            continue
        if state.set_order(d, Order(OrderKind.MOVE, best[1])):
            assigned[best[1]] += d.combat_power(state.quality(d.faction))
            state.emit(EventKind.AI_ORDER, faction=fid, division=d.id, order=AiOrder.MOVE, target=best[1])


def _man_the_front(state: CampaignState, fid: str, divs: list[Division], fronts: set[str], busy: set[str]) -> None:
    """Brigades holding a front province deploy on its forward line facing the
    most threatening hostile neighbour, instead of waiting in the town centre.

    Args:
        state: The campaign.
        fid: Faction id.
        divs: The faction's divisions.
        fronts: The faction's front provinces.
        busy: Ids of divisions already given an order this round.
    """
    from . import contact

    t = state.theater
    for d in divs:
        if d.id in busy or d.battle or d.movement or d.location not in fronts or d.authority is not Authority.CAMPAIGN:
            continue
        hostile = [n for n in t.neighbors(d.location) if state.hostile_province(fid, n)]
        if not hostile:
            continue
        threat = max(hostile, key=lambda n: (_power(state, intel.known_divisions_in(state, fid, n)), -t.link(d.location, n).length, n))
        if d.deployed_toward != threat:
            d.deployed_toward = threat
            d.deploy_m = contact.deploy_progress(state, d.location, threat)
            state.dirty.add(d.id)


def _may_leave(state: CampaignState, d: Division, target: str, committed: set[str]) -> bool:
    """Keep one division in a front province that other enemies also threaten.

    Args:
        state: The campaign.
        d: The division that would attack.
        target: The province it would attack.
        committed: Ids of divisions already committed to an attack this round.

    Returns:
        Whether it may leave its province.
    """
    t = state.theater
    other_threats = any(
        intel.known_divisions_in(state, d.faction, n)
        for n in t.neighbors(d.location)
        if n != target and state.hostile_province(d.faction, n)
    )
    if not other_threats:
        return True
    staying = [
        o
        for o in state.divisions_in(d.location, d.faction)
        if o.id != d.id and o.id not in committed and not o.battle
    ]
    return bool(staying)
