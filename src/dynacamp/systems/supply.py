"""Logistics: depots, physical convoys, consumption, recovery and attrition.

Divisions close to a friendly depot (by road through friendly territory) draw
supply directly. Everyone else depends on convoys: real columns of trucks that
drive along the road graph, so they can be intercepted (and, inside a player
bubble, they become actual trucks in the game).
"""

from __future__ import annotations

import heapq
import math

from ..forces import Authority, Convoy, Division, Movement
from ..state import CampaignState, IdPrefix, Timer
from ..events import EventKind, LossReason

#: Appended to the name of a re-formed brigade.
REFORMED_SUFFIX = "(reformed)"
#: Brigades within this road distance of a depot are supplied directly (no convoy).
DIRECT_RANGE_M = 40_000.0
#: Supply a brigade draws per hour from a depot in direct range.
DIRECT_RATE_PER_HOUR = 10.0
#: Most supply one convoy carries.
CONVOY_CAPACITY = 80.0
#: Supply one truck carries.
TRUCK_CAPACITY = 5.0
#: A depot sends convoys only with at least this many trucks (and the stock to fill them).
MIN_TRUCKS = 10
#: Brigades below this supply ratio call for a convoy.
NEED_THRESHOLD = 0.85
#: Seconds between two rounds of convoy dispatch.
LOGISTICS_INTERVAL_S = 1800.0
#: Organisation a supplied brigade out of combat recovers per hour.
ORG_RECOVERY_PER_HOUR = 0.10
#: Organisation a brigade without supply loses per hour.
ORG_DECAY_NO_SUPPLY_PER_HOUR = 0.02
#: Strength a brigade without supply loses per hour.
ATTRITION_NO_SUPPLY_PER_HOUR = 0.002
# Replacements: supplied brigades out of combat get men and vehicles back.
#: Replacements for supplied brigades out of combat, as a share of nominal strength per hour (half
#: beyond direct depot range).
REINFORCE_PER_HOUR = 0.01
#: Depot stock paid per percent of strength replaced.
REINFORCE_COST = 2.0
#: A destroyed brigade is re-formed this much later...
REFORM_DELAY_S = 48 * 3600.0
#: ...at this strength, at the faction's best depot.
REFORM_STRENGTH = 0.5

#: Authorities of brigades that consume supply (campaign-simulated or delegated to the game).
ACTIVE = (Authority.CAMPAIGN, Authority.DELEGATED)


def run(state: CampaignState, dt: float) -> None:
    """Depot production, re-forming, consumption/recovery, convoy dispatch.

    Args:
        state: The campaign.
        dt: Campaign seconds elapsed.
    """
    hours = dt / 3600.0
    _produce(state, hours)
    _reform(state)
    networks = {f: depot_distances(state, f) for f in state.factions}
    for div in state.divisions.values():
        if div.authority not in ACTIVE:
            continue
        _consume_and_recover(state, div, hours, networks[div.faction])
    if state.time >= state.timers.get(Timer.LOGISTICS, 0.0):
        state.timers[Timer.LOGISTICS] = state.time + LOGISTICS_INTERVAL_S
        for faction in state.factions:
            _dispatch_convoys(state, faction, networks[faction])


def depot_distances(state: CampaignState, faction: str) -> dict[str, tuple[float, str]]:
    """Road distance from every friendly province to its nearest stocked friendly depot.

    Args:
        state: The campaign.
        faction: Faction id.

    Returns:
        For each province reachable through friendly territory, the road distance in metres and the
        id of the nearest depot.
    """
    t = state.theater
    dist: dict[str, tuple[float, str]] = {}
    heap: list[tuple[float, str, str]] = []
    for pid, p in t.provinces.items():
        if p.depot is not None and p.controller == faction:
            heap.append((0.0, pid, pid))
    heapq.heapify(heap)
    while heap:
        d, pid, depot = heapq.heappop(heap)
        if pid in dist:
            continue
        dist[pid] = (d, depot)
        for nxt in t.neighbors(pid):
            if nxt not in dist and t.provinces[nxt].controller == faction:
                heapq.heappush(heap, (d + t.link(pid, nxt).length, nxt, depot))
    return dist


