from collections import Counter

import pytest

from dynacamp import scenario
from dynacamp.engine import Engine
from dynacamp.forces import Order, OrderKind
from dynacamp.geo import Vec2

from .conftest import DEMO


def test_shortest_path_respects_passable(line):
    t = line.theater
    assert t.shortest_path("a", "e") == ["b", "c", "d", "e"]
    friendly = lambda pid: line.friendly_province("blue", pid)  # noqa: E731
    # The goal may be hostile (an attack), intermediate provinces may not.
    assert t.shortest_path("a", "d", passable=friendly) == ["b", "c", "d"]
    assert t.shortest_path("a", "e", passable=friendly) is None


def test_move_order_follows_roads_and_arrives(line):
    div = line.divisions["b1"]
    div.location = "a"
    line.timers["ai"] = float("inf")
    assert line.set_order(div, Order(OrderKind.MOVE, "c"))
    Engine(line).run_for(3 * 3600)
    assert div.location == "c" and div.movement is None
    assert div.order.kind is OrderKind.HOLD


def test_move_order_refuses_hostile_target(line):
    assert not line.set_order(line.divisions["b1"], Order(OrderKind.MOVE, "d"))


def test_attack_wins_and_captures(line):
    line.divisions["r1"].org = 0.3  # weak defender
    engine = Engine(line)
    engine.run_for(2 * 86400)
    kinds = Counter(e.kind for e in line.events)
    assert kinds["battle_started"] >= 1
    assert line.theater.provinces["d"].controller == "blue"


def test_position_interpolates_on_link(line):
    div = line.divisions["b1"]
    line.set_order(div, Order(OrderKind.MOVE, "b"))
    div.movement.progress = line.theater.link("c", "b").length / 2
    assert div.position(line.theater) == Vec2(30_000.0, 0.0)


def test_demo_campaign_runs_and_is_deterministic():
    def run():
        state = scenario.load(DEMO)
        Engine(state).run_for(4 * 86400, dt=300)
        return [(e.kind, e.data) for e in state.events], state

    a, state = run()
    b, _ = run()
    assert a == b
    kinds = Counter(k for k, _ in a)
    assert kinds["battle_started"] > 0 and kinds["convoy_delivered"] > 0
    # No runaway event spam (regression: blocked units re-trying every substep,
    # thousands of "order_cancelled"). Air and artillery events scale with the
    # size of the orders of battle, so they are bounded by rate instead.
    assert kinds["order_cancelled"] < 100
    busy = {"artillery_fire", "flight_takeoff", "flight_complete", "flight_landed", "air_engagement",
            "package_planned", "package_complete", "flight_aborted", "air_strike", "aircraft_lost", "sam_engagement"}
    assert max(v for k, v in kinds.items() if k not in busy) < 500
    assert kinds["artillery_fire"] < 30 * 24 * 4
    assert kinds["flight_takeoff"] < 400 * 4  # sortie budget bounds it
    # Supply is never negative and never above capacity.
    for d in state.divisions.values():
        assert 0 <= d.supply <= d.template.supply_capacity + 1e-6


def test_convoy_supplies_far_division(line):
    # Put the blue division out of direct depot range: a->c is 50 km by road.
    div = line.divisions["b1"]
    div.supply = 10
    line.divisions.pop("r1")  # no war distractions
    engine = Engine(line)
    engine.run_for(6 * 3600)
    kinds = Counter(e.kind for e in line.events)
    assert kinds["convoy_dispatched"] >= 1 and kinds["convoy_delivered"] >= 1
    assert div.supply > 20


def test_snapshot_is_plain_data(demo):
    import json

    json.dumps(demo.snapshot())


@pytest.mark.parametrize("path", sorted((DEMO.parent).glob("*.toml")), ids=lambda p: p.stem)
def test_every_scenario_loads_and_runs(path):
    state = scenario.load(path)
    assert all(state.theater.neighbors(pid) for pid in state.theater.provinces), "isolated province"
    Engine(state).run_for(2 * 86400, dt=600)
    assert any(e.kind == "battle_started" for e in state.events)


def test_ai_garrisons_an_open_flank():
    """A red brigade can walk around the blue front through an empty corridor to
    blue's depot: blue must plug the corridor instead of stacking the front."""
    data = {
        "campaign": {"name": "flank", "seed": 1},
        "factions": [
            {"id": "blue", "coalition": "BLUE", "army": "us", "color": "#4c8dff", "enemies": ["red"]},
            {"id": "red", "coalition": "RED", "army": "russia", "color": "#ff5a5a", "enemies": ["blue"]},
        ],
        "provinces": [
            {"id": "depot", "pos": [0, 0], "controller": "blue", "victory_points": 5,
             "depot": {"stock": 5000, "capacity": 5000, "production_per_hour": 50, "trucks": 50}},
            {"id": "front", "pos": [20_000, 0], "controller": "blue"},
            {"id": "flank", "pos": [20_000, 30_000], "controller": "blue"},
            {"id": "enemy", "pos": [40_000, 0], "controller": "red"},
            {"id": "desert", "pos": [40_000, 30_000], "controller": "red"},
        ],
        "links": [
            {"a": "depot", "b": "front"}, {"a": "front", "b": "enemy"},
            {"a": "depot", "b": "flank"}, {"a": "flank", "b": "desert"}, {"a": "desert", "b": "enemy"},
        ],
        "divisions": [
            {"id": "b1", "faction": "blue", "template": "mech_brigade", "province": "front"},
            {"id": "b2", "faction": "blue", "template": "mech_brigade", "province": "front"},
            {"id": "r1", "faction": "red", "template": "tank_brigade", "province": "desert"},
        ],
    }
    state = scenario.from_dict(data)
    from dynacamp.systems import ai

    ai.plan(state, state.factions["blue"])
    orders = {d.id: (d.order.kind, d.order.target) for d in state.divisions.values() if d.faction == "blue"}
    assert (OrderKind.MOVE, "flank") in orders.values()


