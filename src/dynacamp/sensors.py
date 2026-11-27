"""What units can see: one sensor model for the air picture and for combat.

The same rules decide what a faction *knows* (:mod:`dynacamp.systems.intel`)
and whom a unit *fights*:

* **aircraft radar** - a cone ahead of the aircraft along its track, as wide as
  the radar's scan (``SCAN_HALF_ANGLE_RAD``) and as long as the type's search
  range (:attr:`dynacamp.air.AircraftType.air_radar_m`); targets are tracked
  (and engaged by fighters) within ``TRACK_FACTOR`` of it. Low flyers are harder
  to pick out of the ground clutter (``LOOK_DOWN``). An AWACS sees all round;
* **eyes on the ground** - ``EYEBALL_M``: brigades, convoys and sites see each
  other within direct-fire range, and hostile forces that close fight (or are
  overrun, see :mod:`dynacamp.systems.combat`).

**Jamming (ECM).** Radars see a jammed aircraft much later (:func:`jam`):
within ``JAMMER_RANGE_M`` of a friendly SEAD flight, the side's stand-off
jammer (in DCS there are no AI jammer aircraft: SEAD flights carry the pods),
their range against it drops to ``ESCORT_JAM`` of normal; an aircraft with a
self-protection pod in its loadout to ``SELF_JAM``. Inside ``BURN_THROUGH_M``
radars burn through. This holds for every radar: fighters' cones, AWACS,
ground radars and EWRs, SAM sites. Radar-guided missiles (air-to-air and SAMs)
hit less often a jammed target (:func:`jam_pk`); heat seekers and guns do not
care. A jammer gives itself away: ELINT locates it from far, roughly
(:mod:`dynacamp.systems.intel`).

Ground recon, ground radars and ELINT, which only feed the intelligence picture,
stay in :mod:`dynacamp.systems.intel`.
"""

from __future__ import annotations

import math

from typing import TYPE_CHECKING

from .air import ACTIVE_STATUSES, Flight, FlightStatus, Task, flight_altitude
from .geo import Vec2

if TYPE_CHECKING:
    from .state import CampaignState

#: Half aperture of a fighter's radar scan (about 60 degrees each side).
SCAN_HALF_ANGLE_RAD = math.radians(60.0)
#: Radar range factor against a target below :data:`LOW_ALT_M`.
LOOK_DOWN = 0.7
#: Altitude below which an aircraft is in the ground clutter.
LOW_ALT_M = 300.0
#: A radar tracks (and a fighter engages) within this share of its search range.
TRACK_FACTOR = 0.6
#: Direct fire and visual range on the ground.
EYEBALL_M = 3_000.0
# Jamming
#: A SEAD flight's jammers cover friendly aircraft this close.
JAMMER_RANGE_M = 40_000.0
#: Radar range factor against an aircraft under escort jamming (the SEAD flight's ECM pods
#: stand in for a dedicated jammer: DCS has no AI EA-6B or EA-18G).
ESCORT_JAM = 0.5
#: Radar range factor against an aircraft with its own self-protection pod.
SELF_JAM = 0.7
#: Radars see through any jamming this close.
BURN_THROUGH_M = 10_000.0
#: Kill probability factor of radar-guided missiles at an aircraft under escort jamming.
ESCORT_JAM_PK = 0.6
#: Kill probability factor of radar-guided missiles at an aircraft with a self-protection pod.
SELF_JAM_PK = 0.8


def track(f: Flight) -> float:
    """Direction the aircraft points, radians (0 = north, clockwise): along its
    orbit while on station, else its last track over the ground.

    Args:
        f: The flight.

    Returns:
        The heading of the aircraft's nose.
    """
    if f.status is FlightStatus.ON_STATION:
        return (f.orbit_angle + math.pi / 2) % (2 * math.pi)
    return f.track


def low(f: Flight) -> bool:
    """True if the flight is down in the ground clutter.

    Args:
        f: The flight.

    Returns:
        Whether it flies at or below ``LOW_ALT_M``.
    """
    return flight_altitude(f) <= LOW_ALT_M


