"""Quick reaction alert: scramble interceptors against intruders near our bases.

Every ``CHECK_INTERVAL_S`` each faction looks at the enemy aircraft in its air
picture (:mod:`dynacamp.systems.intel`: an intruder nobody has detected does
not trigger anything). One that comes within ``SCRAMBLE_RADIUS_M`` (30 NM) of
one of its airbases, or of an asset close to a base (a supply depot, a SAM
site or a brigade within ``NEAR_BASE_M``), gets a pair of interceptors
scrambled against it: from the threatened base if it has fighters ready,
otherwise from the best base within ``LAUNCH_RANGE_M``. No alarm for an
intruder already within ``COVERED_M`` of one of our airborne fighters (the
CAP deals with it), for enemy fighters (CAP, escort and interceptors: they
are the CAP's business, the alert is for who comes to attack, bomb or
support), for an intruder inside a known enemy SAM umbrella and, for
``REARM_S``, for an intruder already scrambled against. Interceptors never
chase into a known umbrella either: there they turn back.

Scrambles skip the ATO cycle and its startup lead: the pair takes off
``ALERT_DELAY_S`` after the alarm, flies straight at the intruder and keeps
steering for its last known position (ground control), fights it with the
ordinary air-to-air model (:func:`dynacamp.systems.air._air_combat`) and goes
home when the intruder is gone, out of contact, too far (``CHASE_LIMIT_M``
from home) or at the end of ``STATION_S``. They are CAP flights with target
kind ``flight`` (the intruder's id), so the game flies them as a CAP (engage
air targets) at the intruder's position. They spend sorties of the daily
budget like any other flight, but the alert does not wait for it.
"""

from __future__ import annotations

from ..air import ACTIVE_STATUSES, CAP_ALT_M, FlightStatus, Task
from ..geo import Vec2
from ..state import CampaignState
from ..units import M_PER_NM as NM
from . import ato, intel, routing
from ..events import EventKind

#: Seconds between two checks of the alert.
CHECK_INTERVAL_S = 60.0
#: Alarm distance from a base or a protected asset.
SCRAMBLE_RADIUS_M = 30 * NM
#: Assets this close to a base are protected too.
NEAR_BASE_M = 20 * NM
#: From the alarm to wheels up, seconds.
ALERT_DELAY_S = 5 * 60.0
#: Time at the intruder before giving up, seconds.
STATION_S = 30 * 60.0
#: Interceptors do not chase further than this from home.
CHASE_LIMIT_M = 60 * NM
#: Only tracks this fresh raise the alarm, seconds.
CONTACT_MAX_AGE_S = 120.0
#: Other bases help only if this close to the intruder.
LAUNCH_RANGE_M = 60 * NM
#: An intruder this close to one of our fighters is theirs already.
COVERED_M = 40_000.0
#: No new scramble against the same intruder for this long, seconds.
REARM_S = 60 * 60.0
#: Concurrent scrambled flights per faction.
MAX_ACTIVE = 4
#: Aircraft in a scrambled flight.
SIZE = 2


def run(state: CampaignState, dt: float) -> None:
    """Check every faction with fighters, every ``CHECK_INTERVAL_S``.

    Args:
        state: The campaign.
        dt: Campaign seconds elapsed (unused: the check runs on its own timer).
    """
    if state.time < state.timers.get("scramble", 0.0):
        return
    state.timers["scramble"] = state.time + CHECK_INTERVAL_S
    for fid in state.factions:
        if any(sq.faction == fid and Task.CAP in sq.tasks for sq in state.squadrons.values()):
            check(state, fid)


def active(state: CampaignState, fid: str) -> list:
    """Live scrambled flights of a faction.

    Args:
        state: The campaign.
        fid: Faction id.

    Returns:
        The faction's scramble flights not done or cancelled.
    """
    return [
        f for f in state.flights.values()
        if f.faction == fid and f.target_kind == "flight" and f.status not in (FlightStatus.DONE, FlightStatus.CANCELLED)
    ]


def protected(state: CampaignState, fid: str) -> list[tuple[Vec2, str, str]]:
    """What the faction's alert covers: its airbases and the assets close to them.

    Args:
        state: The campaign.
        fid: Faction id.

    Returns:
        (position, label, airbase id) for each base and asset.
    """
    t = state.theater
    bases = [b for b in state.airbases.values() if b.owner(t) == fid]
    out = [(b.position, b.name, b.id) for b in bases]
    assets: list[tuple[Vec2, str]] = []
    for p in t.provinces.values():
        if p.depot is not None and p.controller == fid:
            assets.append((p.center, f"supply depot {p.name}"))
    for u in state.air_defense.values():
        if u.faction == fid:
            assets.append((u.position(t), f"{u.system.name} site"))
    for d in state.divisions.values():
        if d.faction == fid:
            assets.append((d.position(t), d.name))
    for pos, label in assets:
        near = [b for b in bases if b.position.distance_to(pos) <= NEAR_BASE_M]
        if near:
            base = min(near, key=lambda b: b.position.distance_to(pos))
            out.append((pos, f"{label} near {base.name}", base.id))
    return out


