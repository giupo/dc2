"""Player bubble and handoff protocol, driven through the fake game."""

import pytest


from dynacamp.adapters.base import Dematerialize, Materialize, PlayerKind, UpdateRoute
from dynacamp.adapters.fake import FakeGame
from dynacamp.engine import Engine
from dynacamp.forces import Authority, Order, OrderKind
from dynacamp.geo import Vec2

AIR = PlayerKind.AIR  # 60 km radius, +10 km hysteresis


def setup(line, **kw):
    game = FakeGame(**kw)
    line.timers["ai"] = float("inf")  # keep the AI out of these tests
    return Engine(line, game), game


def commands(game, kind):
    return [c for c in game.received if isinstance(c, kind)]


def test_entity_inside_bubble_is_handed_to_game_and_back(line):
    engine, game = setup(line)
    game.player("p1", Vec2(40_000, 10_000), coalition="BLUE")  # 10 km from b1 (at c), 22 km from r1 (at d)
    engine.step(1)
    assert {c.entity.id for c in commands(game, Materialize)} == {"b1", "r1"}
    assert line.divisions["b1"].authority is Authority.MATERIALIZING
    engine.step(1)  # fake game acked
    assert line.divisions["b1"].authority is Authority.DELEGATED
    assert engine.live

    game.player_leaves("p1")
    engine.step(1)
    assert {c.entity_id for c in commands(game, Dematerialize)} == {"b1", "r1"}
    assert line.divisions["b1"].authority is Authority.DEMATERIALIZING
    engine.step(1)
    assert line.divisions["b1"].authority is Authority.CAMPAIGN
    assert not engine.live


def test_hysteresis_keeps_entity_near_the_edge(line):
    engine, game = setup(line)
    game.player("p1", Vec2(40_000, 55_000), coalition="BLUE")  # 55 km from b1: inside
    engine.step(1)
    engine.step(1)
    assert line.divisions["b1"].authority is Authority.DELEGATED
    game.player("p1", Vec2(40_000, 65_000), coalition="BLUE")  # 65 km: outside radius, inside hysteresis
    engine.step(1)
    assert line.divisions["b1"].authority is Authority.DELEGATED
    game.player("p1", Vec2(40_000, 75_000), coalition="BLUE")  # 75 km: out
    engine.step(1)
    assert line.divisions["b1"].authority is Authority.DEMATERIALIZING


def test_game_losses_flow_back_into_campaign(line):
    engine, game = setup(line)
    game.player("p1", Vec2(40_000, 0), coalition="BLUE")
    engine.step(1)
    engine.step(1)
    div = line.divisions["b1"]
    before_mbt, before_org = div.elements["mbt"], div.org
    game.kill("b1", "mbt", 30)
    game.advance(1)
    engine.step(1)
    assert div.elements["mbt"] == before_mbt - 30
    assert div.org < before_org  # losses shake cohesion too

    game.player_leaves("p1")
    engine.step(1)
    engine.step(1)
    assert div.authority is Authority.CAMPAIGN
    # Back under the campaign, replacements may already trickle in.
    assert div.elements["mbt"] == pytest.approx(before_mbt - 30, abs=0.5)


def test_game_cannot_resurrect_elements(line):
    engine, game = setup(line)
    game.player("p1", Vec2(40_000, 0), coalition="BLUE")
    engine.step(1)
    engine.step(1)
    div = line.divisions["b1"]
    div.elements["mbt"] = 10
    game.entities["b1"].elements["mbt"] = 90
    game.advance(1)
    engine.step(1)
    assert div.elements["mbt"] == 10


def test_campaign_orders_reach_delegated_entities(line):
    engine, game = setup(line)
    game.player("p1", Vec2(40_000, 0), coalition="BLUE")
    engine.step(1)
    engine.step(1)
    div = line.divisions["b1"]
    line.set_order(div, Order(OrderKind.MOVE, "a"))
    engine.step(1)
    routes = [c for c in commands(game, UpdateRoute) if c.entity_id == "b1"]
    assert routes and routes[-1].waypoints[-1] == line.theater.provinces["a"].center


def test_delegated_movement_is_driven_by_game_positions(line):
    engine, game = setup(line)
    div = line.divisions["b1"]
    line.set_order(div, Order(OrderKind.MOVE, "b"))
    game.player("p1", Vec2(30_000, 0), kind=AIR, coalition="BLUE")
    engine.step(1)
    engine.step(1)
    assert div.authority is Authority.DELEGATED
    # Campaign movement is off: only the game moves the unit.
    for _ in range(40):
        game.advance(120)
        engine.step(120)
    assert div.location == "b"
    assert div.order.kind is OrderKind.HOLD


def test_battle_is_never_split(line):
    engine, game = setup(line)
    b1, r1 = line.divisions["b1"], line.divisions["r1"]
    line.set_order(b1, Order(OrderKind.ATTACK, "d"))
    for _ in range(240):  # close in to the assault position
        engine.step(60)
        if b1.battle:
            break
    assert b1.battle is not None
    t = line.theater
    a, d = b1.position(t), r1.position(t)
    # A helicopter (30 km bubble) that sees the attacker but not the defender 3 km beyond it.
    game.player("p1", Vec2(a.x - 28_000, 0), kind=PlayerKind.HELO, coalition="BLUE")
    assert a.distance_to(Vec2(a.x - 28_000, 0)) < 30_000 < d.distance_to(Vec2(a.x - 28_000, 0))
    engine.step(1)
    ids = {c.entity.id for c in commands(game, Materialize)}
    assert ids == {"b1", "r1"}


def test_delegated_battle_takes_no_campaign_damage(line):
    engine, game = setup(line)
    b1, r1 = line.divisions["b1"], line.divisions["r1"]
    line.set_order(b1, Order(OrderKind.ATTACK, "d"))
    game.player("p1", Vec2(50_000, 0), coalition="BLUE")
    engine.step(1)
    engine.step(1)
    before = (b1.strength, r1.strength)
    for _ in range(30):
        game.advance(60)
        engine.step(60)
    assert (b1.strength, r1.strength) == before


def test_disconnect_reclaims_everything(line):
    engine, game = setup(line)
    game.player("p1", Vec2(40_000, 0), coalition="BLUE")
    engine.step(1)
    engine.step(1)
    game.disconnect()
    engine.step(1)
    assert all(e.authority is Authority.CAMPAIGN for e in line.entities())
    assert not engine.bubbles.active and not engine.live


def test_handoff_timeout_reclaims(line):
    engine, game = setup(line, auto_ack=False)
    game.player("p1", Vec2(40_000, 0), coalition="BLUE")
    engine.step(1)
    assert line.divisions["b1"].authority is Authority.MATERIALIZING
    engine.step(engine.config.handoff_timeout_s + 1)
    engine.step(1)
    assert any(e.kind == "handoff_timeout" for e in line.events)


def test_entity_removed_while_in_game_is_despawned(line):
    engine, game = setup(line)
    game.player("p1", Vec2(40_000, 0), coalition="BLUE")
    engine.step(1)
    engine.step(1)
    del line.divisions["r1"]
    engine.step(1)
    assert "r1" in {c.entity_id for c in commands(game, Dematerialize)}
