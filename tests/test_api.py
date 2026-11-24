"""Views and HTTP API."""

import json
import threading
import urllib.request

import pytest

from dynacamp import geometry
from dynacamp.adapters.fake import FakeGame
from dynacamp.api import ApiServer, views
from dynacamp.engine import Engine
from dynacamp.geo import Vec2
from dynacamp.projection import dcs_to_ll, ll_to_dcs, to_latlon


def test_projection_roundtrip_and_known_airfield():
    for theater, (lat, lon) in (("caucasus", (42.17762, 42.48129)), ("syria", (33.42551, 36.51851))):
        x, z = ll_to_dcs(theater, lat, lon)
        la, lo = dcs_to_ll(theater, x, z)
        assert abs(la - lat) < 1e-4 and abs(lo - lon) < 1e-4  # ~10 m: series truncation
    # Kutaisi airdrome, DCS coordinates from pydcs: -284860, 683839
    la, lo = to_latlon("caucasus", Vec2(683839.0, -284860.0))
    assert abs(la - 42.1776) < 1e-3 and abs(lo - 42.4813) < 1e-3
    assert to_latlon("nowhere", Vec2(0, 0)) is None


def test_cells_contain_their_centre_and_neighbours_share_edges(demo):
    cs = geometry.cells(demo.theater)
    for pid, poly in cs.polygons.items():
        center = demo.theater.provinces[pid].center
        assert len(poly) >= 3
        # centre inside a convex polygon: same side of every edge
        signs = set()
        for i, (p, _) in enumerate(poly):
            q = poly[(i + 1) % len(poly)][0]
            cross = (q.x - p.x) * (center.y - p.y) - (q.y - p.y) * (center.x - p.x)
            signs.add(cross > 0)
        assert len(signs) == 1, pid
    # shared edges are symmetric
    for a in demo.theater.provinces:
        for b, _, _ in cs.shared_edges(a):
            assert a in {n for n, _, _ in cs.shared_edges(b)}


def test_front_lines_separate_enemies(demo):
    lines = geometry.front_lines(demo)
    assert lines
    for fl in lines:
        assert fl.factions == ("blue", "red") and len(fl.points) >= 2
        for a, b in fl.provinces:
            assert demo.theater.provinces[a].controller == "blue"
            assert demo.theater.provinces[b].controller == "red"


def test_snapshot_is_json_and_consistent(demo):
    engine = Engine(demo)
    engine.run_for(8 * 3600, dt=300)
    snap = json.loads(json.dumps(views.snapshot(demo), default=str))
    assert snap["campaign"]["theater"] == "caucasus"
    assert {p["id"] for p in snap["map"]["provinces"]} == set(demo.theater.provinces)
    assert all(p["ll"] and p["territory"]["ll"] for p in snap["map"]["provinces"])
    assert {d["id"] for d in snap["forces"]["divisions"]} == set(demo.divisions)
    assert snap["ato"]["packages"] and snap["ato"]["squadrons"]
    flight = snap["ato"]["packages"][0]["flights"][0]
    assert flight["waypoints"]["ll"] and flight["tot"]["label"].startswith("D")
    assert snap["logistics"]["depots"] and "blue" in snap["logistics"]["network"]


def test_events_paging(demo):
    Engine(demo).run_for(4 * 3600, dt=300)
    first = views.events(demo, 0, limit=5)
    assert len(first["events"]) == 5 and first["next"] == 5
    rest = views.events(demo, first["next"], limit=10_000)
    assert rest["next"] == len(demo.events)
    only = views.events(demo, 0, kinds={"package_planned"})
    assert only["events"] and all(e["kind"] == "package_planned" for e in only["events"])


def live_engine(demo):
    game = FakeGame()
    engine = Engine(demo, game)
    game.player("Pilot", Vec2(650_000, -290_000))
    engine.run_for(2 * 3600, dt=300)
    return engine


@pytest.fixture
def client(demo):
    from fastapi.testclient import TestClient

    from dynacamp.api import create_app

    engine = live_engine(demo)
    with TestClient(create_app(engine)) as c:
        yield c, engine


