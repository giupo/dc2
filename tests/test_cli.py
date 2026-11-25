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


def test_simulate_seed_flag_and_environment(monkeypatch, tmp_path):
    from .conftest import DEMO

    monkeypatch.delenv("DYNACAMP_SEED", raising=False)
    # the scenario with its seed rewritten = the scenario with --seed
    text = DEMO.read_text()
    assert "seed = 7" in text
    rewritten = tmp_path / DEMO.name
    rewritten.write_text(text.replace("seed = 7", "seed = 3", 1))
    (tmp_path / DEMO.name.replace(".toml", ".roads.json")).write_text(
        DEMO.with_name(DEMO.stem + ".roads.json").read_text())
    by_flag = _events([str(DEMO), "--seed", "3"], tmp_path, "flag")
    assert by_flag == _events([str(rewritten)], tmp_path, "file")
    assert by_flag != _events([str(DEMO)], tmp_path, "default")
    # the environment wins over the flag
    monkeypatch.setenv("DYNACAMP_SEED", "3")
    assert _events([str(DEMO), "--seed", "99"], tmp_path, "env") == by_flag
