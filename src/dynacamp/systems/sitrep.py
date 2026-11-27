"""Situation report for a player who takes a slot.

When a player enters the game the engine sends them a short text, as their
coalition sees the war (fog of war: enemies only from
:mod:`dynacamp.systems.intel`): who is winning (:mod:`dynacamp.standing`),
the faction's objectives, the front, the known threats, the friendly air
plan and support, the bullseye. It is a picture of the situation, not an
assignment: the packages listed are flown by the AI.

The game shows it as an on-screen message for :data:`DURATION_S`, so it is
kept to about fifteen lines.
"""

from __future__ import annotations

from .. import bullseye, standing
from .. import units as U
from ..air import ACTIVE_STATUSES, FlightStatus, Task
from ..air_defense import SamRole
from ..events import EventKind, clock
from ..forces import EntityKind
from ..projection import to_latlon
from ..state import BattleKind, CampaignState
from . import intel
from .briefing import ddm

# -- What goes in ------------------------------------------------------------

#: Seconds the game shows the report.
DURATION_S = 60.0
#: Province changes reported, looking back this far, seconds.
RECENT_S = 24 * 3600.0
#: Known SAM sites listed, nearest to the bullseye first.
MAX_SAMS = 5
#: Enemy aircraft contacts listed, freshest first.
MAX_AIR_CONTACTS = 3
#: Enemy aircraft contacts older than this are left out, seconds.
AIR_CONTACT_MAX_AGE_S = 15 * 60.0
#: Friendly packages listed, soonest time on target first.
MAX_PACKAGES = 5
#: Separator between the items of a line.
SEP = " | "


def _names(state: CampaignState, pids) -> str:
    """Province names, comma separated.

    Args:
        state: The campaign.
        pids: Province ids.

    Returns:
        The names.
    """
    return ", ".join(state.theater.provinces[p].name for p in pids)


def _standing(state: CampaignState, fid: str) -> str:
    """The standing line: our share, then each enemy's.

    Args:
        state: The campaign.
        fid: The player's faction.

    Returns:
        E.g. ``"62% (Syrian Arab Army 38%)"``.
    """
    shares = standing.shares(state)
    others = SEP.join(f"{f.name} {shares[f.id]:.0%}" for f in state.factions.values() if f.id != fid)
    return f"{shares[fid]:.0%}" + (f" ({others})" if others else "")


def _objectives(state: CampaignState, fid: str) -> str:
    """Objectives held and still to take.

    Args:
        state: The campaign.
        fid: The player's faction.

    Returns:
        The objectives line.
    """
    provinces = state.theater.provinces
    objectives = sorted(state.factions[fid].objectives, key=lambda o: -o.priority)
    held = [o.province for o in objectives if provinces[o.province].controller == fid]
    todo = [o.province for o in objectives if provinces[o.province].controller != fid]
    parts = ([f"held: {_names(state, held)}"] if held else []) + ([f"to take: {_names(state, todo)}"] if todo else [])
    return SEP.join(parts) or "none"


def _front(state: CampaignState, fid: str) -> list[str]:
    """Battles in progress and provinces won or lost lately.

    Args:
        state: The campaign.
        fid: The player's faction.

    Returns:
        The front lines (one or two).
    """
    t = state.theater
    fights = []
    for b in sorted(state.battles.values(), key=lambda b: b.id):
        if fid not in (b.attacker_faction, b.defender_faction):
            continue
        role = "attacking" if b.attacker_faction == fid else "defending"
        where = t.provinces[b.province].name if b.kind is BattleKind.ASSAULT else f"near {t.provinces[b.province].name}"
        fights.append(f"{where} ({role})")
    out = [f"Fighting at {', '.join(fights)}." if fights else "No battles in progress."]
    took: list[str] = []
    lost: list[str] = []
    for e in state.events.of_kind(EventKind.PROVINCE_CAPTURED):
        if e.time < state.time - RECENT_S:
            continue
        pid = e.data["province"]
        if e.data["faction"] == fid and pid not in took:
            took.append(pid)
        elif e.data.get("previous") == fid and pid not in lost:
            lost.append(pid)
    changes = ([f"took {_names(state, took)}"] if took else []) + ([f"lost {_names(state, lost)}"] if lost else [])
    if changes:
        out.append(f"Last 24 h: {'; '.join(changes)}.")
    return out


