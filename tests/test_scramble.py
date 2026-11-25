"""Quick reaction alert: scrambles against intruders near bases and assets."""

from dynacamp.air import AIRCRAFT, Airbase, Flight, FlightStatus, Package, Squadron, Task
from dynacamp.air_defense import SYSTEMS, AirDefenseUnit
from dynacamp.engine import Engine
from dynacamp.geo import Vec2
from dynacamp.systems import air as air_sys
from dynacamp.systems import intel, scramble

from .test_air_defense import world


def intruder(state, pos, task=Task.BAI, aircraft="su25t", known=True):
    """A red flight at ``pos`` (seen by blue if ``known``)."""
    if "rsq" not in state.squadrons:
        state.airbases["rab"] = Airbase("rab", "Red Field", Vec2(100_000, 10_000), province="e")
        state.squadrons["rsq"] = Squadron("rsq", "Rooks", "red", "rab", AIRCRAFT[aircraft], 12, "Rook")
    pkg = Package("rpk", "red", task, "t", Vec2(0, 0), 0)
    state.packages["rpk"] = pkg
    home = state.airbases["rab"].position
    f = Flight(id="rf", faction="red", location="e", package="rpk", squadron="rsq", aircraft_type=AIRCRAFT[aircraft],
               task=task, callsign="Rook 1", size=2, takeoff_time=0, tot=state.time + 3600,
               waypoints=[home, Vec2(0, 0), home], target_index=1, pos=pos, status=FlightStatus.AIRBORNE)
    state.squadrons["rsq"].committed += 2
    state.flights["rf"] = f
    pkg.flights.append(f.id)
    if known:
        intel.observe(state, "blue", f, "flight", "test", identified=True, pos=pos)
    return f


def scrambled(state):
    """Blue scramble flights."""
    return scramble.active(state, "blue")


def test_intruder_near_a_base_gets_a_pair_from_that_base():
    state = world()
    intruder(state, Vec2(10_000, 20_000))  # ~17 NM from Blue Field
    scramble.check(state, "blue")
    (f,) = scrambled(state)
    assert f.size == 2 and f.task is Task.CAP and f.target_id == "rf" and f.squadron == "sq"
    assert f.takeoff_time == state.time + scramble.ALERT_DELAY_S
    assert f.target_point == Vec2(10_000, 20_000)
    (ev,) = state.events.of_kind("scramble")
    assert ev.data["base"] == "Blue Field" and "Blue Field" in ev.data["threatened"]
    scramble.check(state, "blue")  # one pair per intruder
    assert len(scrambled(state)) == 1


def test_no_scramble_for_unknown_far_or_fighter_intruders():
    state = world()
    intruder(state, Vec2(10_000, 20_000), known=False)
    scramble.check(state, "blue")
    assert not scrambled(state)
    state = world()
    intruder(state, Vec2(10_000, 80_000))  # ~50 NM out
    scramble.check(state, "blue")
    assert not scrambled(state)
    state = world()
    intruder(state, Vec2(10_000, 20_000), task=Task.CAP, aircraft="mig29a")  # the CAP's business
    scramble.check(state, "blue")
    assert not scrambled(state)


def test_assets_near_a_base_are_protected_too():
    state = world()
    pos = Vec2(25_000, 52_000)  # 36 NM from the base, 28 NM from province b
    intruder(state, pos)
    scramble.check(state, "blue")
    assert not scrambled(state)
    state.air_defense["bs"] = AirDefenseUnit(id="bs", name="Blue SAM", faction="blue", location="b", system=SYSTEMS["hawk"])
    scramble.check(state, "blue")
    (f,) = scrambled(state)
    assert "Hawk" in state.events.of_kind("scramble")[0].data["threatened"]


def test_no_scramble_into_a_known_umbrella():
    state = world(sam="buk", at="d")
    intruder(state, Vec2(45_000, -5_000))  # 25 NM from the base, under the red Buk
    intel.reveal(state, "blue", state.air_defense["s1"], "test")
    scramble.check(state, "blue")
    assert not scrambled(state)


def test_interceptors_chase_the_track_and_go_home_when_it_is_gone():
    state = world()
    rf = intruder(state, Vec2(10_000, 20_000))
    scramble.check(state, "blue")
    (f,) = scrambled(state)
    f.status, f.pos = FlightStatus.AIRBORNE, Vec2(0, 0)
    rf.pos = Vec2(5_000, 25_000)
    intel.observe(state, "blue", rf, "flight", "test", pos=rf.pos)
    air_sys._fly(state, f, 1)
    assert f.target_point == Vec2(5_000, 25_000)  # vectored on the new position
    rf.status = FlightStatus.DONE
    air_sys._fly(state, f, 1)
    assert f.status is FlightStatus.RTB and f.wp_index == f.target_index + 1


def test_no_new_scramble_on_the_same_intruder_until_rearmed():
    state = world()
    intruder(state, Vec2(10_000, 20_000))
    scramble.check(state, "blue")
    (f,) = scrambled(state)
    f.status = FlightStatus.DONE
    scramble.check(state, "blue")
    assert not scrambled(state)
    state.time += scramble.REARM_S + 1
    intel.observe(state, "blue", state.flights["rf"], "flight", "test", pos=state.flights["rf"].pos)
    scramble.check(state, "blue")
    assert len(scrambled(state)) == 1


def test_scramble_runs_in_the_engine_and_meets_the_intruder():
    state = world()
    del state.air_defense["s1"]  # nowhere to hide: the intruder runs for home
    state.air_defense["ew"] = AirDefenseUnit(id="ew", name="EWR", faction="blue", location="a", system=SYSTEMS["ewr_fps117"])
    rf = intruder(state, Vec2(10_000, 20_000), known=False)
    engine = Engine(state)
    for _ in range(40):
        engine.step(60)
        if state.events.of_kind("air_engagement"):
            break
    (ev,) = state.events.of_kind("scramble")
    fights = state.events.of_kind("air_engagement")
    assert fights and ev.data["flight"] in fights[0].data["flights"] and "rf" in fights[0].data["flights"]
