"""Land combat, HOI4-flavoured.

A battle is fought *for* a province: attackers sit in adjacent provinces,
defenders are inside. Every hour each side fires; shots the enemy cannot absorb
(defense for defenders, breakthrough for attackers) hit four times as often.
Hits erode organisation fast and strength slowly. A side whose organisation
collapses retreats; when no defender is left, the attackers advance.

Battles touching a player bubble are *delegated*: the game produces the losses
(reported by the adapter) and this module only checks who broke.
"""

from __future__ import annotations

from ..forces import Authority, Division, Movement, Order, OrderKind
from ..state import Battle, CampaignState
from ..world import TERRAIN
from . import contact

ORG_PER_HIT = 0.9
STRENGTH_PER_HIT = 0.2
ENTRENCHMENT = 1.25
RIVER_PENALTY = 0.7
DEFENDER_BREAK_ORG = 0.1
ATTACKER_BREAK_ORG = 0.15
DESTROYED_STRENGTH = 0.05
# Strength lost in the game is converted to organisation lost at this rate.
DELEGATED_ORG_PER_STRENGTH = 3.0

FROZEN = (Authority.MATERIALIZING, Authority.DEMATERIALIZING)


def run(state: CampaignState, dt: float) -> None:
    """Trapped divisions, new battles, damage, breaks and battle ends."""
    _resolve_trapped(state)
    _open_battles(state)
    for battle in list(state.battles.values()):
        _refresh(state, battle)
        if not battle.attackers or not battle.defenders:
            _end(state, battle)
            continue
        if is_delegated(state, battle):
            pass  # the game decides the losses
        elif any(state.divisions[i].authority in FROZEN for i in battle.attackers + battle.defenders):
            continue
        else:
            _fight(state, battle, dt / 3600.0)
        _check_breaks(state, battle)
        if not battle.attackers or not battle.defenders:
            _end(state, battle)
    _remove_destroyed(state)


def is_delegated(state: CampaignState, battle: Battle) -> bool:
    """True if the game simulates any participant (it then produces the losses)."""
    return any(state.divisions[i].authority is Authority.DELEGATED for i in battle.attackers + battle.defenders)


def apply_reported_losses(div: Division, strength_lost: float) -> None:
    """Called when the game reports casualties for a delegated division."""
    if strength_lost > 0:
        div.org = max(0.0, div.org - strength_lost * DELEGATED_ORG_PER_STRENGTH)


# ---------------------------------------------------------------- opening


def _open_battles(state: CampaignState) -> None:
    """Open or join battles for attackers that reached their assault position."""
    for div in state.divisions.values():
        if div.battle or div.authority in FROZEN or div.order.kind is not OrderKind.ATTACK:
            continue
        if not contact.at_assault_position(state, div):
            continue  # still closing in: no battle until the forces are in contact
        target = div.movement.path[0]
        if not state.hostile_province(div.faction, target):
            continue
        defenders = state.hostile_divisions_in(div.faction, target)
        if not defenders:
            continue
        battle = next((b for b in state.battles.values() if b.province == target), None)
        if battle is None:
            battle = Battle(
                id=state.new_id("bt"),
                province=target,
                attacker_faction=div.faction,
                defender_faction=state.theater.provinces[target].controller,
                attackers=[],
                defenders=[],
                started=state.time,
            )
            state.battles[battle.id] = battle
            state.emit("battle_started", battle=battle.id, province=target, attacker=div.faction, defender=battle.defender_faction)
        elif battle.attacker_faction != div.faction:
            continue
        battle.attackers.append(div.id)
        battle.attack_from[div.id] = div.location
        div.battle = battle.id


def _refresh(state: CampaignState, battle: Battle) -> None:
    """Drop participants that left/died, pull in defenders that arrived."""
    battle.attackers = [
        i for i in battle.attackers if i in state.divisions and state.divisions[i].battle == battle.id
    ]
    battle.defenders = [
        i
        for i in battle.defenders
        if i in state.divisions
        and state.divisions[i].battle == battle.id
        and state.divisions[i].location == battle.province
    ]
    for d in state.hostile_divisions_in(battle.attacker_faction, battle.province):
        if d.id in battle.defenders:
            continue
        if d.battle and d.battle in state.battles:
            # It was attacking out of this province: it must turn and defend.
            other = state.battles[d.battle]
            if d.id in other.attackers:
                other.attackers.remove(d.id)
        d.battle = battle.id
        battle.defenders.append(d.id)
    _deploy_defenders(state, battle)


