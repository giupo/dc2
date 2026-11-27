"""The equipment catalogue (data/catalog/*.toml) and its loader."""

import json
from dataclasses import dataclass

import pytest

from dynacamp import catalog
from dynacamp.adapters.base import Side
from dynacamp.adapters.dcs.catalog import AIRCRAFT_TYPES, RATIO, SAM_TYPES, UNIT_TYPES
from dynacamp.air import AIRCRAFT, Task
from dynacamp.air_defense import LAUNCHER, RADAR_ELEMENTS, SYSTEMS
from dynacamp.forces import AAA, ARTILLERY, DEFAULT_TEMPLATES, ELEMENTS, MLRS, SAM_SHORT, TRUCK

SUPPORT = {Task.AWACS, Task.TANKER, Task.TRANSPORT}


# ---------------------------------------------------------------- the shipped data


def test_catalogue_is_loaded():
    assert len(AIRCRAFT) == 64 and len(SYSTEMS) == 12
    assert {"infantry", "mbt", "truck"} <= set(ELEMENTS)
    assert set(DEFAULT_TEMPLATES) == {"mech_brigade", "tank_brigade", "infantry_brigade", "artillery_group"}
    f16 = AIRCRAFT["f16c"]
    assert f16.name == "F-16C" and Task.SEAD in f16.tasks and isinstance(f16.radius_km, float)


def test_every_aircraft_has_a_dcs_type_and_armed_ones_have_loadouts():
    assert set(AIRCRAFT_TYPES) == set(AIRCRAFT)
    loadouts = json.loads((catalog.CATALOG_DIR.parent / "loadouts.json").read_text())["aircraft"]
    unarmed = {a.id for a in AIRCRAFT.values() if a.tasks <= SUPPORT}
    assert set(AIRCRAFT) - set(loadouts) == unarmed


def test_every_site_element_has_a_dcs_unit():
    for sid, system in SYSTEMS.items():
        assert set(system.elements) == set(SAM_TYPES[sid]), sid


def test_every_ground_element_has_a_ratio_and_a_unit_per_side():
    assert set(RATIO) == set(ELEMENTS)
    for side in Side:
        assert set(UNIT_TYPES[side]) == set(ELEMENTS), side


def test_templates_use_known_elements():
    for tid, template in DEFAULT_TEMPLATES.items():
        assert set(template.elements) <= set(ELEMENTS), tid


# ---------------------------------------------------------------- the loader


@dataclass(frozen=True)
class Thing:
    """A catalogue entry for the loader tests."""
    id: str
    name: str
    speed: float
    count: int = 1
    armed: bool = False


@pytest.fixture
def catalogue_dir(tmp_path, monkeypatch):
    """A temporary catalogue directory, with the file cache cleared around the test."""
    monkeypatch.setattr(catalog, "CATALOG_DIR", tmp_path)
    catalog._read.cache_clear()
    yield tmp_path
    catalog._read.cache_clear()


def write(directory, text):
    """Write ``things.toml`` in ``directory``."""
    (directory / "things.toml").write_text(text, encoding="utf-8")


def test_build_converts_fields_applies_defaults_and_keeps_order(catalogue_dir):
    write(catalogue_dir, '[b]\nname = "B"\nspeed = 3\n\n[a]\nname = "A"\nspeed = 1.5\ncount = 4\narmed = true\n'
                         '[a.dcs]\ntype = "A-1"\n')
    things = catalog.build("things", Thing)
    assert list(things) == ["b", "a"]
    assert things["b"] == Thing("b", "B", 3.0) and isinstance(things["b"].speed, float)
    assert things["a"] == Thing("a", "A", 1.5, 4, True)
    assert catalog.dcs("things") == {"b": {}, "a": {"type": "A-1"}}


@pytest.mark.parametrize(
    "text, message",
    [
        ('[x]\nname = "X"\nspeed = 1\ncolour = "red"\n', "unknown fields ['colour']"),
        ('[x]\nname = "X"\n', "missing field 'speed'"),
        ('[x]\nname = "X"\nspeed = "fast"\n', "x.speed: expected a number"),
        ('[x]\nname = "X"\nspeed = 1\ncount = 1.5\n', "x.count: expected an integer"),
        ('[x]\nname = "X"\nspeed = 1\narmed = 1\n', "x.armed: expected true/false"),
        ('[x]\nname = "X"\nspeed = true\n', "x.speed: expected a number"),
        ('x = 1\n', "'x' is not a table"),
        ('[x\n', "things.toml:"),
    ],
)
def test_build_rejects_bad_entries(catalogue_dir, text, message):
    write(catalogue_dir, text)
    with pytest.raises(catalog.CatalogError) as err:
        catalog.build("things", Thing)
    assert message in str(err.value)


def test_converters_and_their_errors(catalogue_dir):
    write(catalogue_dir, '[x]\nname = "X"\nspeed = 1\nsets = { a = 2 }\n')

    @dataclass(frozen=True)
    class WithSets:
        """An entry with a converted field."""
        id: str
        name: str
        speed: float
        sets: dict

    assert catalog.build("things", WithSets, convert={"sets": catalog.counts})["x"].sets == {"a": 2}
    with pytest.raises(catalog.CatalogError, match="needs a converter"):
        catalog.build("things", WithSets)
    write(catalogue_dir, '[x]\nname = "X"\nspeed = 1\nsets = { a = -1 }\n')
    catalog._read.cache_clear()
    with pytest.raises(catalog.CatalogError, match="non-negative integer"):
        catalog.build("things", WithSets, convert={"sets": catalog.counts})


def test_unknown_task_or_role_names_the_entry(catalogue_dir):
    from dynacamp.air import AircraftType, _tasks

    write(catalogue_dir, '[x]\nname = "X"\ncruise_kmh = 1\nradius_km = 1\na2a = 0\na2g = 0\ntasks = ["CAP", "DOGFIGHT"]\n')
    with pytest.raises(catalog.CatalogError, match="x.tasks"):
        catalog.build("things", AircraftType, convert={"tasks": _tasks})


def test_missing_file_and_section(catalogue_dir):
    with pytest.raises(catalog.CatalogError, match="not found"):
        catalog.entries("nothing")
    write(catalogue_dir, '[x]\nname = "X"\n')
    with pytest.raises(catalog.CatalogError, match=r"missing \[elements\]"):
        catalog.entries("things", "elements")


def test_element_ids_the_rules_name_are_in_the_catalogue():
    assert {TRUCK, ARTILLERY, MLRS, SAM_SHORT, AAA} <= ELEMENTS.keys()
    assert not ELEMENTS[TRUCK].combat
    site_elements = {e for s in SYSTEMS.values() for e in s.elements}
    assert LAUNCHER in site_elements and set(RADAR_ELEMENTS) <= site_elements
