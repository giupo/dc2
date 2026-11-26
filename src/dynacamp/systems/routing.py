"""Threat-aware routing: flight paths around the enemy SAM umbrellas a faction knows.

The planners only see their intelligence picture (fog of war): a threat is a
known enemy SAM site that can reach the flight's cruise altitude, at its last
known position, with the engagement range of its system plus a margin.

A route between two points is the shortest path that stays out of every
threat circle, found on a visibility graph: the nodes are the two ends plus a
ring of points around each circle (a polygon that encloses it), the edges the
segments that cross no circle. Circles that contain one of the ends cannot be
avoided on that leg (the target itself is under the umbrella): they are left
out of the search and reported as *exposure* instead, which is what calls for
SEAD support in the package.

A detour is accepted only if it is not much longer than the direct leg
(``MAX_DETOUR``); otherwise the direct leg is flown and its threats count as
exposure too.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass

from ..air_defense import SamRole
from ..geo import Vec2
from ..state import CampaignState

#: Routes keep this much outside a SAM's engagement range (multiple of the range).
ROUTE_MARGIN = 1.15
#: Vertices of the polygon a route follows around each threat.
RING_POINTS = 12
#: A detour may be this much longer than the direct leg.
MAX_DETOUR = 1.6
#: Altitude assumed when the caller gives none: jets at cruise (see
#: :attr:`~dynacamp.air.AircraftType.cruise_alt_m`).
DEFAULT_ALTITUDE_M = 6_000.0


@dataclass(frozen=True, slots=True)
class Threat:
    """A known SAM umbrella.

    Attributes:
        site: Air defence unit id.
        center: Last known position of the site.
        radius: Engagement range plus the routing margin, in metres.
        long_range: True for long-range systems (SA-10, Patriot...).
    """
    site: str
    center: Vec2
    radius: float
    long_range: bool

    def contains(self, p: Vec2) -> bool:
        """True if ``p`` is inside the umbrella.

        Args:
            p: The point.

        Returns:
            Whether it is strictly inside the radius.
        """
        return self.center.distance_to(p) < self.radius

    def crosses(self, a: Vec2, b: Vec2) -> bool:
        """True if the segment ``a``-``b`` enters the umbrella.

        Args:
            a: Start of the segment.
            b: End of the segment.

        Returns:
            Whether the segment comes closer than the radius to the centre.
        """
        return _segment_distance(self.center, a, b) < self.radius

    def inside_length(self, a: Vec2, b: Vec2) -> float:
        """Length of the segment ``a``-``b`` that lies inside the umbrella.

        Args:
            a: Start of the segment.
            b: End of the segment.

        Returns:
            The length in metres inside the circle.
        """
        d = b - a
        seg = d.length()
        if seg == 0:
            return 0.0
        # Solve |a + t d - c| = r for t in [0, 1].
        f = a - self.center
        qa = d.x * d.x + d.y * d.y
        qb = 2 * (f.x * d.x + f.y * d.y)
        qc = f.x * f.x + f.y * f.y - self.radius * self.radius
        disc = qb * qb - 4 * qa * qc
        if disc <= 0:
            return 0.0
        root = math.sqrt(disc)
        t0 = max(0.0, (-qb - root) / (2 * qa))
        t1 = min(1.0, (-qb + root) / (2 * qa))
        return max(0.0, t1 - t0) * seg


def _segment_distance(c: Vec2, a: Vec2, b: Vec2) -> float:
    """Distance from point ``c`` to the segment ``a``-``b``.

    Args:
        c: The point.
        a: Start of the segment.
        b: End of the segment.

    Returns:
        The distance in metres.
    """
    d = b - a
    seg2 = d.x * d.x + d.y * d.y
    if seg2 == 0:
        return c.distance_to(a)
    t = max(0.0, min(1.0, ((c.x - a.x) * d.x + (c.y - a.y) * d.y) / seg2))
    return c.distance_to(Vec2(a.x + t * d.x, a.y + t * d.y))


def threats(state: CampaignState, fid: str, altitude: float = DEFAULT_ALTITUDE_M) -> list[Threat]:
    """Known enemy SAM umbrellas of faction ``fid`` for a flight at ``altitude``:
    every medium and long-range site, and the short-range ones that reach that
    high (a Tor reaches a jet at cruise, an Osa does not).

    Args:
        state: The campaign.
        fid: The planning faction.
        altitude: Cruise altitude of the flight, metres.

    Returns:
        One threat per known site, sorted by site id (deterministic).
    """
    from . import intel

    out = []
    for c in intel.known(state, fid, "sam"):
        u = state.air_defense.get(c.entity_id)
        if u is None or not state.at_war(fid, u.faction) or u.system.role is SamRole.EWR or not u.system.engages:
            continue
        if u.system.role is SamRole.SHORT and u.system.ceiling_m < altitude:
            continue
        out.append(Threat(u.id, c.position, u.system.range_m * ROUTE_MARGIN, u.system.role is SamRole.LONG))
    return sorted(out, key=lambda t: t.site)


def leg(a: Vec2, b: Vec2, umbrellas: list[Threat], max_detour: float = MAX_DETOUR) -> list[Vec2]:
    """Intermediate points of the shortest acceptable path from ``a`` to ``b``.

    Args:
        a: Start of the leg.
        b: End of the leg.
        umbrellas: Threats to keep out of.
        max_detour: Longest acceptable path, as a multiple of the direct leg.

    Returns:
        The turning points between ``a`` and ``b`` (empty: fly straight).
    """
    direct = a.distance_to(b)
    if direct == 0:
        return []
    # Circles around an end cannot be avoided; far ones cannot matter.
    reach = direct * max_detour
    blocking = [
        t for t in umbrellas
        if not t.contains(a) and not t.contains(b)
        and t.center.distance_to(a) + t.center.distance_to(b) - 2 * t.radius <= reach
    ]
    if not any(t.crosses(a, b) for t in blocking):
        return []

    def clear(p: Vec2, q: Vec2) -> bool:
        """True if the segment p-q crosses no blocking circle.

        Args:
            p: Start of the segment.
            q: End of the segment.

        Returns:
            Whether the segment is clear.
        """
        return not any(t.crosses(p, q) for t in blocking)

    # Polygon vertices just outside each circle (the polygon encloses it).
    k = 1.0 / math.cos(math.pi / RING_POINTS) * 1.01
    nodes = [a, b]
    for t in blocking:
        for i in range(RING_POINTS):
            ang = 2 * math.pi * i / RING_POINTS
            p = Vec2(t.center.x + t.radius * k * math.sin(ang), t.center.y + t.radius * k * math.cos(ang))
            if not any(o.contains(p) for o in blocking):
                nodes.append(p)

    # Dijkstra from a (node 0) to b (node 1), edges checked lazily.
    best = {0: 0.0}
    prev: dict[int, int] = {}
    heap = [(0.0, 0)]
    done: set[int] = set()
    while heap:
        dist, i = heapq.heappop(heap)
        if i in done:
            continue
        done.add(i)
        if i == 1:
            break
        for j in range(1, len(nodes)):
            if j in done:
                continue
            nd = dist + nodes[i].distance_to(nodes[j])
            if nd > reach or nd >= best.get(j, math.inf):
                continue
            if clear(nodes[i], nodes[j]):
                best[j] = nd
                prev[j] = i
                heapq.heappush(heap, (nd, j))
    if 1 not in prev:
        return []  # no acceptable detour: fly straight
    path = []
    i = prev[1]
    while i != 0:
        path.append(nodes[i])
        i = prev[i]
    return path[::-1]


def route(points: list[Vec2], umbrellas: list[Threat], max_detour: float = MAX_DETOUR) -> tuple[list[Vec2], list[int]]:
    """Route through ``points`` in order, detouring around the umbrellas on each leg.

    Args:
        points: The key waypoints (home, IP, target...).
        umbrellas: Threats to keep out of.
        max_detour: Longest acceptable detour per leg.

    Returns:
        (waypoints, index in the result of each key point).
    """
    out = [points[0]]
    index = [0]
    for a, b in zip(points, points[1:]):
        out += leg(a, b, umbrellas, max_detour)
        out.append(b)
        index.append(len(out) - 1)
    return out, index


def exposure(waypoints: list[Vec2], umbrellas: list[Threat]) -> dict[str, float]:
    """Metres flown inside each threat along a route.

    Args:
        waypoints: The route.
        umbrellas: The threats to measure.

    Returns:
        Site id -> length inside its umbrella, only for the sites entered.
    """
    out: dict[str, float] = {}
    for t in umbrellas:
        inside = sum(t.inside_length(a, b) for a, b in zip(waypoints, waypoints[1:]))
        if inside > 0:
            out[t.site] = inside
    return out


def screen_point(waypoints: list[Vec2], target_index: int, umbrellas: list[Threat], back_off: float = 5_000.0) -> Vec2 | None:
    """Where escorts hold to screen a package: just before its route to the
    target first enters an umbrella (None: the route stays clear).

    Args:
        waypoints: The package's main route.
        target_index: Index of its target waypoint.
        umbrellas: Known threats.
        back_off: How far short of the umbrella to hold.

    Returns:
        The holding point, or None.
    """
    for a, b in zip(waypoints[: target_index + 1], waypoints[1 : target_index + 1]):
        seg = a.distance_to(b)
        if seg == 0:
            continue
        entry = None
        for t in umbrellas:
            if not t.crosses(a, b):
                continue
            if t.contains(a):
                return a if a is not waypoints[0] else None
            # First crossing of the circle along a-b (solve |a + s(b-a) - c| = r).
            d = b - a
            f = a - t.center
            qa = d.x * d.x + d.y * d.y
            qb = 2 * (f.x * d.x + f.y * d.y)
            qc = f.x * f.x + f.y * f.y - t.radius * t.radius
            s0 = (-qb - math.sqrt(max(0.0, qb * qb - 4 * qa * qc))) / (2 * qa)
            if entry is None or s0 < entry:
                entry = s0
        if entry is not None:
            return a.lerp(b, max(0.0, entry - back_off / seg))
    return None


def length(waypoints: list[Vec2]) -> float:
    """Length of a polyline in metres.

    Args:
        waypoints: Polyline vertices.

    Returns:
        The length.
    """
    return sum(a.distance_to(b) for a, b in zip(waypoints, waypoints[1:]))
