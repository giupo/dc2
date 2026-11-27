"""Saving and resuming a campaign."""

import gzip
import io
import pickle

import pytest

from dynacamp import persistence
from dynacamp.cli import main
from dynacamp.engine import Engine
from dynacamp.events import EventKind
from dynacamp.air import TargetKind
from dynacamp.forces import Authority, EntityKind
from dynacamp.state import BattleKind
from dynacamp.systems.ato import SeatState

from .conftest import DEMO
from .test_air import first_flight, live_engine


def test_resumed_campaign_continues_exactly_like_the_original(demo, tmp_path):
    engine = Engine(demo)
    engine.run_for(12 * 3600)
    path = persistence.save(demo, tmp_path / "c.dcsave", compression=600)

    resumed, info = persistence.load(path)
    assert info.name == demo.name and info.campaign_time == demo.time and info.compression == 600
    assert resumed.events.of_kind("campaign_resumed")

    engine.run_for(24 * 3600)
    Engine(resumed).run_for(24 * 3600)
    assert resumed.snapshot() == demo.snapshot()
    assert [e.kind for e in resumed.events if e.kind != "campaign_resumed"] == [e.kind for e in demo.events]


def test_saving_leaves_subscribers_behind(line, tmp_path):
    seen = []
    line.events.subscribe(seen.append)
    resumed, _ = persistence.load(persistence.save(line, tmp_path / "c.dcsave"))
    resumed.emit(EventKind.TIME_CONTROL)
    assert not [e for e in seen if e.kind == EventKind.TIME_CONTROL]
    line.emit(EventKind.CAMPAIGN_SAVED)
    assert seen[-1].kind == EventKind.CAMPAIGN_SAVED


def test_what_the_game_held_comes_back_to_the_campaign(tmp_path):
    state, game, engine = live_engine()
    f = first_flight(state, "blue")
    game.join("Pilot", f.id)
    engine.step(1)
    assert state.player_flights
    state.divisions[next(iter(state.divisions))].authority = Authority.DELEGATED

    resumed, _ = persistence.load(persistence.save(state, tmp_path / "c.dcsave"))
    assert all(e.authority is Authority.CAMPAIGN and e.game_position is None for e in resumed.entities())
    assert not resumed.player_flights and not resumed.player_positions
    rf = resumed.flights[f.id]
    assert rf.players["Pilot"].state is SeatState.LEFT
    # the live campaign is untouched by saving
    assert state.player_flights


def test_atomic_write_keeps_the_old_save_if_writing_fails(line, tmp_path, monkeypatch):
    path = persistence.save(line, tmp_path / "c.dcsave")
    before = path.read_bytes()

    def boom(*_):
        raise OSError("disk full")

    monkeypatch.setattr(persistence.os, "replace", boom)
    with pytest.raises(OSError):
        persistence.save(line, path)
    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["c.dcsave"]


@pytest.mark.parametrize(
    "content",
    [b"not gzip", gzip.compress(b"not a pickle"), gzip.compress(pickle.dumps({"format": "other"}))],
    ids=["text", "garbage", "foreign"],
)
def test_bad_files_are_rejected(tmp_path, content):
    path = tmp_path / "bad.dcsave"
    path.write_bytes(content)
    with pytest.raises(persistence.SaveError):
        persistence.load(path)


def test_other_format_versions_are_rejected(line, tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "FORMAT_VERSION", 0)
    path = persistence.save(line, tmp_path / "old.dcsave")
    monkeypatch.undo()
    with pytest.raises(persistence.SaveError, match="format 0"):
        persistence.load(path)


def test_is_save_tells_saves_from_scenarios(line, tmp_path):
    assert persistence.is_save(persistence.save(line, tmp_path / "c.dcsave"))
    assert not persistence.is_save(DEMO)
    assert not persistence.is_save(tmp_path / "missing")


def test_cli_simulate_saves_and_resumes(tmp_path, capsys):
    save = tmp_path / "c.dcsave"
    main(["simulate", str(DEMO), "--days", "0.5", "--report-hours", "12", "--quiet", "--save", str(save)])
    first, _ = persistence.load(save)
    main(["simulate", str(save), "--days", "0.5", "--report-hours", "12", "--quiet", "--save", str(save)])
    second, _ = persistence.load(save)
    assert second.time == pytest.approx(first.time + 12 * 3600)
    assert "Resumed" in capsys.readouterr().out


def test_cli_reports_unreadable_saves(tmp_path):
    bad = tmp_path / "bad.dcsave"
    bad.write_bytes(gzip.compress(b"junk"))
    with pytest.raises(SystemExit) as exc:
        main(["simulate", str(bad), "--days", "0.1"])
    assert exc.value.code == 1


def test_saver_autosave_interval(line, tmp_path):
    now = [0.0]
    saver = persistence.Saver(tmp_path / "c.dcsave", autosave_s=600, clock=lambda: now[0])
    assert not saver.due()
    now[0] = 600
    assert saver.due()
    record = saver.save(Engine(line), persistence.SaveTrigger.AUTO)
    assert saver.last == record and not saver.due() and saver.path.exists()
    assert not persistence.Saver(tmp_path / "x", autosave_s=0, clock=lambda: 1e9).due()


def test_saves_from_before_a_new_state_field_still_load(line, tmp_path):
    path = persistence.save(line, tmp_path / "c.dcsave")
    with gzip.open(path, "rb") as fh:
        envelope = pickle.load(fh)
    del envelope["state"].__dict__["archive"]  # as written before the field existed
    with gzip.open(path, "wb") as fh:
        pickle.dump(envelope, fh)
    resumed, _ = persistence.load(path)
    assert resumed.archive == {}


def test_saves_with_plain_strings_for_enums_still_load(demo, tmp_path):
    Engine(demo).run_for(12 * 3600)
    assert demo.battles and demo.flights
    for b in demo.battles.values():
        b.kind = str(b.kind)  # as written before the fields were enums
    for ent in demo.entities():
        ent.kind = str(ent.kind)
    for f in demo.flights.values():
        f.target_kind = str(f.target_kind)
    resumed, _ = persistence.load(persistence.save(demo, tmp_path / "c.dcsave"))
    assert all(type(b.kind) is BattleKind for b in resumed.battles.values())
    assert all(type(e.kind) is EntityKind for e in resumed.entities())
    assert all(type(f.target_kind) is TargetKind for f in resumed.flights.values())


def test_renamed_classes_are_found_under_their_new_name():
    found = persistence._Unpickler(io.BytesIO()).find_class("dynacamp.systems.intel", "ContactKind")
    assert found is EntityKind