def _deploy_defenders(state: CampaignState, battle: Battle) -> None:
    """Defenders man forward defence lines facing the attack axes (round robin)."""
    axes = sorted(set(battle.attack_from.get(i) for i in battle.attackers) - {None})
    if not axes:
        return
    t = state.theater
    for n, i in enumerate(battle.defenders):
        d = state.divisions[i]
        if d.movement:
            continue
        axis = axes[n % len(axes)]
        if axis in t.neighbors(d.location):
            d.deployed_toward = axis
            d.deploy_m = contact.deploy_progress(state, d.location, axis)


def withdraw(state: CampaignState, div: Division) -> None:
    """Call off an attack: drive back along the road to the start line."""
    div.order = Order()
    if div.movement and div.movement.progress > 0:
        div.movement.back = True
    else:
        div.movement = None
    state.dirty.add(div.id)


# ---------------------------------------------------------------- fighting


def _fight(state: CampaignState, battle: Battle, hours: float) -> None:
    """One integration step of damage between the two sides."""
    t = state.theater
    attackers = [state.divisions[i] for i in battle.attackers]
    defenders = [state.divisions[i] for i in battle.defenders]
    terrain = TERRAIN[t.provinces[battle.province].terrain]

    def readiness(d: Division) -> float:
        """Firepower multiplier from organisation and supply."""
        return (0.5 + 0.5 * d.org) * (0.6 if d.supply <= 0 else 1.0)

    def side_hardness(ds: list[Division]) -> float:
        """Average hardness of a side, weighted by size."""
        sizes = [_size(d) for d in ds]
        total = sum(sizes)
        return sum(d.hardness() * s for d, s in zip(ds, sizes)) / total if total else 0.0

    def_hard, att_hard = side_hardness(defenders), side_hardness(attackers)

    att_fire = 0.0
    for d in attackers:
        mod = readiness(d) * terrain.attack_factor
        if t.link(battle.attack_from[d.id], battle.province).river:
            mod *= RIVER_PENALTY
        att_fire += d.firepower(def_hard) * mod
    def_fire = sum(d.firepower(att_hard) * readiness(d) for d in defenders)
    def_absorb = sum(d.defense() * readiness(d) for d in defenders) * ENTRENCHMENT
    att_absorb = sum(d.breakthrough() * readiness(d) for d in attackers)

    noise = state.rng.uniform(0.8, 1.2)
    hits_on_def = (0.1 * min(att_fire, def_absorb) + 0.4 * max(0.0, att_fire - def_absorb)) * noise
    noise = state.rng.uniform(0.8, 1.2)
    hits_on_att = (0.1 * min(def_fire, att_absorb) + 0.4 * max(0.0, def_fire - att_absorb)) * noise

    _distribute(defenders, hits_on_def * hours)
    _distribute(attackers, hits_on_att * hours)


def _size(d: Division) -> float:
    """Number of combat elements."""
    return sum(n for k, n in d.elements.items() if k != "truck")


def _distribute(side: list[Division], hits: float) -> None:
    """Spread hits over a side as organisation and strength losses."""
    total = sum(_size(d) for d in side)
    if total <= 0:
        return
    org_loss = hits * ORG_PER_HIT / total
    strength_loss = hits * STRENGTH_PER_HIT / total
    for d in side:
        d.org = max(0.0, d.org - org_loss)
        d.take_losses(strength_loss)


# ---------------------------------------------------------------- outcomes


def _check_breaks(state: CampaignState, battle: Battle) -> None:
    """Attackers that broke withdraw; defenders that broke retreat."""
    for i in list(battle.attackers):
        d = state.divisions[i]
        if d.org < ATTACKER_BREAK_ORG or d.strength < DESTROYED_STRENGTH:
            battle.attackers.remove(i)
            d.battle = None
            withdraw(state, d)
            state.emit("attack_halted", battle=battle.id, division=i, org=round(d.org, 3))
    for i in list(battle.defenders):
        d = state.divisions[i]
        if d.org < DEFENDER_BREAK_ORG or d.strength < DESTROYED_STRENGTH:
            battle.defenders.remove(i)
            d.battle = None
            _retreat(state, d, battle=battle)


