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