def _produce(state: CampaignState, hours: float) -> None:
    """Depots produce supply up to capacity.

    Args:
        state: The campaign.
        hours: Hours elapsed.
    """
    for p in state.theater.provinces.values():
        if p.depot is not None:
            d = p.depot
            d.stock = min(d.capacity, d.stock + d.production_per_hour * hours)


def _consume_and_recover(
    state: CampaignState, div: Division, hours: float, network: dict[str, tuple[float, str]]
) -> None:
    """Consumption, direct resupply, organisation recovery, attrition and replacements.

    Args:
        state: The campaign.
        div: The division.
        hours: Hours elapsed.
        network: The faction's :func:`depot_distances`.
    """
    mult = 2.5 if div.battle else (1.5 if div.movement else 1.0)
    div.supply = max(0.0, div.supply - div.template.supply_use_per_hour * mult * hours)

    reach = network.get(div.location)
    if reach and reach[0] <= DIRECT_RANGE_M:
        depot = state.theater.provinces[reach[1]].depot
        assert depot is not None
        give = min(DIRECT_RATE_PER_HOUR * hours, div.template.supply_capacity - div.supply, depot.stock)
        div.supply += give
        depot.stock -= give

    if div.authority is not Authority.CAMPAIGN:
        return  # the game owns its losses; org only changes through reported losses
    if div.supply <= 0:
        div.org = max(0.0, div.org - ORG_DECAY_NO_SUPPLY_PER_HOUR * hours)
        div.take_losses(ATTRITION_NO_SUPPLY_PER_HOUR * hours)
    elif not div.battle:
        div.org = min(1.0, div.org + ORG_RECOVERY_PER_HOUR * hours)
        if reach and div.supply_ratio > 0.5:
            _reinforce(state, div, hours, direct=reach[0] <= DIRECT_RANGE_M, depot_pid=reach[1])


def _reinforce(state: CampaignState, div: Division, hours: float, direct: bool, depot_pid: str) -> None:
    """Replacements for a supplied brigade out of combat, paid with depot stock.

    Args:
        state: The campaign.
        div: The brigade to reinforce.
        hours: Hours elapsed.
        direct: Whether it is in direct supply range (full rate; half rate otherwise).
        depot_pid: Province of the depot that pays.
    """
    depot = state.theater.provinces[depot_pid].depot
    missing = 1.0 - div.strength
    if depot is None or missing <= 0.001:
        return
    share = min(missing, REINFORCE_PER_HOUR * hours * (1.0 if direct else 0.5))
    cost = share * 100 * REINFORCE_COST
    if depot.stock < cost:
        return
    depot.stock -= cost
    for k, nominal in div.template.elements.items():
        div.elements[k] = min(float(nominal), div.elements.get(k, 0.0) + nominal * share)


def schedule_reform(state: CampaignState, div: Division) -> None:
    """Queue a destroyed brigade for re-forming.

    Args:
        state: The campaign.
        div: The destroyed brigade.
    """
    state.reforming.append((state.time + REFORM_DELAY_S, div.faction, div.template, div.name))
    state.emit(EventKind.DIVISION_REFORMING, division=div.id, faction=div.faction, name=div.name, hours=REFORM_DELAY_S / 3600)


def _reform(state: CampaignState) -> None:
    """Re-raise destroyed brigades at the faction's best stocked depot.

    Args:
        state: The campaign.
    """
    pending = []
    for ready, faction, template, name in state.reforming:
        depots = [
            p for p in state.theater.provinces.values() if p.depot is not None and p.controller == faction
        ]
        if state.time < ready or not depots:
            pending.append((ready if depots else state.time + 12 * 3600, faction, template, name))
            continue
        home = max(depots, key=lambda p: (p.depot.stock, p.id))
        div = Division(id=state.new_id(IdPrefix.REFORMED), name=name if REFORMED_SUFFIX in name else f"{name} {REFORMED_SUFFIX}",
                       faction=faction, location=home.id, template=template, org=0.5)
        for k in div.elements:
            div.elements[k] *= REFORM_STRENGTH
        state.divisions[div.id] = div
        state.emit(EventKind.DIVISION_REFORMED, division=div.id, faction=faction, name=div.name, province=home.id)
    state.reforming[:] = pending


