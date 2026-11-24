"""Planner orders on the ATO: new packages, routes, TOT, cancellation."""

import pytest

from dynacamp.adapters.base import Materialize, UpdateRoute
from dynacamp.adapters.dcs.protocol import DcsTranslator
from dynacamp.air import FlightStatus, SeatState, Task
from dynacamp.engine import Engine
from dynacamp.geo import Vec2
from dynacamp.systems import ato, intel, orders
from dynacamp.systems.orders import OrderError, Target

from .test_air import air_line, first_flight, live_engine


def planned():
    """The line scenario after the first ATO cycle."""
    state = air_line()
    Engine(state).step(60)
    return state


def with_ip(f):
    """A route like the flight's, with an extra turn point before the target."""
    wps = list(f.waypoints)
    extra = wps[f.target_index - 1].lerp(wps[f.target_index], 0.5) + Vec2(0, 5_000)
    wps.insert(f.target_index, extra)
    return wps, f.target_index + 1


# ---------------------------------------------------------------- route


def test_route_change_on_the_ground_moves_the_takeoff():
    state = planned()
    f = first_flight(state, "blue")
    tot, takeoff = f.tot, f.takeoff_time
    wps, ti = with_ip(f)
    orders.set_route(state, f.id, wps, ti, "blue")
    assert f.waypoints == wps and f.target_index == ti
    assert f.tot == tot and f.takeoff_time < takeoff  # longer way in: leave earlier, same TOT
    assert list(state.events)[-1].kind == "flight_replanned"
    assert f.id in state.dirty


def test_route_rules():
    state = planned()
    f = first_flight(state, "blue")
    wps, ti = list(f.waypoints), f.target_index
    with pytest.raises(OrderError, match="no flight"):
        orders.set_route(state, f.id, wps, ti, "red")  # someone else's flight
    moved_home = [wps[0] + Vec2(5_000, 0), *wps[1:]]
    with pytest.raises(OrderError, match="already been flown"):
        orders.set_route(state, f.id, moved_home, ti)
    with pytest.raises(OrderError, match="landing"):
        orders.set_route(state, f.id, [*wps[:-1], wps[-1] + Vec2(0, 30_000)], ti)
    if not f.task.orbits:
        moved_target = list(wps)
        moved_target[ti] = wps[ti] + Vec2(10_000, 0)
        with pytest.raises(OrderError, match="cannot move"):
            orders.set_route(state, f.id, moved_target, ti)
    far = [wps[0], *wps[1:-1], Vec2(0, 2_000_000), wps[-1]]
    with pytest.raises(OrderError, match="too long"):
        orders.set_route(state, f.id, far, ti)
    with pytest.raises(OrderError, match="3 to"):
        orders.set_route(state, f.id, [wps[0], wps[-1]], 1)
    assert f.waypoints == wps  # nothing changed


def test_cap_station_can_move_and_the_package_follows():
    state = planned()
    cap = next((f for f in state.flights.values() if f.faction == "blue" and f.task is Task.CAP), None)
    if cap is None:
        pytest.skip("no blue CAP planned in this scenario")
    wps = list(cap.waypoints)
    wps[cap.target_index] = wps[cap.target_index] + Vec2(0, -8_000)
    orders.set_route(state, cap.id, wps, cap.target_index)
    assert cap.target_point == wps[cap.target_index]
    pkg = state.packages[cap.package]
    if pkg.flights[0] == cap.id:
        assert pkg.target_point == cap.target_point


def test_airborne_flight_keeps_what_it_flew_and_flies_the_new_route():
    state = planned()
    engine = Engine(state)
    f = first_flight(state, "blue")
    engine.run_for(f.takeoff_time - state.time + 120)
    assert f.status is FlightStatus.AIRBORNE and f.wp_index >= 1
    wps, ti = with_ip(f)
    orders.set_route(state, f.id, wps, ti)
    assert f.wp_index <= ti
    engine.run_for(4 * 3600)
    assert f.attacked or f.id not in state.flights


