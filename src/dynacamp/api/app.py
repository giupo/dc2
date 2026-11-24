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
        allow_methods=["GET"],
        allow_headers=["*"],
    )
    app.state.engine = engine
    app.state.active_streams = 0

    def state():
        """The current campaign state."""
        return engine.state

    @app.get("/api/campaign", response_model=schemas.CampaignView, tags=["campaign"])
    def get_campaign():
        """Clock, factions and objectives, connected players."""
        with engine.lock:
            return views.campaign(state(), engine.live, views.players(engine))

    @app.get("/api/map", response_model=schemas.MapView, tags=["map"])
    def get_map():
        """Provinces with territory polygons, links, airbases. Geometry never changes: load it once."""
        with engine.lock:
            return views.map_view(state())

    @app.get("/api/fronts", response_model=schemas.FrontsView, tags=["map"])
    def get_fronts():
        """Front lines, front sectors with local force ratios, battles."""
        with engine.lock:
            return views.fronts(state())

    @app.get("/api/forces", response_model=schemas.ForcesView, tags=["forces"])
    def get_forces():
        """Divisions with state and remaining route, battles."""
        with engine.lock:
            return views.forces(state())

    @app.get("/api/logistics", response_model=schemas.LogisticsView, tags=["logistics"])
    def get_logistics():
        """Depots, convoys, supply network reach, division supply."""
        with engine.lock:
            return views.logistics(state())

    @app.get("/api/ato", response_model=schemas.AtoView, tags=["air"])
    def get_ato():
        """Packages and flights, squadrons, sortie budget, in-game join boards."""
        with engine.lock:
            return views.ato(state())

    @app.get("/api/airdefense", response_model=schemas.AirDefenseView, tags=["air"])
    def get_air_defense():
        """SAM sites and early-warning radars, their state, and the IADS network links."""
        with engine.lock:
            return views.air_defense(state())

    @app.get("/api/events", response_model=schemas.EventsPage, tags=["events"])
    def get_events(
        since: int = Query(0, ge=0, description="first event seq to return"),
        kinds: str | None = Query(None, description="comma separated event kinds to keep"),
        limit: int = Query(500, ge=1, le=5000),
    ):
        """Campaign events in order, paged: call again with `since=next`."""
        with engine.lock:
            return views.events(state(), since, set(kinds.split(",")) if kinds else None, limit)

    @app.get("/api/snapshot", response_model=schemas.SnapshotView, tags=["campaign"])
    def get_snapshot():
        """Everything above in one document."""
        with engine.lock:
            return views.snapshot(state(), engine.live, views.players(engine))

    @app.get(
        "/api/stream",
        tags=["events"],
        response_class=StreamingResponse,
        responses={200: {"content": {"text/event-stream": {}}, "description": "Server-Sent Events"}},
    )
    async def stream(request: Request, since: int | None = Query(None, ge=0, description="replay from this seq")):
        """Server-Sent Events: `event` for each campaign event (data = Event, id = seq) and
        `tick` every second (data = Tick). Without `since` only new events are sent."""

        def read(seq: int):
            """Read the next batch of events and the clock under the engine lock."""
            with engine.lock:
                batch = views.events(engine.state, seq, limit=1000)
                tick = {"time": views.time_view(engine.state.time), "live": engine.live}
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
