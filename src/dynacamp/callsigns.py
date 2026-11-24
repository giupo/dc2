"""DCS radio callsigns of the flights.

DCS does not take free-text callsigns for western aircraft: each unit carries
``{index, flight, unit, name}``, and the AI voices (and the radio menus) only
know the names of a fixed list. The list depends on the role and the type:

* every aeroplane and helicopter has the 8 generic names (``Enfield`` = 1 ..
  ``Pontiac`` = 8);
* some types add their own after them (F-16C ``Viper`` = 9 .. ``Jedi`` = 20,
  A-10 ``Hawg`` = 9 .. ``Tusk`` = 12, ...);
* AWACS and tankers have lists of their own (``Overlord`` = 1 .., ``Texaco`` = 1 ..).

The campaign callsign of a squadron (``Lobo``, ``Victory``...) is used as is
when it is in the list of its type. Otherwise the squadron gets a **radio
callsign** from the generic list (or the AWACS/tanker one), chosen
deterministically among the names no other squadron of the side uses. The
briefing shows it, so the players know what the AI will call them.

Eastern aircraft (``red`` side, Russian country in the missions) use numeric
callsigns: nothing is sent and DCS keeps the template's.

The type lists are copied from pydcs (``callnames``), keyed by campaign
aircraft id; ``tests/test_callsigns.py`` checks them against pydcs when it is
installed, so the runtime does not need it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .air import Task

if TYPE_CHECKING:
    from .air import Flight, Squadron
    from .state import CampaignState

GENERIC = ("Enfield", "Springfield", "Uzi", "Colt", "Dodge", "Ford", "Chevy", "Pontiac")
AWACS = ("Overlord", "Magic", "Wizard", "Focus", "Darkstar")
TANKER = ("Texaco", "Arco", "Shell")

# Type-specific names, numbered from len(GENERIC) + 1 (from pydcs ``callnames``).
TYPE_NAMES: dict[str, tuple[str, ...]] = {
    "f16c": ("Viper", "Venom", "Lobo", "Cowboy", "Python", "Rattler", "Panther", "Wolf", "Weasel", "Wild", "Ninja", "Jedi"),
    "fa18c": ("Hornet", "Squid", "Ragin", "Roman", "Sting", "Jury", "Joker", "Ram", "Hawk", "Devil", "Check", "Snake"),
    "a10c": ("Hawg", "Boar", "Pig", "Tusk"),
    "a10a": ("Hawg", "Boar", "Pig", "Tusk"),
    "ah64d": ("ArmyAir", "Apache", "Crow", "Chaos", "Sioux", "Gatling", "Gunslinger", "Hammerhead", "Bootleg",
              "Palehorse", "Carnivore", "Saber"),
    "b1b": ("Bone", "Dark", "Vader"),
    "b52h": ("Buff", "Dump", "Kenworth"),
    "oh58d": ("Anvil", "Azrael", "Bam-Bam", "Blackjack", "Bootleg", "Burnin' Stogie", "Chaos", "Crazyhorse", "Crusader",
              "Darkhorse", "Eagle", "Lighthorse", "Mustang", "Outcast", "Palehorse", "Pegasus", "Pistol", "Roughneck",
              "Saber", "Shamus", "Spur", "Stetson", "Wrath"),
}

WESTERN_SIDE = "blue"  # the missions put blue in the USA country (western callsigns)


@dataclass(frozen=True)
class RadioCallsign:
    """The DCS callsign of a flight.

    Attributes:
        name: DCS callsign name (``"Lobo"``).
        index: Its index in the DCS list of the type.
        flight: Flight number (1-9).
        voiced: True if it is the squadron's own callsign, False for a generic stand-in.
    """
    name: str
    index: int
    flight: int
    voiced: bool

    @property
    def label(self) -> str:
        """Spoken form of the flight, e.g. ``"Lobo 3"``."""
        return f"{self.name} {self.flight}"

    def unit(self, number: int) -> dict:
        """Callsign of the ``number``-th aircraft, as the bridge expects it.

        Args:
            number: Position in the flight (1 = lead).

        Returns:
            ``{"index": [index, flight, number], "name": "Lobo31"}``.
        """
        return {"index": [self.index, self.flight, number], "name": f"{self.name}{self.flight}{number}"}


def names(aircraft: str, task: Task) -> dict[str, int]:
    """DCS callsign names available to a type for a task, with their index.

    Args:
        aircraft: Campaign aircraft id.
        task: Mission task.

    Returns:
        Name -> index (1-based, as in DCS).
    """
    if task is Task.AWACS:
        return {n: i for i, n in enumerate(AWACS, 1)}
    if task is Task.TANKER:
        return {n: i for i, n in enumerate(TANKER, 1)}
    out = {n: i for i, n in enumerate(GENERIC, 1)}
    out.update({n: i for i, n in enumerate(TYPE_NAMES.get(aircraft, ()), len(GENERIC) + 1)})
    return out


def _main_task(sq: Squadron) -> Task:
    """The task that picks a squadron's callsign list (AWACS, tanker or other)."""
    for t in (Task.AWACS, Task.TANKER):
        if t in sq.tasks:
            return t
    return Task.CAP


