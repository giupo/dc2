"""Fog of war: what each faction knows about the enemy.

Every faction keeps an intelligence picture: a set of **contacts**, one per
enemy entity it has detected, with the last known position, when and by what
it was seen, and whether it has been *identified* (a radar track is just "an
aircraft"; eyes, pods and ground recon tell what it is). Contacts age: a stale
contact is shown at its last known position, and it is forgotten after a while.

Sensors, scanned every ``SCAN_INTERVAL_S``:

* **air picture** - AWACS on station, early-warning radars, SAM radars that are
  on, and the radar of every friendly aircraft (including the players'). An
  aircraft's radar looks along a cone ahead of it (:mod:`dynacamp.sensors`, the
  same that decides what fighters engage); players' are all round, as the
  campaign does not know where they point.
  Low flyers (helicopters, strikers on their attack run) are much harder to see
  for ground radars and somewhat harder for AWACS and fighters. Aircraft seen
  by a friendly aircraft within ``IDENT_RANGE_M`` are identified;
* **ground picture** - brigades see what is within reconnaissance range,
  convoys and SAM sites a little, friendly aircraft with their targeting pods
  and eyes, and every unit knows the enemy it is fighting;
* **ELINT** - AWACS, SEAD aircraft and EWRs locate enemy radars *that emit*,
  and enemy jammers (SEAD flights), roughly (``JAM_FIX_ERROR_M``);
* **jamming** - every radar (aircraft, AWACS, ground) sees a jammed aircraft
  much later (:func:`dynacamp.sensors.jam`);
* **events** - a SAM that fires reveals itself (:func:`reveal`).

At the start each faction gets a pre-war intelligence picture of the enemy's
brigades and air defence sites, which then grows stale.

The picture drives the campaign's own decisions: the ATO only targets known
enemies and plans SEAD only against known SAMs, the ground AI weighs the enemy
it knows about, artillery fires at located targets. Physics (who actually
fights whom) stays true.
"""

from __future__ import annotations

from enum import StrEnum
import math
import zlib
from dataclasses import dataclass, field
from typing import Any

from .. import sensors as radar
from ..air import ACTIVE_STATUSES, Flight, FlightStatus, Task
from ..air_defense import AirDefenseUnit, SamRole, SamStatus
from ..adapters.base import PlayerKind
from ..forces import Convoy, Division, EntityKind
from ..geo import Vec2
from ..state import CampaignState, Timer


class SensorKind(StrEnum):
    """Kind of air-search sensor."""
    #: An AWACS on station: all round, does not identify.
    AWACS = "awacs"
    #: An aircraft's radar (a cone), or a player's (all round).
    AIRCRAFT = "aircraft"
    #: An air defence site's search radar.
    GROUND_RADAR = "ground_radar"


class SizeClass(StrEnum):
    """How many aircraft a flight contact seems to be, when not identified."""
    #: One aircraft.
    SINGLE = "single"
    #: Two aircraft.
    PAIR = "pair"
    #: Three or more.
    GROUP = "group"


#: Aircraft assumed in a flight contact of each size class.
CLASS_SIZE = {SizeClass.SINGLE: 1, SizeClass.PAIR: 2, SizeClass.GROUP: 4}


#: Source of the contacts every faction starts with.
PREWAR_SOURCE = "pre-war intelligence"
#: Name of a flight contact whose type is not known.
UNIDENTIFIED = "unidentified aircraft"
#: Seconds between two sweeps of a faction's sensors.
SCAN_INTERVAL_S = 60.0
#: Contacts are forgotten after this long without news, by kind.
TTL_S = {
    EntityKind.FLIGHT: 5 * 60.0,
    EntityKind.DIVISION: 12 * 3600.0,
    EntityKind.CONVOY: 6 * 3600.0,
    EntityKind.SAM: 48 * 3600.0
}