def _dispatch_convoys(state: CampaignState, faction: str, network: dict[str, tuple[float, str]]) -> None:
    """Send convoys from depots to brigades beyond direct supply range.

    Args:
        state: The campaign.
        faction: Faction id.
        network: The faction's :func:`depot_distances`.
    """
    t = state.theater
    incoming = {c.destination for c in state.convoys.values() if c.faction == faction and not c.returning}
    needs: dict[str, float] = {}
    for div in state.divisions.values():
        if div.faction != faction or div.authority not in ACTIVE:
            continue
        reach = network.get(div.location)
        if reach and reach[0] <= DIRECT_RANGE_M:
            continue
        if div.supply_ratio < NEED_THRESHOLD and div.location not in incoming:
            needs[div.location] = needs.get(div.location, 0.0) + div.template.supply_capacity - div.supply

    for pid, need in sorted(needs.items(), key=lambda kv: -kv[1]):
        best: tuple[float, str, list[str]] | None = None
        for dep_id, p in t.provinces.items():
            d = p.depot
            if d is None or p.controller != faction or d.trucks < MIN_TRUCKS or d.stock < TRUCK_CAPACITY * MIN_TRUCKS:
                continue
            path = t.shortest_path(dep_id, pid, passable=lambda x: t.provinces[x].controller == faction)
            if path is None:
                continue
            length = t.path_length(dep_id, path)
            if best is None or length < best[0]:
                best = (length, dep_id, path)
        if best is None:
            continue
        _, dep_id, path = best
        depot = t.provinces[dep_id].depot
        assert depot is not None
        cargo = min(need * 1.2, CONVOY_CAPACITY, depot.stock, depot.trucks * TRUCK_CAPACITY)
        trucks = math.ceil(cargo / TRUCK_CAPACITY)
        depot.stock -= cargo
        depot.trucks -= trucks
        convoy = Convoy(
            id=state.new_id(IdPrefix.CONVOY),
            faction=faction,
            location=dep_id,
            home=dep_id,
            destination=pid,
            cargo=cargo,
            trucks=float(trucks),
            movement=Movement(path),
        )
        state.convoys[convoy.id] = convoy
        state.emit(EventKind.CONVOY_DISPATCHED, convoy=convoy.id, faction=faction, origin=dep_id, destination=pid, cargo=round(cargo, 1))


def on_convoy_arrived(state: CampaignState, convoy: Convoy) -> None:
    """Deliver supply at destination, or return the trucks at home.

    Args:
        state: The campaign.
        convoy: The convoy that reached its destination (or its home).
    """
    t = state.theater
    if convoy.returning:
        depot = t.provinces[convoy.home].depot
        if depot is not None and t.provinces[convoy.home].controller == convoy.faction:
            depot.trucks += round(convoy.trucks)
            depot.stock = min(depot.capacity, depot.stock + convoy.cargo)
        del state.convoys[convoy.id]
        state.emit(EventKind.CONVOY_RETURNED, convoy=convoy.id, faction=convoy.faction, trucks=round(convoy.trucks, 1))
        return

    divs = [d for d in state.divisions_in(convoy.location, convoy.faction) if d.authority in ACTIVE]
    deficits = {d.id: d.template.supply_capacity - d.supply for d in divs}
    total = sum(deficits.values())
    delivered = 0.0
    if total > 0:
        share = min(1.0, convoy.cargo / total)
        for d in divs:
            amount = deficits[d.id] * share
            d.supply += amount
            delivered += amount
    convoy.cargo -= delivered
    state.emit(EventKind.CONVOY_DELIVERED, convoy=convoy.id, faction=convoy.faction, province=convoy.location, amount=round(delivered, 1))
    convoy.returning = True
    if not state.route_entity(convoy, convoy.home):
        on_convoy_blocked(state, convoy)


def on_convoy_blocked(state: CampaignState, convoy: Convoy) -> None:
    """Re-route a convoy whose road was cut, or lose it.

    Args:
        state: The campaign.
        convoy: The convoy whose next province turned hostile.
    """
    goal = convoy.home if convoy.returning else convoy.destination
    if state.route_entity(convoy, goal):
        return
    if not convoy.returning:
        convoy.returning = True
        if state.route_entity(convoy, convoy.home):
            return
    # Cut off: the column is lost.
    state.convoys.pop(convoy.id, None)
    state.emit(EventKind.CONVOY_DESTROYED, convoy=convoy.id, faction=convoy.faction, reason=LossReason.CUT_OFF)
