import json
import shutil
import socket
import subprocess
import time

import pytest

from dynacamp.adapters.base import (
    DematerializeAck,
    EntitySnapshot,
    EntityState,
    GameDisconnected,
    Materialize,
    PlayerKind,
    PlayerLeft,
    PlayerSeen,
)
from dynacamp.adapters.dcs import LUA_BRIDGE, DcsAdapter, DcsTranslator
from dynacamp.adapters.dcs.protocol import from_dcs, to_dcs
from dynacamp.geo import Vec2

from .conftest import ROOT


def snapshot(**kw):
    base = dict(
        id="b1",
        kind="division",
        name="Tank Bde",
        faction="blue",
        side="blue",
        position=Vec2(650_000, -280_000),
        heading=0.0,
        elements={"mbt": 90.0, "ifv": 40.0, "truck": 60.0},
        waypoints=(Vec2(660_000, -285_000),),
        speed_kmh=36.0,
    )
    base.update(kw)
    return EntitySnapshot(**base)


def test_frames_roundtrip():
    v = Vec2(650_000, -280_000)
    x, y = to_dcs(v)
    assert (x, y) == (-280_000, 650_000)  # DCS x is north
    assert from_dcs(x, y) == v


def test_spawn_scales_elements_and_builds_groups():
    tr = DcsTranslator()
    msg = tr.encode(Materialize(snapshot()))
    assert msg["cmd"] == "spawn"
    units = [u for g in msg["groups"] for u in g["data"]["units"]]
    types = [u["type"] for u in units]
    assert types.count("M-1 Abrams") == 15  # 90 / 6
    assert types.count("M-2 Bradley") == 5  # 40 / 8
    assert "M 818" not in types  # division trucks are not spawned
    assert all(len(g["data"]["units"]) <= 20 for g in msg["groups"])
    assert msg["groups"][0]["country"] == 2
    route = msg["groups"][0]["data"]["route"]["points"]
    assert route[-1]["action"] == "On Road" and route[-1]["speed"] == pytest.approx(10.0)


def test_state_converts_alive_units_back_to_campaign_elements():
    tr = DcsTranslator()
    tr.encode(Materialize(snapshot()))
    units = [{"n": f"DC.b1.mbt.{i}", "x": -280_000, "z": 650_000} for i in range(10)]  # 5 of 15 tanks dead
    units += [{"n": f"DC.b1.ifv.{i}", "x": -280_000, "z": 650_000} for i in range(5)]
    (rep,) = tr.decode({"msg": "state", "entities": [{"id": "b1", "units": units}]})
    assert isinstance(rep, EntityState)
    assert rep.elements["mbt"] == pytest.approx(60.0)
    assert rep.elements["ifv"] == pytest.approx(40.0)
    assert rep.position == Vec2(650_000, -280_000)

    (ack,) = tr.decode({"msg": "despawned", "id": "b1", "units": []})
    assert isinstance(ack, DematerializeAck) and ack.position is None and ack.elements["mbt"] == 0
    assert "b1" not in tr.spawned


def test_players_diff():
    tr = DcsTranslator()
    reps = tr.decode({"msg": "players", "players": [{"id": "Pilot", "coalition": 2, "category": 1, "x": 1, "z": 2}]})
    assert isinstance(reps[0], PlayerSeen) and reps[0].kind is PlayerKind.HELO and reps[0].side == "blue"
    reps = tr.decode({"msg": "players", "players": []})
    assert reps == [PlayerLeft("Pilot")]


def test_adapter_over_tcp():
    adapter = DcsAdapter(port=0)
    adapter.start()
    try:
        client = socket.create_connection(("127.0.0.1", adapter.port), timeout=2)
        client.sendall(b'{"msg":"hello"}\n{"msg":"players","players":[{"id":"P","coalition":1,"category":0,"x":0,"z":0}]}\n')
        reports = []
        deadline = time.monotonic() + 2
        while not reports and time.monotonic() < deadline:
            reports = adapter.poll()
            time.sleep(0.01)
        assert isinstance(reports[0], PlayerSeen) and reports[0].side == "red"
        assert adapter.connected

        adapter.send([Materialize(snapshot())])
        line = client.makefile().readline()
        assert json.loads(line)["cmd"] == "spawn"

        client.close()
        deadline = time.monotonic() + 3
        got = []
        while not any(isinstance(r, GameDisconnected) for r in got) and time.monotonic() < deadline:
            got += adapter.poll()
            time.sleep(0.01)
        assert any(isinstance(r, GameDisconnected) for r in got)
        assert not adapter.connected
    finally:
        adapter.close()


