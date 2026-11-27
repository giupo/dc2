import shutil
import subprocess
from pathlib import Path

import pytest

from dynacamp import scenario
from dynacamp.state import CampaignState

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "scenarios" / "western_georgia.toml"


def _lua51() -> str | None:
    """A Lua 5.1 interpreter, the version DCS embeds: ``lua5.1``, or ``lua`` if it is 5.1."""
    for name in ("lua5.1", "lua"):
        path = shutil.which(name)
        if path:
            res = subprocess.run([path, "-v"], capture_output=True, text=True)
            if "Lua 5.1" in res.stdout + res.stderr:
                return path
    return None


#: Path of a Lua 5.1 interpreter (None: the bridge tests are skipped).
LUA51 = _lua51()


def line_scenario() -> CampaignState:
    """Five provinces in a row, 20 km apart: blue a-b-c | red d-e."""
    provinces = []
    for i, pid in enumerate("abcde"):
        provinces.append(
            {
                "id": pid,
                "pos": [i * 20_000.0, 0.0],
                "terrain": "plains",
                "controller": "blue" if pid in "abc" else "red",
                "infrastructure": 3,
            }
        )
    provinces[0]["depot"] = {"stock": 5000, "capacity": 5000, "production_per_hour": 50, "trucks": 100}
    provinces[4]["depot"] = {"stock": 5000, "capacity": 5000, "production_per_hour": 50, "trucks": 100}
    return scenario.from_dict(
        {
            "campaign": {"name": "line", "seed": 1},
            "factions": [
                {"id": "blue", "side": "blue", "enemies": ["red"], "objectives": [{"province": "e"}]},
                {"id": "red", "side": "red", "enemies": ["blue"], "objectives": [{"province": "a"}]},
            ],
            "provinces": provinces,
            "links": [{"a": a, "b": b} for a, b in zip("abcd", "bcde")],
            "divisions": [
                {"id": "b1", "faction": "blue", "template": "tank_brigade", "province": "c"},
                {"id": "r1", "faction": "red", "template": "infantry_brigade", "province": "d"},
            ],
        }
    )


@pytest.fixture
def line() -> CampaignState:
    return line_scenario()


@pytest.fixture
def demo() -> CampaignState:
    return scenario.load(DEMO)
