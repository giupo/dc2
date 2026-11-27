"""Strategic map: provinces connected by a transport graph (HOI4-style)."""

from __future__ import annotations

import heapq
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from .geo import Vec2


class Terrain(StrEnum):
    """Province terrain types."""
    #: Open ground: full speed, no help for the defender.
    PLAINS = "plains"
    #: Slower movement, some cover for the defender.
    FOREST = "forest"
    #: Slower movement, good positions for the defender.
    HILLS = "hills"
    #: Very slow movement, the strongest defence.
    MOUNTAIN = "mountain"
    #: Streets slow the advance; the defender fights from cover.
    URBAN = "urban"
    #: Very slow movement, hard to attack across.
    MARSH = "marsh"


@dataclass(frozen=True, slots=True)
class TerrainProfile:
    """How a terrain affects operations.

    Attributes:
        move_factor: Multiplier on movement speed.
        attack_factor: Multiplier on an attacker's firepower.
    """
    move_factor: float  # multiplier on movement speed
    attack_factor: float  # multiplier on the attacker's firepower


#: Movement and combat modifiers of each terrain.
TERRAIN: dict[Terrain, TerrainProfile] = {
    Terrain.PLAINS: TerrainProfile(1.0, 1.0),
    Terrain.FOREST: TerrainProfile(0.7, 0.8),
    Terrain.HILLS: TerrainProfile(0.7, 0.75),
    Terrain.MOUNTAIN: TerrainProfile(0.45, 0.55),
    Terrain.URBAN: TerrainProfile(0.8, 0.6),
    Terrain.MARSH: TerrainProfile(0.5, 0.7),
}


class LinkKind(StrEnum):
    """Kind of connection between two provinces."""
    #: A paved road: full speed.
    ROAD = "road"
    #: A dirt track: slower.
    TRACK = "track"
    #: A railway: full speed.
    RAIL = "rail"


#: Speed factor of each kind of link.
LINK_SPEED: dict[LinkKind, float] = {LinkKind.ROAD: 1.0, LinkKind.TRACK: 0.6, LinkKind.RAIL: 1.0}
#: Length of a link without road geometry, as a multiple of the straight line between the centres
#: (straight-line distances underestimate road lengths).
ROAD_WINDING = 1.25


@dataclass(slots=True)
class Depot:
    """A supply source. Convoys leave from here."""

    stock: float
    capacity: float
    production_per_hour: float
    trucks: int  # idle trucks available for convoys


@dataclass(slots=True)
class Province:
    """A node of the strategic map.

    Attributes:
        id: Unique identifier.
        name: Display name.
        center: Position of the province centre (usually its main town).
        terrain: Dominant terrain.
        controller: Faction that holds it now.
        owner: Faction that owns it de jure (defaults to ``controller``).
        infrastructure: 0..5, scales movement and supply.
        victory_points: Value for objectives and the AI.
        depot: Supply depot, if any.
    """
    id: str
    name: str
    center: Vec2
    terrain: Terrain
    controller: str  # faction id that holds it now
    owner: str = ""  # faction id that owns it de jure (defaults to controller)
    infrastructure: int = 2  # 0..5, scales movement and supply throughput
    victory_points: int = 0
    depot: Depot | None = None

    def __post_init__(self) -> None:
        """Default ``owner`` to ``controller``."""
        if not self.owner:
            self.owner = self.controller


