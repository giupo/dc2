"""Map geometry for presentation: province territories and front lines.

Provinces are points in the campaign; to *draw* territory we give each one its
Voronoi cell (the area closer to its centre than to any other), capped to a
radius so border provinces do not stretch to infinity. Two cells that share an
edge are neighbours on the map; the front line is made of the shared edges
between cells held by factions at war, chained into polylines.

Cells depend only on the province centres, so they are computed once per theater.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .geo import Vec2
from .state import CampaignState
from .world import Theater

CELL_RADIUS_M = 60_000.0
CIRCLE_SIDES = 48
OUTER = "__outer__"  # label of cell edges that are not shared with a province

# A polygon is a list of (vertex, label of the edge from this vertex to the next).
Polygon = list[tuple[Vec2, str]]


def _clip(poly: Polygon, point: Vec2, normal: Vec2, offset: float, label: str) -> Polygon:
    """Keep the part of ``poly`` where dot(normal, p) <= offset (Sutherland-Hodgman)."""

    def side(p: Vec2) -> float:
        """Signed distance of ``p`` from the clip line (<= 0 is kept)."""
        return normal.x * p.x + normal.y * p.y - offset

    out: Polygon = []
    n = len(poly)
    for i in range(n):
        p, lab = poly[i]
        q, _ = poly[(i + 1) % n]
        sp, sq = side(p), side(q)
        if sp <= 0:
            out.append((p, lab))
            if sq > 0:  # leaving: the new edge runs along the clip line
                t = sp / (sp - sq)
                out.append((p.lerp(q, t), label))
        elif sq <= 0:  # entering
            t = sp / (sp - sq)
            out.append((p.lerp(q, t), lab))
    return out


def _cell(center: Vec2, others: dict[str, Vec2], radius: float) -> Polygon:
    """Voronoi cell of ``center`` against ``others``, capped to a circle.

    Args:
        center: The province centre.
        others: Other province centres by id.
        radius: Maximum radius of the cell.

    Returns:
        Polygon whose edges are labelled with the neighbouring province id.
    """
    poly: Polygon = [
        (Vec2(center.x + radius * math.cos(a), center.y + radius * math.sin(a)), OUTER)
        for a in (2 * math.pi * i / CIRCLE_SIDES for i in range(CIRCLE_SIDES))
    ]
    for pid, other in others.items():
        if other.distance_to(center) > 2 * radius:
            continue
        normal = other - center
        mid = center.lerp(other, 0.5)
        poly = _clip(poly, center, normal, normal.x * mid.x + normal.y * mid.y, pid)
        if not poly:
            break
    return poly


@dataclass(frozen=True)
class Cells:
    """Voronoi cells of all provinces.

    Attributes:
        polygons: Province id -> labelled polygon.
    """
    polygons: dict[str, Polygon]

    def shared_edges(self, a: str) -> list[tuple[str, Vec2, Vec2]]:
        """Edges of cell ``a`` shared with another province: (neighbour, p, q)."""
        poly = self.polygons[a]
        out = []
        for i, (p, label) in enumerate(poly):
            if label != OUTER:
                out.append((label, p, poly[(i + 1) % len(poly)][0]))
        return out


_cache: dict[tuple, Cells] = {}


def cells(theater: Theater, radius: float = CELL_RADIUS_M) -> Cells:
    """Voronoi cells of a theater (cached: they depend only on province centres).

    Args:
        theater: The map.
        radius: Maximum cell radius in metres.
    """
    key = (theater.name, radius, tuple((p.id, p.center.x, p.center.y) for p in theater.provinces.values()))
    if key not in _cache:
        centers = {pid: p.center for pid, p in theater.provinces.items()}
        _cache[key] = Cells(
            {pid: _cell(c, {o: v for o, v in centers.items() if o != pid}, radius) for pid, c in centers.items()}
        )
    return _cache[key]


@dataclass(frozen=True)
class FrontLine:
    """A front line between two factions.

    Attributes:
        factions: Sorted pair of factions at war.
        points: Polyline.
        provinces: Pairs of facing provinces along the line.
    """
    factions: tuple[str, str]  # sorted pair of factions at war
    points: list[Vec2]
    provinces: list[tuple[str, str]]  # (province of factions[0], province of factions[1]) along the line


def front_lines(state: CampaignState, radius: float = CELL_RADIUS_M) -> list[FrontLine]:
    """Front lines: shared cell edges between provinces held by factions at war,
    chained into polylines.

    Args:
        state: The campaign.
        radius: Cell radius used for the territories.
    """
    t = state.theater
    cs = cells(t, radius)
    # Collect hostile shared edges once per pair, oriented so factions are sorted.
    segments: dict[tuple[str, str], list[tuple[Vec2, Vec2, tuple[str, str]]]] = {}
    for a in t.provinces:
        for b, p, q in cs.shared_edges(a):
            if a >= b:
                continue
            fa, fb = t.provinces[a].controller, t.provinces[b].controller
            if fa == fb or not state.at_war(fa, fb):
                continue
            pair, provs = ((fa, fb), (a, b)) if fa < fb else ((fb, fa), (b, a))
            segments.setdefault(pair, []).append((p, q, provs))

    lines: list[FrontLine] = []
    for pair, segs in segments.items():
        lines.extend(FrontLine(pair, pts, provs) for pts, provs in _chain(segs))
    return lines


def _key(v: Vec2) -> tuple[int, int]:
    """Hashable key of a point rounded to the metre."""
    return round(v.x), round(v.y)


def _chain(segs: list[tuple[Vec2, Vec2, tuple[str, str]]]) -> list[tuple[list[Vec2], list[tuple[str, str]]]]:
    """Join segments that share endpoints into polylines."""
    remaining = list(segs)
    out = []
    while remaining:
        p, q, prov = remaining.pop()
        pts, provs = [p, q], [prov]
        grew = True
        while grew:
            grew = False
            for i, (a, b, pr) in enumerate(remaining):
                if _key(a) == _key(pts[-1]):
                    pts.append(b)
                elif _key(b) == _key(pts[-1]):
                    pts.append(a)
                elif _key(b) == _key(pts[0]):
                    pts.insert(0, a)
                elif _key(a) == _key(pts[0]):
                    pts.insert(0, b)
                else:
                    continue
                provs.append(pr)
                remaining.pop(i)
                grew = True
                break
        out.append((pts, provs))
    return out
