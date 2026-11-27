"""Air-to-air by sensors: radar cones, forced engagement, missiles and fuel."""

import math

from dynacamp import loadouts, sensors
from dynacamp.air import AIRCRAFT, Airbase, Flight, FlightStatus, Package, Squadron, Task
from dynacamp.engine import Engine
from dynacamp.geo import Vec2
from dynacamp.systems import air as air_sys
from dynacamp.systems import ato

from .conftest import line_scenario


def world():
    """The line map with a blue and a red airbase and squadrons; planners off."""
    state = line_scenario()
    state.timers["ai"] = state.timers["ato"] = state.timers["ada"] = state.timers["scramble"] = float("inf")
    state.airbases["ab"] = Airbase("ab", "Blue Field", Vec2(0, -10_000), province="a")
    state.airbases["rb"] = Airbase("rb", "Red Field", Vec2(80_000, 10_000), province="e")
    state.squadrons["bq"] = Squadron("bq", "Eagles", "blue", "ab", AIRCRAFT["f15c"], 12, "Eagle")
    state.squadrons["rq"] = Squadron("rq", "Rooks", "red", "rb", AIRCRAFT["su25t"], 12, "Rook")
    state.squadrons["rf"] = Squadron("rf", "Flankers", "red", "rb", AIRCRAFT["su27"], 12, "Sokol")
    return state


def flight(state, fid, sq, task, pos, track=0.0, status=FlightStatus.AIRBORNE, target=Vec2(40_000, 0)):
    """An airborne flight of two at ``pos`` flying ``track`` (radians)."""
    q = state.squadrons[sq]
    home = state.airbases[q.airbase].position
    state.packages[f"p{fid}"] = Package(f"p{fid}", q.faction, task, "t", target, 0)
    f = Flight(id=fid, faction=q.faction, location="a", package=f"p{fid}", squadron=sq, aircraft_type=q.aircraft_type,
               task=task, callsign=f"{q.callsign} 1", size=2, takeoff_time=0, tot=state.time + 3600,
               waypoints=[home, target, home], target_index=1, pos=pos, status=status, track=track,
               station_until=state.time + 7200 if task is Task.CAP else 0.0)
    q.committed += 2
    state.flights[fid] = f
    state.packages[f"p{fid}"].flights.append(fid)
    air_sys.ready(state, f)
    return f


def test_missiles_come_from_the_loadout():
    f15 = loadouts.air_to_air("f15c", "CAP")
    assert f15.bvr == 4 and f15.bvr_range_m == 50_000
    hornet = loadouts.air_to_air("fa18c", "CAP")
    assert hornet.bvr == 7 and hornet.wvr == 3  # 3 x (2 x AIM-120 on LAU-115) + 1; AIM-9s on the tips and a rail
    assert loadouts.air_to_air("f15c", "CAP").wvr == 2  # heat seekers fill the outer rails first
    assert loadouts.air_to_air("mig23mld", "CAP").wvr == 4  # 2 x R-60 carriers
    assert loadouts.air_to_air("f5e", "CAP") == loadouts.AirToAir()  # training rounds do not count
    assert loadouts.air_to_air("m2000c", "ESCORT").bvr > 0  # escorts carry the CAP load


def test_ready_gives_fuel_and_missiles():
    state = world()
    f = flight(state, "b", "bq", Task.CAP, Vec2(0, 0))
    assert f.fuel_m == air_sys.FUEL_FACTOR * 700_000 and f.aam_bvr == 8 and f.aam_loaded == 12
    state.squadrons["aw"] = Squadron("aw", "AEW", "blue", "ab", AIRCRAFT["e3a"], 2, "Magic")
    assert math.isinf(flight(state, "w", "aw", Task.AWACS, Vec2(0, 0)).fuel_m)


def test_radar_cone():
    state = world()
    f = flight(state, "b", "bq", Task.CAP, Vec2(0, 0), track=0.0)  # flying north
    tgt = flight(state, "r", "rq", Task.CAS, Vec2(0, 50_000))
    assert sensors.radar_sees(f, f.pos, tgt, Vec2(0, 50_000))
    assert sensors.radar_sees(f, f.pos, tgt, Vec2(0, 50_000), track_only=True)  # 50 < 60 km
    assert not sensors.radar_sees(f, f.pos, tgt, Vec2(0, 80_000), track_only=True)
    assert not sensors.radar_sees(f, f.pos, tgt, Vec2(0, -50_000))  # behind
    assert not sensors.radar_sees(f, f.pos, tgt, Vec2(60_000, 10_000))  # 80 degrees off the nose


