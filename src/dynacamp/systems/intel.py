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
from ..air import ACTIVE_STATUSES, Flight, FlightStatus, Task, flight_altitude
from ..air_defense import AirDefenseUnit, SamRole, SamStatus
from ..forces import Convoy, Division
from ..geo import Vec2
from ..state import CampaignState


class ContactKind(StrEnum):
    """Kind of contact"""
    DIVISION = "division"
    CONVOY = "convoy"
    FLIGHT = "flight"
    SAM = "sam"


SCAN_INTERVAL_S = 60.0
# Contacts are forgotten after this long without news...
TTL_S = {
    ContactKind.FLIGHT: 5 * 60.0,
    ContactKind.DIVISION: 12 * 3600.0,
    ContactKind.CONVOY: 6 * 3600.0,
    ContactKind.SAM: 48 * 3600.0
}

# ...and shown as stale (dashed) after this long.
STALE_S = {
    ContactKind.FLIGHT: 90.0,
    ContactKind.DIVISION: 3600.0,
    ContactKind.CONVOY: 1800.0,
    ContactKind.SAM: 6 * 3600.0
}

RECON_M = {
    ContactKind.DIVISION: 12_000.0,
    ContactKind.CONVOY: 3_000.0,
    ContactKind.SAM: 5_000.0
}

PLAYER_RADAR_M = 80_000.0
PLAYER_EYES_M = 10_000.0
IDENT_RANGE_M = 40_000.0  # friendly aircraft identify what they track this close
LOW_ALT_M = radar.LOW_ALT_M

LOW_FACTOR = {
    "ground_radar": 0.3,
    "awacs": 0.6,
    "aircraft": radar.LOOK_DOWN
}

ELINT_AWACS_M = 250_000.0
ELINT_SEAD_M = 150_000.0
ELINT_EWR_M = 120_000.0
JAM_FIX_ERROR_M = 8_000.0  # how far off ELINT places a jammer


@dataclass
class Contact:
    """What a faction knows about one enemy entity.

    Attributes:
        entity_id: Id of the enemy entity.
        kind: ``division``, ``convoy``, ``flight`` or ``sam``.
        faction: The enemy entity's faction.
        position: Last known position.
        seen: Campaign time of the last detection.
        first_seen: Campaign time of the first detection.
        source: Sensor of the last detection (e.g. ``"AWACS Darkstar 1"``).
        identified: Its type is known (else an unidentified track).
        detail: What is known about it (type, name, size, heading...).
    """

    entity_id: str
    kind: ContactKind
    faction: str
    position: Vec2 | None
    seen: float
    first_seen: float
    source: str
    identified: bool
    detail: dict[str, Any] = field(default_factory=dict)

    def age(self, now: float) -> float:
        """Seconds since the last detection."""
        return now - self.seen

    def stale(self, now: float) -> bool:
        """True if the information is getting old."""
        return self.age(now) > STALE_S[self.kind]


# ---------------------------------------------------------------- queries


def picture(state: CampaignState, fid: str) -> dict[str, Contact]:
    """The intelligence picture of a faction (entity id -> contact)."""
    return state.intel.setdefault(fid, {})


def known(state: CampaignState, fid: str, kind: ContactKind | None = None, max_age: float | None = None) -> list[Contact]:
    """Contacts of a faction, optionally of one kind and not older than ``max_age``."""
    now = state.time
    return [
        c for c in picture(state, fid).values()
        if (kind is None or c.kind == kind) and (max_age is None or c.age(now) <= max_age)
    ]


def knows(state: CampaignState, fid: str, entity_id: str, max_age: float | None = None) -> Contact | None:
    """The contact on an entity, if the faction knows about it (recently enough)."""
    c = picture(state, fid).get(entity_id)
    if c is None or (max_age is not None and c.age(state.time) > max_age):
        return None
    return c


def known_divisions_in(state: CampaignState, fid: str, pid: str, max_age: float = 6 * 3600.0) -> list[Division]:
    """Enemy brigades the faction believes to be in province ``pid``."""
    out = []
    for c in known(state, fid, ContactKind.DIVISION, max_age):
        if c.detail.get("province") == pid and c.entity_id in state.divisions:
            out.append(state.divisions[c.entity_id])
    return out


# ---------------------------------------------------------------- updates


def _describe(state: CampaignState, ent, identified: bool) -> dict[str, Any]:
    """What a detection tells about an entity."""
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
        "size_class": "single" if size <= 1 else ("pair" if size == 2 else "group"),
        "helo": ent.aircraft_type.helo,
    }

    if identified:
        out.update(aircraft=ent.aircraft_type.id, aircraft_name=ent.aircraft_type.name, size=size)

    return out


