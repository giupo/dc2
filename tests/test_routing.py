"""Threat-aware routing, SEAD/DEAD packages, ECM screen and evasion."""

import pytest

from dynacamp.air import AIRCRAFT, Flight, FlightStatus, Squadron, Task
from dynacamp.air_defense import SYSTEMS
from dynacamp.engine import Engine
from dynacamp.geo import Vec2
from dynacamp.systems import air as air_sys
from dynacamp.systems import air_defense as ada
from dynacamp.systems import ato, intel, routing

from .test_air_defense import flight, world

UMBRELLA = routing.Threat("s", Vec2(50_000, 0), 20_000, False)


def test_leg_goes_around_an_umbrella_only_when_needed():
    a, b = Vec2(0, 0), Vec2(100_000, 0)
    path = routing.leg(a, b, [UMBRELLA])
    assert path, "the straight leg crosses the umbrella"
    full = [a, *path, b]
    assert not routing.exposure(full, [UMBRELLA])
    assert routing.length(full) <= a.distance_to(b) * routing.MAX_DETOUR
    # a clear leg is flown straight
    assert routing.leg(a, Vec2(100_000, 60_000), [UMBRELLA]) == []
    # a detour longer than allowed is not taken
    assert routing.leg(a, b, [UMBRELLA], max_detour=1.01) == []
    # an umbrella over an end of the leg cannot be avoided: it is exposure
    assert routing.leg(a, Vec2(55_000, 0), [UMBRELLA]) == []
    assert routing.exposure([a, Vec2(55_000, 0)], [UMBRELLA])["s"] == pytest.approx(25_000)


def test_screen_point_holds_short_of_the_first_umbrella():
    route = [Vec2(0, 0), Vec2(100_000, 0)]
    p = routing.screen_point(route, 1, [UMBRELLA])
    assert p is not None and abs(p.x - 25_000) < 1 and p.y == 0  # entry at 30 km, minus 5 km
    assert routing.screen_point(route, 1, []) is None


def test_routes_avoid_known_sams_and_ignore_unknown_ones():
    state = world(sam="buk", at="d")  # Buk at (60 km, 0)
    home, target = Vec2(0, -10_000), Vec2(120_000, 0)
    sq = state.squadrons["sq"]
    # unknown site: straight in
    assert ato.plan_route(state, "blue", Task.BAI, home, target, sq) == ato._key_points(home, target, Task.BAI)
    intel.reveal(state, "blue", state.air_defense["s1"], "test")
    waypoints, ti = ato.plan_route(state, "blue", Task.BAI, home, target, sq)
    assert waypoints[ti] == target
    umbrellas = routing.threats(state, "blue")
    assert routing.exposure(ato._key_points(home, target, Task.BAI)[0], umbrellas)
    assert not routing.exposure(waypoints, umbrellas)
    assert not ato.route_exposed(state, "blue", ato.Request(Task.BAI, 1, "t", target, "point", "x"))


def test_short_range_sams_count_only_if_they_reach_the_cruise_altitude():
    state = world(sam="tor", at="d")
    intel.reveal(state, "blue", state.air_defense["s1"], "test")
    assert routing.threats(state, "blue", 6_000)  # a Tor reaches a jet at cruise
    assert not routing.threats(state, "blue", 9_000)
    state = world(sam="osa", at="d")
    intel.reveal(state, "blue", state.air_defense["s1"], "test")
    assert not routing.threats(state, "blue", 6_000)
    assert routing.threats(state, "blue", 150)  # helicopters are always in reach


def test_cap_station_keeps_the_whole_orbit_out_of_the_umbrella():
    state = world(sam="buk", at="d")
    intel.reveal(state, "blue", state.air_defense["s1"], "test")
    site = state.air_defense["s1"].position(state.theater)
    station = ato.cap_station(state, "blue", state.theater.provinces["c"].center)
    assert station is not None
    clearance = site.distance_to(station) - air_sys.CAP_ORBIT_RADIUS_M
    assert clearance >= SYSTEMS["buk"].range_m * routing.ROUTE_MARGIN


