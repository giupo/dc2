"""FastAPI application exposing the campaign.

OpenAPI schema at ``/openapi.json``, interactive docs at ``/docs``.
Reading is open; commands (``POST``) need ``Authorization: Bearer <token>``
when the app is built with a token.
Endpoints are plain ``def`` functions: FastAPI runs them in its threadpool, so
taking the engine lock never blocks the event loop.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator
from enum import StrEnum
from pathlib import Path


import anyio
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles

from ..engine import Engine
from ..units import UnitDomain
from ..persistence import Saver, SaveTrigger
from . import schemas, views
from ..events import EventKind

#: Seconds between two polls of the event stream (and clock ticks sent).
STREAM_TICK_S = 1.0
#: Answer to a request without the right bearer token.
BAD_TOKEN = "missing or wrong API token"
#: Media type of the Server-Sent Events streams.
SSE_MEDIA_TYPE = "text/event-stream"
#: ``Cache-Control`` of the event streams: never cached.
NO_STORE = {"Cache-Control": "no-store"}
#: OpenAPI description of a 401 answer.
UNAUTHORIZED = {401: {"description": BAD_TOKEN}}
#: OpenAPI description of an endpoint's Server-Sent Events answer.
SSE_RESPONSE = {200: {"content": {SSE_MEDIA_TYPE: {}}, "description": "Server-Sent Events"}}


class Tag(StrEnum):
    """Groups of endpoints in the OpenAPI schema (``/docs``)."""
    #: Campaign status, time control, saves.
    CAMPAIGN = "campaign"
    #: Theatre, front lines, positions.
    MAP = "map"
    #: Ground forces.
    FORCES = "forces"
    #: Depots, convoys, supply.
    LOGISTICS = "logistics"
    #: ATO, flights, briefings, air defence.
    AIR = "air"
    #: A faction's intelligence picture.
    INTEL = "intel"
    #: The event log and its stream.
    EVENTS = "events"


class SseEvent(StrEnum):
    """Names of the Server-Sent Events of the streams (the UI listens to them)."""
    #: A campaign event.
    EVENT = "event"
    #: The clock, every STREAM_TICK_S.
    TICK = "tick"
    #: Where everything is.
    POSITIONS = "positions"


def _event_kinds(kinds: str | None) -> set[EventKind] | None:
    """Parse the ``kinds`` query parameter of ``/api/events``.

    Args:
        kinds: Comma separated event kinds, or None for all of them.

    Returns:
        The kinds to keep, or None to keep every kind.

    Raises:
        HTTPException: 422 if a kind is not an :class:`EventKind`.
    """
    if not kinds:
        return None
    wanted = {k.strip() for k in kinds.split(",") if k.strip()}
    unknown = sorted(wanted - set(EventKind))
    if unknown:
        raise HTTPException(422, f"unknown event kinds: {', '.join(unknown)}")
    return {EventKind(k) for k in wanted}


def create_app(
    engine: Engine | None,
    cors_origins: list[str] | None = None,
    web_dir: str | Path | None = None,
    token: str | None = None,
    saver: Saver | None = None,
) -> FastAPI:
    """Build the app.

    Args:
        engine: The engine; None only to export the OpenAPI schema.
        cors_origins: Allowed origins (default ``*``).
        web_dir: Built React front-end to serve at ``/``, if it exists.
        token: Bearer token required by commands (``POST``); None leaves them open.
        saver: Saves the campaign on ``POST /api/save`` (None: saving disabled).

    Returns:
        The FastAPI application.
    """
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
        """The current campaign state.

        Returns:
            The engine's campaign state.
        """
        return engine.state

    def viewer(faction: str | None) -> str | None:
        """Validate the ``faction`` query parameter (None = umpire view).

        Args:
            faction: The requested faction, or None.

        Returns:
            The same faction.
        """
        if faction is not None and faction not in engine.state.factions:
            raise HTTPException(status_code=404, detail=f"no faction {faction}")
        return faction

    FACTION = Query(None, description="faction whose fog-of-war view to give (omit for the umpire's view)")

    bearer = HTTPBearer(auto_error=False, description="token printed by `dynacamp serve` (or `--api-token`)")

    def authenticated(credentials: HTTPAuthorizationCredentials | None) -> bool:
        """True if no token is required or the request carries the right one.

        Args:
            credentials: The bearer credentials of the request, if any.

        Returns:
            Whether the request may run commands.
        """
        if token is None:
            return True
        return credentials is not None and secrets.compare_digest(credentials.credentials.encode(), token.encode())

    def require_token(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> None:
        """Dependency of commands: refuse (401) a request without the right token.

        Args:
            credentials: The bearer credentials of the request, if any.
        """
        if not authenticated(credentials):
            raise HTTPException(
                status_code=401, detail=BAD_TOKEN, headers={"WWW-Authenticate": "Bearer"}
            )

    @app.get("/api/auth", response_model=schemas.AuthView, tags=[Tag.CAMPAIGN])
    def get_auth(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        """Whether commands need a token and whether the one sent (if any) is valid.

        \f

        Args:
            credentials: The bearer credentials of the request, if any.

        Returns:
            Whether a token is required and whether the request is authenticated.
        """
        return {"required": token is not None, "authenticated": authenticated(credentials)}

    @app.get("/api/campaign", response_model=schemas.CampaignView, tags=[Tag.CAMPAIGN])
    def get_campaign():
        """Clock, factions and objectives, connected players.

        \f

        Returns:
            The campaign view, with the save status.
        """
        with engine.lock:
            view = views.campaign(state(), engine.live, views.players(engine), views.control(engine))
        return {**view, "save": views.save_view(saver)}

    @app.get("/api/map", response_model=schemas.MapView, tags=[Tag.MAP])
    def get_map():
        """Provinces with territory polygons, links, airbases. Geometry never changes: load it once.

        \f

        Returns:
            The map view.
        """
        with engine.lock:
            return views.map_view(state())

    @app.get("/api/fronts", response_model=schemas.FrontsView, tags=[Tag.MAP])
    def get_fronts(faction: str | None = FACTION):
        """Front lines, front sectors with local force ratios, battles.

        \f

        Args:
            faction: Faction whose fog-of-war view to give (None: the umpire's).

        Returns:
            The fronts view.
        """
        with engine.lock:
            return views.fronts(state(), viewer(faction))

    @app.get("/api/forces", response_model=schemas.ForcesView, tags=[Tag.FORCES])
    def get_forces(faction: str | None = FACTION):
        """Divisions with state and remaining route, battles.

        \f

        Args:
            faction: Faction whose fog-of-war view to give (None: the umpire's).

        Returns:
            The forces view.
        """
        with engine.lock:
            return views.forces(state(), viewer(faction))

    @app.get("/api/logistics", response_model=schemas.LogisticsView, tags=[Tag.LOGISTICS])
    def get_logistics(faction: str | None = FACTION):
        """Depots, convoys, supply network reach, division supply.

        \f

        Args:
            faction: Faction whose fog-of-war view to give (None: the umpire's).

        Returns:
            The logistics view.
        """
        with engine.lock:
            return views.logistics(state(), viewer(faction))

    @app.get("/api/ato", response_model=schemas.AtoView, tags=[Tag.AIR])
    def get_ato(faction: str | None = FACTION):
        """Packages and flights, squadrons, sortie budget, in-game join boards
        (and the enemy radar tracks, in a faction's view).

        \f

        Args:
            faction: Faction whose fog-of-war view to give (None: the umpire's).

        Returns:
            The ATO view.
        """
        with engine.lock:
            return views.ato(state(), viewer(faction))

    @app.get("/api/ato/packages/{package_id}", response_model=schemas.PackageView, tags=[Tag.AIR],
             responses={404: {"description": "unknown package (or an enemy's)"}})
    def get_package(package_id: str, faction: str | None = FACTION):
        """A package and its flights, also once it is over (archived).

        \f

        Args:
            package_id: The package (live or archived).
            faction: Faction whose fog-of-war view to give (None: the umpire's).

        Returns:
            The package view.
        """
        with engine.lock:
            p = views.find_package(state(), package_id=package_id, viewer=viewer(faction))
            if p is None:
                raise HTTPException(status_code=404, detail=f"no package {package_id}")
            return views.package_view(state(), p)

    @app.get("/api/ato/flights/{flight_id}/package", response_model=schemas.PackageView, tags=[Tag.AIR],
             responses={404: {"description": "unknown flight (or an enemy's)"}})
    def get_flight_package(flight_id: str, faction: str | None = FACTION):
        """The package of a flight, also once it is over (archived).

        \f

        Args:
            flight_id: A flight of the package (live or archived).
            faction: Faction whose fog-of-war view to give (None: the umpire's).

        Returns:
            The package view.
        """
        with engine.lock:
            p = views.find_package(state(), flight_id=flight_id, viewer=viewer(faction))
            if p is None:
                raise HTTPException(status_code=404, detail=f"no flight {flight_id}")
            return views.package_view(state(), p)

    @app.get("/api/ato/flights/{flight_id}/briefing", response_model=schemas.BriefingView, tags=[Tag.AIR])
    def get_briefing(
        flight_id: str,
        faction: str | None = FACTION,
        units: UnitDomain | None = Query(
            None, description="units of the texts: imperial = kts/ft/NM, metric = km/h/m/km (default: the campaign's)"
        ),
    ):
        """Mission package of a flight: briefing, named waypoints, threats, ordnance, comms.

        \f

        Args:
            flight_id: The flight.
            faction: Faction whose fog-of-war view to give (None: the umpire's).
            units: Units of the texts (None: the campaign's).

        Returns:
            The briefing view.
        """
        from ..systems import briefing

        with engine.lock:
            f = state().flights.get(flight_id)
            if f is None or (viewer(faction) and state().at_war(faction, f.faction)):
                raise HTTPException(status_code=404, detail=f"no flight {flight_id}")
            b = briefing.briefing(state(), f, units)
            return {**b, "text": briefing.render(b, now=state().time)}

    # ------------------------------------------------------------ ATO orders

    ORDER_RESPONSES = {
        **UNAUTHORIZED,
        404: {"description": "unknown flight, package or faction"},
        409: {"description": "order refused: `detail` says why"},
    }

    def point(p: schemas.PointIn):
        """Campaign point of a request position (``ll`` preferred).

        Args:
            p: The position as sent in a request.

        Returns:
            The point on the campaign plane.
        """
        from ..geo import Vec2
        from ..projection import from_latlon

        if p.ll is not None:
            v = from_latlon(state().theater.name, p.ll[0], p.ll[1])
            if v is None:
                raise HTTPException(status_code=422, detail="this theater has no lat/lon projection: send `pos`")
            return v
        if p.pos is not None:
            return Vec2(p.pos[0], p.pos[1])
        raise HTTPException(status_code=422, detail="a point needs `ll` or `pos`")

    def order(fn, *args, **kwargs):
        """Run an order under the engine lock, turning a refusal into an HTTP error.

        Args:
            fn: The order function of :mod:`dynacamp.systems.orders`.
            *args: Its positional arguments.
            **kwargs: Its keyword arguments.

        Returns:
            What the order function returns.
        """
        from ..systems.orders import OrderError

        try:
            return fn(*args, **kwargs)
        except OrderError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    @app.post(
        "/api/ato/packages",
        response_model=schemas.PackageView,
        tags=[Tag.AIR],
        dependencies=[Depends(require_token)],
        responses=ORDER_RESPONSES,
    )
    def post_package(body: schemas.PackageRequest):
        """Order a new package against a target the faction knows (needs the API token).
        Planned as the AI would: main flight, then SEAD and escort when called for.

        \f

        Args:
            body: The package order.

        Returns:
            The new package's view.
        """
        from ..air import Task
        from ..systems import orders

        with engine.lock:
            viewer(body.faction)
            target = orders.Target(
                body.target.kind, body.target.id, point(body.target.point) if body.target.point else None
            )
            pkg = order(orders.order_package, state(), body.faction, Task(body.task), target, body.squadron, body.tot)
            return views.package_view(state(), pkg)

    @app.post(
        "/api/ato/flights/{flight_id}/route",
        response_model=schemas.FlightView,
        tags=[Tag.AIR],
        dependencies=[Depends(require_token)],
        responses=ORDER_RESPONSES,
    )
    def post_route(flight_id: str, body: schemas.RouteRequest):
        """Replace a flight's route, takeoff to landing (needs the API token). Waypoints
        already flown and the landing cannot move; the target only for CAP/AWACS/tanker.
        A flight in the game gets the new route at once; seated players get a new briefing.

        \f

        Args:
            flight_id: The flight.
            body: The new route.

        Returns:
            The flight's view.
        """
        from ..systems import orders

        with engine.lock:
            if body.faction is not None:
                viewer(body.faction)
            pts = [point(p) for p in body.waypoints]
            f = order(orders.set_route, state(), flight_id, pts, body.target_index, body.faction)
            engine.brief_players([f.id])
            return views.flight_view(state(), f)

    @app.post(
        "/api/ato/packages/{package_id}/tot",
        response_model=schemas.PackageView,
        tags=[Tag.AIR],
        dependencies=[Depends(require_token)],
        responses=ORDER_RESPONSES,
    )
    def post_tot(package_id: str, body: schemas.TotRequest):
        """Move the time on target of a package still on the ground (needs the API token).

        \f

        Args:
            package_id: The package.
            body: The new time on target.

        Returns:
            The package's view.
        """
        from ..systems import orders

        with engine.lock:
            pkg = order(orders.set_tot, state(), package_id, body.tot, body.faction)
            engine.brief_players(pkg.flights)
            return views.package_view(state(), pkg)

    @app.post(
        "/api/ato/packages/{package_id}/cancel",
        response_model=schemas.CancelView,
        tags=[Tag.AIR],
        dependencies=[Depends(require_token)],
        responses=ORDER_RESPONSES,
    )
    def post_cancel(package_id: str, body: schemas.CancelRequest):
        """Cancel the flights of a package still on the ground (needs the API token);
        airborne ones carry on.

        \f

        Args:
            package_id: The package.
            body: The ordering faction.

        Returns:
            The callsigns of the cancelled flights.
        """
        from ..systems import orders

        with engine.lock:
            pkg = state().packages.get(package_id)
            seated = [
                (s.player_id, state().flights[i].callsign)
                for i in (pkg.flights if pkg else []) if i in state().flights
                for s in state().flights[i].active_seats
            ]
            cancelled = order(orders.cancel_package, state(), package_id, body.faction)
            for pid, callsign in seated:
                if callsign in cancelled:
                    engine.notify(pid, f"{callsign} has been cancelled. Pick another flight from the ATO menu.")
            return {"cancelled": cancelled}

    @app.get("/api/structures", response_model=schemas.StructuresView, tags=[Tag.AIR])
    def get_structures():
        """Structures that can be struck (depots, airbase facilities, command
        posts) and their health. They are fixed, so every faction knows them.

        \f

        Returns:
            The structures view.
        """
        with engine.lock:
            return views.structures(state())

    @app.get("/api/airdefense",response_model=schemas.AirDefenseView, tags=[Tag.AIR])
    def get_air_defense(faction: str | None = FACTION):
        """SAM sites and early-warning radars, their state, and the IADS network links.

        \f

        Args:
            faction: Faction whose fog-of-war view to give (None: the umpire's).

        Returns:
            The air defence view.
        """
        with engine.lock:
            return views.air_defense(state(), viewer(faction))

    @app.get("/api/intel", response_model=schemas.IntelView, tags=[Tag.INTEL])
    def get_intel(faction: str = Query(..., description="faction whose intelligence picture to give")):
        """Fog of war: the contacts a faction holds on the enemy (last known
        position, age, source, identification) and its air-search sensors.

        \f

        Args:
            faction: Faction whose intelligence picture to give.

        Returns:
            The intelligence view.
        """
        with engine.lock:
            return views.intel_view(state(), viewer(faction))

    @app.get("/api/events", response_model=schemas.EventsPage, tags=[Tag.EVENTS])
    def get_events(
        since: int = Query(0, ge=0, description="first event seq to return"),
        kinds: str | None = Query(None, description="comma separated event kinds to keep"),
        limit: int = Query(500, ge=1, le=5000),
        faction: str | None = FACTION,
    ):
        """Campaign events in order, paged: call again with `since=next`.

        \f

        Args:
            since: First event sequence number to return.
            kinds: Comma separated event kinds to keep (None: all).
            limit: Maximum number of events.
            faction: Faction whose fog-of-war view to give (None: the umpire's).

        Returns:
            A page of events, with the ``next`` sequence number.
        """
        wanted = _event_kinds(kinds)
        with engine.lock:
            return views.events(state(), since, wanted, limit, viewer(faction))

    @app.get("/api/snapshot", response_model=schemas.SnapshotView, tags=[Tag.CAMPAIGN])
    def get_snapshot(faction: str | None = FACTION):
        """Everything above in one document.

        \f

        Args:
            faction: Faction whose fog-of-war view to give (None: the umpire's).

        Returns:
            The whole campaign in one document.
        """
        with engine.lock:
            snap = views.snapshot(state(), engine.live, views.players(engine), viewer(faction), views.control(engine))
        snap["campaign"]["save"] = views.save_view(saver)
        return snap

    @app.get(
        "/api/stream",
        tags=[Tag.EVENTS],
        response_class=StreamingResponse,
        responses=SSE_RESPONSE,
    )
    async def stream(
        request: Request,
        since: int | None = Query(None, ge=0, description="replay from this seq"),
        faction: str | None = FACTION,
    ):
        """Server-Sent Events: `event` for each campaign event (data = Event, id = seq) and
        `tick` every second (data = Tick). Without `since` only new events are sent.

        \f

        Args:
            request: The HTTP request (to notice the client leaving).
            since: Replay from this sequence number (None: only new events).
            faction: Faction whose fog-of-war view to give (None: the umpire's).

        Returns:
            The Server-Sent Events response.
        """
        viewer(faction)

        def read(seq: int):
            """Read the next batch of events and the clock under the engine lock.

            Args:
                seq: First sequence number to read.

            Returns:
                The batch of events and the clock.
            """
            with engine.lock:
                batch = views.events(engine.state, seq, limit=1000, viewer=faction)
                tick = {"time": views.time_view(engine.state.time), "live": engine.live, **views.control(engine)}
                return batch, tick

        async def gen() -> AsyncIterator[str]:
            """Server-Sent Events generator.

            Yields:
                SSE messages: event batches and clock ticks.
            """
            seq = since if since is not None else len(engine.state.events)
            app.state.active_streams += 1
            try:
                while not await request.is_disconnected():
                    batch, tick = await anyio.to_thread.run_sync(read, seq)
                    seq = batch["next"]
                    # One write per batch: cheaper, and a client that just left
                    # costs at most one failed send.
                    yield "".join(_sse(SseEvent.EVENT, e, e["seq"]) for e in batch["events"]) + _sse(SseEvent.TICK, tick)
                    await asyncio.sleep(STREAM_TICK_S)
            finally:
                app.state.active_streams -= 1

        return StreamingResponse(gen(), media_type=SSE_MEDIA_TYPE, headers=NO_STORE)

    @app.get(
        "/api/positions/stream",
        tags=[Tag.MAP],
        response_class=StreamingResponse,
        responses=SSE_RESPONSE,
    )
    async def positions_stream(
        request: Request,
        hz: float = Query(5.0, ge=0.5, le=20.0, description="samples per second"),
        faction: str | None = FACTION,
    ):
        """Server-Sent Events: `positions` events (data = Positions) at `hz` per
        second, for smooth animation: the client interpolates between samples.
        Only what moves, as `id -> [lat, lon]`; fog of war applies with `faction`.

        \f

        Args:
            request: The HTTP request (to notice the client leaving).
            hz: Samples per second.
            faction: Faction whose fog-of-war view to give (None: the umpire's).

        Returns:
            The Server-Sent Events response.
        """
        viewer(faction)
        period = 1.0 / hz

        def read():
            """One sample under the engine lock.

            Returns:
                The positions view.
            """
            with engine.lock:
                return views.positions(engine.state, faction)

        async def gen() -> AsyncIterator[str]:
            """Positions generator: sample, send, wait for the rest of the period.

            Yields:
                One SSE message per sample.
            """
            app.state.active_streams += 1
            loop = asyncio.get_running_loop()
            try:
                while not await request.is_disconnected():
                    start = loop.time()
                    yield _sse(SseEvent.POSITIONS, await anyio.to_thread.run_sync(read))
                    await asyncio.sleep(max(0.01, period - (loop.time() - start)))
            finally:
                app.state.active_streams -= 1

        return StreamingResponse(gen(), media_type=SSE_MEDIA_TYPE, headers=NO_STORE)

    @app.get("/api/positions", response_model=schemas.PositionsView, tags=[Tag.MAP])
    def get_positions(faction: str | None = FACTION):
        """One sample of the positions stream.

        \f

        Args:
            faction: Faction whose fog-of-war view to give (None: the umpire's).

        Returns:
            The positions view.
        """
        with engine.lock:
            return views.positions(state(), viewer(faction))

    @app.get("/api/control", response_model=schemas.ControlView, tags=[Tag.CAMPAIGN])
    def get_control():
        """Pause and speed of the campaign clock.

        \f

        Returns:
            Pause and compression.
        """
        with engine.lock:
            return views.control(engine)

    @app.post(
        "/api/control",
        response_model=schemas.ControlView,
        tags=[Tag.CAMPAIGN],
        dependencies=[Depends(require_token)],
        responses=UNAUTHORIZED,
    )
    def post_control(body: schemas.ControlRequest):
        """Pause / resume and fast-forward the campaign (needs the API token).
        Refused (409) while a game simulates part of the campaign: then time must run 1:1.

        \f

        Args:
            body: The new pause state and/or compression.

        Returns:
            Pause and compression after the change.
        """
        with engine.lock:
            if engine.live and (body.paused or (body.compression not in (None, engine.control.compression))):
                raise HTTPException(status_code=409, detail="a game is live: time runs 1:1 and cannot be paused or accelerated")
            try:
                engine.control.set(body.paused, body.compression)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            engine.state.emit(EventKind.TIME_CONTROL, paused=engine.control.paused, compression=engine.control.compression)
            return views.control(engine)

    @app.get("/api/save", response_model=schemas.SaveView, tags=[Tag.CAMPAIGN])
    def get_save():
        """Whether the server saves the campaign, where, and the latest save.

        \f

        Returns:
            The save status.
        """
        return views.save_view(saver)

    @app.post(
        "/api/save",
        response_model=schemas.SaveView,
        tags=[Tag.CAMPAIGN],
        dependencies=[Depends(require_token)],
        responses={**UNAUTHORIZED, 409: {"description": "server started without --save"}},
    )
    def post_save():
        """Save the campaign now to the server's save file (needs the API token).
        409 if the server was started without `--save`, 500 if the file cannot be written.

        \f

        Returns:
            The save status, with the new save.
        """
        if saver is None:
            raise HTTPException(status_code=409, detail="saving is disabled: start the server with --save FILE")
        try:
            saver.save(engine, SaveTrigger.API)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"save failed: {exc}") from exc
        return views.save_view(saver)

    @app.get("/api", tags=[Tag.CAMPAIGN])
    def index():
        """Entry point: list of endpoints.

        \f

        Returns:
            API version, documentation links and the list of endpoints.
        """
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


def _sse(event: SseEvent, data, event_id: int | None = None) -> str:
    """Format one Server-Sent Event.

    Args:
        event: Event name.
        data: JSON-serialisable payload.
        event_id: Event id, for clients resuming the stream (None: no id).

    Returns:
        The event, ready to write.
    """
    msg = f"event: {event}\n"
    if event_id is not None:
        msg += f"id: {event_id}\n"
    return msg + f"data: {json.dumps(data, default=str, separators=(',', ':'))}\n\n"
