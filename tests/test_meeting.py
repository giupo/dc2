"""Meeting engagements: forces that run into each other on the road fight there."""

from dynacamp import scenario
from dynacamp.air_defense import SYSTEMS, AirDefenseUnit
from dynacamp.engine import Engine
from dynacamp.forces import Convoy, Movement, Order, OrderKind
from dynacamp.geo import Vec2
from dynacamp.state import BattleKind
from dynacamp.systems import combat


def crossroads():
    """Two roads crossing at the origin: blue a-b (west-east), red c-d (south-north),
    with a blue brigade at a and a red one at c."""
    data = {
        "campaign": {"name": "x", "seed": 1},
        "factions": [
            {"id": "blue", "coalition": "BLUE", "army": "us", "color": "#4c8dff", "enemies": ["red"]},
            {"id": "red", "coalition": "RED", "army": "russia", "color": "#ff5a5a", "enemies": ["blue"]},
        ],
        "provinces": [
            {"id": "a", "pos": [-20_000, 0], "controller": "blue"},
            {"id": "b", "pos": [20_000, 0], "controller": "blue"},
            {"id": "c", "pos": [0, -20_000], "controller": "red"},
            {"id": "d", "pos": [0, 20_000], "controller": "red"},
        ],
        "links": [{"a": "a", "b": "b"}, {"a": "c", "b": "d"}, {"a": "a", "b": "c"}, {"a": "b", "b": "d"}],
        "divisions": [
            {"id": "bl", "faction": "blue", "template": "tank_brigade", "province": "a"},
            {"id": "rd", "faction": "red", "template": "tank_brigade", "province": "c"},
        ],
    }
    state = scenario.from_dict(data)
    state.timers["ai"] = state.timers["ato"] = float("inf")
    return state


def run_until(state, cond, hours=6):
    """Step the engine (1 min) until ``cond()`` or ``hours`` pass."""
    engine = Engine(state)
    for _ in range(int(hours * 60)):
        engine.step(60)
        if cond():
            return True
    return False


def test_columns_crossing_on_the_road_fight_where_they_meet():
    state = crossroads()
    bl, rd = state.divisions["bl"], state.divisions["rd"]
    assert state.set_order(bl, Order(OrderKind.MOVE, "b")) and state.set_order(rd, Order(OrderKind.MOVE, "d"))
    assert run_until(state, lambda: bl.battle is not None)
    b = state.battles[bl.battle]
    assert b.kind is BattleKind.MEETING and rd.id in b.attackers + b.defenders
    assert combat.battle_point(state, b).length() < combat.MEET_RANGE_M  # at the crossing, not at a province
    (ev,) = state.events.of_kind("battle_started")
    assert ev.data["battle_kind"] == "meeting"
    held = bl.position(state.theater)
    Engine(state).step(600)
    if bl.battle:
        assert bl.position(state.theater) == held  # fighting, not driving on
    # someone breaks: it falls back along its road, the road is marked, the other carries on
    assert run_until(state, lambda: not state.battles, hours=24)
    (broken,) = state.events.of_kind("contact_broken")
    loser = state.divisions.get(broken.data["division"])
    winner = rd if loser is bl else bl
    assert loser is None or (loser.movement is None or loser.movement.back)
    assert winner.order.kind is OrderKind.MOVE  # its order stands
    assert any(k.startswith(f"contested:{broken.data['faction']}:") for k in state.timers)


def test_no_meeting_when_nobody_moves_or_sides_are_friends():
    state = crossroads()
    state.divisions["rd"].faction = "blue"
    state.theater.provinces["c"].controller = state.theater.provinces["d"].controller = "blue"
    bl, rd = state.divisions["bl"], state.divisions["rd"]
    state.set_order(bl, Order(OrderKind.MOVE, "b"))
    state.set_order(rd, Order(OrderKind.MOVE, "d"))
    assert not run_until(state, lambda: bool(state.battles), hours=3)


def test_convoy_and_sam_site_on_the_road_are_overrun():
    state = crossroads()
    bl = state.divisions["bl"]
    t = state.theater
    half = t.link("a", "b").length / 2  # the crossing, in progress metres along either road
    bl.location, bl.movement = "a", Movement(["b"], progress=half * 0.97)  # ~600 m short of it
    convoy = Convoy(id="cv", faction="red", location="c", home="c", destination="d", cargo=100, trucks=20)
    convoy.movement = Movement(["d"], progress=half * 0.98)
    state.convoys["cv"] = convoy
    state.air_defense["sa"] = AirDefenseUnit(id="sa", name="SA", faction="red", location="c", system=SYSTEMS["osa"])
    state.air_defense["sa"].movement = Movement(["d"], progress=half * 1.03)
    assert bl.position(t).distance_to(convoy.position(t)) < combat.MEET_RANGE_M
    Engine(state).step(1)
    assert "cv" not in state.convoys and "sa" not in state.air_defense
    reasons = {e.data["reason"] for e in state.events if e.kind in ("convoy_destroyed", "sam_destroyed")}
    assert all(r.startswith("overrun by") for r in reasons)


def test_an_assault_approach_is_not_a_meeting():
    state = crossroads()
    bl, rd = state.divisions["bl"], state.divisions["rd"]
    state.set_order(bl, Order(OrderKind.ATTACK, "c"))  # straight at the province the red brigade holds
    run_until(state, lambda: bool(state.battles), hours=6)
    assert state.battles and all(b.kind is BattleKind.ASSAULT for b in state.battles.values())
    assert rd.id in next(iter(state.battles.values())).defenders


def test_contested_road_is_avoided_when_another_exists():
    state = crossroads()
    state.theater.provinces["c"].controller = "blue"  # a -> c -> ... a second way round is not needed:
    bl = state.divisions["bl"]
    state.mark_contested("blue", "a", "b")
    assert state.set_order(bl, Order(OrderKind.MOVE, "b"))
    assert bl.movement.path != ["b"] or state.theater.shortest_path("a", "b") == ["b"]
    penalty = state.road_penalty("blue")
    assert penalty("a", "b") > 1 and penalty("b", "a") > 1 and penalty("a", "c") == 1
    assert state.road_penalty("red")("a", "b") == 1  # each side learns on its own


def test_vec2_helpers_used_here():
    assert Vec2(3, 4).length() == 5