def observe(
    state: CampaignState,
    fid: str,
    ent,
    kind: ContactKind | str | None,
    source: str,
    identified: bool = True,
    pos: Vec2 | None = None
) -> Contact:
    """Record a detection of ``ent`` by faction ``fid`` (at ``pos`` if already known)."""
    pic = picture(state, fid)
    if pos is None:
        pos = ent.position(state.theater)
    c = pic.get(ent.id)
    ident = identified or (c is not None and c.identified and c.kind == ContactKind.FLIGHT and c.age(state.time) < 600)
    detail = _describe(state, ent, ident)
    if c is None:
        c = Contact(ent.id, kind, ent.faction, pos, state.time, state.time, source, ident, detail)
        pic[ent.id] = c
    else:
        c.position = pos
        c.seen = state.time
        c.source = source
        c.identified = ident
        c.detail = detail

    return c


def reveal(state: CampaignState, fid: str, ent, source: str) -> None:
    """An event gave the entity away (a SAM launch, an attack): record it."""
    kind = {item.value: item.value for item in ContactKind}.get(ent.kind, ent.kind)    
    observe(state, fid, ent, kind, source, identified=True)


def seed_prewar(state: CampaignState) -> None:
    """Pre-war intelligence: every faction starts knowing the enemy's brigades and
    air defence sites where they were deployed at the start."""
    for fid in state.factions:
        for division in state.divisions.values():
            if state.at_war(fid, division.faction):
                observe(
                    state,
                    fid,
                    division,
                    ContactKind.DIVISION,
                    "pre-war intelligence"
                )

        for sam in state.air_defense.values():
            if state.at_war(fid, sam.faction):
                observe(
                    state,
                    fid,
                    sam,
                    ContactKind.SAM,
                    "pre-war intelligence"
                )


# ---------------------------------------------------------------- scanning


def _pos(state: CampaignState, ent) -> Vec2:
    """Position of an entity, computed once per scan."""
    p = _POS.get(ent.id)
    if p is None:
        p = _POS[ent.id] = ent.position(state.theater)
    return p


_POS: dict[str, Vec2] = {}


def run(state: CampaignState, dt: float) -> None:
    """Seed the pre-war picture once, then scan every ``SCAN_INTERVAL_S``."""
    if "intel_seeded" not in state.timers:
        state.timers["intel_seeded"] = state.time
        seed_prewar(state)

    if state.time < state.timers.get("intel", 0.0):
        return

    state.timers["intel"] = state.time + SCAN_INTERVAL_S
    _POS.clear()
    for fid in state.factions:
        scan(state, fid)
        _expire(state, fid)


def _expire(state: CampaignState, fid: str) -> None:
    """Forget contacts that are too old or whose entity no longer exists."""
    pic = picture(state, fid)
    for eid, c in list(pic.items()):
        if c.age(state.time) > TTL_S[c.kind] or state.entity(eid) is None:
            del pic[eid]


def air_sensors(state: CampaignState, fid: str) -> list[tuple[Vec2, float, str, str, bool, Flight | None]]:
    """Friendly air-search sensors: (position, range, kind, name, can identify,
    the aircraft carrying it: its radar looks along a cone, see :mod:`dynacamp.sensors`)."""
    t = state.theater
    out: list[tuple[Vec2, float, str, str, bool, Flight | None]] = []
    for f in state.flights.values():
        if f.faction != fid or f.status not in ACTIVE_STATUSES or f.size <= 0:
            continue
        r = f.aircraft_type.air_radar_m

        if f.task is Task.AWACS and f.status is FlightStatus.ON_STATION:
            out.append((_pos(state, f), r, "awacs", f"AWACS {f.callsign}", False, None))

        elif r > 0:
            out.append((_pos(state, f), r, "aircraft", f.callsign, True, f))

    for u in state.air_defense.values():
        if u.faction != fid or not u.operational or u.status is not SamStatus.ACTIVE or not u.system.radar:
            continue
        out.append((_pos(state, u), u.system.detection_m, "ground_radar", f"{u.system.name} radar", False, None))

    side = state.factions[fid].side

    for pid, (pside, pos, pkind) in state.player_positions.items():
        if pside == side and pkind == "air":  # heading unknown: all round
            out.append((pos, PLAYER_RADAR_M, "aircraft", f"player {pid}", True, None))

    return out


def ground_observers(state: CampaignState, fid: str) -> list[tuple[Vec2, float, str]]:
    """Friendly observers of ground units: (position, range, name)."""
    t = state.theater
    out: list[tuple[Vec2, float, str]] = []

    for d in state.divisions.values():
        if d.faction == fid:
            out.append((_pos(state, d), RECON_M[ContactKind.DIVISION], f"recon {d.name}"))

    for c in state.convoys.values():
        if c.faction == fid:
            out.append((_pos(state, c), RECON_M[ContactKind.CONVOY], f"convoy {c.id}"))

    for u in state.air_defense.values():
        if u.faction == fid:
            out.append((_pos(state, u), RECON_M[ContactKind.SAM], u.name))

    for f in state.flights.values():
        if f.faction == fid and f.status in ACTIVE_STATUSES and f.aircraft_type.ground_sensor_m > 0:
            out.append((_pos(state, f), f.aircraft_type.ground_sensor_m, f.callsign))
    side = state.factions[fid].side

    for pid, (pside, pos, _) in state.player_positions.items():
        if pside == side:
            out.append((pos, PLAYER_EYES_M, f"player {pid}"))

    return out