def test_every_endpoint_matches_its_schema(client):
    c, engine = client
    index = c.get("/api").json()
    paths = [p for p in index["endpoints"] if p != "/api/stream"]
    assert {"/api/campaign", "/api/map", "/api/fronts", "/api/forces", "/api/logistics", "/api/ato",
            "/api/airdefense", "/api/events", "/api/snapshot"} <= set(paths)
    ad = c.get("/api/airdefense").json()
    assert ad["sites"] and ad["links"]  # the demo has an IADS
    assert {s["role"] for s in ad["sites"]} >= {"long", "medium", "short", "ewr"}
    for path in paths:
        r = c.get(path)  # FastAPI validates against response_model: a mismatch is a 500
        assert r.status_code == 200, (path, r.text[:300])
    camp = c.get("/api/campaign").json()
    assert camp["live"] is True and camp["players"][0]["name"] == "Pilot"
    assert c.get("/api/nope").status_code == 404


def test_openapi_documents_all_models(client):
    c, _ = client
    spec = c.get("/openapi.json").json()
    for name in ("CampaignView", "MapView", "FrontsView", "ForcesView", "LogisticsView", "AtoView", "FlightView", "EventsPage"):
        assert name in spec["components"]["schemas"]
    assert "text/event-stream" in spec["paths"]["/api/stream"]["get"]["responses"]["200"]["content"]
    assert c.get("/docs").status_code == 200


def test_events_query_validation(client):
    c, engine = client
    assert c.get("/api/events", params={"since": 0, "limit": 3}).json()["next"] == 3
    kinds = c.get("/api/events", params={"kinds": "package_planned"}).json()["events"]
    assert kinds and {e["kind"] for e in kinds} == {"package_planned"}
    assert c.get("/api/events", params={"limit": 0}).status_code == 422


def test_cors(client):
    c, _ = client
    r = c.get("/api/campaign", headers={"Origin": "http://localhost:5173"})
    assert r.headers["access-control-allow-origin"] == "*"


@pytest.fixture
def server(demo):
    engine = live_engine(demo)
    api = ApiServer(engine, port=0)
    api.start()
    yield api, engine
    api.close()


def get(api, path):
    with urllib.request.urlopen(f"http://{api.host}:{api.port}{path}", timeout=5) as r:
        return json.loads(r.read())


def test_stream_pushes_events(server):
    api, engine = server
    since = len(engine.state.events) - 2
    req = urllib.request.urlopen(f"http://{api.host}:{api.port}/api/stream?since={since}", timeout=5)
    lines = []
    while not any(line.startswith("event: tick") for line in lines):
        lines.append(req.readline().decode().strip())
    req.close()
    assert sum(1 for line in lines if line == "event: event") == 2
    data = [json.loads(line[6:]) for line in lines if line.startswith("data: ")]
    assert data[0]["seq"] == since


def test_api_reads_while_engine_steps(server):
    api, engine = server
    errors = []

    def hammer():
        try:
            for _ in range(20):
                get(api, "/api/snapshot")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=hammer)
    t.start()
    for _ in range(50):
        engine.step(300)
    t.join()
    assert not errors


def test_stream_stops_when_client_leaves(server):
    import time

    api, engine = server
    r = urllib.request.urlopen(f"http://{api.host}:{api.port}/api/stream", timeout=5)
    r.readline()
    assert api.app.state.active_streams == 1
    r.close()
    deadline = time.monotonic() + 3
    while api.app.state.active_streams and time.monotonic() < deadline:
        time.sleep(0.05)
    assert api.app.state.active_streams == 0


def test_openapi_export_needs_no_engine():
    from dynacamp.api import create_app

    spec = create_app(None).openapi()
    assert "/api/ato" in spec["paths"] and "FlightView" in spec["components"]["schemas"]


def test_serves_built_front_end(demo, tmp_path):
    from fastapi.testclient import TestClient

    from dynacamp.api import create_app

    (tmp_path / "index.html").write_text("<html>dynacamp ui</html>")
    with TestClient(create_app(Engine(demo), web_dir=tmp_path)) as c:
        assert "dynacamp ui" in c.get("/").text
        assert c.get("/api/campaign").status_code == 200  # the API still wins over static files
        assert c.get("/docs").status_code == 200
