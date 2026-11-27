"""Factions: coalition, army and colour come from the scenario and the catalogue."""

import pytest

from dynacamp import scenario
from dynacamp.adapters.dcs import catalog as dcs_catalog
from dynacamp.scenario import ScenarioError

from .conftest import ROOT


def two_factions(**override):
    """A minimal scenario whose first faction gets ``override``."""
    first = {"id": "x", "coalition": "BLUE", "army": "us", "color": "#4C8DFF", "enemies": ["y"], **override}
    return {
        "factions": [first, {"id": "y", "coalition": "RED", "army": "russia", "color": "#ff5a5a", "enemies": ["x"]}],
        "provinces": [{"id": "a", "pos": [0, 0], "controller": "x"}, {"id": "b", "pos": [20_000, 0], "controller": "y"}],
        "links": [{"a": "a", "b": "b"}],
    }


def test_faction_fields_come_from_the_scenario():
    f = scenario.from_dict(two_factions()).factions["x"]
    assert (f.coalition, f.army, f.color) == ("BLUE", "us", "#4c8dff")


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"coalition": None}, "missing coalition"),
        ({"army": None}, "missing army"),
        ({"color": None}, "missing color"),
        ({"army": "atlantis"}, "unknown army"),
        ({"color": "blue"}, "not #rrggbb"),
    ],
)
def test_bad_factions_are_rejected(override, message):
    data = two_factions(**override)
    data["factions"][0] = {k: v for k, v in data["factions"][0].items() if v is not None}
    with pytest.raises(ScenarioError, match=message):
        scenario.from_dict(data)


def test_shipped_scenarios_use_catalogue_armies_with_dcs_data():
    for path in sorted((ROOT / "scenarios").glob("*.toml")):
        for f in scenario.load(path, with_roads=False).factions.values():
            assert f.army in dcs_catalog.COUNTRY and dcs_catalog.UNIT_TYPES[f.army], (path.name, f.id)


def test_quality_comes_from_the_army_unless_the_scenario_sets_it():
    assert scenario.from_dict(two_factions(army="israel")).factions["x"].quality == 1.5
    assert scenario.from_dict(two_factions(army="israel", quality=1.2)).factions["x"].quality == 1.2
    with pytest.raises(ScenarioError, match="quality"):
        scenario.from_dict(two_factions(quality=10))


def test_quality_scales_land_combat():
    from dynacamp.engine import Engine

    def red_losses(quality):
        """Strength the red brigade has left after a day against a blue one of ``quality``."""
        data = two_factions(quality=quality)
        data["divisions"] = [
            {"id": "b", "faction": "x", "template": "tank_brigade", "province": "a"},
            {"id": "r", "faction": "y", "template": "tank_brigade", "province": "b"},
        ]
        state = scenario.from_dict(data)
        Engine(state).run_for(24 * 3600)
        return state.divisions["r"].strength if "r" in state.divisions else 0.0

    assert red_losses(2.0) < red_losses(0.5)
