"""Fog of war and support aircraft: AWACS, tankers, transports."""

import math

from dynacamp.air import AIRCRAFT, Airbase, Flight, FlightStatus, Package, Squadron, TargetKind, Task
from dynacamp.air_defense import SYSTEMS, AirDefenseUnit, SamStatus
from dynacamp.engine import Engine
from dynacamp.geo import Vec2
from dynacamp.systems import air, ato, intel

from .conftest import line_scenario


def world():
    """The line scenario with nothing planned automatically."""
    state = line_scenario()
    state.timers["ai"] = state.timers["ato"] = state.timers["ada"] = float("inf")
    state.airbases["ab"] = Airbase("ab", "Blue Field", Vec2(0, -10_000), province="a")
    state.airbases["rb"] = Airbase("rb", "Red Field", Vec2(80_000, 10_000), province="e")
    return state


def add_flight(state, fid, faction, ac, task, pos, size=2, status=FlightStatus.AIRBORNE, target=None):
    """Put a flight in the air at ``pos``."""
    sq_id = f"sq_{fid}"
    base = "ab" if faction == "blue" else "rb"
    state.squadrons[sq_id] = Squadron(sq_id, sq_id, faction, base, AIRCRAFT[ac], 12, fid)
    target = target or pos
    pkg = Package(f"pk_{fid}", faction, task, "t", target, 0)
    state.packages[pkg.id] = pkg
    f = Flight(id=fid, faction=faction, location="a", package=pkg.id, squadron=sq_id, aircraft_type=AIRCRAFT[ac],
               task=task, callsign=f"{fid} 1", size=size, takeoff_time=state.time, tot=state.time,
               waypoints=[state.airbases[base].position, target, state.airbases[base].position], target_index=1,
               pos=pos, status=status)
    if task.orbits:
        f.station_until = 99_999.0
    state.squadrons[sq_id].committed += size
    state.flights[fid] = f
    pkg.flights.append(fid)
    return f


def test_prewar_picture_knows_brigades_and_sites_but_not_aircraft():
    state = world()
    state.air_defense["s1"] = AirDefenseUnit(id="s1", name="SAM", faction="red", location="e", system=SYSTEMS["buk"])
    Engine(state).step(1)
    blue = intel.picture(state, "blue")
    assert {"r1", "s1"} <= set(blue) and blue["r1"].source == "pre-war intelligence"
    assert "b1" not in blue  # own forces are not contacts


def test_awacs_sees_far_but_does_not_identify_and_misses_low_flyers():
    state = world()
    add_flight(state, "awacs", "blue", "e3a", Task.AWACS, Vec2(0, -150_000), size=1, status=FlightStatus.ON_STATION)
    add_flight(state, "bandit", "red", "su27", Task.CAP, Vec2(80_000, 100_000))  # ~270 km away, high
    add_flight(state, "hind", "red", "mi24p", Task.CAS, Vec2(80_000, 50_000))  # helicopter, low
    state.flights["hind"].pos = Vec2(0, 150_000)  # 300 km: beyond 0.6 * 400 km for low flyers
    Engine(state).step(1)
    c = intel.knows(state, "blue", "bandit")
    assert c is not None and not c.identified and c.source.startswith("AWACS")
    assert "aircraft" not in c.detail  # an unidentified track: no type
    assert intel.knows(state, "blue", "hind") is None


def test_fighter_identifies_what_it_tracks_up_close():
    state = world()
    add_flight(state, "viper", "blue", "f16c", Task.CAP, Vec2(0, 0))
    add_flight(state, "bandit", "red", "su27", Task.CAP, Vec2(30_000, 0))
    state.flights["viper"].track = math.pi / 2  # radar cone facing east, at the bandit
    Engine(state).step(1)
    c = intel.knows(state, "blue", "bandit")
    assert c is not None and c.identified and c.detail["aircraft"] == "su27"


def test_an_aircraft_radar_only_looks_ahead():
    state = world()
    add_flight(state, "viper", "blue", "f16c", Task.CAP, Vec2(0, 0))
    add_flight(state, "bandit", "red", "su27", Task.CAP, Vec2(30_000, 0))
    state.flights["viper"].track = 3 * math.pi / 2  # facing west: the bandit is behind
    state.timers["intel"] = 0.0
    from dynacamp.systems import intel as intel_sys

    intel_sys.scan(state, "blue")
    assert intel.knows(state, "blue", "bandit") is None


def test_contacts_age_and_are_forgotten():
    state = world()
    eng = Engine(state)
    add_flight(state, "viper", "blue", "f16c", Task.CAP, Vec2(0, 0))
    add_flight(state, "bandit", "red", "su27", Task.CAP, Vec2(30_000, 0))
    state.flights["viper"].track = math.pi / 2
    eng.step(1)
    state.flights["viper"].status = FlightStatus.DONE  # the only sensor goes home
    state.flights["bandit"].pos = Vec2(30_000, 400_000)
    state.flights["bandit"].status = FlightStatus.ON_STATION
    c = intel.knows(state, "blue", "bandit")
    seen_at = Vec2(c.position.x, c.position.y)
    eng.run_for(120, dt=60)
    c = intel.knows(state, "blue", "bandit")
    assert c is not None and c.stale(state.time) and c.position == seen_at  # last known position
    eng.run_for(intel.TTL_S["flight"], dt=60)
    assert intel.knows(state, "blue", "bandit") is None


