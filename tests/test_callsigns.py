"""DCS radio callsigns of the flights."""

import pytest

from dynacamp import callsigns
from dynacamp.adapters.base import Materialize
from dynacamp.adapters.dcs import DcsTranslator
from dynacamp.adapters.dcs.catalog import AIRCRAFT_TYPES
from dynacamp.air import Task
from dynacamp.scenario import load

from .conftest import ROOT


@pytest.fixture(scope="module")
def syria():
    """The Syria scenario, without roads."""
    return load(ROOT / "scenarios" / "syria_m5.toml", with_roads=False)


def test_type_lists_match_pydcs():
    """The copied lists are the ones of DCS (via pydcs), in the same order."""
    pytest.importorskip("dcs")
    from dcs import helicopters, planes

    types = {**planes.plane_map, **helicopters.helicopter_map}
    for aircraft, dcs_type in AIRCRAFT_TYPES.items():
        if dcs_type not in types:
            continue
        expected = tuple((getattr(types[dcs_type], "callnames", None) or {}).get("USA", ()))
        assert callsigns.TYPE_NAMES.get(aircraft, ()) == expected, aircraft


def test_names_number_type_lists_after_the_generic_ones():
    """Enfield is 1, the type's own names start at 9; AWACS and tankers have their own lists."""
    f16 = callsigns.names("f16c", Task.CAS)
    assert f16["Enfield"] == 1 and f16["Pontiac"] == 8 and f16["Viper"] == 9 and f16["Lobo"] == 11
    assert callsigns.names("e3a", Task.AWACS)["Darkstar"] == 5
    assert callsigns.names("kc135", Task.TANKER)["Shell"] == 3
    assert "Viper" not in callsigns.names("f14b", Task.CAP)


def test_squadron_callsigns(syria):
    """Own names where DCS knows them, generic stand-ins otherwise, nothing for red."""
    sq = syria.squadrons

    def get(i):
        c = callsigns.squadron_callsign(syria, sq[i])
        return c and (c.name, c.index, c.voiced)

    assert get("b_viper") == ("Lobo", 11, True)
    assert get("b_hawg") == ("Tusk", 12, True)
    assert get("b_awacs") == ("Darkstar", 5, True)
    assert get("b_tanker") == ("Shell", 3, True)
    assert get("r_flanker") is None
    tomcat = get("b_tomcat")  # "Victory" is not a DCS name
    assert tomcat[0] in callsigns.GENERIC and tomcat[2] is False


def test_shared_stand_ins_never_collide(syria):
    """Squadrons sharing a stand-in name split the flight numbers between them."""
    used: dict[tuple[str, int], str] = {}
    for q in syria.squadrons.values():
        c = callsigns.squadron_callsign(syria, q)
        if c is None:
            continue
        for n in range(1, 10):
            key = (c.name, c.flight_number(n))
            assert used.setdefault(key, q.id) == q.id, f"{key} used by {used[key]} and {q.id}"
            assert 1 <= key[1] <= 9


def test_flight_spawn_carries_the_callsign():
    """The spawn message gives every AI aircraft its callsign, after the players' seats."""
    from .test_dcs import flight_snapshot

    snap = flight_snapshot()
    snap.attrs["radio_callsign"] = callsigns.RadioCallsign("Lobo", 11, 3, True)
    snap.attrs["first_unit"] = 2  # a player is lead
    (g,) = DcsTranslator().encode(Materialize(snap))["groups"]
    assert [u["callsign"] for u in g["data"]["units"]] == [
        {"index": [11, 3, 2], "name": "Lobo32"},
        {"index": [11, 3, 3], "name": "Lobo33"},
    ]
    snap.attrs["radio_callsign"] = None  # eastern side: the template's numeric callsign stays
    (g,) = DcsTranslator().encode(Materialize(snap))["groups"]
    assert all("callsign" not in u for u in g["data"]["units"])