@dataclass(frozen=True, slots=True)
class Link:
    """An edge of the province graph (road, track or rail).

    Attributes:
        a: First province id.
        b: Second province id.
        length: Length in metres along the route.
        kind: Road, track or rail.
        river: Attacks across it are penalised.
        route: Road geometry from ``a``'s centre to ``b``'s, or None for a
            straight segment.
    """
    a: str
    b: str
    length: float  # metres along the route
    kind: LinkKind = LinkKind.ROAD
    river: bool = False  # attacking across it is penalised
    # Road geometry from a's centre to b's centre. None = straight segment.
    route: tuple[Vec2, ...] | None = None

    def other(self, pid: str) -> str:
        """The province at the other end.

        Args:
            pid: One end of the link.

        Returns:
            The other province id.
        """
        return self.b if pid == self.a else self.a

    def points(self, frm: str, a_center: Vec2, b_center: Vec2) -> list[Vec2]:
        """Polyline oriented from province ``frm`` to the other end.

        Args:
            frm: Province the polyline starts from.
            a_center: Centre of province ``a`` (used without a road).
            b_center: Centre of province ``b`` (used without a road).

        Returns:
            The road polyline, or the straight segment between the centres.
        """
        pts = list(self.route) if self.route else [a_center, b_center]
        return pts if frm == self.a else pts[::-1]


def polyline_length(pts: list[Vec2]) -> float:
    """Length of a polyline in metres.

    Args:
        pts: Polyline vertices.

    Returns:
        The length.
    """
    return sum(p.distance_to(q) for p, q in zip(pts, pts[1:]))


def point_along(pts: list[Vec2], s: float) -> tuple[Vec2, int]:
    """Point at arc length ``s`` and the index of the segment it lies on.

    Args:
        pts: Polyline vertices.
        s: Arc length in metres from the first vertex.

    Returns:
        The point and its segment index (clamped to the ends).
    """
    if s <= 0:
        return pts[0], 0
    for i, (p, q) in enumerate(zip(pts, pts[1:])):
        seg = p.distance_to(q)
        if s <= seg:
            return (p.lerp(q, s / seg) if seg else p), i
        s -= seg
    return pts[-1], len(pts) - 2


def project_on(pts: list[Vec2], pos: Vec2) -> float:
    """Arc length of the point of the polyline closest to ``pos``.

    Args:
        pts: Polyline vertices.
        pos: The point to project.

    Returns:
        The arc length in metres.
    """
    best, best_s, acc = float("inf"), 0.0, 0.0
    for p, q in zip(pts, pts[1:]):
        seg = p.distance_to(q)
        t = pos.project_t(p, q)
        d = pos.distance_to(p.lerp(q, t))
        if d < best:
            best, best_s = d, acc + t * seg
        acc += seg
    return best_s


@dataclass
class Theater:
    """The strategic map: provinces connected by links.

    Provides graph queries (neighbours, shortest paths) and geometry along links
    (points, headings and projections on the road polylines).

    Attributes:
        name: Theater name; also selects the lat/lon projection
            (see :mod:`dynacamp.projection`).
        provinces: Provinces by id.
    """
    name: str
    provinces: dict[str, Province] = field(default_factory=dict)
    _links: dict[str, dict[str, Link]] = field(default_factory=dict)
    # (frm, to) -> (oriented polyline, polyline metres per progress metre)
    _geom: dict[tuple[str, str], tuple[list[Vec2], float]] = field(default_factory=dict, repr=False, compare=False)

    def add_province(self, province: Province) -> Province:
        """Add a province.

        Args:
            province: The province to add.

        Returns:
            The same province.

        Raises:
            ValueError: If the id is already used.
        """
        if province.id in self.provinces:
            raise ValueError(f"duplicate province {province.id!r}")

        self.provinces[province.id] = province
        self._links[province.id] = {}
        return province

    def connect(
        self,
        a: str,
        b: str,
        kind: LinkKind = LinkKind.ROAD,
        river: bool = False,
        length: float | None = None,
        route: list[Vec2] | None = None,
    ) -> Link:
        """Link two provinces.

        Args:
            a: First province id.
            b: Second province id.
            kind: Road, track or rail.
            river: Whether attacks across it are penalised.
            length: Length in metres (default: straight distance x ``ROAD_WINDING``,
                or the route length when ``route`` is given).
            route: Optional road geometry between the two centres (intermediate
                points only; the centres are added).

        Returns:
            The new link.
        """
        pa, pb = self.provinces[a], self.provinces[b]
        if route:
            # The road starts and ends at the province centres.
            pts = [pa.center, *route, pb.center]
            length = polyline_length(pts)
            route_t: tuple[Vec2, ...] | None = tuple(pts)

        else:
            route_t = None
            if length is None:
                length = pa.center.distance_to(pb.center) * ROAD_WINDING

        link = Link(a, b, length, kind, river, route_t)
        self._links[a][b] = link
        self._links[b][a] = link
        self._geom.clear()
        return link

    def neighbors(self, pid: str) -> Iterable[str]:
        """Ids of the provinces linked to ``pid``.

        Args:
            pid: Province id.

        Returns:
            The neighbouring province ids.
        """
        return self._links[pid].keys()

    def link(self, a: str, b: str) -> Link:
        """The link between two adjacent provinces.

        Args:
            a: Province id.
            b: Province id.

        Returns:
            The link.

        Raises:
            KeyError: If they are not adjacent.
        """
        return self._links[a][b]

    def links(self) -> Iterable[Link]:
        """Iterate over every link once.

        Yields:
            Each link once.
        """
        seen: set[tuple[str, str]] = set()
        for a, out in self._links.items():
            for b, link in out.items():
                key = (a, b) if a < b else (b, a)
                if key not in seen:
                    seen.add(key)
                    yield link

    # ------------------------------------------------------------ positions on links
    # ``s`` is progress in metres along the link from ``frm`` (0 = frm's centre,
    # link.length = the other centre). Without road geometry the link is a straight
    # segment and ``s`` maps proportionally onto it.

    def link_points(self, frm: str, to: str) -> list[Vec2]:
        """Road polyline of a link, oriented from ``frm`` to ``to``.

        Args:
            frm: Start province id.
            to: End province id.

        Returns:
            The polyline.
        """
        return self._geometry(frm, to)[0]

    def _geometry(self, frm: str, to: str) -> tuple[list[Vec2], float]:
        """Cached oriented polyline and scale of a link (geometry never changes
        after the theatre is built; :meth:`connect` clears the cache).

        Args:
            frm: Start province id.
            to: End province id.

        Returns:
            The oriented polyline and its length per link-length metre.
        """
        g = self._geom.get((frm, to))
        if g is None:
            link = self._links[frm][to]
            pts = link.points(frm, self.provinces[link.a].center, self.provinces[link.b].center)
            g = (pts, polyline_length(pts) / link.length if link.length else 0.0)
            self._geom[(frm, to)] = g
        return g

    def _scale(self, frm: str, to: str, pts: list[Vec2]) -> float:
        """Polyline metres per progress metre (1 on real roads).

        Args:
            frm: Start province id.
            to: End province id.
            pts: The oriented polyline (unused: the scale is cached with it).

        Returns:
            The scale.
        """
        return self._geometry(frm, to)[1]

    def point_on_link(self, frm: str, to: str, s: float) -> Vec2:
        """Position at progress ``s`` along the link ``frm -> to``.

        Args:
            frm: Start province id.
            to: End province id.
            s: Progress in metres from ``frm`` (0 .. link length).

        Returns:
            The point on the road.
        """
        pts = self.link_points(frm, to)
        return point_along(pts, s * self._scale(frm, to, pts))[0]

    def heading_on_link(self, frm: str, to: str, s: float) -> float:
        """Compass heading of the road at progress ``s`` (radians, 0 = north).

        Args:
            frm: Start province id.
            to: End province id.
            s: Progress in metres from ``frm``.

        Returns:
            The heading.
        """
        pts = self.link_points(frm, to)
        _, i = point_along(pts, s * self._scale(frm, to, pts))
        return pts[i].heading_to(pts[i + 1]) if len(pts) > 1 else 0.0

    def project_on_link(self, frm: str, to: str, pos: Vec2) -> float:
        """Progress along the link of the road point closest to ``pos``.

        Args:
            frm: Start province id.
            to: End province id.
            pos: Any point.

        Returns:
            Progress in metres from ``frm``.
        """
        pts = self.link_points(frm, to)
        scale = self._scale(frm, to, pts)
        return project_on(pts, pos) / scale if scale else 0.0

    def link_points_from(self, frm: str, to: str, s: float) -> list[Vec2]:
        """The rest of the road from progress ``s`` to ``to``.

        Args:
            frm: Start province id.
            to: End province id.
            s: Progress in metres from ``frm``.

        Returns:
            The current point followed by the remaining road vertices.
        """
        pts = self.link_points(frm, to)
        p, i = point_along(pts, s * self._scale(frm, to, pts))
        return [p, *pts[i + 1 :]]

    def nearest(self, pos: Vec2, candidates: Iterable[str] | None = None) -> Province:
        """Province whose centre is closest to ``pos``.

        Args:
            pos: Point.
            candidates: Restrict the search to these province ids.

        Returns:
            The nearest province.
        """
        ids = candidates if candidates is not None else self.provinces.keys()
        return min((self.provinces[i] for i in ids), key=lambda p: p.center.distance_to(pos))

    def travel_cost(self, a: str, b: str) -> float:
        """Seconds-like cost of moving a->b: length scaled by link, terrain and infrastructure.

        Args:
            a: Start province id.
            b: Adjacent province id.

        Returns:
            The cost, link length divided by the effective speed.
        """
        link = self._links[a][b]
        dest = self.provinces[b]
        speed = LINK_SPEED[link.kind] * TERRAIN[dest.terrain].move_factor * infra_factor(dest.infrastructure)
        return link.length / speed

    def shortest_path(
        self,
        start: str,
        goal: str,
        passable: Callable[[str], bool] | None = None,
        penalty: Callable[[str, str], float] | None = None,
    ) -> list[str] | None:
        """Dijkstra on travel cost. Returns the provinces after ``start`` up to ``goal``.

        ``passable`` filters intermediate provinces; ``goal`` itself is always allowed
        so that a path can end on a hostile province (an attack). ``penalty(a, b)``
        multiplies the cost of a link (e.g. a road the enemy is known to cover).

        Args:
            start: Start province id.
            goal: Destination province id.
            passable: Whether an intermediate province may be crossed (None = all).
            penalty: Cost multiplier of a link (None = 1).

        Returns:
            The path (empty if ``start == goal``), or None if the goal cannot be reached.
        """
        if start == goal:
            return []
        dist = {start: 0.0}
        prev: dict[str, str] = {}
        heap = [(0.0, start)]
        while heap:
            d, pid = heapq.heappop(heap)
            if pid == goal:
                break

            if d > dist.get(pid, float("inf")):
                continue

            for nxt in self._links[pid]:
                if nxt != goal and passable is not None and not passable(nxt):
                    continue

                nd = d + self.travel_cost(pid, nxt) * (penalty(pid, nxt) if penalty else 1.0)
                if nd < dist.get(nxt, float("inf")):
                    dist[nxt] = nd
                    prev[nxt] = pid
                    heapq.heappush(heap, (nd, nxt))

        if goal not in prev:
            return None

        path = [goal]
        while path[-1] in prev and prev[path[-1]] != start:
            path.append(prev[path[-1]])

        return list(reversed(path))

    def path_length(self, start: str, path: list[str]) -> float:
        """Total link length of a path.

        Args:
            start: Start province id.
            path: Following province ids.

        Returns:
            Metres along the route.
        """
        total, cur = 0.0, start

        for nxt in path:
            total += self._links[cur][nxt].length
            cur = nxt

        return total


def infra_factor(level: int) -> float:
    """Speed multiplier for an infrastructure level.

    Args:
        level: 0..5.

    Returns:
        0.6 (none) .. 1.1 (excellent).
    """
    return 0.6 + 0.1 * max(0, min(level, 5))  # 0.6 .. 1.1
