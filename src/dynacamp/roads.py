"""Road geometry for scenario links.

Each link of a scenario is routed once on the real road network with OSRM
(OpenStreetMap data; DCS maps follow the real roads) and stored next to the
scenario as ``<scenario>.roads.json``, as ``[lat, lon]`` points (format
version 2; version 1 files, in theater-plane metres, still load). Units then move
along these polylines instead of straight lines: no more driving across the sea
between two coastal towns, and they spawn on roads in the game.

    uv run dynacamp roads scenarios/syria_m5.toml

The file is versioned with the scenario, so the engine itself never needs the
network.
"""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path
from typing import Any

from .geo import Vec2
from .projection import THEATERS, from_latlon, ll_to_dcs, to_latlon

FORMAT_VERSION = 2  # 1: [east, north] theater metres; 2: [lat, lon] WGS84 degrees

OSRM_URL = "https://router.project-osrm.org/route/v1/driving/{lon1},{lat1};{lon2},{lat2}?overview=full&geometries=geojson"
USER_AGENT = "dynacamp-route-tool"  # generic client name
SIMPLIFY_M = 100.0
REQUEST_INTERVAL_S = 1.1  # the public OSRM demo server asks for at most 1 request/s
# A route this many times longer than the straight line is a detour around a road
# that is closed in today's OpenStreetMap (e.g. a border crossing) but open in
# the game: keep the link straight instead.
MAX_DETOUR = 2.5


def roads_path(scenario_path: str | Path) -> Path:
    """Path of the roads file for a scenario (``<scenario>.roads.json``).

    Args:
        scenario_path: Path of the scenario TOML.
    """
    p = Path(scenario_path)
    return p.with_name(p.stem + ".roads.json")


def key(a: str, b: str) -> str:
    """Key of a link in the roads file: ``a|b``."""
    return f"{a}|{b}"


def load(scenario_path: str | Path, theater: str) -> dict[str, list[Vec2]]:
    """Road polylines keyed ``"a|b"`` (oriented a -> b) on the theater plane,
    or {} if there is no roads file.

    Args:
        scenario_path: Path of the scenario TOML.
        theater: The scenario's theater (projection of the ``[lat, lon]`` points).

    Raises:
        ValueError: A version 2 file for a theater without a projection, or an
            unknown format version.
    """
    path = roads_path(scenario_path)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    version = data.get("version", 1)
    if version == 1:
        return {k: [Vec2(x, y) for x, y in pts] for k, pts in data["links"].items()}
    if version != FORMAT_VERSION:
        raise ValueError(f"{path}: roads format {version}, this version reads 1 and {FORMAT_VERSION}")
    if theater.lower() not in THEATERS:
        raise ValueError(f"{path}: [lat, lon] roads need a projection, theater {theater!r} has none")
    return {k: [from_latlon(theater, lat, lon) for lat, lon in pts] for k, pts in data["links"].items()}


def dump(links: dict[str, list[Vec2]], theater: str) -> str:
    """The roads file (format :data:`FORMAT_VERSION`) for polylines on the plane.

    Args:
        links: Polylines keyed ``"a|b"``.
        theater: Theater whose projection gives the ``[lat, lon]`` points.
    """
    out = {k: [[round(c, 6) for c in to_latlon(theater, p)] for p in pts] for k, pts in links.items()}
    return json.dumps(
        {
            "version": FORMAT_VERSION,
            "source": "OSRM routing on OpenStreetMap data (c) OpenStreetMap contributors, ODbL",
            "frame": "WGS84 [lat, lon] degrees",
            "links": out,
        },
        indent=1,
    ) + "\n"


def simplify(pts: list[Vec2], tolerance: float = SIMPLIFY_M) -> list[Vec2]:
    """Douglas-Peucker."""
    if len(pts) < 3:
        return pts
    a, b = pts[0], pts[-1]
    best_i, best_d = 0, -1.0
    for i in range(1, len(pts) - 1):
        d = pts[i].distance_to(a.lerp(b, pts[i].project_t(a, b)))
        if d > best_d:
            best_i, best_d = i, d
    if best_d <= tolerance:
        return [a, b]
    left = simplify(pts[: best_i + 1], tolerance)
    right = simplify(pts[best_i:], tolerance)
    return left[:-1] + right


def fetch_route(theater: str, a: Vec2, b: Vec2) -> list[Vec2]:
    """Road route a -> b from OSRM, in theater coordinates."""
    lla, llb = to_latlon(theater, a), to_latlon(theater, b)
    assert lla and llb
    url = OSRM_URL.format(lat1=lla[0], lon1=lla[1], lat2=llb[0], lon2=llb[1])
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        data: dict[str, Any] = json.loads(r.read())
    if data.get("code") != "Ok" or not data.get("routes"):
        raise RuntimeError(f"OSRM: {data.get('code')} {data.get('message', '')}")
    out = []
    for lon, lat in data["routes"][0]["geometry"]["coordinates"]:
        x, z = ll_to_dcs(theater, lat, lon)
        out.append(Vec2(z, x))
    return out


def build(scenario_path: str | Path, log=print) -> Path:
    """Route every link of the scenario and write the roads file."""
    from . import scenario  # local import: scenario imports this module

    state = scenario.load(scenario_path, with_roads=False)
    theater = state.theater.name.lower()
    if theater not in THEATERS:
        raise ValueError(f"no projection for theater {theater!r}: cannot route on real roads")
    links: dict[str, list[Vec2]] = {}
    for link in state.theater.links():
        a, b = state.theater.provinces[link.a].center, state.theater.provinces[link.b].center
        try:
            pts = simplify(fetch_route(theater, a, b))
        except Exception as exc:  # noqa: BLE001 - keep going, the link stays straight
            log(f"  {link.a} - {link.b}: no road ({exc}); keeping a straight link")
            time.sleep(REQUEST_INTERVAL_S)
            continue
        km = sum(p.distance_to(q) for p, q in zip(pts, pts[1:])) / 1000
        straight_km = a.distance_to(b) / 1000
        if km > MAX_DETOUR * straight_km:
            log(f"  {link.a} - {link.b}: {km:.0f} km detour for {straight_km:.1f} km straight (road closed in OSM?); keeping a straight link")
            time.sleep(REQUEST_INTERVAL_S)
            continue
        log(f"  {link.a} - {link.b}: {km:.1f} km of road, {len(pts)} points (straight {straight_km:.1f} km)")
        links[key(link.a, link.b)] = pts
        time.sleep(REQUEST_INTERVAL_S)
    out = roads_path(scenario_path)
    out.write_text(dump(links, theater), encoding="utf-8")
    return out
