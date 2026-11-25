"""Jamming: radars see jammed aircraft late, radar missiles miss more, ELINT finds jammers."""

import math

import pytest

from dynacamp import loadouts, sensors
from dynacamp.air import FlightStatus, Task
from dynacamp.air_defense import SYSTEMS, AirDefenseUnit
from dynacamp.geo import Vec2
from dynacamp.systems import air_defense as ada
from dynacamp.systems import intel

from .test_air_combat import flight, world


def test_reach_shrinks_but_radars_burn_through():
    assert sensors.reach(100_000, 1.0) == 100_000
    assert sensors.reach(100_000, sensors.ESCORT_JAM) == 50_000
    assert sensors.reach(15_000, sensors.ESCORT_JAM) == sensors.BURN_THROUGH_M  # never below burn-through
    assert sensors.reach(8_000, sensors.ESCORT_JAM) == 8_000  # nor above the radar's own range


def test_who_jams():
    state = world()
    cap = flight(state, "b", "bq", Task.CAP, Vec2(0, 0))
    assert sensors.jam(state, cap) == 1.0
    from dynacamp.air import AIRCRAFT, Squadron

    state.squadrons["vq"] = Squadron("vq", "Vipers", "blue", "ab", AIRCRAFT["f16c"], 12, "Viper")
    assert loadouts.has_ecm_pod("f16c", "CAS") and not loadouts.has_ecm_pod("f15c", "CAP")
    cas = flight(state, "c", "vq", Task.CAS, Vec2(100_000, 0))
    assert sensors.jam(state, cas) == sensors.SELF_JAM  # ALQ-131 on its CAS loadout
    sead = flight(state, "s", "vq", Task.SEAD, Vec2(20_000, 0))
    assert sensors.jam(state, cap) == sensors.ESCORT_JAM  # 20 km from a friendly SEAD flight
    assert sensors.escort_jammer(state, sead) is None  # not its own escort
    assert sensors.jam_pk(sensors.ESCORT_JAM, cap) == sensors.ESCORT_JAM_PK
    assert sensors.jam_pk(sensors.SELF_JAM, cas) == sensors.SELF_JAM_PK
    assert sensors.jam_pk(sensors.SELF_JAM, sead) == 1.0  # SEAD's own pods are in its SAM odds already


def test_sam_sees_a_screened_flight_late():
    state = world()
    state.air_defense["s1"] = AirDefenseUnit(id="s1", name="SAM", faction="red", location="d", system=SYSTEMS["buk"])
    site = state.air_defense["s1"]
    here = site.position(state.theater)
    far = here + Vec2(0, site.system.detection_m * 0.8)
    assert ada.detects(state, site, far)
    assert not ada.detects(state, site, far, sensors.ESCORT_JAM)
    assert ada.detects(state, site, here + Vec2(0, 5_000), sensors.ESCORT_JAM)  # burn-through


def test_fighters_commit_later_on_jammed_targets():
    state = world()
    cap = flight(state, "b", "rf", Task.CAP, Vec2(0, 0), track=math.pi / 2)  # red Su-27 looking east
    from dynacamp.air import AIRCRAFT, Squadron

    state.squadrons["vq"] = Squadron("vq", "Vipers", "blue", "ab", AIRCRAFT["f16c"], 12, "Viper")
    strike = flight(state, "x", "vq", Task.STRIKE, Vec2(45_000, 0))  # inside the 60 km tracking range
    assert sensors.radar_sees(cap, cap.pos, strike, strike.pos, track_only=True, jamming=1.0)
    flight(state, "s", "vq", Task.SEAD, Vec2(50_000, 5_000))  # its jammer
    j = sensors.jam(state, strike)
    assert j == sensors.ESCORT_JAM
    assert not sensors.radar_sees(cap, cap.pos, strike, strike.pos, track_only=True, jamming=j)


def test_elint_finds_a_jammer_roughly():
    state = world()
    from dynacamp.air import AIRCRAFT, Squadron

    state.squadrons["vq"] = Squadron("vq", "Vipers", "blue", "ab", AIRCRAFT["f16c"], 12, "Viper")
    state.squadrons["aw"] = Squadron("aw", "AEW", "red", "rb", AIRCRAFT["a50"], 2, "Overlord")
    awacs = flight(state, "w", "aw", Task.AWACS, Vec2(0, 0), status=FlightStatus.ON_STATION)
    # 230 km out: beyond the A-50's radar against a pod-jammer (300 x 0.7 = 210 km),
    # within its ELINT ears (250 km)
    sead = flight(state, "s", "vq", Task.SEAD, Vec2(230_000, 0))
    assert awacs.aircraft_type.air_radar_m * sensors.SELF_JAM < 230_000 < intel.ELINT_AWACS_M
    intel.scan(state, "red")
    c = intel.knows(state, "red", "s")
    assert c is not None and not c.identified and c.source.startswith("jamming strobe")
    assert c.position.distance_to(sead.pos) == pytest.approx(intel.JAM_FIX_ERROR_M, rel=1e-6)