#: Contacts are shown as stale (dashed) after this long, by kind.
STALE_S = {
    EntityKind.FLIGHT: 90.0,
    EntityKind.DIVISION: 3600.0,
    EntityKind.CONVOY: 1800.0,
    EntityKind.SAM: 6 * 3600.0
}

#: Range of ground observation, by kind of target.
RECON_M = {
    EntityKind.DIVISION: 12_000.0,
    EntityKind.CONVOY: 3_000.0,
    EntityKind.SAM: 5_000.0
}

#: Air-search range credited to a player's aircraft, all round (its heading is not known).
PLAYER_RADAR_M = 80_000.0
#: Range at which any player, in the air or on the ground, spots enemy ground units.
PLAYER_EYES_M = 10_000.0
#: Friendly aircraft identify what they track this close.
IDENT_RANGE_M = 40_000.0

#: Range factor of each kind of air sensor against a target in the ground clutter.
LOW_FACTOR = {
    SensorKind.GROUND_RADAR: 0.3,
    SensorKind.AWACS: 0.6,
    SensorKind.AIRCRAFT: radar.LOOK_DOWN
}

#: ELINT range of an AWACS on station: it locates emitting radars and jamming SEAD flights.
ELINT_AWACS_M = 250_000.0
#: ELINT range of a SEAD flight (emitting radars and jamming SEAD flights).
ELINT_SEAD_M = 150_000.0
#: ELINT range of an operational EWR (emitting radars and jamming SEAD flights).
ELINT_EWR_M = 120_000.0
#: How far off its real position ELINT places a jammer.
JAM_FIX_ERROR_M = 8_000.0


@dataclass
class Contact:
    """What a faction knows about one enemy entity.

    Attributes:
        entity_id: Id of the enemy entity.
        kind: What the entity is.
        faction: The enemy entity's faction.
        position: Last known position.
        seen: Campaign time of the last detection.
        first_seen: Campaign time of the first detection.
        source: Sensor of the last detection (e.g. ``"AWACS Darkstar 1"``).
        identified: Its type is known (else an unidentified track).
        detail: What is known about it (type, name, size, heading...).
    """

    entity_id: str
    kind: EntityKind
    faction: str
    position: Vec2 | None
    seen: float
    first_seen: float
    source: str
    identified: bool
    detail: dict[str, Any] = field(default_factory=dict)

    def age(self, now: float) -> float:
        """Seconds since the last detection.

        Args:
            now: Current campaign time.

        Returns:
            The age in seconds.
        """
        return now - self.seen

    def stale(self, now: float) -> bool:
        """True if the information is getting old.

        Args:
            now: Current campaign time.

        Returns:
            Whether it is older than its kind's ``STALE_S``.
        """
        return self.age(now) > STALE_S[self.kind]


# ---------------------------------------------------------------- queries


def picture(state: CampaignState, fid: str) -> dict[str, Contact]:
    """The intelligence picture of a faction (entity id -> contact).

    Args:
        state: The campaign.
        fid: Faction id.

    Returns:
        The faction's contacts, created empty if needed (a live dictionary).
    """
    return state.intel.setdefault(fid, {})


def known(state: CampaignState, fid: str, kind: EntityKind | None = None, max_age: float | None = None) -> list[Contact]:
    """Contacts of a faction, optionally of one kind and not older than ``max_age``.

    Args:
        state: The campaign.
        fid: Faction id.
        kind: Contact kind (None = any).
        max_age: Maximum age in seconds (None = any).

    Returns:
        The matching contacts.
    """
    now = state.time
    return [
        c for c in picture(state, fid).values()
        if (kind is None or c.kind == kind) and (max_age is None or c.age(now) <= max_age)
    ]


def knows(state: CampaignState, fid: str, entity_id: str, max_age: float | None = None) -> Contact | None:
    """The contact on an entity, if the faction knows about it (recently enough).

    Args:
        state: The campaign.
        fid: Faction id.
        entity_id: The entity looked for.
        max_age: Maximum age in seconds (None = any).

    Returns:
        The contact, or None.
    """
    c = picture(state, fid).get(entity_id)
    if c is None or (max_age is not None and c.age(state.time) > max_age):
        return None
    return c


