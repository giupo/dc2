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
#: The unit systems texts can use.
UNITS: tuple[str, ...] = (UnitDomain.METRIC, UnitDomain.IMPERIAL)
#: Unit system of a campaign that does not choose one.
DEFAULT_UNITS: Units = UnitDomain.IMPERIAL

#: Metres per nautical mile.
M_PER_NM = 1852.0
#: Metres per foot.
M_PER_FT = 0.3048
#: Kilometres per hour per knot.
KMH_PER_KT = M_PER_NM / 1000.0


def check(units: str) -> Units:
    """Validate a units name.

    Args:
        units: The name to check.

    Returns:
        The same name, typed as :data:`Units`.

    Raises:
        ValueError: If it is not ``metric`` or ``imperial``.
    """
    if units not in UNITS:
        raise ValueError(f"unknown units {units!r}: use one of {', '.join(UNITS)}")
    return units  # type: ignore[return-value]


def distance(m: float, units: Units) -> str:
    """A distance: ``"64 km"`` or ``"35 NM"``.

    Args:
        m: Distance in metres.
        units: Unit system.

    Returns:
        The formatted distance.
    """
    if units == UnitDomain.IMPERIAL:
        return f"{m / M_PER_NM:.0f} NM"
    return f"{m / 1000:.0f} km"


def altitude(m: float, units: Units) -> str:
    """An altitude: ``"1500 m"`` or ``"4900 ft"`` (feet rounded to 100).

    Args:
        m: Altitude in metres.
        units: Unit system.

    Returns:
        The formatted altitude.
    """
    if units == UnitDomain.IMPERIAL:
        return f"{round(m / M_PER_FT / 100) * 100:.0f} ft"
    return f"{m:.0f} m"


def speed(kmh: float, units: Units) -> str:
    """A speed: ``"830 km/h"`` or ``"450 kts"`` (rounded to 5).

    Args:
        kmh: Speed in km/h.
        units: Unit system.

    Returns:
        The formatted speed.
    """
    if units == UnitDomain.IMPERIAL:
        return f"{round(kmh / KMH_PER_KT / 5) * 5:.0f} kts"
    return f"{kmh:.0f} km/h"
