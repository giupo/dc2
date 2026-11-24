"""Generated DCS missions (needs the optional pydcs dependency)."""

import json
import shutil
import subprocess
import warnings
import zipfile

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore", SyntaxWarning)
    dcs = pytest.importorskip("dcs")

from dynacamp import scenario
from dynacamp.adapters.base import EntitySnapshot, Materialize
from dynacamp.adapters.dcs import LUA_BRIDGE, DcsTranslator
from dynacamp.adapters.dcs.miz import compose_loadout, _unit_type, write_mission
from dynacamp.geo import Vec2

from .conftest import DEMO

SCENARIOS = sorted(DEMO.parent.glob("*.toml"))


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.stem)
def test_mission_has_bridge_templates_and_slots(path, tmp_path):
    state = scenario.load(path)
    out = tmp_path / "m.miz"
    notes = write_mission(state, out)
    assert notes == []
    with zipfile.ZipFile(out) as z:
        assert "l10n/DEFAULT/dynacamp_bridge.lua" in z.namelist()
        assert z.read("l10n/DEFAULT/dynacamp_bridge.lua") == LUA_BRIDGE.read_bytes()
    m = dcs.Mission()
    m.load_file(str(out))
    groups = [g for c in m.coalition.values() for co in c.countries.values() for g in co.plane_group + co.helicopter_group]
    templates = [g for g in groups if g.name.startswith("DC_TPL")]
    for sq in state.squadrons.values():
        for task in sq.tasks:
            assert any(g.name == f"DC_TPL_{sq.aircraft_type.id}_{task.value}" for g in templates)
    assert all(g.late_activation and g.units[0].pylons for g in templates)
    assert any(u.skill == dcs.unit.Skill.Client for g in groups for u in g.units)
    actions = [type(a).__name__ for r in m.triggerrules.triggers for a in r.actions]
    assert actions == ["DoScript", "DoScriptFile"]


def test_loadouts_use_real_weapons():
    picks = compose_loadout(_unit_type("F-16C_50"), "SEAD")
    names = " ".join(w["name"] for _, w in picks)
    assert "AGM-88C" in names and "AIM-120C" in names
    rockets = " ".join(w["name"] for _, w in compose_loadout(_unit_type("Mi-24P"), "HELO"))
    assert "TsM" not in rockets  # no marker rockets


@pytest.mark.skipif(shutil.which("lua5.1") is None, reason="lua5.1 not installed")
def test_bridge_applies_templates_from_generated_mission(tmp_path):
    state = scenario.load(SCENARIOS[0])
    out = tmp_path / "m.miz"
    write_mission(state, out)
    with zipfile.ZipFile(out) as z:
        z.extract("mission", tmp_path)
    snap = EntitySnapshot(
        id="fl9", kind="flight", name="X", faction="blue", side="blue", position=Vec2(640_000, -300_000),
        heading=1.0, elements={"aircraft": 2.0}, waypoints=(Vec2(650_000, -280_000),), speed_kmh=550, altitude=6000,
        attrs={"aircraft": "a10c", "task": "CAS", "target": Vec2(650_000, -280_000), "target_index": 0, "home": "Kobuleti"},
    )
    (tmp_path / "spawn.json").write_text(json.dumps(DcsTranslator().encode(Materialize(snap))))
    script = tmp_path / "check.lua"
    script.write_text(
        """
dofile(arg[1] .. "/mission")
timer = { getTime = function() return 0 end, scheduleFunction = function() end }
env = { info = function() end, mission = mission }
coalition = { addGroup = function(country, cat, data) added = data end }
Group = { Category = { AIRPLANE = 0, HELICOPTER = 1, GROUND = 2, SHIP = 3 } }
DYNACAMP_NO_AUTOSTART = true
local DC = dofile(arg[2])
DC.send = function() end
local f = io.open(arg[1] .. "/spawn.json"); DC.handle(DC.json.decode(f:read("*a"))); f:close()
local u = added.units[1]
local n = 0; for _ in pairs(u.payload.pylons) do n = n + 1 end
assert(u.type == "A-10C_2" and n > 0, "template not applied")
"""
    )
    res = subprocess.run(["lua5.1", str(script), str(tmp_path), str(LUA_BRIDGE)], capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr
