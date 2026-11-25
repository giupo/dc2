"""Command line entry point.

    dynacamp simulate scenarios/western_georgia.toml --days 5
    dynacamp serve scenarios/western_georgia.toml --port 50555 --compression 60
    dynacamp serve scenarios/syria_m5.toml --save syria.dcsave  # resumes syria.dcsave if it exists
    dynacamp lua-path
"""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import signal
import time
from collections import Counter
from pathlib import Path

from dynacamp.units import UnitDomain

from . import persistence, scenario
from .engine import Engine
from .state import CampaignState
from .units import UnitDomain


NOTABLE = {
    "battle_started",
    "battle_ended",
    "contact_broken",
    "province_captured",
    "division_retreated",
    "division_destroyed",
    "objective_changed",
    "player_joined",
    "player_left",
    "handoff",
    "handoff_failed",
    "handoff_timeout",
    "game_disconnected",
    "package_planned",
    "air_strike",
    "air_engagement",
    "aircraft_lost",
    "flight_aborted",
    "flight_rtb",
    "flight_rerouted",
    "scramble",
    "flight_cancelled",
    "seat_taken",
    "seat_released",
}


from .events import clock as _clock


def _summary(state: CampaignState) -> str:
    """Text summary of the campaign: provinces, battles, convoys and every division."""
    ctrl = Counter(p.controller for p in state.theater.provinces.values())
    lines = [f"--- {_clock(state.time)}  provinces {dict(ctrl)}  battles {len(state.battles)}  convoys {len(state.convoys)}"]

    for d in sorted(state.divisions.values(), key=lambda d: (d.faction, d.id)):
        lines.append(
            f"    {d.faction:5} {d.id:4} {d.name[:28]:28} @{d.location:11} str {d.strength:4.0%} "
            f"org {d.org:4.0%} sup {d.supply_ratio:4.0%} {d.order.kind.value}"
            + (f"->{d.order.target}" if d.order.target else "")
            + (f" [{d.authority.value}]" if d.authority.value != "campaign" else "")
        )

    return "\n".join(lines)


def _print_events(state: CampaignState, verbose: bool) -> None:
    """Print notable (or all) events as they happen."""
    def show(e) -> None:
        """Print one event."""
        if verbose or e.kind in NOTABLE:
            print(f"{_clock(e.time)}  {e.kind:20} {e.data}")

    state.events.subscribe(show)


def _load_campaign(path: str) -> tuple[CampaignState, persistence.SaveInfo | None]:
    """Load a scenario (``.toml``) or resume a save file.

    Args:
        path: Scenario or save file.

    Returns:
        The campaign, and the save's details when resuming (None for a scenario).
    """
    if persistence.is_save(path):
        state, info = persistence.load(path)
        print(f"Resumed '{info.name}' at {_clock(info.campaign_time)} from {path} "
              f"(saved {time.strftime('%Y-%m-%d %H:%M', time.localtime(info.saved_at))} by {info.version})", flush=True)
        return state, info
    
    return scenario.load(path), None


def campaign_seed(flag: int | None) -> tuple[int, str] | None:
    """Random seed asked for: ``$DYNACAMP_SEED`` first (it wins over everything),
    else ``--seed``; None if neither is given.

    Args:
        flag: Value of ``--seed`` (None: not given).

    Returns:
        (seed, where it came from), or None.

    Raises:
        SystemExit: ``$DYNACAMP_SEED`` is not an integer.
    """
    env = os.environ.get("DYNACAMP_SEED", "").strip()
    if env:
        try:
            return int(env), "DYNACAMP_SEED"
        except ValueError:
            raise SystemExit(f"DYNACAMP_SEED must be an integer, not {env!r}") from None
    if flag is not None:
        return flag, "--seed"
    return None