def known_divisions_in(state: CampaignState, fid: str, pid: str, max_age: float = 6 * 3600.0) -> list[Division]:
    """Enemy brigades the faction believes to be in province ``pid``.

    Args:
        state: The campaign.
        fid: Faction id.
        pid: Province id.
        max_age: Maximum age of the contacts in seconds.

    Returns:
        The brigades last reported in the province.
    """
    out = []
    for c in known(state, fid, EntityKind.DIVISION, max_age):
        if c.detail.get("province") == pid and c.entity_id in state.divisions:
            out.append(state.divisions[c.entity_id])
    return out


# ---------------------------------------------------------------- updates


def _describe(state: CampaignState, ent, identified: bool) -> dict[str, Any]:
    """What a detection tells about an entity.

    Args:
        state: The campaign.
        ent: The detected entity.
        identified: Whether the detection identifies it (type, name).

    Returns:
        The details for the contact.
    """
    t = state.theater
    if isinstance(ent, Division):
        return {
            "name": ent.name,
            "template": ent.template.id,
            "template_name": ent.template.name,
            "strength": round(ent.strength, 1),
            "moving": ent.movement is not None,
            "province": ent.location,
        }
    if isinstance(ent, Convoy):
        return {"trucks": round(ent.trucks), "province": ent.location}
    if isinstance(ent, AirDefenseUnit):
        return {
            "name": ent.name,
            "system": ent.system.id,
            "system_name": ent.system.name,
            "role": ent.system.role.value,
            "range_km": round(ent.system.range_m / 1000, 1),
            "status": ent.status.value,
            "province": ent.location,
        }
    # flight
    size = ent.size
    out = {
        "heading_deg": round((ent.heading(t) * 57.29578) % 360),
        "size_class": SizeClass.SINGLE if size <= 1 else (SizeClass.PAIR if size == 2 else SizeClass.GROUP),
        "helo": ent.aircraft_type.helo,
    }

    if identified:
        out.update(aircraft=ent.aircraft_type.id, aircraft_name=ent.aircraft_type.name, size=size)

    return out


def observe(
    state: CampaignState,
    fid: str,
    ent,
    kind: EntityKind | None,
    source: str,
    identified: bool = True,
    pos: Vec2 | None = None
) -> Contact:
    """Record a detection of ``ent`` by faction ``fid`` (at ``pos`` if already known).

    Args:
        state: The campaign.
        fid: Observing faction.
        ent: The detected entity.
        kind: Contact kind (None: the entity's own).
        source: Which sensor made the detection, for the picture.
        identified: Whether the detection identifies the entity.
        pos: Where it was seen (None: its true position).

    Returns:
        The new or updated contact.
    """
    pic = picture(state, fid)
    if pos is None:
        pos = ent.position(state.theater)
    c = pic.get(ent.id)
    ident = identified or (c is not None and c.identified and c.kind == EntityKind.FLIGHT and c.age(state.time) < 600)
    detail = _describe(state, ent, ident)
    if c is None:
        c = Contact(ent.id, kind or ent.kind, ent.faction, pos, state.time, state.time, source, ident, detail)
        pic[ent.id] = c
    else:
        c.position = pos
        c.seen = state.time
        c.source = source
        c.identified = ident
        c.detail = detail

    return c


def reveal(state: CampaignState, fid: str, ent, source: str) -> None:
    """An event gave the entity away (a SAM launch, an attack): record it.

    Args:
        state: The campaign.
        fid: Faction that learns about it.
        ent: The entity.
        source: What gave it away.
    """
    observe(state, fid, ent, None, source, identified=True)


