"""Scenario coordinates: [lat, lon] in the files, the theater plane inside."""

import json

import pytest

from dynacamp import roads, scenario
from dynacamp.geo import Vec2
from dynacamp.projection import from_latlon, to_latlon
from dynacamp.scenario import ScenarioError

from .conftest import ROOT


def data(theater, a, b):
    """Two linked provinces on ``theater`` with the coordinate tables ``a`` and ``b``."""
    return {
        "campaign": {"name": "t", "theater": theater},
        "factions": [{"id": "blue", "side": "blue", "enemies": ["red"]}, {"id": "red", "side": "red", "enemies": ["blue"]}],
        "provinces": [{"id": "a", "controller": "blue", **a}, {"id": "b", "controller": "red", **b}],
        "links": [{"a": "a", "b": "b"}],
        "airbases": [{"id": "ab", "name": "AB", "province": "a", **a}],
    }


def test_positions_are_lat_lon_projected_on_the_plane():
    state = scenario.from_dict(data("syria", {"ll": [35.86, 36.80]}, {"ll": [34.73, 36.71]}))
    a = state.theater.provinces["a"].center
    assert a == from_latlon("syria", 35.86, 36.80) and state.airbases["ab"].position == a
    lat, lon = to_latlon("syria", a)
    assert lat == pytest.approx(35.86, abs=1e-6) and lon == pytest.approx(36.80, abs=1e-6)


def test_plane_metres_only_without_a_projection():
    state = scenario.from_dict(data("unnamed", {"pos": [0, 0]}, {"pos": [20_000, 0]}))
    assert state.theater.provinces["b"].center == Vec2(20_000, 0)
    with pytest.raises(ScenarioError, match="no projection"):
        scenario.from_dict(data("unnamed", {"ll": [35.0, 36.0]}, {"pos": [20_000, 0]}))


@pytest.mark.parametrize("bad", [{}, {"ll": [35.0]}, {"ll": ["35", "36"]}, {"ll": [135.0, 36.0]}, {"ll": [36.0, 236.0]}])
def test_bad_coordinates_are_refused(bad):
    with pytest.raises(ScenarioError):
        scenario.from_dict(data("syria", bad, {"ll": [34.73, 36.71]}))


def test_shipped_scenarios_use_lat_lon_only():
    import tomllib

    for path in sorted((ROOT / "scenarios").glob("*.toml")):
        doc = tomllib.loads(path.read_text())
        for table in ("provinces", "airbases"):
            for item in doc.get(table, []):
                assert "ll" in item and "pos" not in item, (path.name, item["id"])
        r = json.loads(roads.roads_path(path).read_text())
        assert r["version"] == roads.FORMAT_VERSION and r["frame"].startswith("WGS84")
        for pts in r["links"].values():
            assert all(-90 <= lat <= 90 and -180 <= lon <= 180 for lat, lon in pts)


def test_roads_files_old_and_new_formats(tmp_path):
    scen = tmp_path / "s.toml"
    line = [Vec2(100_000, 120_000), Vec2(101_000, 121_500)]
    roads.roads_path(scen).write_text(json.dumps({"version": 1, "links": {"a|b": [[p.x, p.y] for p in line]}}))
    assert roads.load(scen, "syria")["a|b"] == line  # version 1: plane metres, as they are
    roads.roads_path(scen).write_text(roads.dump({"a|b": line}, "syria"))
    back = roads.load(scen, "syria")["a|b"]
    assert all(p.distance_to(q) < 0.2 for p, q in zip(back, line))  # 6 decimals ~ 0.1 m
    with pytest.raises(ValueError, match="projection"):
        roads.load(scen, "unnamed")


def test_squadron_insignia_exist_and_are_credited():
    import tomllib

    folder = ROOT / "web" / "public" / "insignia"
    credits = (folder / "CREDITS.md").read_text()
    used = set()
    for path in sorted((ROOT / "scenarios").glob("*.toml")):
        for q in tomllib.loads(path.read_text()).get("squadrons", []):
            if q.get("insignia"):
                used.add(q["insignia"])
                assert (folder / q["insignia"]).is_file(), (path.name, q["id"], q["insignia"])
    assert used
    files = {p.name for p in folder.iterdir() if p.name != "CREDITS.md"}
    assert files == used, "unused or missing insignia files"
    assert all(f"`{f}`" in credits for f in files)
    state = scenario.load(ROOT / "scenarios" / "syria_m5.toml")
    assert state.squadrons["b_tomcat"].insignia == "usn_vf103.svg"
