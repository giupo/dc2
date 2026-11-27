"""DCS names live in data files, never in the code.

Eagle Dynamics renames aircraft types and weapons between versions. The names
are kept in data (the ``dcs`` tables of the catalogue, ``adapters/dcs/data/*.toml``
and the generated ``data/loadouts.json``), so a rename is fixed there, not by
hunting through the code. This test collects every DCS name those files know and
fails if one appears as a string literal in the Python sources.
"""

import ast
from pathlib import Path

from dynacamp import catalog, loadouts
from dynacamp.adapters.dcs import catalog as dcs_catalog

SRC = Path(__file__).resolve().parents[1] / "src" / "dynacamp"
#: Names shorter than this only match a literal equal to them (else "HOT" would match "SHOT").
MIN_FRAGMENT = 5


def _strings(table: dict) -> set[str]:
    """Every string in a (nested) TOML table."""
    out: set[str] = set()
    for v in table.values():
        if isinstance(v, str):
            out.add(v)
        elif isinstance(v, list):
            out |= {x for x in v if isinstance(x, str)}
        elif isinstance(v, dict):
            out |= _strings(v)
    return out


def dcs_names() -> set[str]:
    """Aircraft and unit types, callsigns, weapons and store names DCS defines."""
    names: set[str] = set()
    for name, section in (("aircraft", None), ("air_defense", None), ("ground", "elements"), ("callsigns", None)):
        for table in catalog.dcs(name, section).values():
            names |= _strings(table)
    names |= set(dcs_catalog.data("players")["types"])
    weapons = dcs_catalog.data("weapons")
    names |= set(weapons["wvr"]) | set(weapons["bvr_range_m"]) | set(weapons["not_live"])
    for pattern in dcs_catalog.data("recipes")["patterns"].values():
        names |= set(pattern.split("|"))
    for tasks in loadouts._data().values():
        for stores in tasks.values():
            names |= {s["name"] for s in stores}
    return names


def _literals(path: Path):
    """String literals of a module that are not docstrings: (line, value)."""
    tree = ast.parse(path.read_text())
    docs = {
        id(n.body[0].value)
        for n in ast.walk(tree)
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)
    }
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
            yield n.lineno, n.value


def test_the_data_files_name_dcs_things():
    names = dcs_names()
    assert {"F-16C_50", "AIM-120", "Overlord"} <= names  # the collection itself works


def test_no_dcs_name_in_the_code():
    names = dcs_names()
    found = [
        f"{path.relative_to(SRC)}:{line} {value!r} ({name})"
        for path in sorted(SRC.rglob("*.py"))
        for line, value in _literals(path)
        for name in names
        if value == name or (len(name) >= MIN_FRAGMENT and name in value)
    ]
    assert not found, "DCS names belong in the data files:\n" + "\n".join(found)
