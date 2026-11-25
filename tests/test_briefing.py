"""Mission packages: briefing, waypoints, threats and ordnance."""

from dynacamp import loadouts
from dynacamp.air import AIRCRAFT, Task
from dynacamp.air_defense import SYSTEMS, AirDefenseUnit
from dynacamp.engine import Engine
from dynacamp.systems import briefing

from .test_air import air_line


def planned(state, task=None):
    Engine(state).step(60)
    return next(f for f in state.flights.values() if f.faction == "blue" and (task is None or f.task is task))


def test_every_aircraft_task_has_ordnance_except_template_only_ones():
    missing = [(a.id, t.value) for a in AIRCRAFT.values() for t in a.tasks if not t.support and not loadouts.stores(a.id, t.value)]
    assert set(missing) <= {("sa342", "CAS"), ("sa342", "BAI")}  # Gazelle: HOT set in the ME template
    names = " ".join(s["name"] for s in loadouts.stores("f16c", "SEAD"))
    assert "AGM-88" in names
    assert any("Phoenix" in s["name"] or "AIM-54" in s["name"] for s in loadouts.stores("f14b", "CAP"))


def test_waypoints_are_named_and_timed_back_from_tot():
    state = air_line()
    f = planned(state, Task.BAI)
    plan = briefing.waypoint_plan(state, f)
    names = [w["name"] for w in plan]
    assert names[0] == "TAKEOFF" and names[-1] == "LAND"
    assert "TARGET" in names and "IP" in names and "EGRESS" in names
    tgt = next(w for w in plan if w["name"] == "TARGET")
    assert tgt["eta"]["t"] == round(f.tot, 1)
    etas = [w["eta"]["t"] for w in plan]
    assert etas == sorted(etas)


def test_briefing_lists_route_threats_and_ordnance():
    state = air_line()
    state.theater.name = "caucasus"  # give the test map a projection for coordinates
    state.air_defense["s1"] = AirDefenseUnit(id="s1", name="SAM", faction="red", location="e", system=SYSTEMS["s300ps"])
    Engine(state).step(60)
    # an attack flight (CAPs are deliberately kept outside SAM coverage)
    f = next(f for f in state.flights.values() if f.faction == "blue" and f.task is not Task.CAP)
    b = briefing.briefing(state, f)
    assert any(t["kind"] == "sam" and t["name"] == SYSTEMS["s300ps"].name for t in b["threats"])
    assert b["loadout"] and b["comms"]["package"].startswith("UHF")
    assert b["target"]["coordinates"].startswith(("N", "S"))
    text = briefing.render(b, now=state.time)
    assert f.callsign in text and "ORDNANCE" in text and "WP0 TAKEOFF" in text


def test_briefing_endpoint():
    from fastapi.testclient import TestClient

    from dynacamp.api import create_app

    state = air_line()
    engine = Engine(state)
    f = planned(state)
    with TestClient(create_app(engine)) as c:
        r = c.get(f"/api/ato/flights/{f.id}/briefing")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["callsign"] == f.callsign and body["waypoints"] and body["text"]
        assert c.get("/api/ato/flights/nope/briefing").status_code == 404
        assert body["units"] == "imperial" and " kts" in body["text"]
        assert " km/h" in c.get(f"/api/ato/flights/{f.id}/briefing?units=metric").json()["text"]
        assert c.get(f"/api/ato/flights/{f.id}/briefing?units=furlongs").status_code == 422


def test_packages_have_unique_four_digit_numbers():
    from dynacamp.air import package_number

    numbers = {package_number(f"pk{i}") for i in range(1, 9001)}
    assert len(numbers) == 9000 and all(len(n) == 4 and n.isdigit() for n in numbers)
    state = air_line()
    f = planned(state)
    pkg = state.packages[f.package]
    assert pkg.number == package_number(pkg.id)
    b = briefing.briefing(state, f)
    assert b["package_number"] == pkg.number and b["title"].startswith(f"PKG {pkg.number}")


def test_briefing_units_imperial_by_default_or_metric():
    from dynacamp import units

    assert units.altitude(1500, "imperial") == "4900 ft" and units.speed(830, "imperial") == "450 kts"
    assert units.distance(64_820, "imperial") == "35 NM" and units.distance(64_820, "metric") == "65 km"
    state = air_line()
    f = planned(state, Task.BAI)
    imperial = briefing.render(briefing.briefing(state, f))
    assert " ft" in imperial and " kts" in imperial and "km/h" not in imperial and " NM from " in imperial
    metric = briefing.render(briefing.briefing(state, f, "metric"))
    assert " km/h" in metric and " kts" not in metric and " km from " in metric
    state.units = "metric"
    assert all(" m" in label or "TAKEOFF" in label or "LAND" in label for label, _ in briefing.waypoint_marks(state, f))


def test_ato_frequencies_are_clear_of_airfields_guard_and_each_other(demo):
    from dynacamp import radio

    reserved = radio.reserved(demo.theater.name)
    assert len(reserved) > 40 and 243.0 in reserved and 121.5 in reserved  # Caucasus ATC + guard
    engine = Engine(demo)
    checked = 0
    for _ in range(8):  # every 3 hours for a day: what is in the air at that moment
        engine.run_for(3 * 3600, dt=300)
        live = [p for p in demo.packages.values() if not p.done]
        flights = [f for f in demo.flights.values() if f.status.value not in ("done", "cancelled")]
        if not (live and flights):
            continue
        checked += 1
        sample = flights[0]
        used = [p.uhf_mhz for p in live] + [f.vhf_mhz for f in flights]
        assert all(m and radio.clear(m, reserved) for m in used)  # no airfield, no guard
        assert all(radio.clear(m, used[:i] + used[i + 1:]) for i, m in enumerate(used))  # no two the same
        assert all(radio.PACKAGE_BAND[0] <= p.uhf_mhz <= radio.PACKAGE_BAND[1] for p in live)
        b = briefing.briefing(demo, sample)  # the briefing gives the same frequencies
        assert f"{demo.packages[sample.package].uhf_mhz:.3f}" in b["comms"]["package"]
        assert f"{sample.vhf_mhz:.3f}" in b["comms"]["flight"]
    assert checked >= 4
