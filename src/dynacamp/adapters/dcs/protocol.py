"""Translation between campaign messages and the DCS bridge's JSON lines.

Pure, socket-free and therefore unit-testable. The Lua side is kept dumb on
purpose: Python builds complete DCS group tables, Lua just calls
``coalition.addGroup`` and reports unit names and positions back.

Frames: campaign (x east, y north) <-> DCS (x north, z east). In DCS group
tables the east coordinate is called ``y``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ...air import AIRCRAFT_ELEMENT, Task
from ...forces import TRUCK, EntityKind
from ...geo import Vec2
from ..base import (
    Side,
    BriefingRequest,
    Command,
    DematerializeAck,
    Dematerialize,
    EntitySnapshot,
    EntityState,
    GameDisconnected,
    JoinRequest,
    LeaveRequest,
    Materialize,
    MaterializeAck,
    NotifyPlayer,
    PlayerEvent,
    PlayerEventKind,
    PlayerKind,
    PlayerLeft,
    PlayerSeen,
    PublishAto,
    Report,
    UpdateRoute,
)
from . import catalog


class Cmd(StrEnum):
    """Commands to the Lua bridge (``cmd`` of an outgoing message)."""
    #: Create the groups of an entity.
    SPAWN = "spawn"
    #: Remove an entity's groups and report their last state.
    DESPAWN = "despawn"
    #: Give an entity's groups a new route.
    ROUTE = "route"
    #: The packages a coalition's players can join (F10 menu).
    ATO = "ato"
    #: Text and map marks for one player.
    NOTIFY = "notify"


class Msg(StrEnum):
    """Messages from the Lua bridge (``msg`` of an incoming line)."""
    #: The human players in the game, with position, coalition and category.
    PLAYERS = "players"
    #: Outcome of a spawn command.
    SPAWNED = "spawned"
    #: Alive units and positions of the spawned entities.
    STATE = "state"
    #: An entity was removed: its units at that moment.
    DESPAWNED = "despawned"
    #: A player asks for a seat in a flight (F10 menu).
    JOIN = "join"
    #: A player leaves their seat (F10 menu).
    LEAVE = "leave"
    #: A player asks for their briefing (F10 menu).
    BRIEFING = "briefing"
    #: Takeoff, landing or loss of a player's aircraft.
    PLAYER_EVENT = "player_event"
    #: Queued by the adapter itself when the connection drops.
    DISCONNECTED = "_disconnected"


class GroupCategory(StrEnum):
    """Category of a group the bridge spawns (``coalition.addGroup``): a DCS enum."""
    #: Ground vehicles.
    GROUND = "Group.Category.GROUND"
    #: Fixed-wing aircraft.
    AIRPLANE = "Group.Category.AIRPLANE"
    #: Helicopters.
    HELICOPTER = "Group.Category.HELICOPTER"


# -- DCS enums --
# DCS numbers never appear here: an enum travels by name, as ``{"enum": "<path>"}``
# (see :func:`dcs_enum`), and the bridge looks it up in DCS' own tables. What the
# bridge reports comes back the same way, as the name of the DCS enum key.

#: DCS AI option "ECM using" (to verify in DCS that AI jamming follows it).
ECM_USING = "AI.Option.Air.id.ECM_USING"
#: Value "always on" of the ECM option.
ECM_ALWAYS = "AI.Option.Air.val.ECM_USING.ALWAYS_USE"
#: Weapon choice "auto" of the bombing task.
WEAPON_AUTO = "Weapon.flag.Auto"
#: Values the bridge falls back to if DCS lacks an enum: only for the names above
#: not yet checked in the game. The bridge logs when it uses one.
ENUM_FALLBACK = {ECM_USING: 13, ECM_ALWAYS: 3, WEAPON_AUTO: 1073741822}
#: Campaign side of each DCS coalition (``coalition.side`` key).
COALITION_SIDE = {"RED": Side.RED, "BLUE": Side.BLUE}
#: DCS coalition (``coalition.side`` key) of each campaign side.
SIDE_COALITION = {v: k for k, v in COALITION_SIDE.items()}
#: Kind of player by the DCS category of their unit (``Unit.Category`` key).
CATEGORY_KIND = {
    "AIRPLANE": PlayerKind.AIR,
    "HELICOPTER": PlayerKind.HELO,
    "GROUND_UNIT": PlayerKind.GROUND,
    "SHIP": PlayerKind.GROUND,
}

# -- DCS mission vocabulary --

#: Altitude above ground level (helicopters).
ALT_RADIO = "RADIO"
#: Altitude above sea level.
ALT_BARO = "BARO"
#: Type and action of a plain route point.
TURNING_POINT = "Turning Point"
#: Action of a ground route point that follows the roads.
ON_ROAD = "On Road"
#: Action of a ground route point across country.
OFF_ROAD = "Off Road"
#: Task id wrapping the tasks of a route point.
COMBO_TASK = "ComboTask"
#: Main task of ground groups (they just follow their route).
GROUND_NOTHING = "Ground Nothing"
#: Skill of the AI ground units.
GROUND_SKILL = "Average"
#: Skill of the AI aircraft.
AIR_SKILL = "High"
#: Target category of air-to-air engage tasks.
TARGETS_AIR = "Air"
#: Target category of the SAM sites and AAA.
TARGETS_AIR_DEFENCE = "Air Defence"
#: Target category of ground forces.
TARGETS_GROUND = "Ground Units"

# -- Campaign <-> DCS --

#: Prefix of the DCS names of the units the campaign spawns (``DC.<entity>.<element>.<n>``).
UNIT_PREFIX = "DC"
#: Prefix of the template groups in the missions (``DC_TPL_<aircraft>[_<task>]``).
TEMPLATE_PREFIX = "DC_TPL"


def dcs_enum(path: str) -> dict[str, Any]:
    """A DCS enum value by name, for the bridge to look up in DCS' tables.

    Args:
        path: The enum's path in the DCS scripting environment, e.g. ``"country.id.USA"``.

    Returns:
        ``{"enum": path}``, with the ``default`` of :data:`ENUM_FALLBACK` if it has one.
    """
    ref: dict[str, Any] = {"enum": path}
    if path in ENUM_FALLBACK:
        ref["default"] = ENUM_FALLBACK[path]
    return ref


def to_dcs(v: Vec2) -> tuple[float, float]:
    """Campaign point -> (DCS x north, DCS y/z east).

    Args:
        v: Point on the campaign plane (east, north).

    Returns:
        The DCS ``(x, z)`` coordinates.
    """
    return v.y, v.x


def from_dcs(x: float, z: float) -> Vec2:
    """DCS (x north, z east) -> campaign point.

    Args:
        x: DCS x (north).
        z: DCS z (east).

    Returns:
        The point on the campaign plane.
    """
    return Vec2(z, x)


def unit_name(entity_id: str, element: str, index: int) -> str:
    """DCS unit name encoding entity and element: ``DC.<entity>.<element>.<n>``.

    Args:
        entity_id: Campaign entity the unit belongs to.
        element: Element of the entity the unit stands for.
        index: Number of the unit within that element.

    Returns:
        The DCS unit name.
    """
    return f"{UNIT_PREFIX}.{entity_id}.{element}.{index}"


def parse_unit_name(name: str) -> tuple[str, str] | None:
    """(entity id, element) from a unit name, or None.

    Args:
        name: DCS unit name.

    Returns:
        The entity id and the element, or None if the name was not made by :func:`unit_name`.
    """
    parts = name.split(".")
    if len(parts) != 4 or parts[0] != UNIT_PREFIX:
        return None
    return parts[1], parts[2]


@dataclass
class Spawned:
    """What was spawned for an entity, to convert alive units back to elements.

    Attributes:
        groups: DCS group names.
        units: Element -> DCS units spawned.
        represents: Element -> campaign elements they stand for.
    """
    groups: list[str]
    units: dict[str, int]  # element -> DCS units spawned
    represents: dict[str, float]  # element -> campaign elements they stand for


@dataclass
class DcsTranslator:
    """Converts engine commands to bridge messages and bridge messages to reports."""
    max_units_per_entity: int = 40
    max_units_per_group: int = 20
    unit_spacing_m: float = 30.0
    group_spacing_m: float = 200.0
    spawned: dict[str, Spawned] = field(default_factory=dict)
    known_players: set[str] = field(default_factory=set)
    player_types: dict[str, tuple[str, str]] = field(default_factory=dict)  # player -> (DCS type, Unit.Category key)
    ato_version: int = 0

    # ------------------------------------------------------------ outgoing

    def encode(self, cmd: Command) -> dict[str, Any]:
        """Bridge message for a command.

        Args:
            cmd: The command to translate.

        Returns:
            The JSON-ready message for the Lua bridge.
        """
        match cmd:
            case Materialize(entity=snap):
                return self._spawn(snap)
            case Dematerialize(entity_id=eid):
                return {"cmd": Cmd.DESPAWN, "id": eid}
            case PublishAto(side=side, packages=packages):
                self.ato_version += 1
                return {
                    "cmd": Cmd.ATO,
                    "coalition": SIDE_COALITION[side],
                    "version": self.ato_version,
                    "packages": list(packages),
                }
            case NotifyPlayer(player_id=pid, text=text, marks=marks):
                return {
                    "cmd": Cmd.NOTIFY,
                    "player": pid,
                    "text": text,
                    "marks": [{"text": label, "x": to_dcs(p)[0], "z": to_dcs(p)[1]} for label, p in marks],
                }
            case UpdateRoute(entity_id=eid, waypoints=wps, speed_kmh=speed, snapshot=snap):
                sp = self.spawned.get(eid)
                if snap is not None and snap.kind is EntityKind.FLIGHT:
                    points = self._flight_route(snap)  # air points, with the tasks at the target
                else:
                    points = [route_point(p, speed) for p in wps]
                return {"cmd": Cmd.ROUTE, "id": eid, "groups": sp.groups if sp else [], "points": points}
        raise TypeError(cmd)  # pragma: no cover

    def _spawn(self, snap: EntitySnapshot) -> dict[str, Any]:
        """``spawn`` message for a ground entity, convoy or SAM site.

        Args:
            snap: Snapshot of the entity to spawn.

        Returns:
            The ``spawn`` message.
        """
        if snap.kind is EntityKind.FLIGHT:
            return self._spawn_flight(snap)
        if snap.kind is EntityKind.SAM:
            types = catalog.SAM_TYPES[snap.attrs["system"]]
            plans = [
                catalog.SpawnPlan(el, types[el], int(round(n)), float(n))
                for el, n in snap.elements.items()
                if n >= 0.5 and el in types
            ]
        elif snap.kind is EntityKind.CONVOY:
            plans = catalog.plan_convoy(snap.side, snap.elements.get(TRUCK, 0.0))
        else:
            plans = catalog.plan_division(snap.side, snap.elements, self.max_units_per_entity)

        units: list[dict[str, Any]] = []
        for plan in plans:
            for i in range(plan.units):
                units.append({"type": plan.dcs_type, "name": unit_name(snap.id, plan.element, i)})

        road = list(snap.attrs.get("road_back") or [])
        if len(road) >= 2:
            msg = self._spawn_on_road(snap, units, plans, road)
            if snap.kind is EntityKind.SAM:
                for g in msg["groups"]:
                    g["alarm_red"] = True  # radars on, weapons free
                    g["data"]["route"]["points"] = g["data"]["route"]["points"][:1] if snap.attrs.get("deployed") else g["data"]["route"]["points"]
            return msg
        chunks = [units[i : i + self.max_units_per_group] for i in range(0, len(units), self.max_units_per_group)]
        # No road known: column formation, groups side by side, units in file behind their leader.
        back = Vec2(-math.sin(snap.heading), -math.cos(snap.heading))
        side = Vec2(math.cos(snap.heading), -math.sin(snap.heading))
        groups, names = [], []
        for g, chunk in enumerate(chunks):
            name = f"{UNIT_PREFIX}.{snap.id}.g{g}"
            names.append(name)
            lateral = (g - (len(chunks) - 1) / 2) * self.group_spacing_m
            origin = snap.position + side * lateral
            for i, u in enumerate(chunk):
                x, y = to_dcs(origin + back * (i * self.unit_spacing_m))
                u.update(x=x, y=y, heading=snap.heading, skill=GROUND_SKILL, playerCanDrive=False)
            gx, gy = to_dcs(origin)
            points = [route_point(origin, snap.speed_kmh, on_road=False)]
            points += [route_point(p + side * lateral, snap.speed_kmh) for p in snap.waypoints]
            groups.append(
                {
                    "country": dcs_enum(f"country.id.{catalog.COUNTRY[snap.side]}"),
                    "category": dcs_enum(GroupCategory.GROUND),
                    "data": {
                        "name": name,
                        "task": GROUND_NOTHING,
                        "visible": False,
                        "x": gx,
                        "y": gy,
                        "units": chunk,
                        "route": {"points": points},
                    },
                }
            )
        self.spawned[snap.id] = Spawned(
            groups=names,
            units={p.element: p.units for p in plans},
            represents={p.element: p.represents for p in plans},
        )
        return {"cmd": Cmd.SPAWN, "id": snap.id, "groups": groups}

    def _spawn_on_road(self, snap: EntitySnapshot, units: list[dict[str, Any]], plans, road: list[Vec2]) -> dict[str, Any]:
        """Line the vehicles up in a column along the road behind the unit (no
        vehicle dropped in a forest), groups one after the other.

        Args:
            snap: Snapshot of the entity to spawn.
            units: The DCS units to place, already named and typed.
            plans: Spawn plans the units come from.
            road: Road polyline behind the unit, nearest point first.

        Returns:
            The ``spawn`` message with one group per ``max_units_per_group`` vehicles.
        """
        from ...world import point_along, polyline_length

        total = polyline_length(road)
        chunks = [units[i : i + self.max_units_per_group] for i in range(0, len(units), self.max_units_per_group)]
        groups, names, n = [], [], 0
        for g, chunk in enumerate(chunks):
            name = f"{UNIT_PREFIX}.{snap.id}.g{g}"
            names.append(name)
            for u in chunk:
                s = min(n * self.unit_spacing_m, total)
                p, i = point_along(road, s)
                # vehicles face away from the road behind them, i.e. along the advance
                heading = road[i + 1].heading_to(road[i]) if i + 1 < len(road) else snap.heading
                x, y = to_dcs(p)
                u.update(x=x, y=y, heading=heading, skill=GROUND_SKILL, playerCanDrive=False)
                n += 1
            lead = chunk[0]
            points = [route_point(from_dcs(lead["x"], lead["y"]), snap.speed_kmh)]
            points += [route_point(p, snap.speed_kmh) for p in snap.waypoints]
            groups.append(
                {
                    "country": dcs_enum(f"country.id.{catalog.COUNTRY[snap.side]}"),
                    "category": dcs_enum(GroupCategory.GROUND),
                    "snap_to_road": True,
                    "data": {
                        "name": name,
                        "task": GROUND_NOTHING,
                        "visible": False,
                        "x": lead["x"],
                        "y": lead["y"],
                        "units": chunk,
                        "route": {"points": points},
                    },
                }
            )
        self.spawned[snap.id] = Spawned(
            groups=names,
            units={p.element: p.units for p in plans},
            represents={p.element: p.represents for p in plans},
        )
        return {"cmd": Cmd.SPAWN, "id": snap.id, "groups": groups}

    def _spawn_flight(self, snap: EntitySnapshot) -> dict[str, Any]:
        """``spawn`` message for a flight (templates, route tasks, landing).

        Args:
            snap: Snapshot of the flight to spawn.

        Returns:
            The ``spawn`` message for the flight group.
        """
        a = snap.attrs
        aircraft, task = a["aircraft"], Task(a["task"])
        count = int(round(snap.elements.get(AIRCRAFT_ELEMENT, 0)))
        helo = bool(a.get("helo"))
        alt = snap.altitude
        speed = snap.speed_kmh / 3.6
        name = f"{UNIT_PREFIX}.{snap.id}.g0"
        back = Vec2(-math.sin(snap.heading), -math.cos(snap.heading))
        radio = a.get("radio_callsign")  # callsigns.RadioCallsign, None for eastern sides
        first = int(a.get("first_unit", 1))
        units = []
        for i in range(count):
            x, y = to_dcs(snap.position + back * (i * 150.0))
            units.append(
                {
                    **({"callsign": radio.unit(first + i)} if radio else {}),
                    "type": catalog.AIRCRAFT_TYPES.get(aircraft, aircraft),
                    "name": unit_name(snap.id, AIRCRAFT_ELEMENT, i),
                    "x": x,
                    "y": y,
                    "alt": alt,
                    "alt_type": ALT_RADIO if helo else ALT_BARO,
                    "speed": speed,
                    "heading": snap.heading,
                    "skill": AIR_SKILL,
                    "payload": {"pylons": {}, "fuel": 3000, "flare": 30, "chaff": 30, "gun": 100},
                }
            )
        points = self._flight_route(snap)
        self.spawned[snap.id] = Spawned(groups=[name], units={AIRCRAFT_ELEMENT: count}, represents={AIRCRAFT_ELEMENT: float(count)})
        return {
            "cmd": Cmd.SPAWN,
            "id": snap.id,
            "groups": [
                {
                    "country": dcs_enum(f"country.id.{catalog.COUNTRY[snap.side]}"),
                    "category": dcs_enum(GroupCategory.HELICOPTER if helo else GroupCategory.AIRPLANE),
                    # Mission-editor groups named like this give type, livery and payload.
                    "templates": [f"{TEMPLATE_PREFIX}_{aircraft}_{task}", f"{TEMPLATE_PREFIX}_{aircraft}"],
                    "land_at": a.get("home"),
                    "data": {
                        "name": name,
                        "task": catalog.DCS_MAIN_TASK.get(task, "Nothing"),
                        "units": units,
                        "route": {"points": points},
                        # package frequency from the ATO (clear of the airfields);
                        # null keeps the template's
                        "frequency": a.get("frequency_mhz") or None,
                        "modulation": 0,  # AM
                    },
                }
            ],
        }

    def _flight_route(self, snap: EntitySnapshot) -> list[dict[str, Any]]:
        """DCS route of a flight: from where it is, along its remaining
        waypoints, with en-route tasks first and the attack/orbit at the target.

        Args:
            snap: Snapshot of the flight.

        Returns:
            The DCS route points.
        """
        a = snap.attrs
        task, helo = Task(a["task"]), bool(a.get("helo"))
        alt, speed = snap.altitude, snap.speed_kmh / 3.6
        points = [self._air_point(snap.position, alt, speed, helo, self._enroute_tasks(task))]
        target_index = int(a.get("target_index", -1))
        alts = a.get("altitudes") or ()  # the briefing's profile, one per waypoint
        for i, wp in enumerate(snap.waypoints):
            tasks = self._target_tasks(task, a.get("target"), a.get("on_station_s", 0.0), helo) if i == target_index else []
            points.append(self._air_point(wp, alts[i] if i < len(alts) else alt, speed, helo, tasks))
        return points

    @staticmethod
    def _air_point(p: Vec2, alt: float, speed: float, helo: bool, tasks: list[dict[str, Any]]) -> dict[str, Any]:
        """DCS route point for aircraft.

        Args:
            p: Position of the route point.
            alt: Altitude in metres (above ground for helicopters, barometric otherwise).
            speed: Speed in m/s.
            helo: Whether the flight is a helicopter.
            tasks: DCS tasks to run at this point.

        Returns:
            The DCS route point.
        """
        x, y = to_dcs(p)
        return {
            "x": x,
            "y": y,
            "alt": alt,
            "alt_type": ALT_RADIO if helo else ALT_BARO,
            "type": TURNING_POINT,
            "action": TURNING_POINT,
            "speed": speed,
            "speed_locked": True,
            "ETA": 0,
            "ETA_locked": False,
            "task": {"id": COMBO_TASK, "params": {"tasks": tasks}},
        }

    @staticmethod
    def _enroute_tasks(task: Task) -> list[dict[str, Any]]:
        """En-route DCS tasks of a flight (engage air / air defence).

        Args:
            task: The flight's campaign task.

        Returns:
            The en-route DCS tasks (empty for tasks without one).
        """
        if task is Task.AWACS:
            return [{"number": 1, "auto": False, "enabled": True, "id": "AWACS", "params": {}}]
        if task is Task.TANKER:
            return [{"number": 1, "auto": False, "enabled": True, "id": "Tanker", "params": {}}]
        if task.air_to_air:
            targets, dist = [TARGETS_AIR], 60_000
        elif task is Task.SEAD:
            targets, dist = [TARGETS_AIR_DEFENCE], 40_000
        else:
            return []
        tasks = [
            {
                "number": 1,
                "auto": False,
                "enabled": True,
                "id": "EngageTargets",
                "params": {"targetTypes": targets, "maxDist": dist, "maxDistEnabled": True, "priority": 0},
            }
        ]
        if task is Task.SEAD:
            # The SEAD flight is the package's jammer: ECM always on.
            tasks.append({
                "number": 2,
                "auto": False,
                "enabled": True,
                "id": "WrappedAction",
                "params": {"action": {"id": "Option", "params": {"name": dcs_enum(ECM_USING), "value": dcs_enum(ECM_ALWAYS)}}},
            })
        return tasks

    @staticmethod
    def _target_tasks(task: Task, target: Vec2 | None, station_s: float, helo: bool) -> list[dict[str, Any]]:
        """DCS tasks at the target waypoint (orbit, bombing, engage in zone).

        Args:
            task: The flight's campaign task.
            target: Target position, if the task has one.
            station_s: Time on station in seconds (orbit tasks).
            helo: Whether the flight is a helicopter.

        Returns:
            The DCS tasks for the target waypoint.
        """
        if task.orbits:
            return [
                {
                    "number": 1,
                    "auto": False,
                    "enabled": True,
                    "id": "ControlledTask",
                    "params": {
                        "task": {"id": "Orbit", "params": {"pattern": "Race-Track"}},
                        "stopCondition": {"duration": max(60.0, station_s)},
                    },
                }
            ]
        if target is None or task is Task.TRANSPORT:
            return []
        tx, ty = to_dcs(target)
        if task is Task.STRIKE:
            params = {"point": {"x": tx, "y": ty}, "attackQty": 1, "expend": "All", "groupAttack": True, "weaponType": dcs_enum(WEAPON_AUTO)}
            return [{"number": 1, "auto": False, "enabled": True, "id": "Bombing", "params": params}]
        zone = 8_000 if helo else 12_000
        targets = [TARGETS_AIR_DEFENCE] if task is Task.SEAD else [TARGETS_GROUND]
        params = {"point": {"x": tx, "y": ty}, "zoneRadius": zone, "targetTypes": targets, "priority": 0}
        return [{"number": 1, "auto": False, "enabled": True, "id": "EngageTargetsInZone", "params": params}]

    # ------------------------------------------------------------ incoming

    def decode(self, msg: dict[str, Any]) -> list[Report]:
        """Reports for one bridge message.

        Args:
            msg: A decoded JSON line from the bridge.

        Returns:
            The campaign reports it carries (empty for unknown messages).
        """
        kind = msg.get("msg")
        if kind == Msg.PLAYERS:
            return self._players(msg.get("players") or [])
        if kind == Msg.SPAWNED:
            ok = bool(msg.get("ok"))
            if not ok:
                self.spawned.pop(msg["id"], None)
            return [MaterializeAck(msg["id"], ok, msg.get("error", ""))]
        if kind == Msg.STATE:
            return [
                EntityState(e["id"], *self._aggregate(e["id"], e.get("units") or []))
                for e in msg.get("entities") or []
                if e.get("id") in self.spawned
            ]
        if kind == Msg.DESPAWNED:
            eid = msg["id"]
            pos, elements = self._aggregate(eid, msg.get("units") or [])
            self.spawned.pop(eid, None)
            return [DematerializeAck(eid, pos, elements)]
        if kind == Msg.JOIN:
            pid = str(msg["player"])
            dcs_type, category = self.player_types.get(pid, (str(msg.get("type", "")), str(msg.get("category", ""))))
            if msg.get("type"):
                dcs_type = str(msg["type"])
            return [JoinRequest(pid, str(msg["flight"]), catalog.player_tasks(dcs_type, CATEGORY_KIND.get(category, PlayerKind.AIR)))]
        if kind == Msg.LEAVE:
            return [LeaveRequest(str(msg["player"]))]
        if kind == Msg.BRIEFING:
            return [BriefingRequest(str(msg["player"]))]
        if kind == Msg.PLAYER_EVENT:
            try:
                return [PlayerEvent(str(msg["player"]), PlayerEventKind(msg["event"]))]
            except ValueError:
                return []
        if kind == Msg.DISCONNECTED:
            self.spawned.clear()
            self.known_players.clear()
            return [GameDisconnected(msg.get("reason", "connection closed"))]
        return []  # hello, pong, log ... are informational

    def _players(self, players: list[dict[str, Any]]) -> list[Report]:
        """Player reports, including players that disappeared.

        Args:
            players: Player entries of a ``players`` message.

        Returns:
            One :class:`PlayerSeen` per player and one :class:`PlayerGone` per player no longer
            listed.
        """
        out: list[Report] = []
        seen: set[str] = set()
        for p in players:
            side = COALITION_SIDE.get(str(p.get("coalition", "")))
            if side is None:
                continue
            pid = str(p["id"])
            seen.add(pid)
            self.player_types[pid] = (str(p.get("type", "")), str(p.get("category", "")))
            out.append(
                PlayerSeen(
                    player_id=pid,
                    name=str(p.get("name", pid)),
                    side=side,
                    kind=CATEGORY_KIND.get(str(p.get("category", "")), PlayerKind.AIR),
                    position=from_dcs(p["x"], p["z"]),
                    altitude=float(p.get("alt", 0.0)),
                    aircraft_type=str(p.get("type", "")),
                )
            )
        out += [PlayerLeft(pid) for pid in sorted(self.known_players - seen)]
        self.known_players = seen
        return out

    def _aggregate(self, eid: str, units: list[dict[str, Any]]) -> tuple[Vec2 | None, dict[str, float]]:
        """Mean position and alive campaign elements of an entity's units.

        Args:
            eid: Campaign entity id.
            units: Unit entries (name and position) reported by the bridge.

        Returns:
            The mean position of the matching units (None if none is alive) and the campaign
            elements they still represent.
        """
        sp = self.spawned.get(eid)
        alive: dict[str, int] = {}
        xs, zs = [], []
        for u in units:
            parsed = parse_unit_name(str(u.get("n", "")))
            if parsed is None or parsed[0] != eid:
                continue
            alive[parsed[1]] = alive.get(parsed[1], 0) + 1
            xs.append(float(u["x"]))
            zs.append(float(u["z"]))
        pos = from_dcs(sum(xs) / len(xs), sum(zs) / len(zs)) if xs else None
        if sp is None:
            return pos, {}
        elements = {
            e: sp.represents[e] * alive.get(e, 0) / n for e, n in sp.units.items() if n > 0
        }
        return pos, elements


def route_point(p: Vec2, speed_kmh: float, on_road: bool = True) -> dict[str, Any]:
    """DCS ground route point.

    Args:
        p: Position of the point.
        speed_kmh: Speed in km/h.
        on_road: Drive on roads to reach it.

    Returns:
        The DCS route point.
    """
    x, y = to_dcs(p)
    return {
        "x": x,
        "y": y,
        "type": TURNING_POINT,
        "action": ON_ROAD if on_road else OFF_ROAD,
        "speed": speed_kmh / 3.6,
        "speed_locked": True,
        "ETA": 0,
        "ETA_locked": False,
        "alt": 0,
        "alt_type": ALT_BARO,
        "formation_template": "",
        "task": {"id": COMBO_TASK, "params": {"tasks": []}},
    }