def scan(state: CampaignState, fid: str) -> None:
    """One sweep of all the faction's sensors."""
    from . import air_defense

    t = state.theater
    # -- air picture
    sensors = air_sensors(state, fid)
    for f in state.flights.values():
        if not state.at_war(fid, f.faction) or f.status not in ACTIVE_STATUSES or f.ai_count + len(f.active_seats) <= 0:
            continue
        pos = _pos(state, f)
        low = flight_altitude(f) <= LOW_ALT_M
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
            observe(state, fid, f, ContactKind.FLIGHT, seen_by, identified=identify, pos=pos)

    # -- ground picture
    observers = ground_observers(state, fid)
    targets = [
        (ContactKind.DIVISION, d)
        for d in state.divisions.values()
        if state.at_war(fid, d.faction)
    ]
    
    targets += [
        (ContactKind.CONVOY, c)
        for c in state.convoys.values()
        if state.at_war(fid, c.faction)
    ]
    
    targets += [
        (ContactKind.SAM, u)
        for u in state.air_defense.values()
        if state.at_war(fid, u.faction)
    ]
    
    for kind, ent in targets:
        pos = _pos(state, ent)
        for opos, rng, name in observers:
            if abs(opos.x - pos.x) <= rng and abs(opos.y - pos.y) <= rng and opos.distance_to(pos) <= rng:
                observe(state, fid, ent, kind, name, pos=pos)
                break
    for b in state.battles.values():
        if fid in (b.attacker_faction, b.defender_faction):
            for i in b.attackers + b.defenders:
                d = state.divisions.get(i)
                if d is not None and state.at_war(fid, d.faction):
                    observe(state, fid, d, ContactKind.DIVISION, f"contact at {t.provinces[b.province].name}")

    # -- ELINT: emitting radars
    listeners: list[tuple[Vec2, float, str]] = []
    for f in state.flights.values():
        if f.faction != fid or f.status not in ACTIVE_STATUSES:
            continue

        if f.task is Task.AWACS and f.status is FlightStatus.ON_STATION:
            listeners.append((_pos(state, f), ELINT_AWACS_M, f"ELINT AWACS {f.callsign}"))

        elif f.task is Task.SEAD:
            listeners.append((_pos(state, f), ELINT_SEAD_M, f"ELINT {f.callsign}"))

    for u in state.air_defense.values():
        if u.faction == fid and u.system.role is SamRole.EWR and u.operational:
            listeners.append((_pos(state, u), ELINT_EWR_M, f"ELINT {u.name}"))

    for u in state.air_defense.values():
        if not state.at_war(fid, u.faction) or not (u.emitting or (u.system.role is SamRole.EWR and u.operational)):
            continue
        pos = _pos(state, u)
        
        for lpos, rng, name in listeners:
            if lpos.distance_to(pos) <= rng:
                observe(state, fid, u, ContactKind.SAM, name)
                break
    
    # Jammers are loud: ELINT finds a SEAD flight jamming, roughly (a strobe, not a track).
    for f in state.flights.values():
        if not state.at_war(fid, f.faction) or f.task is not Task.SEAD or f.status not in ACTIVE_STATUSES or f.ai_count <= 0:
            continue
        
        c = picture(state, fid).get(f.id)

        if c is not None and c.seen == state.time:
            continue  # radar has it this sweep, precisely

        pos = _pos(state, f)

        for lpos, rng, name in listeners:
            if lpos.distance_to(pos) <= rng:
                observe(
                    state,
                    fid,
                    f,
                    ContactKind.FLIGHT,
                    f"jamming strobe ({name})",
                    identified=False,
                    pos=_strobe(state, f, pos)
                )
                break


def _strobe(state: CampaignState, f, pos: Vec2) -> Vec2:
    """Where ELINT puts a jammer: ``JAM_FIX_ERROR_M`` off its real position, in
    a direction that changes every 10 minutes (stable for a given flight and time)."""
    seed = zlib.crc32(f"{f.id}:{int(state.time // 600)}".encode())
    a = (seed % 3600) / 3600 * 2 * math.pi
    return Vec2(
        pos.x + JAM_FIX_ERROR_M * math.sin(a),
        pos.y + JAM_FIX_ERROR_M * math.cos(a)
    )


def known_covering(state: CampaignState, fid: str, point: Vec2, roles=(SamRole.LONG, SamRole.MEDIUM)) -> list[Contact]:
    """Known enemy SAM sites whose engagement range covers ``point``, at their
    last known position (what the planners believe, not the truth)."""
    out = []
    for c in known(state, fid, ContactKind.SAM):
        u = state.air_defense.get(c.entity_id)
        if u is None or u.system.role not in roles:
            continue
        if c.position and c.position.distance_to(point) <= u.system.range_m:
            out.append(c)
    return out


def located(state: CampaignState, fid: str, entity_id: str, max_age: float) -> Vec2 | None:
    """Last known position of an enemy entity if located within ``max_age``."""
    c = knows(state, fid, entity_id, max_age)
    return None if c is None else c.position
