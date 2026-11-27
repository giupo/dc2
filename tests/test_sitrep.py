"""Situation report sent to a player who takes a slot, and the standing it shows."""

import pytest

from dynacamp import standing
from dynacamp.adapters.base import NotifyPlayer
from dynacamp.adapters.dcs.protocol import DcsTranslator
from dynacamp.adapters.fake import FakeGame
from dynacamp.air import Package, Task
from dynacamp.air_defense import SYSTEMS, AirDefenseUnit
from dynacamp.engine import Engine
from dynacamp.geo import Vec2
from dynacamp.systems import intel, sitrep

from .conftest import line_scenario


def test_standing_shares_sum_to_one_and_favour_who_holds_more():
    state = line_scenario()
    shares = standing.shares(state)
    assert shares.keys() == {"blue", "red"} and sum(shares.values()) == pytest.approx(1.0)
    before = shares["blue"]
    state.theater.provinces["d"].controller = "blue"  # blue takes a province
    assert standing.shares(state)["blue"] > before


def test_a_player_taking_a_slot_gets_the_sitrep_of_their_side():
    state = line_scenario()
    game = FakeGame()
    engine = Engine(state, game)
    game.player("Pilot", Vec2(0, -60_000), coalition="BLUE")
    engine.step(1)
    notes = [c for c in game.received if isinstance(c, NotifyPlayer) and c.player_id == "Pilot"]
    assert len(notes) == 1 and notes[0].duration_s == sitrep.DURATION_S
    text = notes[0].text
    assert text.startswith("SITREP D1") and "OBJECTIVES" in text and "BULLSEYE" in text
    game.player("Pilot", Vec2(0, -59_000), coalition="BLUE")
    engine.step(1)
    assert len([c for c in game.received if isinstance(c, NotifyPlayer)]) == 1  # once per slot, not every report


def test_the_sitrep_shows_only_what_the_side_knows():
    state = line_scenario()
    state.air_defense["s1"] = AirDefenseUnit(id="s1", name="SAM", faction="red", location="e", system=SYSTEMS["kub"])
    assert "SA-6" not in sitrep.text(state, "blue")
    intel.reveal(state, "blue", state.air_defense["s1"], "test")
    assert "SA-6 Gainful" in sitrep.text(state, "blue").split("SAMS", 1)[1].splitlines()[0]


def test_the_sitrep_lists_packages_without_offering_seats():
    state = line_scenario()
    state.packages["pk"] = Package("pk", "blue", Task.BAI, "enemy brigade at D", Vec2(60_000, 0), 3 * 3600, number="4521")
    state.packages["rk"] = Package("rk", "red", Task.CAS, "secret", Vec2(0, 0), 3 * 3600, number="9999")
    plan = sitrep.text(state, "blue").split("PACKAGES (AI)\n", 1)[1].split("\nSUPPORT", 1)[0]
    assert plan == "  4521 BAI enemy brigade at D TOT 03:00"  # ours only, and no seats on offer


def test_the_sitrep_reaches_the_bridge_with_its_duration():
    msg = DcsTranslator().encode(NotifyPlayer("Pilot", "SITREP", duration_s=60.0))
    assert msg["duration"] == 60.0 and msg["text"] == "SITREP"
    assert "duration" not in DcsTranslator().encode(NotifyPlayer("Pilot", "x"))
