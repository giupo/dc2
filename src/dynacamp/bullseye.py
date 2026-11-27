"""Bullseye: each side's fixed reference point for positions on the radio.

"Bandits BULLS 045/32" is bearing 045 and 32 NM from the side's bullseye.
The point is :attr:`dynacamp.state.Faction.bullseye`, chosen by the scenario
(a point or a province) or, by default, the centre of the provinces the side
holds at the start. It never moves during the campaign: pilots learn it, and
the game has it in the mission file (``build-miz`` writes it there, one per
coalition, so factions on the same side share it).

Bearings are **magnetic**, as in the cockpits: the true bearing (great circle,
from latitude/longitude when the theatre has a projection, else the plane's
grid) minus the magnetic variation of the theatre (:data:`MAGNETIC_VARIATION_DEG`,
approximate for the mission year; DCS computes its own from the date).
"""

from __future__ import annotations

import math
from collections.abc import Iterable

from .geo import Vec2
from .projection import TheaterMap, to_latlon
from .state import CampaignState
from .units import M_PER_NM, UnitDomain

#: Magnetic variation of each theatre, degrees, east positive: around 2024, from the World
#: Magnetic Model at the theatre's centre (to verify against what DCS computes from the date).
MAGNETIC_VARIATION_DEG = {TheaterMap.CAUCASUS: 6.5, TheaterMap.SYRIA: 5.0}


def centre(points: Iterable[Vec2]) -> Vec2:
    """Centroid of some points (the default bullseye: of a side's provinces).

    Args:
        points: The points.

    Returns:
        Their centroid.

    Raises:
        ValueError: With no points.
    """
    pts = list(points)
    if not pts:
        raise ValueError("no points")
    return Vec2(sum(p.x for p in pts) / len(pts), sum(p.y for p in pts) / len(pts))


def true_bearing(theater: str, a: Vec2, b: Vec2) -> float:
    """True bearing from ``a`` to ``b``, degrees [0, 360): great circle on
    WGS84 latitude/longitude, or the plane's grid without a projection.

    Args:
        theater: Theatre name (picks the projection).
        a: From this point.
        b: To this point.

    Returns:
        The true bearing in degrees.
    """
    la, lb = to_latlon(theater, a), to_latlon(theater, b)
    if la is None or lb is None:
        return math.degrees(a.heading_to(b)) % 360
    p1, p2 = math.radians(la[0]), math.radians(lb[0])
    dl = math.radians(lb[1] - la[1])
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360


def bearing_range(state: CampaignState, fid: str, point: Vec2) -> tuple[float, float]:
    """Magnetic bearing (degrees) and range (metres) of ``point`` from the
    bullseye of faction ``fid``.

    Args:
        state: The campaign.
        fid: Faction whose bullseye is the reference.
        point: The point to describe.

    Returns:
        Magnetic bearing in degrees and range in metres.
    """
    bulls = state.factions[fid].bullseye
    theater = state.theater.name
    variation = MAGNETIC_VARIATION_DEG.get(theater.lower(), 0.0)
    return (true_bearing(theater, bulls, point) - variation) % 360, bulls.distance_to(point)


def label(state: CampaignState, fid: str, point: Vec2, units: UnitDomain) -> str:
    """``point`` as the radio says it, ``"045/32"``: magnetic bearing and range
    from faction ``fid``'s bullseye (NM in imperial units, km in metric).

    Args:
        state: The campaign.
        fid: Faction whose bullseye is the reference.
        point: The point to describe.
        units: Unit system for the range.

    Returns:
        The ``bearing/range`` label.
    """
    brg, rng = bearing_range(state, fid, point)
    dist = rng / M_PER_NM if units == UnitDomain.IMPERIAL else rng / 1000
    return f"{round(brg) % 360:03d}/{round(dist)}"
