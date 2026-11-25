"""The event catalogue: every event is an EventKind, and every EventKind is used."""

import ast
import pickle
from dataclasses import replace
from pathlib import Path

import pytest

from dynacamp.events import NOTABLE, PUBLIC, Event, EventKind, EventLog

SRC = Path(__file__).resolve().parents[1] / "src" / "dynacamp"


def _emit_kinds() -> tuple[set[str], list[str]]:
    """Kinds named at every ``.emit(...)`` call in the package, and the calls
    that name one with something other than an ``EventKind.X`` member."""
    used: set[str] = set()
    bad: list[str] = []
    for path in SRC.rglob("*.py"):
        if path.name.startswith(".#"):  # editor lock files
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "emit"):
                continue
            arg = next(
                (a for a in node.args[:2] if not (isinstance(a, ast.Name) and a.id in ("time", "kind"))),
                None,
            )
            if arg is None:  # forwarding wrapper: state.emit -> log.emit(self.time, kind, ...)
                continue
            if (
                isinstance(arg, ast.Attribute)
                and isinstance(arg.value, ast.Name)
                and arg.value.id == "EventKind"
            ):
                used.add(arg.attr)
            elif not (isinstance(arg, ast.Attribute) and arg.attr == "time"):
                bad.append(f"{path.relative_to(SRC)}:{node.lineno} {ast.unparse(arg)}")
    return used, bad


def test_every_emit_names_an_event_kind_member():
    used, bad = _emit_kinds()
    assert not bad, f"emit() with a string or expression instead of EventKind.X: {bad}"
    unknown = used - set(EventKind.__members__)
    assert not unknown


def test_every_event_kind_is_emitted_somewhere():
    used, _ = _emit_kinds()
    assert set(EventKind.__members__) - used == set(), "dead EventKind members"


def test_classifications_are_subsets_of_the_catalogue():
    assert NOTABLE <= set(EventKind) and PUBLIC <= set(EventKind)


def test_emit_rejects_unknown_kinds_and_accepts_member_strings():
    log = EventLog()
    with pytest.raises(ValueError):
        log.emit(0.0, "flight_abortd")
    ev = log.emit(0.0, "flight_aborted", flight="fl1")
    assert ev.kind is EventKind.FLIGHT_ABORTED
    assert ev.kind == "flight_aborted"  # still a plain string to JSON and the API


def test_old_saves_get_their_kinds_upgraded():
    log = EventLog()
    log.emit(0.0, EventKind.SCRAMBLE)
    # as an old save had it: plain strings, one of them a kind since retired
    log._events = [replace(log._events[0], kind="scramble"), Event(seq=1, time=1.0, kind="war_declared")]
    restored = pickle.loads(pickle.dumps(log))
    kinds = [e.kind for e in restored]
    assert kinds[0] is EventKind.SCRAMBLE
    assert kinds[1] == "war_declared" and not isinstance(kinds[1], EventKind)


def test_events_endpoint_validates_kinds(demo):
    from fastapi.testclient import TestClient

    from dynacamp.api import create_app
    from dynacamp.engine import Engine

    with TestClient(create_app(Engine(demo))) as c:
        r = c.get("/api/events", params={"kinds": "province_captured,flight_abortd"})
        assert r.status_code == 422 and "flight_abortd" in r.text
        assert c.get("/api/events", params={"kinds": "province_captured, package_planned"}).status_code == 200