def in_cone(origin: Vec2, heading: float, half_angle: float, reach: float, point: Vec2) -> bool:
    """True if ``point`` is within ``reach`` of ``origin`` and at most
    ``half_angle`` off ``heading`` (radians).

    Args:
        origin: Apex of the cone.
        heading: Axis of the cone, radians.
        half_angle: Half aperture, radians.
        reach: Length of the cone, metres.
        point: The point to test.

    Returns:
        Whether the point is in the cone (always True within a metre of the apex).
    """
    d = origin.distance_to(point)
    if d > reach:
        return False

    if d < 1.0:
        return True

    off = (origin.heading_to(point) - heading + math.pi) % (2 * math.pi) - math.pi
    return abs(off) <= half_angle


def escort_jammer(state: CampaignState, f: Flight) -> Flight | None:
    """A friendly SEAD flight (not ``f`` itself) close enough to jam for ``f``.

    Args:
        state: The campaign.
        f: The flight to protect.

    Returns:
        The jamming SEAD flight, or None.
    """
    for o in state.flights.values():
        if (
            o is not f and o.faction == f.faction and o.task is Task.SEAD and o.ai_count > 0
            and o.status in ACTIVE_STATUSES and o.pos.distance_to(f.pos) <= JAMMER_RANGE_M
        ):
            return o
    return None


def jam(state: CampaignState, f: Flight) -> float:
    """Factor on enemy radar range against ``f``: ``ESCORT_JAM`` under a SEAD
    flight's jamming, ``SELF_JAM`` with its own ECM pod, else 1.

    Args:
        state: The campaign.
        f: The target flight.

    Returns:
        The range factor (1 = no jamming).
    """
    from . import loadouts

    if escort_jammer(state, f) is not None:
        return ESCORT_JAM

    if loadouts.has_ecm_pod(f.aircraft_type.id, f.task):
        return SELF_JAM

    return 1.0


def reach(range_m: float, jamming: float = 1.0) -> float:
    """Range of a radar of ``range_m`` against a target jamming by ``jamming``
    (never less than the burn-through distance, nor more than the range).

    Args:
        range_m: Nominal range of the radar, metres.
        jamming: The target's :func:`jam` factor.

    Returns:
        The effective range in metres.
    """
    if jamming >= 1.0:
        return range_m

    return max(range_m * jamming, min(range_m, BURN_THROUGH_M))


def jam_pk(jamming: float, target: Flight) -> float:
    """Factor on the kill probability of a radar-guided missile at a jammed
    target (a SEAD flight's own pods are already in its SAM odds).

    Args:
        jamming: The target's :func:`jam` factor.
        target: The target flight.

    Returns:
        The factor on the kill probability (1 = no jamming).
    """
    if jamming <= ESCORT_JAM:
        return ESCORT_JAM_PK

    if jamming < 1.0 and target.task is not Task.SEAD:
        return SELF_JAM_PK

    return 1.0


def radar_sees(
    observer: Flight, origin: Vec2, target: Flight, where: Vec2, track_only: bool = False, jamming: float = 1.0
) -> bool:
    """True if ``observer``'s air-search radar, at ``origin``, sees ``target`` at ``where``.

    Args:
        observer: The aircraft with the radar.
        origin: Its position.
        target: The aircraft looked for.
        where: Its position.
        track_only: Within tracking range only (``TRACK_FACTOR`` of the search
            range): what a fighter engages, as opposed to what it reports.
        jamming: The target's :func:`jam` factor.

    Returns:
        Whether the target is within the radar's reach (and, except for AWACS, within its scan
        cone).
    """
    base = observer.aircraft_type.air_radar_m * (LOOK_DOWN if low(target) else 1.0) * (TRACK_FACTOR if track_only else 1.0)
    if base <= 0:
        return False
    r = reach(base, jamming)
    if observer.task is Task.AWACS:
        return origin.distance_to(where) <= r
    return in_cone(origin, track(observer), SCAN_HALF_ANGLE_RAD, r, where)