def _contact_name(state: CampaignState, c: intel.Contact) -> str:
    """How the report names a contact: system or aircraft, and where.

    Args:
        state: The campaign.
        c: The contact.

    Returns:
        E.g. ``"SA-6 Gainful Sa'sa'"``, ``"2x MiG-21bis"``, ``"unknown aircraft"``.
    """
    d = c.detail
    if c.kind is EntityKind.SAM:
        where = state.theater.provinces.get(d.get("province", ""))
        return f"{d.get('system_name', 'SAM')} {where.name}" if where else d.get("system_name", "SAM")
    if c.identified and "aircraft_name" in d:
        return f"{d.get('size', 1)}x {d['aircraft_name']}"
    return "unknown aircraft"


def _threats(state: CampaignState, fid: str, units: U.UnitDomain) -> list[str]:
    """Known enemy SAM sites and fresh enemy air contacts, with their BULLS.

    Args:
        state: The campaign.
        fid: The player's faction.
        units: Units of the BULLS ranges.

    Returns:
        The SAM line and the air line.
    """
    def item(c: intel.Contact) -> str:
        """A contact with its BULLS, flagged if old.

        Args:
            c: The contact (with a position).

        Returns:
            The list item.
        """
        assert c.position is not None
        old = " (old)" if c.stale(state.time) else ""
        return f"{_contact_name(state, c)} BULLS {bullseye.label(state, fid, c.position, units)}{old}"

    sams = [
        c for c in intel.known(state, fid, EntityKind.SAM)
        if c.position is not None and c.detail.get("role") != SamRole.EWR
    ]
    sams.sort(key=lambda c: (bullseye.bearing_range(state, fid, c.position)[1], c.entity_id))
    air = [c for c in intel.known(state, fid, EntityKind.FLIGHT, max_age=AIR_CONTACT_MAX_AGE_S) if c.position is not None]
    air.sort(key=lambda c: (-c.seen, c.entity_id))
    return [
        "SAMS     " + (SEP.join(item(c) for c in sams[:MAX_SAMS]) or "none known"),
        "AIR      " + (SEP.join(item(c) for c in air[:MAX_AIR_CONTACTS]) or "no enemy aircraft tracked"),
    ]


def _air_plan(state: CampaignState, fid: str) -> list[str]:
    """The coalition's packages still to fly or in the air (flown by the AI).

    Args:
        state: The campaign.
        fid: The player's faction.

    Returns:
        One line per package, soonest TOT first.
    """
    coalition = state.factions[fid].coalition
    pkgs = sorted(
        (p for p in state.packages.values()
         if not p.done and state.factions[p.faction].coalition == coalition and not p.task.support),
        key=lambda p: (p.tot, p.id),
    )
    out = []
    for p in pkgs[:MAX_PACKAGES]:
        flights = [state.flights[i] for i in p.flights if i in state.flights]
        airborne = any(f.status in ACTIVE_STATUSES for f in flights)
        out.append(f"  {p.number} {p.task.value} {p.target_label} TOT {clock(p.tot).split()[-1]}" + (" (airborne)" if airborne else ""))
    if len(pkgs) > MAX_PACKAGES:
        out.append(f"  ... and {len(pkgs) - MAX_PACKAGES} more")
    return out or ["  no packages planned"]


def _support(state: CampaignState, fid: str) -> str:
    """Friendly AWACS and tankers on station.

    Args:
        state: The campaign.
        fid: The player's faction.

    Returns:
        The support line.
    """
    coalition = state.factions[fid].coalition
    on_station = [
        f for f in state.flights.values()
        if f.task in (Task.AWACS, Task.TANKER) and f.status is FlightStatus.ON_STATION
        and state.factions[f.faction].coalition == coalition
    ]
    items = [f"{f.task.value} {f.callsign}" for f in sorted(on_station, key=lambda f: (f.task.value, f.callsign))]
    return SEP.join(items) if items else "no AWACS or tanker on station"


def text(state: CampaignState, fid: str, units: U.UnitDomain | None = None) -> str:
    """The situation report of a faction, ready to show.

    Args:
        state: The campaign.
        fid: The player's faction.
        units: Units of the ranges (default: the campaign's).

    Returns:
        The report, about fifteen lines.
    """
    units = units or state.units
    f = state.factions[fid]
    ll = to_latlon(state.theater.name, f.bullseye)
    lines = [
        f"SITREP {clock(state.time)} - {f.name}",
        f"STANDING {_standing(state, fid)}",
        f"OBJECTIVES {_objectives(state, fid)}",
        *(f"FRONT    {line}" for line in _front(state, fid)),
        *_threats(state, fid, units),
        "PACKAGES (AI)",
        *_air_plan(state, fid),
        f"SUPPORT  {_support(state, fid)}",
        f"BULLSEYE {ddm(None if ll is None else (ll[0], ll[1]))}",
    ]
    return "\n".join(lines)

