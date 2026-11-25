"""Bullseye: scenario definition, BULLS bearing/range, briefings and the API."""

import pytest

from dynacamp import bullseye, scenario
from dynacamp.geo import Vec2
from dynacamp.scenario import ScenarioError
from dynacamp.systems import briefing

from .conftest import DEMO
from .test_briefing import planned
from .test_air import air_line


def line_data(blue=None, red=None):
    """The five-province line scenario as data, with optional bullseyes."""
    data = {
        "campaign": {"name": "line", "seed": 1},
        "factions": [
            {"id": "blue", "side": "blue", "enemies": ["red"]},
            {"id": "red", "side": "red", "enemies": ["blue"]},
        ],
        "provinces": [
            {"id": pid, "pos": [i * 20_000.0, 0.0], "controller": "blue" if pid in "abc" else "red"}
            for i, pid in enumerate("abcde")
        ],
        "links": [{"a": a, "b": b} for a, b in zip("abcd", "bcde")],
    }
    if blue is not None:
        data["factions"][0]["bullseye"] = blue
    if red is not None:
        data["factions"][1]["bullseye"] = red
    return data


def test_scenario_bullseye_point_province_or_default():
    state = scenario.from_dict(line_data(blue={"pos": [5_000, 7_000]}, red="e"))
    assert state.factions["blue"].bullseye == Vec2(5_000, 7_000)
    assert state.factions["red"].bullseye == Vec2(80_000, 0)
    state = scenario.from_dict(line_data())  # default: centre of the side's provinces
    assert state.factions["blue"].bullseye == Vec2(20_000, 0)
    assert state.factions["red"].bullseye == Vec2(70_000, 0)


@pytest.mark.parametrize("bad", ["nowhere", [5_000, 7_000], 42, {"pos": [1, 2, 3]}, {"ll": [35.0, 36.0]}, {}])
def test_scenario_rejects_bad_bullseyes(bad):
    with pytest.raises(ScenarioError):
        scenario.from_dict(line_data(blue=bad))


def test_one_bullseye_per_side():
    data = line_data(blue="a")
    data["factions"].append({"id": "ally", "side": "blue", "enemies": ["red"], "bullseye": "b"})
    with pytest.raises(ScenarioError, match="same side"):
        scenario.from_dict(data)
    data["factions"][-1]["bullseye"] = "a"
    assert scenario.from_dict(data).factions["ally"].bullseye == Vec2(0, 0)
    del data["factions"][-1]["bullseye"]  # takes the side's
    assert scenario.from_dict(data).factions["ally"].bullseye == Vec2(0, 0)


def test_bulls_bearing_is_magnetic_and_range_in_the_units():
    state = scenario.from_dict(line_data(blue="a"))  # no projection: grid bearings, no variation
    assert bullseye.label(state, "blue", Vec2(0, 1852 * 32), "imperial") == "000/32"
    assert bullseye.label(state, "blue", Vec2(40_000, 0), "metric") == "090/40"
    from dynacamp.projection import from_latlon

    state.theater.name = "caucasus"  # a real projection and its magnetic variation
    variation = bullseye.MAGNETIC_VARIATION_DEG["caucasus"]
    state.factions["blue"].bullseye = from_latlon("caucasus", 42.0, 42.5)
    north = from_latlon("caucasus", 42.5, 42.5)  # due TRUE north, ~55.6 km
    brg, rng = bullseye.bearing_range(state, "blue", north)
    assert rng == pytest.approx(55_600, rel=0.01)
    assert brg == pytest.approx(360 - variation, abs=0.1)
    # the DCS grid is not true north out here (central meridian 33E): ~6 degrees of convergence
    grid = bullseye.true_bearing("caucasus", state.factions["blue"].bullseye, state.factions["blue"].bullseye + Vec2(0, 50_000))
    assert 4 < grid < 8


def test_briefing_gives_bulls_for_target_waypoints_and_threats():
    state = air_line()
    state.theater.name = "caucasus"
    f = planned(state)
    b = briefing.briefing(state, f, "imperial")
    assert b["bullseye"]["coordinates"].startswith(("N", "S"))
    assert b["target"]["bulls"] == bullseye.label(state, f.faction, f.target_point, "imperial")
    assert all("/" in wp["bulls"] for wp in b["waypoints"])
    assert all("/" in t["bulls"] for t in b["threats"])
    text = briefing.render(b)
    assert "BULLSEYE:" in text and f"BULLS {b['target']['bulls']}" in text


def test_campaign_view_has_each_sides_bullseye():
    from dynacamp.api import views

    state = scenario.load(DEMO)
    view = views.campaign(state)
    for fv in view["factions"]:
        bulls = state.factions[fv["id"]].bullseye
        assert fv["bullseye"]["pos"] == [round(bulls.x), round(bulls.y)] and fv["bullseye"]["ll"]


def test_old_saves_get_a_bullseye(tmp_path):
    import gzip
    import pickle

    from dynacamp import persistence

    state = scenario.from_dict(line_data(blue="a"))
    path = persistence.save(state, tmp_path / "c.dcsave")
    with gzip.open(path, "rb") as fh:
        envelope = pickle.load(fh)
    for f in envelope["state"].factions.values():
        del f.__dict__["bullseye"]  # as saved before bullseyes
    with gzip.open(path, "wb") as fh:
        pickle.dump(envelope, fh)
    resumed, _ = persistence.load(path)
    assert resumed.factions["blue"].bullseye == Vec2(20_000, 0)  # the default: centre of the side's provinces
    assert resumed.factions["red"].bullseye == Vec2(70_000, 0)
