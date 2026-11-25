"""Province control: captures and objectives."""

from __future__ import annotations

from ..state import CampaignState
from ..events import EventKind


def capture(state: CampaignState, pid: str, faction: str, by: str) -> None:
    """Change a province's controller and apply the consequences.

    Burns half the depot stock, overruns air defence sites and destroys parked
    enemy convoys, and reports objectives.

    Args:
        state: The campaign.
        pid: Province id.
        faction: New controller.
        by: Id of the capturing division.
    """
    province = state.theater.provinces[pid]
    previous = province.controller
    if previous == faction:
        return
    province.controller = faction
    state.emit(EventKind.PROVINCE_CAPTURED, province=pid, previous=previous, faction=faction, by=by)

    if province.depot is not None:
        # Retreating troops burn what they cannot carry.
        province.depot.stock *= 0.5
        province.depot.trucks = 0

    # Air defence sites still in the province are overrun.
    for u in list(state.air_defense.values()):
        if u.location == pid and state.at_war(faction, u.faction) and not (u.movement and u.movement.progress > 0):
            del state.air_defense[u.id]
            state.emit(EventKind.SAM_DESTROYED, sam=u.id, system=u.system.name, faction=u.faction, province=pid, reason="overrun")
            from . import air_defense

            air_defense.schedule_replacement(state, u)

    # Enemy convoys parked here are lost.
    for convoy in list(state.convoys.values()):
        if convoy.location == pid and state.at_war(faction, convoy.faction) and not (
            convoy.movement and convoy.movement.progress > 0
        ):
            del state.convoys[convoy.id]
            state.emit(EventKind.CONVOY_DESTROYED, convoy=convoy.id, faction=convoy.faction, reason="captured")

    for f in state.factions.values():
        for obj in f.objectives:
            if obj.province == pid:
                state.emit(
                    EventKind.OBJECTIVE_CHANGED,
                    province=pid,
                    faction=f.id,
                    achieved=(faction == f.id),
                    priority=obj.priority,
                )