def _resolve_trapped(state: CampaignState) -> None:
    """Divisions standing in a province the enemy took (e.g. they were on the
    road when it fell and had to turn back). With no enemy troops there they
    retake it; otherwise they try to break out, or surrender."""
    from . import control

    for div in list(state.divisions.values()):
        if div.battle or div.authority in FROZEN or (div.movement and div.movement.progress > 0):
            continue
        if not state.hostile_province(div.faction, div.location):
            continue
        if div.order.kind is OrderKind.ATTACK and div.movement:
            continue  # it is just about to attack out of here
        if not state.hostile_divisions_in(div.faction, div.location):
            control.capture(state, div.location, div.faction, by=div.id)
        else:
            _retreat(state, div, avoid=set())


def _retreat(state: CampaignState, div: Division, battle: Battle | None = None, avoid: set[str] | None = None) -> None:
    """Retreat a broken defender to a safe neighbour, or destroy it if encircled.

    Args:
        state: The campaign.
        div: The division.
        battle: The battle (its attack axes are avoided).
        avoid: Provinces to avoid when there is no battle.
    """
    t = state.theater
    attacker_sources = set(battle.attack_from.values()) if battle else (avoid or set())
    options = [
        n
        for n in t.neighbors(div.location)
        if state.friendly_province(div.faction, n)
        and n not in attacker_sources
        and not state.hostile_divisions_in(div.faction, n)
    ]
    if not options or div.strength < DESTROYED_STRENGTH:
        div.elements = {k: 0.0 for k in div.elements}
        state.emit("division_destroyed", division=div.id, faction=div.faction, province=div.location, reason="encircled" if not options else "shattered")
        return

    def danger(pid: str) -> int:
        """Number of hostile neighbours of a province."""
        return sum(1 for n in t.neighbors(pid) if state.hostile_province(div.faction, n))

    dest = min(options, key=lambda pid: (danger(pid), pid))
    div.order = Order(OrderKind.MOVE, dest)
    div.deployed_toward = None
    # A metre on the road: the division is already out of the province, so it
    # cannot be pulled back into this battle as a defender.
    div.movement = Movement([dest], progress=1.0)
    state.dirty.add(div.id)
    state.emit("division_retreated", division=div.id, faction=div.faction, origin=div.location, destination=dest)


def _end(state: CampaignState, battle: Battle) -> None:
    """Close a battle and let the victorious attackers advance."""
    winner = battle.attacker_faction if battle.attackers else battle.defender_faction
    for i in battle.attackers + battle.defenders:
        if i in state.divisions and state.divisions[i].battle == battle.id:
            state.divisions[i].battle = None
    del state.battles[battle.id]
    state.emit("battle_ended", battle=battle.id, province=battle.province, winner=winner, hours=round((state.time - battle.started) / 3600, 1))
    if battle.attackers:
        _advance_after_victory(state, battle)


def _advance_after_victory(state: CampaignState, battle: Battle) -> None:
    """The fittest half of the attackers advance (the movement system moves them
    in and captures the province); the others hold the line they attacked from,
    so a breakthrough does not leave the start line empty behind it."""
    survivors = sorted(
        (state.divisions[i] for i in battle.attackers if i in state.divisions),
        key=lambda d: (-d.org * d.strength, d.id),
    )
    advancing = max(1, (len(survivors) + 1) // 2)
    for d in survivors[advancing:]:
        withdraw(state, d)


def _remove_destroyed(state: CampaignState) -> None:
    """Remove destroyed divisions and schedule their re-forming."""
    for div in list(state.divisions.values()):
        if div.strength < DESTROYED_STRENGTH and div.authority is Authority.CAMPAIGN:
            if div.battle and div.battle in state.battles:
                b = state.battles[div.battle]
                b.attackers = [i for i in b.attackers if i != div.id]
                b.defenders = [i for i in b.defenders if i != div.id]
            del state.divisions[div.id]
            state.emit("division_removed", division=div.id, faction=div.faction)
            from . import supply

            supply.schedule_reform(state, div)
