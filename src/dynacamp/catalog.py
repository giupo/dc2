"""The equipment catalogue: data files for aircraft, air defence and ground forces.

The catalogue is what the campaign knows about kinds of things, independent of
any scenario: an F-16C's radius, an SA-11 site's composition, a tank brigade's
supply use. It lives in TOML files under ``dynacamp/data/catalog/``:

* ``aircraft.toml``: one table per aircraft type;
* ``air_defense.toml``: one table per SAM system or early-warning radar;
* ``ground.toml``: ``[elements.<id>]`` and ``[templates.<id>]``.

Each entry may have a ``dcs`` sub-table (DCS unit types, callsigns,
representation ratios). The engine ignores it; only the DCS adapter and the
callsign module read it, through :func:`dcs`.

This module knows nothing about the domain classes: each owner module builds
its own objects with :func:`build` (``air.AIRCRAFT``, ``air_defense.SYSTEMS``,
``forces.ELEMENTS``, ``forces.DEFAULT_TEMPLATES``), so there are no import
cycles. Loading is strict: unknown or missing fields and values of the wrong
type raise :class:`CatalogError`, naming the file and the entry.
"""

from __future__ import annotations

import dataclasses
import tomllib
from collections.abc import Callable, Mapping
from functools import cache
from pathlib import Path
from typing import Any, TypeVar

#: Directory of the catalogue files.
CATALOG_DIR = Path(__file__).parent / "data" / "catalog"

#: Sub-table of an entry holding its DCS data (``[<id>.dcs]``).
DCS_KEY = "dcs"

#: Type of the dataclass built from a catalogue file.
T = TypeVar("T")


class CatalogError(ValueError):
    """A catalogue file is missing, malformed or inconsistent."""


@cache
def _read(name: str) -> dict[str, Any]:
    """Parse one catalogue file (cached: the files do not change at runtime).

    Args:
        name: File name without extension, e.g. ``"aircraft"``.

    Returns:
        The parsed TOML document.

    Raises:
        CatalogError: If the file is missing or is not valid TOML.
    """
    path = CATALOG_DIR / f"{name}.toml"
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)

    except FileNotFoundError as exc:
        raise CatalogError(f"catalogue file not found: {path}") from exc

    except tomllib.TOMLDecodeError as exc:
        raise CatalogError(f"{path.name}: {exc}") from exc


def entries(name: str, section: str | None = None) -> dict[str, dict[str, Any]]:
    """The raw entries of a catalogue file, in file order.

    Args:
        name: File name without extension.
        section: Top-level table holding the entries (``"elements"`` in
            ``ground.toml``), or None if the entries are the top-level tables.

    Returns:
        Entry id -> its table (a fresh copy of the top level; do not mutate the
        nested values).

    Raises:
        CatalogError: If the section is missing or an entry is not a table.
    """
    data = _read(name)
    where = f"{name}.toml"
    if section is not None:
        if not isinstance(data.get(section), dict):
            raise CatalogError(f"{where}: missing [{section}] tables")
        data = data[section]
        where += f" [{section}]"

    out: dict[str, dict[str, Any]] = {}

    for eid, raw in data.items():
        if not isinstance(raw, dict):
            raise CatalogError(f"{where}: {eid!r} is not a table")
        out[eid] = dict(raw)

    return out


def dcs(name: str, section: str | None = None) -> dict[str, dict[str, Any]]:
    """The ``dcs`` sub-tables of a catalogue file (empty for entries without one).

    Args:
        name: File name without extension.
        section: As in :func:`entries`.

    Returns:
        Entry id -> its ``dcs`` table.
    """
    return {
        eid: dict(raw.get(DCS_KEY, {}))
        for eid, raw in entries(name, section).items()
    }


