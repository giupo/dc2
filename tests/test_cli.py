"""Command line options."""

import pytest

from dynacamp.cli import serve_compression


def test_compression_flag_then_environment_then_save(monkeypatch):
    monkeypatch.delenv("DYNACAMP_COMPRESSION", raising=False)
    assert serve_compression(None, None) == 60.0
    assert serve_compression(None, 300.0) == 300.0
    monkeypatch.setenv("DYNACAMP_COMPRESSION", " 900 ")
    assert serve_compression(None, 300.0) == 900.0
    assert serve_compression(10.0, 300.0) == 10.0
    monkeypatch.setenv("DYNACAMP_COMPRESSION", "")
    assert serve_compression(None, 300.0) == 300.0


@pytest.mark.parametrize("value", ["fast", "0", "100000"])
def test_bad_compression_from_the_environment_is_refused(monkeypatch, value):
    monkeypatch.setenv("DYNACAMP_COMPRESSION", value)
    with pytest.raises(SystemExit, match="DYNACAMP_COMPRESSION"):
        serve_compression(None, None)


def test_seed_environment_wins_over_flag_over_scenario(monkeypatch):
    from dynacamp.cli import campaign_seed

    monkeypatch.delenv("DYNACAMP_SEED", raising=False)
    assert campaign_seed(None) is None
    assert campaign_seed(5) == (5, "--seed")
    monkeypatch.setenv("DYNACAMP_SEED", " 42 ")
    assert campaign_seed(5) == (42, "DYNACAMP_SEED")
    assert campaign_seed(None) == (42, "DYNACAMP_SEED")
    monkeypatch.setenv("DYNACAMP_SEED", "")
    assert campaign_seed(5) == (5, "--seed")
    monkeypatch.setenv("DYNACAMP_SEED", "lucky")
    with pytest.raises(SystemExit, match="DYNACAMP_SEED"):
        campaign_seed(None)


def _events(argv, tmp_path, name):
    from dynacamp.cli import main

    out = tmp_path / f"{name}.jsonl"
    main(["simulate", *argv, "--days", "0.5", "--report-hours", "12", "--quiet", "--events", str(out)])
    return out.read_text()


def test_simulate_seed_flag_environment_and_clock(monkeypatch, tmp_path, capsys):
    import re

    from .conftest import DEMO

    monkeypatch.delenv("DYNACAMP_SEED", raising=False)
    by_flag = _events([str(DEMO), "--seed", "3"], tmp_path, "flag")
    assert by_flag == _events([str(DEMO), "--seed", "3"], tmp_path, "again")
    assert by_flag != _events([str(DEMO), "--seed", "4"], tmp_path, "other")
    # the environment wins over the flag
    monkeypatch.setenv("DYNACAMP_SEED", "3")
    assert _events([str(DEMO), "--seed", "99"], tmp_path, "env") == by_flag
    # neither: a seed from the clock, printed so the campaign can be repeated
    monkeypatch.delenv("DYNACAMP_SEED")
    monkeypatch.setattr("time.time", lambda: 1_764_000_000.5)
    capsys.readouterr()
    clocked = _events([str(DEMO)], tmp_path, "clock")
    seed = int(re.search(r"Seed (\d+) from the clock", capsys.readouterr().out).group(1))
    assert seed == 1_764_000_000
    monkeypatch.undo()
    monkeypatch.delenv("DYNACAMP_SEED", raising=False)
    assert _events([str(DEMO), "--seed", str(seed)], tmp_path, "repeat") == clocked


def test_shipped_scenarios_carry_no_seed():
    import tomllib

    from .conftest import ROOT

    for path in sorted((ROOT / "scenarios").glob("*.toml")):
        assert "seed" not in tomllib.loads(path.read_text()).get("campaign", {}), path.name
