"""Air defence: site life cycle, IADS, engagements, SEAD, mobility, game handoff."""

import pytest

from dynacamp.adapters.base import Materialize, PlayerKind
from dynacamp.adapters.fake import FakeGame
from dynacamp.air import AIRCRAFT, Airbase, Flight, FlightStatus, Package, Squadron, Task
from dynacamp.air_defense import SYSTEMS, AirDefenseUnit, SamStatus
from dynacamp.engine import Engine
from dynacamp.geo import Vec2
from dynacamp.systems import air_defense as ada
from dynacamp.systems import ato

from .conftest import line_scenario


def world(sam="buk", at="d"):
    state = line_scenario()
    state.timers["ai"] = state.timers["ato"] = state.timers["ada"] = float("inf")
    state.air_defense["s1"] = AirDefenseUnit(id="s1", name="SAM", faction="red", location=at, system=SYSTEMS[sam])
    state.airbases["ab"] = Airbase("ab", "Blue Field", Vec2(0, -10_000), province="a")
    state.squadrons["sq"] = Squadron("sq", "Vipers", "blue", "ab", AIRCRAFT["f16c"], 12, "Viper")
    return state


def flight(state, pos, task=Task.BAI, size=2, target=Vec2(80_000, 0)):
    pkg = Package("pk", "blue", task, "t", target, 0)
    state.packages["pk"] = pkg
    f = Flight(id="fl", faction="blue", location="a", package="pk", squadron="sq", aircraft_type=AIRCRAFT["f16c"],
               task=task, callsign="Viper 1", size=size, takeoff_time=0, tot=0,
               waypoints=[Vec2(0, -10_000), target, Vec2(0, -10_000)], target_index=1, pos=pos,
               status=FlightStatus.AIRBORNE)
    state.squadrons["sq"].committed += size
    state.flights["fl"] = f
    pkg.flights.append(f.id)
    return f


def test_site_engages_flight_in_range_and_spends_missiles():
    state = world()
    f = flight(state, Vec2(60_000, 10_000))  # 10 km from the Buk at d (60 km, 0)
    state.rng.seed(3)
    Engine(state).step(1)
    ev = [e for e in state.events if e.kind == "sam_engagement"]
    assert ev and ev[0].data["flight"] == "fl" and ev[0].data["fired"] == 2
    assert state.air_defense["s1"].missiles == SYSTEMS["buk"].missiles - 2


def test_no_engagement_out_of_range_above_ceiling_or_while_moving():
    state = world(sam="tor")
    flight(state, Vec2(60_000, 8_000))  # in range but a jet at 6000 m is above... Tor reaches 6000: put it far
    state.flights["fl"].pos = Vec2(60_000, 30_000)  # beyond 12 km
    Engine(state).step(1)
    assert not [e for e in state.events if e.kind == "sam_engagement"]

    state = world(sam="avenger")  # ceiling 3800 m: cruising jets are out of reach
    flight(state, Vec2(60_000, 5_000), target=Vec2(300_000, 0))
    Engine(state).step(1)
    assert not [e for e in state.events if e.kind == "sam_engagement"]

    state = world()
    flight(state, Vec2(60_000, 10_000))
    state.air_defense["s1"].status = SamStatus.MOVING
    state.route_entity(state.air_defense["s1"], "e")
    Engine(state).step(1)
    assert not [e for e in state.events if e.kind == "sam_engagement"]


def test_iads_cues_sites_beyond_their_own_radar():
    state = world(sam="kub")  # own detection 70 km, range 25 km
    site = state.air_defense["s1"]
    # target inside range but pretend the site's own radar cannot see it: shrink detection via EWR test
    far = Vec2(60_000, 200_000)
    assert not ada.detects(state, site, far)
    state.air_defense["e1"] = AirDefenseUnit(id="e1", name="EWR", faction="red", location="e", system=SYSTEMS["ewr_55g6"])
    assert ada.network_node(state, site) is not None
    assert ada.detects(state, site, far)