def _type_name(annotation: Any) -> str:
    """A field annotation as a name: ``"float"`` for both ``float`` and the
    string ``"float"`` (modules with ``from __future__ import annotations``).

    Args:
        annotation: ``dataclasses.Field.type``.

    Returns:
        The type name.
    """
    return annotation if isinstance(annotation, str) else getattr(annotation, "__name__", str(annotation))


def _scalar(kind: str, value: Any) -> Any:
    """Check and convert a TOML value for a field annotated ``kind``.

    Args:
        kind: The field's annotation, as a string (``"float"``, ``"int"``...).
        value: The TOML value.

    Returns:
        The value converted to the annotated type.

    Raises:
        TypeError: If the value does not fit the annotation.
        LookupError: If the annotation is not a plain scalar (needs a converter).
    """
    if kind == "bool":
        if not isinstance(value, bool):
            raise TypeError(f"expected true/false, got {value!r}")
        return value

    if kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"expected a number, got {value!r}")
        return float(value)

    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"expected an integer, got {value!r}")
        return value

    if kind == "str":
        if not isinstance(value, str):
            raise TypeError(f"expected a string, got {value!r}")
        return value

    raise LookupError(kind)


def build(
    name: str,
    cls: type[T],
    *,
    section: str | None = None,
    convert: Mapping[str, Callable[[Any], Any]] | None = None,
) -> dict[str, T]:
    """Build one frozen dataclass per entry of a catalogue file.

    The entry's key becomes the ``id`` field; every other field comes from the
    table, or from the dataclass default when the table leaves it out. Plain
    ``bool``/``int``/``float``/``str`` fields are checked and converted here;
    any other field needs a converter in ``convert``.

    Args:
        name: File name without extension.
        cls: Dataclass with an ``id`` field.
        section: As in :func:`entries`.
        convert: Field name -> function turning the TOML value into the field's
            value (it may raise ``ValueError``/``TypeError``/``KeyError``).

    Returns:
        Entry id -> object, in file order.

    Raises:
        CatalogError: On unknown, missing or ill-typed fields.
    """
    convert = convert or {}
    fields = {f.name: f for f in dataclasses.fields(cls) if f.name != "id"}  # type: ignore[arg-type]
    where = f"{name}.toml" + (f" [{section}]" if section else "")
    out: dict[str, T] = {}

    for eid, raw in entries(name, section).items():
        raw.pop(DCS_KEY, None)
        unknown = sorted(set(raw) - set(fields))
        if unknown:
            raise CatalogError(f"{where} {eid}: unknown fields {unknown} (known: {sorted(fields)})")

        kwargs: dict[str, Any] = {}

        for fname, f in fields.items():
            if fname not in raw:
                if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
                    raise CatalogError(f"{where} {eid}: missing field {fname!r}")
                continue

            try:
                if fname in convert:
                    kwargs[fname] = convert[fname](raw[fname])

                else:
                    kwargs[fname] = _scalar(_type_name(f.type), raw[fname])

            except LookupError as exc:
                if fname in convert:
                    raise CatalogError(f"{where} {eid}.{fname}: unknown value {exc}") from exc
                raise CatalogError(f"{cls.__name__}.{fname} ({f.type}) needs a converter") from exc

            except (TypeError, ValueError) as exc:
                raise CatalogError(f"{where} {eid}.{fname}: {exc}") from exc

        out[eid] = cls(id=eid, **kwargs)  # type: ignore[call-arg]

    return out


def counts(value: Any) -> dict[str, int]:
    """Converter for element-count tables (``{ radar = 1, launcher = 4 }``).

    Args:
        value: The TOML inline table.

    Returns:
        The counts by element id.

    Raises:
        TypeError: If it is not a table of non-negative integers.
    """
    if not isinstance(value, dict):
        raise TypeError(f"expected a table of counts, got {value!r}")
    out: dict[str, int] = {}
    for k, n in value.items():
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            raise TypeError(f"{k}: expected a non-negative integer, got {n!r}")
        out[k] = n
    return out