def test_route_change_reaches_the_game_and_the_players():
    state, game, engine = live_engine()
    f = first_flight(state, "blue")
    game.join("Pilot", f.id)
    engine.step(1)
    engine.run_for(f.takeoff_time - state.time + 60)
    engine.step(1)
    engine.step(1)  # spawned and acknowledged: the game holds the AI part
    assert any(isinstance(c, Materialize) and c.entity.id == f.id for c in game.received)
    wps, ti = with_ip(f)
    orders.set_route(state, f.id, wps, ti)
    assert engine.brief_players([f.id]) == 1
    engine.step(1)
    routes = [c for c in game.received if isinstance(c, UpdateRoute) and c.entity_id == f.id]
    assert routes and routes[-1].snapshot is not None and routes[-1].snapshot.kind == "flight"
    note = game.messages_for("Pilot")[-1]
    assert note.text.startswith("FLIGHT PLAN CHANGED") and note.marks

    # the DCS bridge gets an air route with the attack at the target point
    msg = DcsTranslator().encode(routes[-1])
    assert msg["cmd"] == "route"
    alts = {p["alt"] for p in msg["points"]}
    assert alts == {f.aircraft_type.cruise_alt_m}
    target = msg["points"][1 + routes[-1].snapshot.attrs["target_index"]]
    assert target["task"]["params"]["tasks"], "no task at the target waypoint"


# ---------------------------------------------------------------- TOT and cancel


def test_tot_moves_the_whole_package():
    state = planned()
    pkg = next(p for p in state.packages.values() if p.faction == "blue")
    flights = [state.flights[i] for i in pkg.flights]
    before = {f.id: (f.tot, f.takeoff_time) for f in flights}
    with pytest.raises(OrderError, match="earliest TOT"):
        orders.set_tot(state, pkg.id, state.time + 60)
    orders.set_tot(state, pkg.id, pkg.tot + 1800)
    for f in flights:
        assert f.tot == before[f.id][0] + 1800 and f.takeoff_time == before[f.id][1] + 1800
    assert orders.earliest_tot(state, pkg) <= pkg.tot


def test_tot_can_always_move_later_even_inside_the_startup_lead():
    state = planned()
    engine = Engine(state)
    f = first_flight(state, "blue")
    pkg = state.packages[f.package]
    engine.run_for(min(x.takeoff_time for x in (state.flights[i] for i in pkg.flights)) - state.time - 300)
    assert orders.earliest_tot(state, pkg) == pkg.tot  # inside the lead: no earlier...
    orders.set_tot(state, pkg.id, pkg.tot + 300)  # ...but later is fine
    with pytest.raises(OrderError, match="earliest TOT"):
        orders.set_tot(state, pkg.id, pkg.tot - 60)


def test_tot_cannot_move_once_airborne():
    state = planned()
    engine = Engine(state)
    f = first_flight(state, "blue")
    engine.run_for(f.takeoff_time - state.time + 60)
    with pytest.raises(OrderError, match="already took off"):
        orders.set_tot(state, f.package, state.packages[f.package].tot + 3600)


def test_cancel_returns_aircraft_and_frees_seats():
    state, game, engine = live_engine()
    f = first_flight(state, "blue")
    game.join("Pilot", f.id)
    engine.step(1)
    sq = state.squadrons[f.squadron]
    committed = sq.committed
    cancelled = orders.cancel_package(state, f.package)
    assert f.callsign in cancelled and f.status is FlightStatus.CANCELLED
    assert sq.committed == committed - sum(
        state.flights[i].size for i in state.packages[f.package].flights if state.flights[i].squadron == sq.id
    )
    assert f.players["Pilot"].state is SeatState.LEFT
    with pytest.raises(OrderError, match="no flights on the ground"):
        orders.cancel_package(state, f.package)


# ---------------------------------------------------------------- new packages


