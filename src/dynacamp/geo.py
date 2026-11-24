"""Flat theater geometry.

The core works on a flat local plane in metres: ``x`` grows east, ``y`` grows north.
Every adapter converts to its own game's frame (for DCS: core x = DCS z, core y = DCS x).
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Vec2:
    """Immutable 2-D vector / point on the theater plane, in metres.

    Attributes:
        x: East coordinate (metres).
        y: North coordinate (metres).
    """
    x: float  # east, metres
    y: float  # north, metres

    def __add__(self, other: Vec2) -> Vec2:
        """Component-wise sum."""
        return Vec2(self.x + other.x, self.y + other.y)

    def __sub__(self, other: Vec2) -> Vec2:
        """Component-wise difference."""
        return Vec2(self.x - other.x, self.y - other.y)

    def __mul__(self, k: float) -> Vec2:
        """Scale by a number.

        Args:
            k: Scale factor.
        """
        return Vec2(self.x * k, self.y * k)

    def length(self) -> float:
        """Euclidean norm of the vector, in metres."""
        return math.hypot(self.x, self.y)

    def distance_to(self, other: Vec2) -> float:
        """Straight-line distance to another point.

        Args:
            other: The other point.

        Returns:
            Distance in metres.
        """
        return math.hypot(self.x - other.x, self.y - other.y)

    def lerp(self, other: Vec2, t: float) -> Vec2:
        """Linear interpolation towards ``other``.

        Args:
            other: End point.
            t: Fraction in [0, 1] (clamped); 0 gives ``self``, 1 gives ``other``.

        Returns:
            The interpolated point.
        """
        t = min(max(t, 0.0), 1.0)
        return Vec2(self.x + (other.x - self.x) * t, self.y + (other.y - self.y) * t)

    def heading_to(self, other: Vec2) -> float:
        """Compass heading in radians (0 = north, clockwise)."""
        return math.atan2(other.x - self.x, other.y - self.y) % (2 * math.pi)

    def project_t(self, a: Vec2, b: Vec2) -> float:
        """Parameter t in [0, 1] of this point projected on segment a-b."""
        ab = b - a
        denom = ab.x * ab.x + ab.y * ab.y
        if denom == 0:
            return 0.0
        ap = self - a
        return min(max((ap.x * ab.x + ap.y * ab.y) / denom, 0.0), 1.0)