def test_sead_suppresses_or_destroys_emitting_radar():
    results = set()
    for seed in range(12):
        state = world()
        f = flight(state, Vec2(60_000, 40_000), task=Task.SEAD)
        state.rng.seed(seed)
        ada._sead(state)
        results |= {e.data["result"] for e in state.events if e.kind == "harm_strike"}
    assert results & {"suppressed", "radar destroyed"}


def test_networked_site_can_go_dark():
    state = world()
    state.air_defense["e1"] = AirDefenseUnit(id="e1", name="EWR", faction="red", location="e", system=SYSTEMS["ewr_55g6"])
    dark = 0
    for seed in range(20):
        state.air_defense["s1"].status = SamStatus.ACTIVE
        state.rng.seed(seed)
        ada.hit_on_radar(state, state.air_defense["s1"], 2.0, by="x")
        dark += state.events.of_kind("harm_strike")[-1].data["result"] == "went dark"
    assert 0 < dark < 20


def test_site_moves_on_roads_then_sets_up():
    state = world(at="e")
    site = state.air_defense["s1"]
    assert state.route_entity(site, "d")
    engine = Engine(state)
    engine.step(60)
    assert site.status is SamStatus.MOVING
    for _ in range(200):
        engine.step(60)
        if site.status is not SamStatus.MOVING:
            break
    assert site.location == "d" and site.status is SamStatus.SETTING_UP
    engine.run_for(site.system.setup_s + 60, dt=60)
    assert site.status is SamStatus.ACTIVE


def test_commander_moves_mobile_sams_towards_assets():
    state = world(sam="tor", at="e")  # short range, far from the red brigade at d
    state.divisions["r1"].battle = "bt"  # a brigade fighting: SHORAD should go with it
    ada.plan(state, "red")
    site = state.air_defense["s1"]
    assert site.movement is not None and site.movement.path[-1] == "d"


def test_capture_overruns_site_and_a_replacement_arrives():
    state = world(at="d")
    from dynacamp.systems import control

    control.capture(state, "d", "blue", by="test")
    assert "s1" not in state.air_defense
    Engine(state).run_for(ada.REFORM_DELAY_S + 600, dt=600)
    assert any(u.system.id == "buk" and u.faction == "red" for u in state.air_defense.values())


def test_ato_escorts_packages_into_sam_coverage_with_sead():
    state = world(sam="s300ps", at="e")
    point = state.theater.provinces["d"].center
    assert ada.covering(state, "blue", point)
    assert ato.needs_sead(state, "blue", point)
    # and keeps CAP stations out of the SAM umbrella
    station = ato.cap_station(state, "blue", point)
    assert station is None or not ada.covering(state, "blue", station)


def test_player_inside_sam_range_brings_the_site_into_the_game():
    state = world(sam="s300ps", at="e")
    game = FakeGame()
    engine = Engine(state, game)
    site_pos = state.air_defense["s1"].position(state.theater)
    # 70 km away: outside the 60 km air bubble, inside the S-300's 75 km range
    game.player("p1", site_pos + Vec2(0, 70_000), kind=PlayerKind.AIR)
    engine.step(1)
    assert "s1" in {c.entity.id for c in game.received if isinstance(c, Materialize)}


def test_dcs_spawns_real_site_components_on_alarm_red():
    from dynacamp.adapters.dcs import DcsTranslator

    state = world(sam="buk")
    engine = Engine(state)
    snap = engine._snapshot(state.air_defense["s1"])
    msg = DcsTranslator().encode(Materialize(snap))
    types = sorted(u["type"] for g in msg["groups"] for u in g["data"]["units"])
    assert types == sorted(["SA-11 Buk SR 9S18M1", "SA-11 Buk CC 9S470M1"] + ["SA-11 Buk LN 9A310M1"] * 4)
    assert all(g["alarm_red"] for g in msg["groups"])


@pytest.mark.parametrize("system", sorted(SYSTEMS))
def test_every_system_has_dcs_types(system):
    from dynacamp.adapters.dcs import catalog

    assert set(SYSTEMS[system].elements) <= set(catalog.SAM_TYPES[system])
