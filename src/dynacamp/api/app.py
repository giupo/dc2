"""FastAPI application exposing the campaign (read-only for now).

OpenAPI schema at ``/openapi.json``, interactive docs at ``/docs``.
Endpoints are plain ``def`` functions: FastAPI runs them in its threadpool, so
taking the engine lock never blocks the event loop.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal

import anyio
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..engine import Engine
from . import schemas, views

STREAM_TICK_S = 1.0


def create_app(
    engine: Engine | None, cors_origins: list[str] | None = None, web_dir: str | Path | None = None
) -> FastAPI:
    """Build the app. ``engine`` may be None only to export the OpenAPI schema.
    ``web_dir``, if it exists, is served at ``/`` (the built React front-end)."""
    app = FastAPI(
        title="DynaCamp",
        version=str(views.API_VERSION),
        summary="Dynamic campaign engine: map, fronts, forces, logistics, ATO, events.",
        description=(
            "Positions come as `pos` = [east, north] metres on the theater plane and `ll` = [lat, lon]. "
            "Times are `{t, label}` with `t` in campaign seconds. Every response is taken under the "
            "engine lock, so it describes a consistent state between two engine steps."
        ),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins if cors_origins is not None else ["*"],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )
    app.state.engine = engine
    app.state.active_streams = 0

    def state():
        """The current campaign state."""
        return engine.state

    def viewer(faction: str | None) -> str | None:
        """Validate the ``faction`` query parameter (None = umpire view)."""
        if faction is not None and faction not in engine.state.factions:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail=f"no faction {faction}")
        return faction

    FACTION = Query(None, description="faction whose fog-of-war view to give (omit for the umpire's view)")

    @app.get("/api/campaign", response_model=schemas.CampaignView, tags=["campaign"])
    def get_campaign():
        """Clock, factions and objectives, connected players."""
        with engine.lock:
            return views.campaign(state(), engine.live, views.players(engine), views.control(engine))

    @app.get("/api/map", response_model=schemas.MapView, tags=["map"])
    def get_map():
        """Provinces with territory polygons, links, airbases. Geometry never changes: load it once."""
        with engine.lock:
            return views.map_view(state())

    @app.get("/api/fronts", response_model=schemas.FrontsView, tags=["map"])
    def get_fronts(faction: str | None = FACTION):
        """Front lines, front sectors with local force ratios, battles."""
        with engine.lock:
            return views.fronts(state(), viewer(faction))

    @app.get("/api/forces", response_model=schemas.ForcesView, tags=["forces"])
    def get_forces(faction: str | None = FACTION):
        """Divisions with state and remaining route, battles."""
        with engine.lock:
            return views.forces(state(), viewer(faction))

    @app.get("/api/logistics", response_model=schemas.LogisticsView, tags=["logistics"])
    def get_logistics(faction: str | None = FACTION):
        """Depots, convoys, supply network reach, division supply."""
        with engine.lock:
            return views.logistics(state(), viewer(faction))

    @app.get("/api/ato", response_model=schemas.AtoView, tags=["air"])
    def get_ato(faction: str | None = FACTION):
        """Packages and flights, squadrons, sortie budget, in-game join boards
        (and the enemy radar tracks, in a faction's view)."""
        with engine.lock:
            return views.ato(state(), viewer(faction))

    @app.get("/api/ato/flights/{flight_id}/briefing", response_model=schemas.BriefingView, tags=["air"])
    def get_briefing(
        flight_id: str,
        faction: str | None = FACTION,
        units: Literal["metric", "imperial"] | None = Query(
            None, description="units of the texts: imperial = kts/ft/NM, metric = km/h/m/km (default: the campaign's)"
        ),
    ):
        """Mission package of a flight: briefing, named waypoints, threats, ordnance, comms."""
        from fastapi import HTTPException

        from ..systems import briefing

        with engine.lock:
            f = state().flights.get(flight_id)
            if f is None or (viewer(faction) and state().at_war(faction, f.faction)):
                raise HTTPException(status_code=404, detail=f"no flight {flight_id}")
            b = briefing.briefing(state(), f, units)
            return {**b, "text": briefing.render(b, now=state().time)}

    @app.get("/api/airdefense", response_model=schemas.AirDefenseView, tags=["air"])
    def get_air_defense(faction: str | None = FACTION):
        """SAM sites and early-warning radars, their state, and the IADS network links."""
        with engine.lock:
            return views.air_defense(state(), viewer(faction))

    @app.get("/api/intel", response_model=schemas.IntelView, tags=["intel"])
    def get_intel(faction: str = Query(..., description="faction whose intelligence picture to give")):
        """Fog of war: the contacts a faction holds on the enemy (last known
        position, age, source, identification) and its air-search sensors."""
        with engine.lock:
            return views.intel_view(state(), viewer(faction))

    @app.get("/api/events", response_model=schemas.EventsPage, tags=["events"])
    def get_events(
        since: int = Query(0, ge=0, description="first event seq to return"),
        kinds: str | None = Query(None, description="comma separated event kinds to keep"),
        limit: int = Query(500, ge=1, le=5000),
        faction: str | None = FACTION,
    ):
        """Campaign events in order, paged: call again with `since=next`."""
        with engine.lock:
            return views.events(state(), since, set(kinds.split(",")) if kinds else None, limit, viewer(faction))

    @app.get("/api/snapshot", response_model=schemas.SnapshotView, tags=["campaign"])
    def get_snapshot(faction: str | None = FACTION):
        """Everything above in one document."""
        with engine.lock:
            return views.snapshot(state(), engine.live, views.players(engine), viewer(faction), views.control(engine))

    @app.get(
        "/api/stream",
        tags=["events"],
        response_class=StreamingResponse,
        responses={200: {"content": {"text/event-stream": {}}, "description": "Server-Sent Events"}},
    )
    async def stream(
        request: Request,
        since: int | None = Query(None, ge=0, description="replay from this seq"),
        faction: str | None = FACTION,
    ):
        """Server-Sent Events: `event` for each campaign event (data = Event, id = seq) and
        `tick` every second (data = Tick). Without `since` only new events are sent."""
        viewer(faction)

        def read(seq: int):
            """Read the next batch of events and the clock under the engine lock."""
            with engine.lock:
                batch = views.events(engine.state, seq, limit=1000, viewer=faction)
                tick = {"time": views.time_view(engine.state.time), "live": engine.live, **views.control(engine)}
                return batch, tick

        async def gen() -> AsyncIterator[str]:
            """Server-Sent Events generator."""
            seq = since if since is not None else len(engine.state.events)
            app.state.active_streams += 1
            try:
                while not await request.is_disconnected():
                    batch, tick = await anyio.to_thread.run_sync(read, seq)
                    seq = batch["next"]
                    # One write per batch: cheaper, and a client that just left
                    # costs at most one failed send.
                    yield "".join(_sse("event", e, e["seq"]) for e in batch["events"]) + _sse("tick", tick)
                    await asyncio.sleep(STREAM_TICK_S)
            finally:
                app.state.active_streams -= 1

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    @app.get(
        "/api/positions/stream",
        tags=["map"],
        response_class=StreamingResponse,
        responses={200: {"content": {"text/event-stream": {}}, "description": "Server-Sent Events"}},
    )
    async def positions_stream(
        request: Request,
        hz: float = Query(5.0, ge=0.5, le=20.0, description="samples per second"),
        faction: str | None = FACTION,
    ):
        """Server-Sent Events: `positions` events (data = Positions) at `hz` per
        second, for smooth animation: the client interpolates between samples.
        Only what moves, as `id -> [lat, lon]`; fog of war applies with `faction`."""
        viewer(faction)
        period = 1.0 / hz

        def read():
            """One sample under the engine lock."""
            with engine.lock:
                return views.positions(engine.state, faction)

        async def gen() -> AsyncIterator[str]:
            """Positions generator: sample, send, wait for the rest of the period."""
            app.state.active_streams += 1
            loop = asyncio.get_running_loop()
            try:
                while not await request.is_disconnected():
                    start = loop.time()
                    yield _sse("positions", await anyio.to_thread.run_sync(read))
                    await asyncio.sleep(max(0.01, period - (loop.time() - start)))
            finally:
                app.state.active_streams -= 1

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    @app.get("/api/positions", response_model=schemas.PositionsView, tags=["map"])
    def get_positions(faction: str | None = FACTION):
        """One sample of the positions stream."""
        with engine.lock:
            return views.positions(state(), viewer(faction))

    @app.get("/api/control", response_model=schemas.ControlView, tags=["campaign"])
    def get_control():
        """Pause and speed of the campaign clock."""
        with engine.lock:
            return views.control(engine)

    @app.post("/api/control", response_model=schemas.ControlView, tags=["campaign"])
    def post_control(body: schemas.ControlRequest):
        """Pause / resume and fast-forward the campaign. Refused (409) while a
        game simulates part of the campaign: then time must run 1:1."""
        from fastapi import HTTPException

        with engine.lock:
            if engine.live and (body.paused or (body.compression not in (None, engine.control.compression))):
                raise HTTPException(status_code=409, detail="a game is live: time runs 1:1 and cannot be paused or accelerated")
            try:
                engine.control.set(body.paused, body.compression)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            engine.state.emit("time_control", paused=engine.control.paused, compression=engine.control.compression)
            return views.control(engine)

    @app.get("/api", tags=["campaign"])
    def index():
        """Entry point: list of endpoints."""
        return {
            "api_version": views.API_VERSION,
            "docs": "/docs",
            "openapi": "/openapi.json",
            "endpoints": [r.path for r in app.routes if getattr(r, "path", "").startswith("/api/")],
        }

    if web_dir is not None and Path(web_dir).is_dir():
        # Mounted last so that /api, /docs and /openapi.json win.
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")

    return app


def _sse(event: str, data, event_id: int | None = None) -> str:
    """Format one Server-Sent Event."""
    msg = f"event: {event}\n"
    if event_id is not None:
        msg += f"id: {event_id}\n"
    return msg + f"data: {json.dumps(data, default=str, separators=(',', ':'))}\n\n"