def apply_seed(state: CampaignState, flag: int | None, resumed: bool) -> None:
    """Seed the campaign's random generator and record it in ``state.seed``.

    The seed is ``$DYNACAMP_SEED``, else ``--seed`` (see :func:`campaign_seed`),
    else, for a new campaign, generated from the clock (Unix time in seconds)
    and printed, so that the same campaign can be run again with ``--seed``.
    Scenario files carry no seed. A resumed save without an explicit seed keeps
    its saved random sequence; with one, it goes on with the new sequence.

    Args:
        state: The campaign just loaded.
        flag: Value of ``--seed``.
        resumed: The campaign comes from a save file.
    """
    asked = campaign_seed(flag)
    if asked is None:
        if resumed:
            return  # the save's own random sequence goes on
        asked = int(time.time()), "the clock (repeat this campaign with --seed)"
    seed, source = asked
    state.seed = seed
    state.rng.seed(seed)
    note = " (resumed save: the random sequence restarts from it)" if resumed else ""
    print(f"Seed {seed} from {source}{note}", flush=True)


def cmd_simulate(args: argparse.Namespace) -> None:
    """``dynacamp simulate``: run a campaign headless as fast as possible."""
    state, info = _load_campaign(args.scenario)
    apply_seed(state, args.seed, resumed=info is not None)

    if not args.quiet:
        _print_events(state, args.verbose)

    engine = Engine(state)

    for _ in range(int(args.days * 24 / args.report_hours)):
        engine.run_for(args.report_hours * 3600)
        print(_summary(state))

    if args.events:
        state.events.write_jsonl(Path(args.events))
        print(f"{len(state.events)} events written to {args.events}")

    if args.save:
        persistence.save(state, args.save)
        print(f"Campaign saved to {args.save} at {_clock(state.time)}")


def serve_compression(flag: float | None, saved: float | None) -> float:
    """Time compression ``serve`` starts with: ``--compression``, else
    ``$DYNACAMP_COMPRESSION`` (handy in Docker), else the save's, else 60.

    Args:
        flag: Value of ``--compression`` (None: not given).
        saved: Compression stored in the save being resumed (None or 0: none).

    Returns:
        The compression.

    Raises:
        SystemExit: The value is not a number in the allowed range.
    """
    from .engine import MAX_COMPRESSION, MIN_COMPRESSION

    env = os.environ.get("DYNACAMP_COMPRESSION", "").strip()
    if flag is not None:
        value, source = flag, "--compression"
    elif env:
        try:
            value = float(env)
        except ValueError:
            raise SystemExit(f"DYNACAMP_COMPRESSION must be a number, not {env!r}") from None
        source = "DYNACAMP_COMPRESSION"
    else:
        return saved or 60.0
    if not MIN_COMPRESSION <= value <= MAX_COMPRESSION:
        raise SystemExit(f"{source} must be between {MIN_COMPRESSION:g} and {MAX_COMPRESSION:g}, not {value:g}")
    return value