def test_a_fighter_must_engage_what_it_sees():
    state = world()
    cap = flight(state, "b", "bq", Task.CAP, Vec2(0, 0), track=math.pi / 2)  # flying east
    flight(state, "r", "rq", Task.CAS, Vec2(40_000, 0), track=-math.pi / 2)
    flight(state, "r2", "rq", Task.CAS, Vec2(-40_000, 0), track=math.pi / 2)  # behind it
    state.rng.seed(1)
    engine = Engine(state)
    engine.step(1)
    assert cap.engaging == "r"  # in the cone: committed, the one behind is not seen
    assert state.events.of_kind("air_engagement")[0].data["phase"] == "BVR"  # 40 km: radar missiles
    for _ in range(10):
        engine.step(30)
    fights = state.events.of_kind("air_engagement")
    assert fights and fights[0].data["flights"] == ["b", "r"] and fights[0].data["phase"] in ("BVR", "WVR")
    assert cap.aam_bvr + cap.aam_wvr < cap.aam_loaded  # it fired
    first = [e for e in fights if e.time <= fights[0].time]
    assert not any("r2" in e.data["flights"] for e in first)  # out of the cone at first


def test_no_commit_when_winchester_or_going_home():
    state = world()
    cap = flight(state, "b", "bq", Task.CAP, Vec2(0, 0), track=math.pi / 2)
    flight(state, "r", "rq", Task.CAS, Vec2(40_000, 0))
    cap.aam_bvr = cap.aam_wvr = 0  # fired everything
    Engine(state).step(1)
    assert not cap.engaging and cap.status is FlightStatus.RTB
    assert state.events.of_kind("flight_rtb")[-1].data["reason"] == "winchester"


def test_bingo_fuel_sends_a_flight_home():
    state = world()
    cap = flight(state, "b", "bq", Task.CAP, Vec2(0, 60_000), track=0.0)
    cap.fuel_m = 80_000.0  # 70 km from home: just the way back and the reserve
    Engine(state).step(10)
    assert cap.status is FlightStatus.RTB
    assert state.events.of_kind("flight_rtb")[-1].data["reason"] == "bingo fuel"


def test_one_pass_then_the_fighter_leaves_the_target_alone():
    state = world()
    cap = flight(state, "b", "bq", Task.CAP, Vec2(0, 0), track=math.pi / 2)
    striker = flight(state, "r", "rq", Task.CAS, Vec2(5_000, 0))  # already in the merge
    striker.aam_wvr = 0  # cannot fire back
    Engine(state).step(1)
    assert len(state.events.of_kind("air_engagement")) == 1
    assert not cap.engaging and state.timers.get("aa_pass:b:r", 0) > state.time
    for _ in range(5):
        Engine(state).step(60)
    assert len(state.events.of_kind("air_engagement")) == 1  # no second pass yet


def test_fighters_under_fire_turn_on_their_attacker():
    state = world()
    cap = flight(state, "b", "bq", Task.CAP, Vec2(0, 0), track=math.pi / 2)
    bandit = flight(state, "r", "rf", Task.CAP, Vec2(45_000, 0), track=math.pi / 2)  # flying away: does not see it
    state.rng.seed(2)
    engine = Engine(state)
    engine.step(1)
    assert cap.engaging == "r" and not bandit.engaging or bandit.engaging == "b"
    for _ in range(4):
        engine.step(30)
    if state.events.of_kind("air_engagement") and bandit.ai_count:
        assert bandit.engaging in ("b", "")  # it turned (or already broke off after its pass)


def test_cap_station_time_fits_the_fuel():
    state = world()
    sq = state.squadrons["bq"]
    r = ato.Request(Task.CAP, 9, "far CAP", Vec2(600_000, 0), "point", "x")
    pkg = ato._new_package(state, "blue", r, state.time + 3600)
    f = ato._add_flight(state, pkg, sq, Task.CAP, 2, state.time + 3600, r)
    assert f.station_until < f.tot + ato.CAP_STATION_S  # 600 km out: less time on station
    near = ato.Request(Task.CAP, 9, "near CAP", Vec2(20_000, 0), "point", "y")
    g = ato._add_flight(state, ato._new_package(state, "blue", near, state.time + 3600), sq, Task.CAP, 2, state.time + 3600, near)
    assert f.station_until < g.station_until <= g.tot + ato.CAP_STATION_S