def seed_prewar(state: CampaignState) -> None:
    """Pre-war intelligence: every faction starts knowing the enemy's brigades and
    air defence sites where they were deployed at the start.

    Args:
        state: The campaign.
    """
    for fid in state.factions:
        for division in state.divisions.values():
            if state.at_war(fid, division.faction):
                observe(
                    state,
                    fid,
                    division,
                    EntityKind.DIVISION,
                    PREWAR_SOURCE
                )

        for sam in state.air_defense.values():
            if state.at_war(fid, sam.faction):
                observe(
                    state,
                    fid,
                    sam,
                    EntityKind.SAM,
                    PREWAR_SOURCE
                )


# ---------------------------------------------------------------- scanning


#: Positions of entities computed during one scan round, by entity id. A round
#: makes a fresh one and passes it down; nothing outlives the round.
Positions = dict[str, Vec2]


def _pos(state: CampaignState, ent, cache: Positions | None) -> Vec2:
    """Position of an entity, computed once per scan round.

    Args:
        state: The campaign.
        ent: The entity.
        cache: The round's positions (None: compute it, no caching).

    Returns:
        The position now.
    """
    if cache is None:
        return ent.position(state.theater)
    p = cache.get(ent.id)
    if p is None:
        p = cache[ent.id] = ent.position(state.theater)
    return p


def run(state: CampaignState, dt: float) -> None:
    """Seed the pre-war picture once, then scan every ``SCAN_INTERVAL_S``.

    Args:
        state: The campaign.
        dt: Campaign seconds elapsed (unused: scans run on their own timer).
    """
    if Timer.INTEL_SEEDED not in state.timers:
        state.timers[Timer.INTEL_SEEDED] = state.time
        seed_prewar(state)

    if state.time < state.timers.get(Timer.INTEL, 0.0):
        return

    state.timers[Timer.INTEL] = state.time + SCAN_INTERVAL_S
    cache: Positions = {}  # nothing moves during the round: every faction shares it
    for fid in state.factions:
        scan(state, fid, cache)
        _expire(state, fid)


def _expire(state: CampaignState, fid: str) -> None:
    """Forget contacts that are too old or whose entity no longer exists.

    Args:
        state: The campaign.
        fid: Faction id.
    """
    pic = picture(state, fid)
    for eid, c in list(pic.items()):
        if c.age(state.time) > TTL_S[c.kind] or state.entity(eid) is None:
            del pic[eid]


def air_sensors(
    state: CampaignState,
    fid: str,
    cache: Positions | None = None
) -> list[tuple[Vec2, float, SensorKind, str, bool, Flight | None]]:
    """Friendly air-search sensors: (position, range, kind, name, can identify,
    the aircraft carrying it: its radar looks along a cone, see :mod:`dynacamp.sensors`).

    Args:
        state: The campaign.
        fid: Faction id.
        cache: Positions of the current scan round (None: computed afresh).

    Returns:
        The sensors.
    """
    out: list[tuple[Vec2, float, SensorKind, str, bool, Flight | None]] = []
    for f in state.flights.values():
        if not f.same_faction(fid) or not f.has_active_status or f.empty:
            continue

        r = f.aircraft_type.air_radar_m

        if f.is_awacs and f.on_station:
            out.append(
                (
                    _pos(state, f, cache),
                    r,
                    SensorKind.AWACS,
                    f"AWACS {f.callsign}",
                    False,
                    None
                 )
            )

        elif f.aircraft_type.has_aa_radar:
            out.append((_pos(state, f, cache), r, SensorKind.AIRCRAFT, f.callsign, True, f))

    for u in state.air_defense.values():
        if u.faction != fid or not u.operational or u.status is not SamStatus.ACTIVE or not u.system.radar:
            continue
        out.append((_pos(state, u, cache), u.system.detection_m, SensorKind.GROUND_RADAR, f"{u.system.name} radar", False, None))

    coalition = state.factions[fid].coalition

    for pid, (pcoalition, pos, pkind) in state.player_positions.items():
        if pcoalition == coalition and pkind is PlayerKind.AIR:  # heading unknown: all round
            out.append((pos, PLAYER_RADAR_M, SensorKind.AIRCRAFT, f"player {pid}", True, None))

    return out