def cmd_serve(args: argparse.Namespace) -> None:
    """``dynacamp serve``: real-time campaign with the DCS adapter, the API and the web UI."""
    from .adapters.dcs import LUA_BRIDGE, DcsAdapter
    from .runtime import run_realtime

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    source = args.scenario

    if args.save and not args.new and Path(args.save).exists() and not persistence.is_save(source):
        # Never overwrite a running campaign with a fresh one by accident.
        source = args.save

    state, info = _load_campaign(source)
    apply_seed(state, args.seed, resumed=info is not None)
    compression = serve_compression(args.compression, info.compression if info else None)
    if args.units:
        state.units = args.units

    if not args.quiet:
        _print_events(state, args.verbose)

    adapter = None

    if not args.no_dcs:
        adapter = DcsAdapter(args.host, args.port)
        adapter.start()
        print(f"Waiting for DCS on {args.host}:{adapter.port}. Bridge script: {LUA_BRIDGE}")

    engine = Engine(state, adapter)
    saver = persistence.Saver(args.save, args.autosave_minutes * 60) if args.save else None
    api = None

    if args.api_port:
        from .api import ApiServer

        token = args.api_token or os.environ.get("DYNACAMP_API_TOKEN") or secrets.token_urlsafe(18)
        api = ApiServer(engine, args.api_host, args.api_port, web_dir=args.web_dir, token=token, saver=saver)
        api.start()
        host = "localhost" if api.host in ("0.0.0.0", "::") else api.host
        print(f"API on http://{host}:{api.port}/api  (docs /docs)")
        if args.api_token or os.environ.get("DYNACAMP_API_TOKEN"):
            print("API token: the one given (--api-token / DYNACAMP_API_TOKEN)")
        else:
            print(f"API token (new at every start; fix it with DYNACAMP_API_TOKEN): {token}")
        if Path(args.web_dir).is_dir():
            # The UI takes the token from the fragment, which never reaches the server.
            print(f"Web UI on http://{host}:{api.port}/#token={token}", flush=True)
    last_report = [state.time]
    log = logging.getLogger(__name__)

    def save_now(e: Engine, trigger: str) -> None:
        """Save to ``--save`` and log it."""
        assert saver is not None
        record = saver.save(e, trigger)
        log.info("campaign saved to %s at %s (%s)", saver.path, _clock(record.campaign_time), trigger)

    def on_tick(e: Engine) -> None:
        """Print a summary every ``--report-hours``; autosave every ``--autosave-minutes``."""
        if e.state.time - last_report[0] >= args.report_hours * 3600:
            last_report[0] = e.state.time
            print(_summary(e.state))
        if saver is not None and saver.due():
            try:
                save_now(e, "auto")
            except OSError as exc:  # a full disk must not stop the campaign
                log.error("autosave to %s failed: %s", saver.path, exc)

    def stop(signum: int, _frame: object) -> None:
        """SIGTERM (systemd, ``docker stop``) stops the server like Ctrl-C: cleanly, saving."""
        raise KeyboardInterrupt(signal.Signals(signum).name)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)  # also when started with SIGINT ignored (background jobs)

    try:
        run_realtime(
            engine,
            compression=compression,
            tick_s=1.0 / args.tick_hz,
            on_tick=on_tick
        )

    except KeyboardInterrupt:
        pass

    finally:
        if adapter is not None:
            adapter.close()

        if api is not None:
            api.close()

        if args.events:
            state.events.write_jsonl(Path(args.events))

        if saver is not None:
            save_now(engine, "exit")
            print(f"Campaign saved to {args.save}")


def cmd_build_miz(args: argparse.Namespace) -> None:
    """``dynacamp build-miz``: generate the DCS mission of each scenario."""
    try:
        from .adapters.dcs.miz import write_mission

    except ImportError:
        raise SystemExit("pydcs is required: uv sync --extra miz") from None

    for path in args.scenarios:
        state = scenario.load(path)
        out = Path(args.out_dir) / (Path(path).stem + ".miz")
        notes = write_mission(state, out, port=args.port)
        print(f"{out}")

        for n in notes:
            print(f"   note: {n}")


def cmd_radio(args: argparse.Namespace) -> None:
    """``dynacamp radio``: regenerate the airfields' ATC frequencies from DCS' data."""
    try:
        from .adapters.dcs.miz import generate_radio_data
    except ImportError as exc:
        raise SystemExit(f"needs pydcs (uv sync --extra miz): {exc}") from exc
    print(f"Airfield frequencies written to {generate_radio_data()}")


def cmd_loadouts(args: argparse.Namespace) -> None:
    """``dynacamp loadouts``: regenerate the aircraft loadouts from DCS' pylon tables."""
    try:
        from .adapters.dcs.miz import generate_loadouts
    except ImportError:
        raise SystemExit("pydcs is required: uv sync --extra miz") from None
    print(f"Loadouts written to {generate_loadouts()}")


def cmd_roads(args: argparse.Namespace) -> None:
    """``dynacamp roads``: route scenario links on real roads."""
    from . import roads

    for path in args.scenarios:
        print(f"Routing links of {path} on OpenStreetMap roads (OSRM)...")
        out = roads.build(path)
        print(f"-> {out}")


def cmd_openapi(args: argparse.Namespace) -> None:
    """``dynacamp openapi``: print or save the OpenAPI schema."""
    import json

    from .api import create_app

    schema = json.dumps(create_app(None).openapi(), indent=2)
    if args.output:
        Path(args.output).write_text(schema + "\n", encoding="utf-8")
        print(f"OpenAPI schema written to {args.output}")

    else:
        print(schema)


