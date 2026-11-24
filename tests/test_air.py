"""ATO planning, flight operations and players taking seats."""

import pytest

from dynacamp import scenario
from dynacamp.adapters.base import Materialize, PlayerKind, PublishAto
from dynacamp.adapters.fake import FakeGame
from dynacamp.air import FlightStatus, SeatState, Task
from dynacamp.engine import Engine
from dynacamp.geo import Vec2
from dynacamp.systems import ato

from .conftest import DEMO, line_scenario


def air_line():
    """The line scenario plus one airbase and squadron per side."""
    state = line_scenario()
    from dynacamp.air import AIRCRAFT, Airbase, Squadron

    state.airbases["ab_blue"] = Airbase("ab_blue", "Blue Field", Vec2(0, -10_000), province="a")
    state.airbases["ab_red"] = Airbase("ab_red", "Red Field", Vec2(80_000, -10_000), province="e")
    state.squadrons["viper"] = Squadron("viper", "Vipers", "blue", "ab_blue", AIRCRAFT["f16c"], 12, "Viper")
    state.squadrons["hawg"] = Squadron("hawg", "Hawgs", "blue", "ab_blue", AIRCRAFT["a10c"], 12, "Hawg")
    state.squadrons["grach"] = Squadron("grach", "Grach", "red", "ab_red", AIRCRAFT["su25t"], 12, "Grach")
    state.timers["ai"] = float("inf")
    return state


def first_flight(state, faction, task=None):
    return next(f for f in state.flights.values() if f.faction == faction and (task is None or f.task is task))


def test_ato_plans_packages_with_lead_time():
    state = air_line()
    Engine(state).step(60)
    assert state.packages, "no packages planned"
    for f in state.flights.values():
        assert f.takeoff_time >= state.time - 60 + ato.STARTUP_LEAD_S - 600
        sq = state.squadrons[f.squadron]
        assert f.task in sq.tasks
    assert sum(sq.committed for sq in state.squadrons.values()) == sum(f.size for f in state.flights.values())


def test_flights_fly_attack_and_return_aircraft():
    state = air_line()
    engine = Engine(state)
    engine.run_for(8 * 3600)
    kinds = {e.kind for e in state.events}
    assert {"flight_takeoff", "air_strike", "flight_complete"} <= kinds
    for sq in state.squadrons.values():
        assert sq.committed >= 0 and sq.aircraft >= 0
        assert sq.available_at(state.time) <= sq.aircraft


def test_squadron_accounting_balances_over_a_long_run(demo):
    Engine(demo).run_for(3 * 86400, dt=300)
    for sq in demo.squadrons.values():
        live = sum(f.size for f in demo.flights.values() if f.squadron == sq.id and f.status not in (FlightStatus.DONE, FlightStatus.CANCELLED))
        assert sq.committed == live, sq.id


def test_airbase_capture_cancels_scheduled_flights():
    state = air_line()
    Engine(state).step(60)
    f = first_flight(state, "red")
    state.theater.provinces["e"].controller = "blue"
    Engine(state).run_for(ato.STARTUP_LEAD_S + 600)
    assert f.status is FlightStatus.CANCELLED or f.id not in state.flights


# ---------------------------------------------------------------- players


def live_engine():
    state = air_line()
    game = FakeGame()
    engine = Engine(state, game)
    game.player("Pilot", Vec2(0, -60_000), side="blue", kind=PlayerKind.AIR)
    engine.step(60)
    return state, game, engine


def test_ato_board_is_published_to_the_game():
    state, game, engine = live_engine()
    boards = [c for c in game.received if isinstance(c, PublishAto) and c.side == "blue"]
    assert boards and boards[-1].packages
    assert all(fl["seats"] > 0 for p in boards[-1].packages for fl in p["flights"])


def test_player_joins_gets_briefing_and_replaces_an_ai_aircraft():
    state, game, engine = live_engine()
    f = first_flight(state, "blue")
    before = f.ai_count
    game.join("Pilot", f.id)
    engine.step(1)
    assert f.players["Pilot"].state is SeatState.ASSIGNED
    assert f.ai_count == before - 1
    note = game.messages_for("Pilot")[-1]
    assert f.callsign in note.text and "TOT" in note.text
    assert note.marks and any(("TARGET" in label or "CAP STATION" in label) for label, _ in note.marks)
    assert "ORDNANCE" in note.text and "WAYPOINTS" in note.text


def test_join_respects_aircraft_capability_and_side():
    state, game, engine = live_engine()
    f = first_flight(state, "blue")
    other = "CAP" if f.task is not Task.CAP else "CAS"
    game.join("Pilot", f.id, frozenset({other}))
    engine.step(1)
    assert "Pilot" not in f.players and "cannot fly" in game.messages_for("Pilot")[-1].text
    red = first_flight(state, "red")
    game.join("Pilot", red.id)
    engine.step(1)
    assert "Pilot" not in red.players


def test_player_only_flight_is_not_simulated_and_completes_on_landing():
    state, game, engine = live_engine()
    f = first_flight(state, "blue")
    pilots = [f"P{i}" for i in range(f.size)]
    for p in pilots:
        game.player(p, Vec2(0, -60_000), side="blue")
    engine.step(1)
    for p in pilots:
        game.join(p, f.id)
    engine.step(1)
    assert f.ai_count == 0
    engine.run_for(f.takeoff_time - state.time + 120)
    assert f.status is FlightStatus.AIRBORNE and f.ai_done
    assert f.id not in game.entities  # nothing for the game to spawn: players are the flight
    for p in pilots:
        game.player_event(p, "takeoff")
    engine.step(1)
    game.player_event(pilots[0], "lost")
    for p in pilots[1:]:
        game.player_event(p, "landed")
    engine.step(1)
    sq = state.squadrons[f.squadron]
    assert sq.aircraft == 11
    assert f.status is FlightStatus.DONE or f.id not in state.flights


def test_ai_flight_in_bubble_is_spawned_by_the_game():
    state, game, engine = live_engine()
    f = first_flight(state, "blue")
    engine.run_for(f.takeoff_time - state.time + 60)
    assert f.status is FlightStatus.AIRBORNE
    engine.step(1)  # handoffs are decided at the start of a step
    spawned = [c.entity for c in game.received if isinstance(c, Materialize) and c.entity.id == f.id]
    assert spawned and spawned[0].kind == "flight" and spawned[0].attrs["task"] == f.task.value
    engine.step(1)
    assert not f.simulated_by_campaign
    # the game shoots one down
    game.kill(f.id, "aircraft", 1)
    game.advance(1)
    engine.step(1)
    assert f.size == 1


def test_disconnect_frees_unflown_seats():
    state, game, engine = live_engine()
    f = first_flight(state, "blue")
    game.join("Pilot", f.id)
    engine.step(1)
    game.disconnect()
    engine.step(1)
    assert f.players["Pilot"].state is SeatState.LEFT
    assert "Pilot" not in state.player_flights


@pytest.mark.parametrize("path", sorted(DEMO.parent.glob("*.toml")), ids=lambda p: p.stem)
def test_scenarios_have_working_air_orders_of_battle(path):
    state = scenario.load(path)
    assert state.squadrons and state.airbases
    Engine(state).run_for(12 * 3600, dt=300)
    assert any(e.kind == "air_strike" for e in state.events)