def test_strikers_stay_home_if_the_route_is_exposed_and_nobody_flies_sead():
    state = world(sam="s300ps", at="e")
    state.squadrons["sq"] = Squadron("sq", "Hogs", "blue", "ab", AIRCRAFT["a10c"], 12, "Hawg")  # no SEAD
    intel.reveal(state, "blue", state.air_defense["s1"], "test")
    target = state.theater.provinces["d"].center
    r = ato.Request(Task.BAI, 5, "column", target, "point", "x")
    assert ato.route_exposed(state, "blue", r)
    assert ato.build_package(state, state.factions["blue"], r) is None
    # with a SEAD squadron the package goes, SEAD two minutes ahead
    state.squadrons["wv"] = Squadron("wv", "Weasels", "blue", "ab", AIRCRAFT["f16c"], 12, "Weasel")
    pkg = ato.build_package(state, state.factions["blue"], r)
    tasks = {state.flights[f].task: state.flights[f] for f in pkg.flights}
    assert set(tasks) == {Task.BAI, Task.SEAD}
    assert tasks[Task.SEAD].tot == tasks[Task.BAI].tot - 120


def test_dead_package_puts_strikers_behind_the_sead():
    state = world(sam="s300ps", at="e")
    state.squadrons["sq2"] = Squadron("sq2", "Vipers 2", "blue", "ab", AIRCRAFT["f16c"], 12, "Venom")
    site = state.air_defense["s1"]
    intel.reveal(state, "blue", site, "test")
    reqs = [r for r in ato.requests(state, state.factions["blue"]) if r.target_kind == "sam"]
    assert reqs and reqs[0].task is Task.DEAD
    pkg = ato.build_package(state, state.factions["blue"], reqs[0])
    assert pkg is not None and pkg.task is Task.DEAD
    flights = {state.flights[f].task: state.flights[f] for f in pkg.flights}
    assert set(flights) == {Task.SEAD, Task.STRIKE}
    assert flights[Task.STRIKE].target_kind == "sam" and flights[Task.STRIKE].target_id == site.id
    assert flights[Task.SEAD].tot == flights[Task.STRIKE].tot - 120
    assert ato.package_size(state, "blue", reqs[0]) == 4


def test_strikers_bomb_a_sam_site_even_with_its_radar_on():
    state = world(sam="buk", at="d")
    site = state.air_defense["s1"]
    f = flight(state, site.position(state.theater), task=Task.STRIKE, target=site.position(state.theater))
    f.target_kind, f.target_id = "sam", site.id
    state.rng.seed(1)
    air_sys.attack(state, f)
    assert [e for e in state.events if e.kind == "sam_struck"]
    assert not [e for e in state.events if e.kind == "harm_strike"]


def test_sead_ecm_screens_the_package_flights_near_it():
    state = world()
    f = flight(state, Vec2(50_000, 0))
    sead = Flight(id="fl2", faction="blue", location="a", package="pk", squadron="sq", aircraft_type=AIRCRAFT["f16c"],
                  task=Task.SEAD, callsign="Weasel 1", size=2, takeoff_time=0, tot=0,
                  waypoints=[Vec2(0, -10_000), Vec2(80_000, 0), Vec2(0, -10_000)], target_index=1,
                  pos=Vec2(60_000, 0), status=FlightStatus.AIRBORNE)
    state.flights["fl2"] = sead
    state.packages["pk"].flights.append("fl2")
    assert ada.ecm_screened(state, f)
    assert not ada.ecm_screened(state, sead)  # its own ECM is SEAD_PK_FACTOR
    sead.pos = Vec2(50_000 + ada.ECM_SCREEN_RANGE_M + 1_000, 0)
    assert not ada.ecm_screened(state, f)


def test_cap_moves_its_station_out_of_a_sam_that_fires_on_it():
    state = world(sam="buk", at="d")
    station = Vec2(60_000, 20_000)  # 20 km from the Buk: inside its 35 km
    f = flight(state, station, task=Task.CAP, size=4, target=station)
    f.status, f.wp_index, f.station_until = FlightStatus.ON_STATION, 1, 1e9
    state.rng.seed(5)
    Engine(state).step(1)
    assert [e for e in state.events if e.kind == "sam_engagement"]
    assert f.ai_count > 0  # seed 5: nobody shot down
    assert [e for e in state.events if e.kind == "flight_rerouted"]
    site = state.air_defense["s1"].position(state.theater)
    assert f.status is FlightStatus.AIRBORNE  # flying to the new station
    assert f.target_point.distance_to(site) >= SYSTEMS["buk"].range_m * routing.ROUTE_MARGIN + air_sys.CAP_ORBIT_RADIUS_M