def ground_observers(state: CampaignState, fid: str, cache: Positions | None = None) -> list[tuple[Vec2, float, str]]:
    """Friendly observers of ground units: (position, range, name).

    Args:
        state: The campaign.
        fid: Faction id.
        cache: Positions of the current scan round (None: computed afresh).

    Returns:
        The observers.
    """
    out: list[tuple[Vec2, float, str]] = []

    for d in state.divisions.values():
        if d.faction == fid:
            out.append((_pos(state, d, cache), RECON_M[EntityKind.DIVISION], f"recon {d.name}"))

    for c in state.convoys.values():
        if c.faction == fid:
            out.append((_pos(state, c, cache), RECON_M[EntityKind.CONVOY], f"convoy {c.id}"))

    for u in state.air_defense.values():
        if u.faction == fid:
            out.append((_pos(state, u, cache), RECON_M[EntityKind.SAM], u.name))

    for f in state.flights.values():
        if f.faction == fid and f.status in ACTIVE_STATUSES and f.aircraft_type.ground_sensor_m > 0:
            out.append((_pos(state, f, cache), f.aircraft_type.ground_sensor_m, f.callsign))
    coalition = state.factions[fid].coalition

    for pid, (pcoalition, pos, _) in state.player_positions.items():
        if pcoalition == coalition:
            out.append((pos, PLAYER_EYES_M, f"player {pid}"))

    return out


def scan(state: CampaignState, fid: str, cache: Positions | None = None) -> None:
    """One sweep of all the faction's sensors.

    Args:
        state: The campaign.
        fid: Faction id.
        cache: Positions of the current scan round (None: a round of its own).
    """

    cache = {} if cache is None else cache
    t = state.theater
    # -- air picture
    sensors = air_sensors(state, fid, cache)
    for f in state.flights.values():
        if not state.at_war(fid, f.faction) or f.status not in ACTIVE_STATUSES or f.ai_count + len(f.active_seats) <= 0:
            continue
        pos = _pos(state, f, cache)
        low = radar.low(f)
        jamming = radar.jam(state, f)
        seen_by, identify = None, False
        for spos, rng, skind, name, can_id, carrier in sensors:
            d = spos.distance_to(pos)
            if carrier is not None:
                seen = radar.radar_sees(carrier, spos, f, pos, jamming=jamming)  # the cone ahead of the aircraft
            else:
                seen = d <= radar.reach(rng * (LOW_FACTOR[skind] if low else 1.0), jamming)
            if seen:
                seen_by = seen_by or name
                if can_id and d <= IDENT_RANGE_M:
                    identify, seen_by = True, name
                    break
        if seen_by:
            observe(state, fid, f, EntityKind.FLIGHT, seen_by, identified=identify, pos=pos)

    # -- ground picture
    observers = ground_observers(state, fid, cache)
    targets = [
        (EntityKind.DIVISION, d)
        for d in state.divisions.values()
        if state.at_war(fid, d.faction)
    ]

    targets += [
        (EntityKind.CONVOY, c)
        for c in state.convoys.values()
        if state.at_war(fid, c.faction)
    ]

    targets += [
        (EntityKind.SAM, u)
        for u in state.air_defense.values()
        if state.at_war(fid, u.faction)
    ]

    for kind, ent in targets:
        pos = _pos(state, ent, cache)
        for opos, rng, name in observers:
            if abs(opos.x - pos.x) <= rng and abs(opos.y - pos.y) <= rng and opos.distance_to(pos) <= rng:
                observe(state, fid, ent, kind, name, pos=pos)
                break
    for b in state.battles.values():
        if fid in (b.attacker_faction, b.defender_faction):
            for i in b.attackers + b.defenders:
                d = state.divisions.get(i)
                if d is not None and state.at_war(fid, d.faction):
                    observe(state, fid, d, EntityKind.DIVISION, f"contact at {t.provinces[b.province].name}")

    # -- ELINT: emitting radars
    listeners: list[tuple[Vec2, float, str]] = []
    for f in state.flights.values():
        if f.faction != fid or f.status not in ACTIVE_STATUSES:
            continue

        if f.task is Task.AWACS and f.status is FlightStatus.ON_STATION:
            listeners.append((_pos(state, f, cache), ELINT_AWACS_M, f"ELINT AWACS {f.callsign}"))

        elif f.task is Task.SEAD:
            listeners.append((_pos(state, f, cache), ELINT_SEAD_M, f"ELINT {f.callsign}"))

    for u in state.air_defense.values():
        if u.faction == fid and u.system.role is SamRole.EWR and u.operational:
            listeners.append((_pos(state, u, cache), ELINT_EWR_M, f"ELINT {u.name}"))

    for u in state.air_defense.values():
        if not state.at_war(fid, u.faction) or not (u.emitting or (u.system.role is SamRole.EWR and u.operational)):
            continue
        pos = _pos(state, u, cache)

        for lpos, rng, name in listeners:
            if lpos.distance_to(pos) <= rng:
                observe(state, fid, u, EntityKind.SAM, name)
                break

    # Jammers are loud: ELINT finds a SEAD flight jamming, roughly (a strobe, not a track).
    for f in state.flights.values():
        if not state.at_war(fid, f.faction) or f.task is not Task.SEAD or f.status not in ACTIVE_STATUSES or f.ai_count <= 0:
            continue

        c = picture(state, fid).get(f.id)

        if c is not None and c.seen == state.time:
            continue  # radar has it this sweep, precisely

        pos = _pos(state, f, cache)

        for lpos, rng, name in listeners:
            if lpos.distance_to(pos) <= rng:
                observe(
                    state,
                    fid,
                    f,
                    EntityKind.FLIGHT,
                    f"jamming strobe ({name})",
                    identified=False,
                    pos=_strobe(state, f, pos)
                )
                break