def main(argv: list[str] | None = None) -> None:
    """Entry point of the ``dynacamp`` command.

    Args:
        argv: Arguments (default: ``sys.argv``).
    """
    p = argparse.ArgumentParser(prog="dynacamp", description="Dynamic campaign engine")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("simulate", help="run a campaign headless, as fast as possible")
    s.add_argument("scenario", help="scenario (.toml) or save file to resume")
    s.add_argument("--days", type=float, default=5)
    s.add_argument("--report-hours", type=float, default=24)
    s.add_argument("--seed", type=int,
                   help="random seed (default: from the clock, printed at start; $DYNACAMP_SEED wins over this flag)")
    s.add_argument("--events", help="write the event log to this JSONL file")
    s.add_argument("--quiet", action="store_true", help="only print periodic summaries")
    s.add_argument("--verbose", action="store_true", help="print every event")
    s.add_argument("--save", help="save the campaign to this file at the end")
    s.set_defaults(func=cmd_simulate)

    v = sub.add_parser("serve", help="run in real time with the DCS adapter")
    v.add_argument("scenario", help="scenario (.toml) or save file to resume")
    v.add_argument("--host", default="127.0.0.1")
    v.add_argument("--port", type=int, default=50555)
    v.add_argument("--compression", type=float,
                   help="time compression with no players (default: $DYNACAMP_COMPRESSION, else the save's, else 60)")
    v.add_argument("--seed", type=int,
                   help="random seed (default: from the clock, printed at start; $DYNACAMP_SEED wins over this flag)")
    v.add_argument("--tick-hz", type=float, default=10.0,
                   help="engine steps per real second (smoother movement, more CPU)")
    v.add_argument("--units", choices=[UnitDomain.IMPERIAL, UnitDomain.METRIC],
                   help="units of briefings and map marks for players (default: the scenario's, else imperial)")
    v.add_argument("--report-hours", type=float, default=1)
    v.add_argument("--events")
    v.add_argument("--verbose", action="store_true")
    v.add_argument("--quiet", action="store_true", help="do not print events")
    v.add_argument("--save", help="save the campaign to this file periodically and on exit")
    v.add_argument("--autosave-minutes", type=float, default=10.0,
                   help="real minutes between autosaves with --save (0: only on exit)")
    v.add_argument("--new", action="store_true",
                   help="start the scenario afresh even if the --save file exists (overwrites it)")
    v.add_argument("--no-dcs", action="store_true", help="run without the DCS adapter (e.g. to develop the web UI)")
    v.add_argument("--api-host", default="127.0.0.1")
    v.add_argument("--api-port", type=int, default=8080, help="HTTP API port (0 disables it)")
    v.add_argument("--api-token",
                   help="token for API commands (default: $DYNACAMP_API_TOKEN, else a random one printed at start)")
    v.add_argument("--web-dir", default="web/dist", help="built front-end to serve at / (if it exists)")
    v.set_defaults(func=cmd_serve)

    b = sub.add_parser("build-miz", help="generate a DCS mission for each scenario (needs pydcs)")
    b.add_argument("scenarios", nargs="+")
    b.add_argument("--out-dir", default="missions")
    b.add_argument("--port", type=int, default=50555)
    b.set_defaults(func=cmd_build_miz)

    lo = sub.add_parser("loadouts", help="regenerate aircraft loadouts from DCS pylon tables (needs pydcs)")
    lo.set_defaults(func=cmd_loadouts)
    ra = sub.add_parser("radio", help="regenerate the airfields' ATC frequencies from DCS data (needs pydcs)")
    ra.set_defaults(func=cmd_radio)

    r = sub.add_parser("roads", help="route scenario links on real roads (OSRM, needs network) -> <scenario>.roads.json")
    r.add_argument("scenarios", nargs="+")
    r.set_defaults(func=cmd_roads)

    o = sub.add_parser("openapi", help="print the HTTP API OpenAPI schema (for front-end type generation)")
    o.add_argument("-o", "--output")
    o.set_defaults(func=cmd_openapi)

    lp = sub.add_parser("lua-path", help="print the path of the DCS Lua bridge script")
    lp.set_defaults(func=lambda a: print(__import__("dynacamp.adapters.dcs", fromlist=["LUA_BRIDGE"]).LUA_BRIDGE))

    args = p.parse_args(argv)
    try:
        args.func(args)
    except persistence.SaveError as exc:
        p.exit(1, f"dynacamp: {exc}\n")


if __name__ == "__main__":
    main()