def _attack_until_battle(state, attacker="b1", target="d", hours=4):
    engine = Engine(state)
    state.timers["ai"] = float("inf")
    div = state.divisions[attacker]
    state.set_order(div, Order(OrderKind.ATTACK, target))
    for _ in range(hours * 60):
        engine.step(60)
        if div.battle:
            break
    return engine, div


def test_battle_opens_only_in_contact(line):
    from dynacamp.systems import contact

    engine, b1 = _attack_until_battle(line)
    r1 = line.divisions["r1"]
    t = line.theater
    assert b1.battle is not None
    # The defender stands on its forward line, facing the attack axis...
    assert r1.deployed_toward == "c"
    assert r1.position(t) == t.point_on_link("d", "c", contact.deploy_progress(line, "d", "c"))
    # ...and the attacker is in direct-fire contact with it, not 20 km away at home.
    assert b1.position(t).distance_to(r1.position(t)) == pytest.approx(contact.DIRECT_CONTACT_M, abs=contact.SEARCH_STEP_M)


def test_artillery_engages_from_depth(line):
    from dynacamp.forces import DEFAULT_TEMPLATES, Division
    from dynacamp.systems import contact

    line.divisions["b1"] = Division(id="b1", name="Guns", faction="blue", location="c", template=DEFAULT_TEMPLATES["artillery_group"])
    engine, guns = _attack_until_battle(line)
    gap = guns.position(line.theater).distance_to(line.divisions["r1"].position(line.theater))
    assert gap == pytest.approx(contact.INDIRECT_CONTACT_M, abs=contact.SEARCH_STEP_M)


def test_failed_attack_withdraws_along_the_road(line):
    from dynacamp.systems import combat

    engine, b1 = _attack_until_battle(line)
    t = line.theater
    start = b1.position(t)
    combat.withdraw(line, b1)  # what happens when the attack is called off
    b1.battle = None
    positions = [start]
    for _ in range(120):
        engine.step(60)
        positions.append(b1.position(t))
        if b1.movement is None:
            break
    assert b1.movement is None and b1.location == "c"
    steps = [p.distance_to(q) for p, q in zip(positions, positions[1:])]
    assert max(steps) < 1_000  # no teleport: at most ~35 km/h * 60 s per step


def test_units_move_on_roads(tmp_path):
    """With road geometry a unit's position is always on the road polyline."""
    from dynacamp.world import project_on, polyline_length  # noqa: F401

    state = scenario.load(DEMO)
    link = state.theater.link("poti", "kobuleti")
    assert link.route is not None and len(link.route) > 5
    road = state.theater.link_points("poti", "kobuleti")
    for s in (0.0, link.length * 0.3, link.length * 0.77, link.length):
        p = state.theater.point_on_link("poti", "kobuleti", s)
        nearest = min(q.distance_to(p) for q in road) if p in road else None
        on_road = state.theater.point_on_link("poti", "kobuleti", state.theater.project_on_link("poti", "kobuleti", p))
        assert on_road.distance_to(p) < 1.0
        assert nearest is None or nearest == 0
    # road length is the real one, longer than the straight line
    a, b = state.theater.provinces["poti"].center, state.theater.provinces["kobuleti"].center
    assert link.length > a.distance_to(b)


def test_destroyed_brigade_is_reformed(line):
    from dynacamp.systems import supply

    div = line.divisions["r1"]
    div.elements = {k: 0.0 for k in div.elements}
    engine = Engine(line)
    line.timers["ai"] = float("inf")
    engine.step(60)
    assert "r1" not in line.divisions
    engine.run_for(supply.REFORM_DELAY_S + 3600, dt=600)
    reformed = [d for d in line.divisions.values() if d.faction == "red"]
    assert reformed and "(reformed)" in reformed[0].name
    assert reformed[0].location == "e"  # red's only depot


def test_replacements_restore_strength(line):
    div = line.divisions["b1"]
    div.location = "a"  # at the depot
    div.take_losses(0.4)
    line.divisions.pop("r1")
    line.timers["ai"] = float("inf")
    before = div.strength
    Engine(line).run_for(24 * 3600, dt=600)
    assert div.strength > before + 0.15


def test_realtime_loop_ticks_at_a_fixed_rate_and_advances_compressed_time(line):
    import time

    from dynacamp.runtime import run_realtime

    engine = Engine(line)
    start, t0 = time.monotonic(), line.time
    ticks = []
    run_realtime(engine, compression=600, tick_s=0.05, stop=lambda: time.monotonic() - start > 0.5,
                 on_tick=lambda e: ticks.append(time.monotonic()))
    assert 6 <= len(ticks) <= 11  # ~10 ticks of 50 ms in 0.5 s: it sleeps, it does not spin
    assert 200 <= line.time - t0 <= 400  # ~0.5 s * 600