def test_elint_locates_an_emitting_radar_the_planners_did_not_know():
    state = world()
    eng = Engine(state)
    eng.step(1)  # pre-war picture taken before the site deploys
    u = AirDefenseUnit(id="s9", name="Pop-up", faction="red", location="e", system=SYSTEMS["s300ps"])
    state.air_defense["s9"] = u
    target = state.theater.provinces["e"].center
    assert not intel.known_covering(state, "blue", target)
    assert not ato.needs_sead(state, "blue", target)  # the planners walk into it...
    u.status = SamStatus.ACTIVE
    add_flight(state, "awacs", "blue", "e3a", Task.AWACS, Vec2(0, -150_000), size=1, status=FlightStatus.ON_STATION)
    eng.run_for(120, dt=60)
    c = intel.knows(state, "blue", "s9")
    assert c is not None and "ELINT" in c.source
    assert ato.needs_sead(state, "blue", target)  # ...until the radar gives itself away


def test_bai_is_planned_only_on_located_brigades():
    state = world()
    state.squadrons["hogs"] = Squadron("hogs", "Hogs", "blue", "ab", AIRCRAFT["a10c"], 12, "Hog")
    blue = state.factions["blue"]
    assert not [r for r in ato.requests(state, blue) if r.target_id == "r1"]
    intel.seed_prewar(state)
    assert [r for r in ato.requests(state, blue) if r.target_id == "r1" and r.task is Task.BAI]


def test_artillery_needs_a_located_target():
    from dynacamp.systems import artillery

    state = world()
    assert artillery.guns(state.divisions["b1"]) >= artillery.MIN_GUNS  # the tank brigade has guns
    state.rng.seed(1)
    artillery.run(state, artillery.FIRE_INTERVAL_S)
    assert not state.fires  # nobody has located r1
    intel.seed_prewar(state)
    artillery.run(state, artillery.FIRE_INTERVAL_S)
    assert state.fires


def test_awacs_and_tankers_are_requested_and_a_tanker_stretches_the_radius():
    state = world()
    state.squadrons["awx"] = Squadron("awx", "AEW", "blue", "ab", AIRCRAFT["e3a"], 2, "Darkstar")
    state.squadrons["tkr"] = Squadron("tkr", "Tankers", "blue", "ab", AIRCRAFT["kc135"], 2, "Texaco")
    tasks = {r.task for r in ato.requests(state, state.factions["blue"])}
    assert {Task.AWACS, Task.TANKER} <= tasks
    state.squadrons["hogs"] = Squadron("hogs", "Hogs", "blue", "ab", AIRCRAFT["a10c"], 12, "Hog")
    far = Vec2(0, AIRCRAFT["a10c"].radius_km * 1000 * 1.2)
    assert ato._allocate(state, "blue", Task.BAI, far, 2) is None
    add_flight(state, "tk", "blue", "kc135", Task.TANKER, Vec2(0, -50_000), size=1, status=FlightStatus.ON_STATION)
    assert air.tanker_on_station(state, "blue") is not None
    assert ato._allocate(state, "blue", Task.BAI, far, 2) is not None


def test_airlift_fills_a_forward_depot():
    state = world()
    depot = state.theater.provinces["a"].depot
    depot.stock = 1000.0
    state.airbases["rear"] = Airbase("rear", "Rear", Vec2(-200_000, 0), province=None)
    add_flight(state, "herky", "blue", "c130", Task.TRANSPORT, Vec2(0, -10_000), target=Vec2(0, -10_000))
    f = state.flights["herky"]
    f.target_kind, f.target_id, f.cargo = TargetKind.AIRLIFT, "ab", 300.0
    air.attack(state, f)
    assert depot.stock == 1300.0 and f.cargo == 0.0
    ev = [e for e in state.events if e.kind == "airlift_delivered"]
    assert ev and ev[0].data["amount"] == 300.0


def test_faction_events_hide_enemy_internals():
    from dynacamp.api import views

    state = world()
    state.emit("ai_order", faction="red", division="r1", order="attack", target="c")
    state.emit("battle_started", province="c")
    blue = [e["kind"] for e in views.events(state, viewer="blue")["events"]]
    assert "battle_started" in blue and "ai_order" not in blue
    assert "ai_order" in [e["kind"] for e in views.events(state, viewer="red")["events"]]


def test_a_sam_launch_reveals_the_site():
    state = world()
    u = AirDefenseUnit(id="s1", name="Ambush", faction="red", location="d", system=SYSTEMS["buk"])
    state.air_defense["s1"] = u
    state.timers["intel_seeded"] = 0.0  # no pre-war picture: the site is unknown
    add_flight(state, "viper", "blue", "f16c", Task.BAI, Vec2(60_000, 10_000), target=Vec2(80_000, 0))
    state.rng.seed(3)
    Engine(state).step(1)
    assert [e for e in state.events if e.kind == "sam_engagement"]
    c = intel.knows(state, "blue", "s1")
    assert c is not None and c.source.startswith("launch on")