def check(state: CampaignState, fid: str) -> None:
    """Scramble a pair against every fresh enemy track inside the alarm radius
    of a base or protected asset that nobody is intercepting yet.

    Args:
        state: The campaign.
        fid: Faction id.
    """
    busy = active(state, fid)
    if len(busy) >= MAX_ACTIVE:
        return
    chased = {f.target_id for f in busy}
    umbrellas = routing.threats(state, fid, CAP_ALT_M)
    points = protected(state, fid)
    if not points:
        return
    fighters = [
        f.pos for f in state.flights.values()
        if f.faction == fid and f.task.air_to_air and f.status in ACTIVE_STATUSES and f.ai_count > 0
    ]
    for c in sorted(intel.known(state, fid, "flight", CONTACT_MAX_AGE_S), key=lambda c: c.entity_id):
        intruder = state.flights.get(c.entity_id)
        if c.entity_id in chased or intruder is None or intruder.status not in ACTIVE_STATUSES:
            continue
        if not state.at_war(fid, intruder.faction) or intruder.task.air_to_air:
            continue
        if state.timers.get(f"scramble:{fid}:{c.entity_id}", 0.0) > state.time:
            continue
        pos, label, base_id = min(points, key=lambda p: p[0].distance_to(c.position))
        if pos.distance_to(c.position) > SCRAMBLE_RADIUS_M:
            continue
        if any(p.distance_to(c.position) <= COVERED_M for p in fighters):
            continue
        if any(t.contains(c.position) for t in umbrellas):
            continue
        if launch(state, fid, c, base_id, label):
            state.timers[f"scramble:{fid}:{c.entity_id}"] = state.time + REARM_S
            if len(busy) + 1 >= MAX_ACTIVE:
                return
            busy.append(None)


def launch(state: CampaignState, fid: str, c: intel.Contact, base_id: str, label: str) -> bool:
    """Scramble a pair of interceptors against the track ``c``.

    Args:
        state: The campaign.
        fid: Defending faction.
        c: The intruder's contact.
        base_id: The threatened airbase (its fighters go first).
        label: What the intruder threatens, for the package and the log.

    Returns:
        True if a pair is on its way.
    """
    found = ato._allocate(state, fid, Task.CAP, c.position, SIZE, require=lambda sq: sq.airbase == base_id)
    found = found or ato._allocate(
        state, fid, Task.CAP, c.position, SIZE,
        require=lambda sq: state.airbases[sq.airbase].position.distance_to(c.position) <= LAUNCH_RANGE_M,
    )
    if found is None:
        return False
    sq, dist = found
    home = state.airbases[sq.airbase].position
    what = c.detail.get("aircraft_name") or "unidentified aircraft"
    tot = state.time + ALERT_DELAY_S + dist / (sq.aircraft_type.cruise_kmh / 3.6)
    r = ato.Request(Task.CAP, 10.0, f"scramble: {what} over {label}", c.position, "flight", c.entity_id)
    pkg = ato._new_package(state, fid, r, tot)
    f = ato._add_flight(state, pkg, sq, Task.CAP, SIZE, tot, r, ([home, c.position, home], 1))
    f.station_until = tot + STATION_S
    state.emit(
        EventKind.SCRAMBLE,
        flight=f.id,
        callsign=f.callsign,
        faction=fid,
        base=state.airbases[sq.airbase].name,
        intruder=c.entity_id,
        aircraft=what,
        threatened=label,
        distance_nm=round(c.position.distance_to(home) / NM),
    )
    ato._announce(state, pkg)
    return True


def pursue(state: CampaignState, f) -> None:
    """Steer a scrambled flight for its intruder's last known position, or send
    it home once the intruder is gone, lost from the picture or too far.

    Args:
        state: The campaign.
        f: A flight with target kind ``flight``.
    """
    if f.status is FlightStatus.RTB or f.wp_index > f.target_index and f.status is not FlightStatus.ON_STATION:
        return
    target = state.flights.get(f.target_id)
    pos = None
    if target is not None and target.status in ACTIVE_STATUSES and target.size > 0:
        pos = intel.located(state, f.faction, f.target_id, intel.TTL_S["flight"])
    if pos is not None and (
        pos.distance_to(f.waypoints[-1]) > CHASE_LIMIT_M
        or any(t.contains(pos) for t in routing.threats(state, f.faction, CAP_ALT_M))
    ):
        pos = None
    if pos is None:
        f.status = FlightStatus.RTB
        f.wp_index = f.target_index + 1
        f.station_until = min(f.station_until, state.time)
        return
    f.waypoints[f.target_index] = pos