def _strobe(state: CampaignState, f, pos: Vec2) -> Vec2:
    """Where ELINT puts a jammer: ``JAM_FIX_ERROR_M`` off its real position, in
    a direction that changes every 10 minutes (stable for a given flight and time).

    Args:
        state: The campaign.
        f: The jamming flight.
        pos: Its true position.

    Returns:
        The estimated position.
    """
    seed = zlib.crc32(f"{f.id}:{int(state.time // 600)}".encode())
    a = (seed % 3600) / 3600 * 2 * math.pi
    return Vec2(
        pos.x + JAM_FIX_ERROR_M * math.sin(a),
        pos.y + JAM_FIX_ERROR_M * math.cos(a)
    )


def known_covering(state: CampaignState, fid: str, point: Vec2, roles=(SamRole.LONG, SamRole.MEDIUM)) -> list[Contact]:
    """Known enemy SAM sites whose engagement range covers ``point``, at their
    last known position (what the planners believe, not the truth).

    Args:
        state: The campaign.
        fid: Faction id.
        point: The point.
        roles: SAM roles to consider.

    Returns:
        The contacts on those sites.
    """
    out = []
    for c in known(state, fid, EntityKind.SAM):
        u = state.air_defense.get(c.entity_id)
        if u is None or u.system.role not in roles:
            continue
        if c.position and c.position.distance_to(point) <= u.system.range_m:
            out.append(c)
    return out


def located(state: CampaignState, fid: str, entity_id: str, max_age: float) -> Vec2 | None:
    """Last known position of an enemy entity if located within ``max_age``.

    Args:
        state: The campaign.
        fid: Faction id.
        entity_id: The enemy entity.
        max_age: Maximum age of the contact in seconds.

    Returns:
        The position, or None.
    """
    c = knows(state, fid, entity_id, max_age)
    return None if c is None else c.position
