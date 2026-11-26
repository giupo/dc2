"""Theater plane <-> WGS84 lat/lon.

DCS maps use a Transverse Mercator projection per theater (parameters from
pydcs, generated from DCS itself). The campaign plane is the same one with axes
renamed: campaign x (east) = DCS z, campaign y (north) = DCS x.

Theaters without a known projection simply have no lat/lon.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .geo import Vec2


@dataclass(frozen=True)
class TransverseMercator:
    """Parameters of a DCS theater's Transverse Mercator projection (WGS84).

    Attributes:
        central_meridian: Longitude of origin, degrees.
        false_easting: Metres added to eastings.
        false_northing: Metres added to northings.
        scale_factor: Scale on the central meridian.
    """
    central_meridian: float
    false_easting: float
    false_northing: float
    scale_factor: float = 0.9996


#: Transverse Mercator parameters of the DCS theatres, by lower-case name.
THEATERS: dict[str, TransverseMercator] = {
    "caucasus": TransverseMercator(33, -99516.9999999732, -4998114.999999984),
    "syria": TransverseMercator(39, 282801.00000003993, -3879865.9999999935),
}

# WGS84
_A = 6378137.0
_F = 1 / 298.257223563
_E2 = _F * (2 - _F)
_EP2 = _E2 / (1 - _E2)


def _meridian_arc(phi: float) -> float:
    """Meridian arc length from the equator to latitude ``phi`` (radians), WGS84.

    Args:
        phi: Latitude in radians.

    Returns:
        The arc length in metres.
    """
    e2, e4, e6 = _E2, _E2**2, _E2**3
    return _A * (
        (1 - e2 / 4 - 3 * e4 / 64 - 5 * e6 / 256) * phi
        - (3 * e2 / 8 + 3 * e4 / 32 + 45 * e6 / 1024) * math.sin(2 * phi)
        + (15 * e4 / 256 + 45 * e6 / 1024) * math.sin(4 * phi)
        - (35 * e6 / 3072) * math.sin(6 * phi)
    )


def ll_to_dcs(theater: str, lat: float, lon: float) -> tuple[float, float]:
    """(lat, lon) -> (DCS x north, DCS z east). Snyder's series.

    Args:
        theater: Theatre name, lower case (a key of ``THEATERS``).
        lat: Latitude in degrees (WGS84).
        lon: Longitude in degrees (WGS84).

    Returns:
        DCS ``(x, z)`` in metres.
    """
    p = THEATERS[theater]
    phi = math.radians(lat)
    lam = math.radians(lon - p.central_meridian)
    k0 = p.scale_factor
    n = _A / math.sqrt(1 - _E2 * math.sin(phi) ** 2)
    t = math.tan(phi) ** 2
    c = _EP2 * math.cos(phi) ** 2
    a = math.cos(phi) * lam
    m = _meridian_arc(phi)
    easting = k0 * n * (a + (1 - t + c) * a**3 / 6 + (5 - 18 * t + t**2 + 72 * c - 58 * _EP2) * a**5 / 120)
    northing = k0 * (
        m
        + n
        * math.tan(phi)
        * (a**2 / 2 + (5 - t + 9 * c + 4 * c**2) * a**4 / 24 + (61 - 58 * t + t**2 + 600 * c - 330 * _EP2) * a**6 / 720)
    )
    return northing + p.false_northing, easting + p.false_easting


def dcs_to_ll(theater: str, x: float, z: float) -> tuple[float, float]:
    """(DCS x north, DCS z east) -> (lat, lon). Inverse of :func:`ll_to_dcs`.

    Args:
        theater: Theatre name, lower case (a key of ``THEATERS``).
        x: DCS x (north) in metres.
        z: DCS z (east) in metres.

    Returns:
        Latitude and longitude in degrees (WGS84).
    """
    p = THEATERS[theater]
    k0 = p.scale_factor
    m = (x - p.false_northing) / k0
    mu = m / (_A * (1 - _E2 / 4 - 3 * _E2**2 / 64 - 5 * _E2**3 / 256))
    e1 = (1 - math.sqrt(1 - _E2)) / (1 + math.sqrt(1 - _E2))
    phi1 = (
        mu
        + (3 * e1 / 2 - 27 * e1**3 / 32) * math.sin(2 * mu)
        + (21 * e1**2 / 16 - 55 * e1**4 / 32) * math.sin(4 * mu)
        + (151 * e1**3 / 96) * math.sin(6 * mu)
        + (1097 * e1**4 / 512) * math.sin(8 * mu)
    )
    sin1, cos1, tan1 = math.sin(phi1), math.cos(phi1), math.tan(phi1)
    c1 = _EP2 * cos1**2
    t1 = tan1**2
    n1 = _A / math.sqrt(1 - _E2 * sin1**2)
    r1 = _A * (1 - _E2) / (1 - _E2 * sin1**2) ** 1.5
    d = (z - p.false_easting) / (n1 * k0)
    lat = phi1 - (n1 * tan1 / r1) * (
        d**2 / 2
        - (5 + 3 * t1 + 10 * c1 - 4 * c1**2 - 9 * _EP2) * d**4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1**2 - 252 * _EP2 - 3 * c1**2) * d**6 / 720
    )
    lon = (
        d - (1 + 2 * t1 + c1) * d**3 / 6 + (5 - 2 * c1 + 28 * t1 - 3 * c1**2 + 8 * _EP2 + 24 * t1**2) * d**5 / 120
    ) / cos1
    return math.degrees(lat), p.central_meridian + math.degrees(lon)


def to_latlon(theater: str, v: Vec2) -> tuple[float, float] | None:
    """Campaign point -> (lat, lon), or None for theaters without a projection.

    Args:
        theater: Theatre name (any case).
        v: Point on the campaign plane.

    Returns:
        Latitude and longitude in degrees, or None.
    """
    if theater.lower() not in THEATERS:
        return None
    return dcs_to_ll(theater.lower(), v.y, v.x)


def from_latlon(theater: str, lat: float, lon: float) -> Vec2 | None:
    """(lat, lon) -> campaign point, or None for theaters without a projection.
    Inverse of :func:`to_latlon`.

    Args:
        theater: Theatre name (any case).
        lat: Latitude in degrees (WGS84).
        lon: Longitude in degrees (WGS84).

    Returns:
        The point on the campaign plane, or None.
    """
    if theater.lower() not in THEATERS:
        return None
    x, z = ll_to_dcs(theater.lower(), lat, lon)
    return Vec2(z, x)
