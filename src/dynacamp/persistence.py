"""Saving and resuming a campaign.

A save file is a gzip-compressed pickle of an envelope (a dict) holding the
whole :class:`~dynacamp.state.CampaignState` plus a few facts about the save:
format version, package version, wall-clock time, campaign time and the time
compression in use. The state carries its random generator, so a resumed
campaign continues exactly as the uninterrupted one would have.

Pickle keeps the format free of hand-written serialisers, at a price:

* only load save files you trust (unpickling can run arbitrary code);
* a save may stop loading after deep refactors of the state classes; the
  envelope's :data:`FORMAT_VERSION` is bumped when that is done on purpose.
  Classes that were only moved or renamed are found through
  :data:`RENAMED_CLASSES`.

What a game was doing is not saved: when a campaign is loaded, entities that a
game was simulating come back under campaign control with their last known
state, and players' seats are released (as on a game disconnection).
"""

from __future__ import annotations

import gzip
import os
import pickle
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .forces import Authority, EntityKind
from .state import CampaignState
from .events import EventKind

if TYPE_CHECKING:  # pragma: no cover
    from .engine import Engine

#: Marks a save file's envelope.
MAGIC = "dynacamp-save"
#: Version of the save format: bumped only for incompatible changes (renamed fields, changed types).
FORMAT_VERSION = 1
#: First bytes of a gzip file: how a save is told from a scenario.
_GZIP_MAGIC = b"\x1f\x8b"
#: Classes moved or renamed since saves were first written: (module, name) as
#: pickled -> (module, name) now.
RENAMED_CLASSES = {
    ("dynacamp.systems.intel", "ContactKind"): ("dynacamp.forces", "EntityKind"),
}


class SaveTrigger(StrEnum):
    """What asked for a save."""
    #: The periodic autosave.
    AUTO = "auto"
    #: A request (POST /api/save).
    API = "api"
    #: The server shutting down.
    EXIT = "exit"


class _Unpickler(pickle.Unpickler):
    """Unpickler that finds moved or renamed classes (see :data:`RENAMED_CLASSES`)."""

    def find_class(self, module: str, name: str) -> Any:
        """Resolve a pickled class reference, under its current name.

        Args:
            module: Module the class was pickled from.
            name: Name it was pickled under.

        Returns:
            The class.
        """
        module, name = RENAMED_CLASSES.get((module, name), (module, name))
        return super().find_class(module, name)


class SaveError(ValueError):
    """A save file cannot be read (not a save, other format version, corrupt)."""


@dataclass(frozen=True)
class SaveInfo:
    """What a save file says about itself.

    Attributes:
        name: Campaign name.
        campaign_time: Campaign seconds since day 1 00:00.
        saved_at: Wall-clock time of the save (Unix seconds).
        version: Package version that wrote it.
        compression: Time compression in use when saved, if known.
    """
    name: str
    campaign_time: float
    saved_at: float
    version: str
    compression: float | None = None


def _package_version() -> str:
    """Installed version of the package (``"unknown"`` if not installed).

    Returns:
        The version string.
    """
    try:
        return metadata.version("dynacamp")
    except metadata.PackageNotFoundError:  # pragma: no cover - always installed in practice
        return "unknown"


def dumps(state: CampaignState, compression: float | None = None) -> bytes:
    """Serialise a campaign to the bytes of a save file.

    The caller must keep the state still while this runs (hold
    :attr:`Engine.lock <dynacamp.engine.Engine.lock>` in a live server).

    Args:
        state: The campaign.
        compression: Time compression to remember, if any.

    Returns:
        The gzip-compressed save.
    """
    envelope = {
        "format": MAGIC,
        "format_version": FORMAT_VERSION,
        "version": _package_version(),
        "saved_at": time.time(),
        "name": state.name,
        "campaign_time": state.time,
        "compression": compression,
        "state": state,
    }
    return gzip.compress(pickle.dumps(envelope, protocol=pickle.HIGHEST_PROTOCOL), compresslevel=6)


