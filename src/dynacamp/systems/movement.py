"""Movement along the province graph."""

from __future__ import annotations

from ..forces import Convoy, Division, MobileEntity, OrderKind
from ..state import CampaignState
from ..world import LINK_SPEED, TERRAIN, infra_factor
from . import contact, control, supply
from ..events import EventKind


def run(state: CampaignState, dt: float) -> None:
    """Advance every campaign-simulated entity along its route."""
    for ent in list(state.entities()):
        if not ent.simulated_by_campaign or not ent.movement or not ent.movement.path:
            continue
        if isinstance(ent, Division):
            if ent.battle:
                continue
            ent.deployed_toward = None  # leaving its defensive position
        _advance(state, ent, dt)


def link_speed(state: CampaignState, ent: MobileEntity, a: str, b: str) -> float:
    """Speed of ``ent`` on the link ``a -> b`` (link kind, terrain, infrastructure)."""
    link = state.theater.link(a, b)
    dest = state.theater.provinces[b]
    base = ent.speed_ms  # type: ignore[attr-defined]
    return base * LINK_SPEED[link.kind] * TERRAIN[dest.terrain].move_factor * infra_factor(dest.infrastructure)


def _advance(state: CampaignState, ent: MobileEntity, dt: float) -> None:
    """Move along the route for ``dt`` seconds: withdraw, close in to attack, or travel."""
    t = state.theater
    remaining = dt
    while remaining > 0 and ent.movement and ent.movement.path:
        mv = ent.movement
        nxt = mv.path[0]
        speed = link_speed(state, ent, ent.location, nxt)
        if mv.back:
            # Withdrawing along the road to the province it came from.
            if mv.progress <= speed * remaining:
                ent.movement = None
                state.emit(EventKind.WITHDRAWAL_COMPLETED, entity=ent.id, province=ent.location)
            else:
                mv.progress -= speed * remaining
            return
        if state.hostile_province(ent.faction, nxt):
            if isinstance(ent, Division) and ent.order.kind is OrderKind.ATTACK:
                if state.hostile_divisions_in(ent.faction, nxt):
                    # Close in to the assault position, in contact with the defence
                    # line; the combat system opens the battle from there.
                    limit = contact.assault_progress(state, ent, nxt)
                    if mv.progress < limit:
                        mv.progress = min(limit, mv.progress + speed * remaining)
                    return
            else:
                _blocked(state, ent)
                return
        length = t.link(ent.location, nxt).length
        needed = (length - ent.movement.progress) / speed
        if needed <= remaining:
            remaining -= needed
            arrive(state, ent)
        else:
            ent.movement.progress += speed * remaining
            remaining = 0


def arrive(state: CampaignState, ent: MobileEntity) -> None:
    """Entity reaches the next province on its path. Also used for delegated entities."""
    assert ent.movement and ent.movement.path
    nxt = ent.movement.path.pop(0)
    ent.location = nxt
    ent.movement.progress = 0.0
    if not ent.movement.path:
        ent.movement = None

    if isinstance(ent, Division):
        ent.deployed_toward = None
        if state.hostile_province(ent.faction, nxt) and not state.hostile_divisions_in(ent.faction, nxt):
            control.capture(state, nxt, ent.faction, by=ent.id)
        if ent.movement is None and ent.order.kind is not OrderKind.HOLD:
            state.emit(EventKind.ORDER_COMPLETED, division=ent.id, order=ent.order.kind.value, province=nxt)
            ent.order.kind, ent.order.target = OrderKind.HOLD, None
    elif isinstance(ent, Convoy) and ent.movement is None:
        supply.on_convoy_arrived(state, ent)


def _blocked(state: CampaignState, ent: MobileEntity) -> None:
    """The next province turned hostile: re-route, turn back, or stop."""
    if isinstance(ent, Convoy):
        supply.on_convoy_blocked(state, ent)
        return
    if not isinstance(ent, Division):
        # e.g. an air defence site: stop where it is; its commander will re-plan.
        ent.movement = None
        return
    if ent.order.target and state.set_order(ent, ent.order):
        if ent.movement and ent.movement.path and not state.hostile_province(ent.faction, ent.movement.path[0]):
            return
    # No way forward: turn back to the province it left (approximated as a snap back).
    ent.movement = None
    state.set_order(ent, type(ent.order)())
    state.emit(EventKind.ORDER_CANCELLED, division=ent.id, reason="route blocked")
