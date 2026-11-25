"""Units of measure for what players and users read.

The campaign computes in SI (metres, seconds). Text for people is formatted in
one of two systems:

* ``metric`` - km, m, km/h;
* ``imperial`` - aviation units: nautical miles, feet, knots (the default for
  briefings, as in the cockpit).
"""

from __future__ import annotations

from typing import Literal
from enum import StrEnum

class UnitDomain(StrEnum):
    """
    Unit types of the system.
    """

    "Metric system"
    METRIC = "metric"

    "Imperial system"
    IMPERIAL = "imperial"

Units = Literal[UnitDomain.METRIC, UnitDomain.IMPERIAL]
UNITS: tuple[str, ...] = (UnitDomain.METRIC, UnitDomain.IMPERIAL)
DEFAULT_UNITS: Units = UnitDomain.IMPERIAL

M_PER_NM = 1852.0
M_PER_FT = 0.3048
KMH_PER_KT = 1.852


def check(units: str) -> Units:
    """Validate a units name.

    Raises:
        ValueError: If it is not ``metric`` or ``imperial``.
    """
    if units not in UNITS:
        raise ValueError(f"unknown units {units!r}: use one of {', '.join(UNITS)}")
    return units  # type: ignore[return-value]


def distance(m: float, units: Units) -> str:
    """A distance: ``"64 km"`` or ``"35 NM"``."""
    if units == UnitDomain.IMPERIAL:
        return f"{m / M_PER_NM:.0f} NM"
    return f"{m / 1000:.0f} km"


def altitude(m: float, units: Units) -> str:
    """An altitude: ``"1500 m"`` or ``"4900 ft"`` (feet rounded to 100)."""
    if units == UnitDomain.IMPERIAL:
        return f"{round(m / M_PER_FT / 100) * 100:.0f} ft"
    return f"{m:.0f} m"


def speed(kmh: float, units: Units) -> str:
    """A speed: ``"830 km/h"`` or ``"450 kts"`` (rounded to 5)."""
    if units == UnitDomain.IMPERIAL:
        return f"{round(kmh / KMH_PER_KT / 5) * 5:.0f} kts"
    return f"{kmh:.0f} km/h"