@pytest.mark.skipif(shutil.which("lua5.1") is None, reason="lua5.1 not installed")
def test_lua_bridge():
    res = subprocess.run(
        ["lua5.1", str(ROOT / "tests" / "lua" / "test_bridge.lua"), str(LUA_BRIDGE)],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stdout + res.stderr


def flight_snapshot():
    return snapshot(
        id="fl7",
        kind="flight",
        name="Viper 1",
        elements={"aircraft": 2.0},
        waypoints=(Vec2(660_000, -285_000), Vec2(683_980, -284_988)),
        speed_kmh=800.0,
        altitude=6000.0,
        attrs={"aircraft": "f16c", "task": "CAS", "callsign": "Viper 1", "helo": False,
               "target": Vec2(660_000, -285_000), "target_index": 0, "home": "Kutaisi"},
    )


def test_flight_spawn_uses_templates_and_tasks():
    from dynacamp.adapters.base import PublishAto

    tr = DcsTranslator()
    msg = tr.encode(Materialize(flight_snapshot()))
    (g,) = msg["groups"]
    assert g["category"] == "airplane" and g["land_at"] == "Kutaisi"
    assert g["templates"] == ["DC_TPL_f16c_CAS", "DC_TPL_f16c"]
    assert [u["type"] for u in g["data"]["units"]] == ["F-16C_50", "F-16C_50"]
    target_wp = g["data"]["route"]["points"][1]
    assert target_wp["task"]["params"]["tasks"][0]["id"] == "EngageTargetsInZone"
    assert target_wp["alt"] == 6000.0

    board = tr.encode(PublishAto("blue", ({"id": "pk1", "label": "x", "flights": []},)))
    assert board["cmd"] == "ato" and board["coalition"] == 2 and board["version"] == 1


def test_join_carries_player_capabilities():
    from dynacamp.adapters.base import JoinRequest

    tr = DcsTranslator()
    tr.decode({"msg": "players", "players": [{"id": "Hog", "coalition": 2, "category": 0, "type": "A-10C_2", "x": 0, "z": 0}]})
    (req,) = tr.decode({"msg": "join", "player": "Hog", "flight": "fl7"})
    assert isinstance(req, JoinRequest) and req.capable_tasks == frozenset({"CAS", "BAI"})
    tr.decode({"msg": "players", "players": [{"id": "X", "coalition": 2, "category": 0, "type": "Unknown-Jet", "x": 0, "z": 0}]})
    (req,) = tr.decode({"msg": "join", "player": "X", "flight": "fl7"})
    assert req.capable_tasks is None


def test_ground_spawn_lines_vehicles_up_on_the_road():
    from dynacamp.world import project_on

    road = (Vec2(650_000, -280_000), Vec2(650_500, -281_000), Vec2(651_500, -281_400), Vec2(653_000, -281_500))
    tr = DcsTranslator()
    msg = tr.encode(Materialize(snapshot(attrs={"road_back": road})))
    for g in msg["groups"]:
        assert g["snap_to_road"] is True
        for u in g["data"]["units"]:
            p = from_dcs(u["x"], u["y"])
            on = project_on(list(road), p)
            from dynacamp.world import point_along

            assert point_along(list(road), on)[0].distance_to(p) < 1.0  # exactly on the polyline


@pytest.mark.parametrize("miz", sorted((ROOT / "missions").glob("*.miz")), ids=lambda p: p.stem)
def test_shipped_missions_embed_the_current_bridge(miz):
    """Regenerate with `dynacamp build-miz scenarios/*.toml` after changing the bridge."""
    import zipfile

    with zipfile.ZipFile(miz) as z:
        assert z.read("l10n/DEFAULT/dynacamp_bridge.lua") == LUA_BRIDGE.read_bytes()