def test_order_package_against_a_known_brigade():
    state = air_line()
    Engine(state).step(1)
    with pytest.raises(OrderError, match="intelligence picture"):
        orders.order_package(state, "blue", Task.BAI, Target("division", "nope"))
    c = intel.knows(state, "blue", "r1")
    if c is None:
        intel.observe(state, "blue", state.divisions["r1"], "division", "test")
    pkg = orders.order_package(state, "blue", Task.BAI, Target("division", "r1"), squadron="hawg")
    main = state.flights[pkg.flights[0]]
    assert main.squadron == "hawg" and main.target_id == "r1" and main.task is Task.BAI
    assert pkg.tot >= state.time + ato.STARTUP_LEAD_S
    assert {"package_planned", "package_ordered"} <= {e.kind for e in state.events}


def test_order_package_checks_task_target_squadron_and_tot():
    state = air_line()
    Engine(state).step(1)
    with pytest.raises(OrderError, match="flown against"):
        orders.order_package(state, "blue", Task.CAS, Target("depot", "e"))
    with pytest.raises(OrderError, match="does not fly"):
        orders.order_package(state, "blue", Task.CAP, Target("point", point=Vec2(30_000, 0)), squadron="hawg")
    with pytest.raises(OrderError, match="earliest TOT"):
        orders.order_package(state, "blue", Task.STRIKE, Target("depot", "e"), tot=state.time + 600)
    pkg = orders.order_package(state, "blue", Task.CAP, Target("point", point=Vec2(30_000, -5_000)))
    assert state.flights[pkg.flights[0]].target_point == Vec2(30_000, -5_000)


# ---------------------------------------------------------------- API


@pytest.fixture
def api():
    from fastapi.testclient import TestClient

    from dynacamp.api import create_app

    state, game, engine = live_engine()
    with TestClient(create_app(engine, token="t")) as c:
        yield c, state, engine


def test_api_orders(api):
    c, state, engine = api
    auth = {"Authorization": "Bearer t"}
    f = first_flight(state, "blue")
    wps, ti = with_ip(f)
    body = {"waypoints": [{"pos": [p.x, p.y]} for p in wps], "target_index": ti, "faction": "blue"}
    assert c.post(f"/api/ato/flights/{f.id}/route", json=body).status_code == 401
    r = c.post(f"/api/ato/flights/{f.id}/route", json=body, headers=auth)
    assert r.status_code == 200, r.text
    assert len(r.json()["waypoints"]["pos"]) == len(wps) and r.json()["locked_waypoints"] == 1
    bad = {**body, "waypoints": body["waypoints"][:2] + body["waypoints"][-1:], "target_index": 1}
    bad["waypoints"][0] = {"pos": [1, 1]}
    assert c.post(f"/api/ato/flights/{f.id}/route", json=bad, headers=auth).status_code == 409
    assert c.post("/api/ato/flights/nope/route", json=body, headers=auth).status_code == 404

    pkg = state.packages[f.package]
    r = c.post(f"/api/ato/packages/{pkg.id}/tot", json={"tot": pkg.tot + 900}, headers=auth)
    assert r.status_code == 200 and r.json()["tot"]["t"] == pkg.tot
    assert c.post(f"/api/ato/packages/{pkg.id}/tot", json={"tot": 0}, headers=auth).status_code == 409

    order = {"faction": "blue", "task": "CAP", "target": {"kind": "point", "point": {"pos": [30_000, -5_000]}}}
    r = c.post("/api/ato/packages", json=order, headers=auth)
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["task"] == "CAP" and new["flights"] and new["earliest_tot"]["t"] <= new["tot"]["t"]
    r = c.post(f"/api/ato/packages/{new['id']}/cancel", json={}, headers=auth)
    assert r.status_code == 200 and r.json()["cancelled"]
    order["faction"] = "nobody"
    assert c.post("/api/ato/packages", json=order, headers=auth).status_code == 404