def write(data: bytes, path: str | Path) -> Path:
    """Write save bytes atomically: a crash mid-write never leaves a broken save.

    Args:
        data: Bytes from :func:`dumps`.
        path: Destination file.

    Returns:
        The destination path.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def save(state: CampaignState, path: str | Path, compression: float | None = None) -> Path:
    """Save a campaign to a file (see :func:`dumps` and :func:`write`).

    Args:
        state: The campaign.
        path: Destination file.
        compression: Time compression to remember, if any.

    Returns:
        The destination path.
    """
    return write(dumps(state, compression), path)


def _read_envelope(path: str | Path) -> dict[str, Any]:
    """Read and check the envelope of a save file.

    Args:
        path: The save file.

    Returns:
        The envelope dictionary, with the campaign in ``state``.

    Raises:
        SaveError: If the file is not a readable save of this format version.
    """
    try:
        with gzip.open(path, "rb") as fh:
            envelope = _Unpickler(fh).load()
    except (OSError, EOFError, pickle.UnpicklingError, AttributeError, ImportError, TypeError) as exc:
        raise SaveError(f"{path}: not a readable DynaCamp save ({exc})") from exc
    if not isinstance(envelope, dict) or envelope.get("format") != MAGIC:
        raise SaveError(f"{path}: not a DynaCamp save")
    if envelope.get("format_version") != FORMAT_VERSION:
        raise SaveError(
            f"{path}: save format {envelope.get('format_version')} (written by {envelope.get('version')}), "
            f"this version reads format {FORMAT_VERSION}"
        )
    if not isinstance(envelope.get("state"), CampaignState):
        raise SaveError(f"{path}: the save holds no campaign")
    return envelope


def _info(envelope: dict[str, Any]) -> SaveInfo:
    """The :class:`SaveInfo` of a checked envelope.

    Args:
        envelope: An envelope checked by :func:`_read_envelope`.

    Returns:
        Name, times, version and compression of the save.
    """
    return SaveInfo(
        name=envelope["name"],
        campaign_time=envelope["campaign_time"],
        saved_at=envelope["saved_at"],
        version=envelope["version"],
        compression=envelope.get("compression"),
    )


def _add_missing_fields(state: CampaignState) -> None:
    """Give a campaign saved by an older version the state fields added since,
    at their defaults (unpickling restores only the attributes it had), and
    the factions their bullseye (the default one: centre of the side's provinces now).

    Args:
        state: The unpickled campaign, updated in place.
    """
    import dataclasses

    for f in dataclasses.fields(state):
        if hasattr(state, f.name):
            continue
        if f.default_factory is not dataclasses.MISSING:
            setattr(state, f.name, f.default_factory())
        elif f.default is not dataclasses.MISSING:
            setattr(state, f.name, f.default)
    for faction in state.factions.values():
        if not hasattr(faction, "bullseye"):  # saved before bullseyes: the default one
            from . import bullseye

            side = {o.id for o in state.factions.values() if o.side == faction.side}
            held = [p.center for p in state.theater.provinces.values() if p.controller in side]
            faction.bullseye = bullseye.centre(held or [p.center for p in state.theater.provinces.values()])


def _strings_to_enums(state: CampaignState) -> None:
    """Turn into enum members the plain strings that older saves hold in
    fields that are enums now (the code compares them by identity).

    Args:
        state: The unpickled campaign, updated in place.
    """
    from .adapters.base import Side
    from .air import TargetKind
    from .state import BattleKind

    for faction in state.factions.values():
        faction.side = Side(faction.side)
    for battle in state.battles.values():
        battle.kind = BattleKind(battle.kind)
    for ent in [*state.entities(), *state.archived_flights.values()]:
        ent.kind = EntityKind(ent.kind)
    for f in [*state.flights.values(), *state.archived_flights.values()]:
        f.target_kind = TargetKind(f.target_kind or TargetKind.POINT)


def _detach_from_game(state: CampaignState) -> None:
    """Bring back under campaign control what a game held when the save was made.

    Args:
        state: The unpickled campaign, updated in place.
    """
    from .systems import ato

    for ent in state.entities():
        if ent.authority is not Authority.CAMPAIGN:
            ent.authority = Authority.CAMPAIGN
            ent.game_position = None
            ent.handoff_started = None
    for pid in list(state.player_flights):
        ato.player_disconnected(state, pid)
    state.player_positions = {}
    state.dirty.clear()


def load(path: str | Path) -> tuple[CampaignState, SaveInfo]:
    """Load a campaign from a save file, ready for a new engine.

    Entities a game was simulating come back to the campaign and players'
    seats are released. A ``campaign_resumed`` event marks the point in the log.

    Args:
        path: Save file.

    Returns:
        The campaign and what the save says about itself.

    Raises:
        SaveError: If the file is not a readable save of this format version.
    """
    envelope = _read_envelope(path)
    state: CampaignState = envelope["state"]
    _add_missing_fields(state)
    _strings_to_enums(state)
    _detach_from_game(state)
    info = _info(envelope)
    state.emit(EventKind.CAMPAIGN_RESUMED, saved_by=info.version, saved_at=info.saved_at)
    return state, info


def is_save(path: str | Path) -> bool:
    """True if ``path`` looks like a save file (gzip data) rather than a scenario.

    Args:
        path: File to check.

    Returns:
        Whether the file starts with the gzip magic bytes (False if it cannot be read).
    """
    try:
        with open(path, "rb") as fh:
            return fh.read(2) == _GZIP_MAGIC
    except OSError:
        return False


@dataclass(frozen=True)
class SaveRecord:
    """One save made by a :class:`Saver`.

    Attributes:
        saved_at: Wall-clock time (Unix seconds).
        campaign_time: Campaign seconds at the save.
        trigger: What asked for it.
    """
    saved_at: float
    campaign_time: float
    trigger: SaveTrigger


class Saver:
    """Saves a running engine's campaign to one file: periodically, on request, on exit.

    Safe to call from the engine thread and from API threads at once: the
    state is pickled under :attr:`Engine.lock <dynacamp.engine.Engine.lock>`
    (a consistent state between two steps), the file is written outside it,
    and saves are serialised by a lock of their own.

    Attributes:
        path: The save file.
        autosave_s: Real seconds between autosaves (0 = never).
        last: The latest successful save, if any.
    """

    def __init__(self, path: str | Path, autosave_s: float = 600.0, clock: Callable[[], float] = time.monotonic) -> None:
        """Set up a saver for one file.

        Args:
            path: The save file.
            autosave_s: Real seconds between autosaves (0 = never).
            clock: Monotonic real-time clock (for the autosave interval).
        """
        self.path = Path(path)
        self.autosave_s = autosave_s
        self.last: SaveRecord | None = None
        self._clock = clock
        self._last_attempt = clock()
        self._lock = threading.Lock()

    def save(self, engine: Engine, trigger: SaveTrigger) -> SaveRecord:
        """Save now and record a ``campaign_saved`` event.

        Args:
            engine: The running engine.
            trigger: What asks for it.

        Returns:
            The record of this save.

        Raises:
            OSError: If the file cannot be written (the previous save is kept).
        """
        with self._lock:
            self._last_attempt = self._clock()
            with engine.lock:
                data = dumps(engine.state, engine.control.compression)
                when = engine.state.time
            write(data, self.path)
            record = SaveRecord(saved_at=time.time(), campaign_time=when, trigger=trigger)
            self.last = record
        with engine.lock:
            engine.state.emit(EventKind.CAMPAIGN_SAVED, file=self.path.name, trigger=trigger.value, campaign_time=when)
        return record

    def due(self) -> bool:
        """True if an autosave is due (the interval elapsed since the last attempt).

        Returns:
            Whether to save now.
        """
        return self.autosave_s > 0 and self._clock() - self._last_attempt >= self.autosave_s