@dataclass(frozen=True)
class SquadronCallsign:
    """The DCS callsign name of a squadron.

    Attributes:
        name: DCS callsign name.
        index: Its index in the DCS list of the type.
        voiced: True if it is the squadron's own callsign, False for a stand-in.
        share: Position among the squadrons sharing the same stand-in (0 = first).
        sharers: How many squadrons share it (1 = nobody else).
    """
    name: str
    index: int
    voiced: bool
    share: int = 0
    sharers: int = 1

    def flight_number(self, n: int) -> int:
        """DCS flight number of the squadron's ``n``-th flight (1-9).

        Squadrons sharing a stand-in split the 1-9 range (1-4 and 5-8 for two),
        so their flights are never called the same at the same time.
        """
        per = max(1, 9 // self.sharers)
        return (n - 1) % per + 1 + self.share * per


def squadron_callsign(state: CampaignState, sq: Squadron, task: Task | None = None) -> SquadronCallsign | None:
    """DCS callsign name of a squadron.

    Args:
        state: Campaign state.
        sq: Squadron.
        task: Task of the flight (default: the squadron's main role).

    Returns:
        The callsign, or None for sides without western callsigns.
    """
    if state.factions[sq.faction].side != WESTERN_SIDE:
        return None
    task = task or _main_task(sq)
    table = names(sq.aircraft_type.id, task)
    if sq.callsign in table:
        return SquadronCallsign(sq.callsign, table[sq.callsign], True)

    # Stand-in: generic names in order, skipping those other squadrons of the
    # side already use; squadrons sorted by id, so it is deterministic. With
    # more squadrons than names they wrap, and share the flight numbers.
    fallback = AWACS if task is Task.AWACS else TANKER if task is Task.TANKER else GENERIC
    side = [q for q in sorted(state.squadrons.values(), key=lambda q: q.id)
            if state.factions[q.faction].side == WESTERN_SIDE]
    pool = [n for n in fallback if n not in {q.callsign for q in side}] or list(fallback)
    kind = _main_task(sq)
    unvoiced = [q.id for q in side
                if _main_task(q) is kind and q.callsign not in names(q.aircraft_type.id, kind)]
    if sq.id not in unvoiced:  # flown on a task outside its role: no sharing to plan
        return SquadronCallsign(pool[0], table[pool[0]], False)
    i = unvoiced.index(sq.id)
    slot = i % len(pool)
    sharers = len(range(slot, len(unvoiced), len(pool)))
    return SquadronCallsign(pool[slot], table[pool[slot]], False, i // len(pool), sharers)


def radio_callsign(state: CampaignState, f: Flight) -> RadioCallsign | None:
    """DCS callsign of a flight.

    The flight number comes from the campaign callsign (``"Victory 3"`` -> 3),
    moved into the squadron's range when it shares a stand-in.

    Args:
        state: Campaign state.
        f: Flight.

    Returns:
        The callsign, or None for sides without western callsigns.
    """
    sq = state.squadrons.get(f.squadron)
    found = squadron_callsign(state, sq, f.task) if sq else None
    if found is None:
        return None
    number = f.callsign.rsplit(" ", 1)[-1]
    n = int(number) if number.isdigit() else 1
    return RadioCallsign(found.name, found.index, found.flight_number(n), found.voiced)
