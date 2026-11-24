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
from typing import Any

from ...geo import Vec2
from ..base import (
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

UNIT_PREFIX = "DC"
COALITION_SIDE = {1: "red", 2: "blue"}
SIDE_COALITION = {v: k for k, v in COALITION_SIDE.items()}
TEMPLATE_PREFIX = "DC_TPL"
CATEGORY_KIND = {0: PlayerKind.AIR, 1: PlayerKind.HELO, 2: PlayerKind.GROUND, 3: PlayerKind.GROUND}


def to_dcs(v: Vec2) -> tuple[float, float]:
    """Campaign point -> (DCS x north, DCS y/z east)."""
    return v.y, v.x


def from_dcs(x: float, z: float) -> Vec2:
    """DCS (x north, z east) -> campaign point."""
    return Vec2(z, x)


def unit_name(entity_id: str, element: str, index: int) -> str:
    """DCS unit name encoding entity and element: ``DC.<entity>.<element>.<n>``."""
    return f"{UNIT_PREFIX}.{entity_id}.{element}.{index}"


def parse_unit_name(name: str) -> tuple[str, str] | None:
    """(entity id, element) from a unit name, or None."""
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
    player_types: dict[str, tuple[str, int]] = field(default_factory=dict)  # player -> (DCS type, category)
    ato_version: int = 0

    # ------------------------------------------------------------ outgoing

    def encode(self, cmd: Command) -> dict[str, Any]:
        """Bridge message for a command."""
        match cmd:
            case Materialize(entity=snap):
                return self._spawn(snap)
            case Dematerialize(entity_id=eid):
                return {"cmd": "despawn", "id": eid}
            case PublishAto(side=side, packages=packages):
                self.ato_version += 1
                return {
                    "cmd": "ato",
                    "coalition": SIDE_COALITION.get(side, 0),
                    "version": self.ato_version,
                    "packages": list(packages),
                }
            case NotifyPlayer(player_id=pid, text=text, marks=marks):
                return {
                    "cmd": "notify",
                    "player": pid,
                    "text": text,
                    "marks": [{"text": label, "x": to_dcs(p)[0], "z": to_dcs(p)[1]} for label, p in marks],
                }
            case UpdateRoute(entity_id=eid, waypoints=wps, speed_kmh=speed):
                sp = self.spawned.get(eid)
                return {
                    "cmd": "route",
                    "id": eid,
                    "groups": sp.groups if sp else [],
                    "points": [route_point(p, speed) for p in wps],
                }
        raise TypeError(cmd)  # pragma: no cover

    def _spawn(self, snap: EntitySnapshot) -> dict[str, Any]:
        """``spawn`` message for a ground entity, convoy or SAM site."""
        if snap.kind == "flight":
            return self._spawn_flight(snap)
        if snap.kind == "sam":
            types = catalog.SAM_TYPES[snap.attrs["system"]]
            plans = [
                catalog.SpawnPlan(el, types[el], int(round(n)), float(n))
                for el, n in snap.elements.items()
                if n >= 0.5 and el in types
            ]
        elif snap.kind == "convoy":
            plans = catalog.plan_convoy(snap.side, snap.elements.get("truck", 0.0))
        else:
            plans = catalog.plan_division(snap.side, snap.elements, self.max_units_per_entity)

        units: list[dict[str, Any]] = []
        for plan in plans:
            for i in range(plan.units):
                units.append({"type": plan.dcs_type, "name": unit_name(snap.id, plan.element, i)})

        road = list(snap.attrs.get("road_back") or [])
        if len(road) >= 2:
            msg = self._spawn_on_road(snap, units, plans, road)
            if snap.kind == "sam":
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
                u.update(x=x, y=y, heading=snap.heading, skill="Average", playerCanDrive=False)
            gx, gy = to_dcs(origin)
            points = [route_point(origin, snap.speed_kmh, on_road=False)]
            points += [route_point(p + side * lateral, snap.speed_kmh) for p in snap.waypoints]
            groups.append(
                {
                    "country": catalog.COUNTRY[snap.side],
                    "category": "ground",
                    "data": {
                        "name": name,
                        "task": "Ground Nothing",
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
        return {"cmd": "spawn", "id": snap.id, "groups": groups}

    def _spawn_on_road(self, snap: EntitySnapshot, units: list[dict[str, Any]], plans, road: list[Vec2]) -> dict[str, Any]:
        """Line the vehicles up in a column along the road behind the unit (no
        vehicle dropped in a forest), groups one after the other."""
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
                u.update(x=x, y=y, heading=heading, skill="Average", playerCanDrive=False)
                n += 1
            lead = chunk[0]
            points = [route_point(from_dcs(lead["x"], lead["y"]), snap.speed_kmh)]
            points += [route_point(p, snap.speed_kmh) for p in snap.waypoints]
            groups.append(
                {
                    "country": catalog.COUNTRY[snap.side],
                    "category": "ground",
                    "snap_to_road": True,
                    "data": {
                        "name": name,
                        "task": "Ground Nothing",
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
        return {"cmd": "spawn", "id": snap.id, "groups": groups}

    def _spawn_flight(self, snap: EntitySnapshot) -> dict[str, Any]:
        """``spawn`` message for a flight (templates, route tasks, landing)."""
        a = snap.attrs
        aircraft, task = a["aircraft"], a["task"]
        count = int(round(snap.elements.get("aircraft", 0)))
        helo = bool(a.get("helo"))
        alt = snap.altitude
        speed = snap.speed_kmh / 3.6
        name = f"{UNIT_PREFIX}.{snap.id}.g0"
        back = Vec2(-math.sin(snap.heading), -math.cos(snap.heading))
        units = []
        for i in range(count):
            x, y = to_dcs(snap.position + back * (i * 150.0))
            units.append(
                {
                    "type": catalog.AIRCRAFT_TYPES.get(aircraft, aircraft),
                    "name": unit_name(snap.id, "aircraft", i),
                    "x": x,
                    "y": y,
                    "alt": alt,
                    "alt_type": "RADIO" if helo else "BARO",
                    "speed": speed,
                    "heading": snap.heading,
                    "skill": "High",
                    "payload": {"pylons": {}, "fuel": 3000, "flare": 30, "chaff": 30, "gun": 100},
                }
            )
        points = [self._air_point(snap.position, alt, speed, helo, self._enroute_tasks(task))]
        target_index = int(a.get("target_index", -1))
        for i, wp in enumerate(snap.waypoints):
            tasks = self._target_tasks(task, a.get("target"), a.get("on_station_s", 0.0), helo) if i == target_index else []
            points.append(self._air_point(wp, alt, speed, helo, tasks))
        self.spawned[snap.id] = Spawned(groups=[name], units={"aircraft": count}, represents={"aircraft": float(count)})
        return {
            "cmd": "spawn",
            "id": snap.id,
            "groups": [
                {
                    "country": catalog.COUNTRY[snap.side],
                    "category": "helicopter" if helo else "airplane",
                    # Mission-editor groups named like this give type, livery and payload.
                    "templates": [f"{TEMPLATE_PREFIX}_{aircraft}_{task}", f"{TEMPLATE_PREFIX}_{aircraft}"],
                    "land_at": a.get("home"),
                    "data": {
                        "name": name,
                        "task": catalog.DCS_MAIN_TASK.get(task, "Nothing"),
                        "units": units,
                        "route": {"points": points},
                    },
                }
            ],
        }

    @staticmethod
    def _air_point(p: Vec2, alt: float, speed: float, helo: bool, tasks: list[dict[str, Any]]) -> dict[str, Any]:
        """DCS route point for aircraft."""
        x, y = to_dcs(p)
        return {
            "x": x,
            "y": y,
            "alt": alt,
            "alt_type": "RADIO" if helo else "BARO",
            "type": "Turning Point",
            "action": "Turning Point",
            "speed": speed,
            "speed_locked": True,
            "ETA": 0,
            "ETA_locked": False,
            "task": {"id": "ComboTask", "params": {"tasks": tasks}},
        }

    @staticmethod
    def _enroute_tasks(task: str) -> list[dict[str, Any]]:
        """En-route DCS tasks of a flight (engage air / air defence)."""
        if task == "AWACS":
            return [{"number": 1, "auto": False, "enabled": True, "id": "AWACS", "params": {}}]
        if task == "TANKER":
            return [{"number": 1, "auto": False, "enabled": True, "id": "Tanker", "params": {}}]
        if task in ("CAP", "ESCORT"):
            targets, dist = ["Air"], 60_000
        elif task == "SEAD":
            targets, dist = ["Air Defence"], 40_000
        else:
            return []
        return [
            {
                "number": 1,
                "auto": False,
                "enabled": True,
                "id": "EngageTargets",
                "params": {"targetTypes": targets, "maxDist": dist, "maxDistEnabled": True, "priority": 0},
            }
        ]

    @staticmethod
    def _target_tasks(task: str, target: Vec2 | None, station_s: float, helo: bool) -> list[dict[str, Any]]:
        """DCS tasks at the target waypoint (orbit, bombing, engage in zone)."""
        if task in ("CAP", "AWACS", "TANKER"):
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
        if target is None or task == "TRANSPORT":
            return []
        tx, ty = to_dcs(target)
        if task == "STRIKE":
            # NOTE: weaponType 1073741822 is "Auto" in exported missions; verify in DCS.
            params = {"point": {"x": tx, "y": ty}, "attackQty": 1, "expend": "All", "groupAttack": True, "weaponType": 1073741822}
            return [{"number": 1, "auto": False, "enabled": True, "id": "Bombing", "params": params}]
        zone = 8_000 if helo else 12_000
        targets = ["Air Defence"] if task == "SEAD" else ["Ground Units"]
        params = {"point": {"x": tx, "y": ty}, "zoneRadius": zone, "targetTypes": targets, "priority": 0}
        return [{"number": 1, "auto": False, "enabled": True, "id": "EngageTargetsInZone", "params": params}]

    # ------------------------------------------------------------ incoming

    def decode(self, msg: dict[str, Any]) -> list[Report]:
        """Reports for one bridge message."""
        kind = msg.get("msg")
        if kind == "players":
            return self._players(msg.get("players") or [])
        if kind == "spawned":
            ok = bool(msg.get("ok"))
            if not ok:
                self.spawned.pop(msg["id"], None)
            return [MaterializeAck(msg["id"], ok, msg.get("error", ""))]
        if kind == "state":
            return [
                EntityState(e["id"], *self._aggregate(e["id"], e.get("units") or []))
                for e in msg.get("entities") or []
                if e.get("id") in self.spawned
            ]
        if kind == "despawned":
            eid = msg["id"]
            pos, elements = self._aggregate(eid, msg.get("units") or [])
            self.spawned.pop(eid, None)
            return [DematerializeAck(eid, pos, elements)]
        if kind == "join":
            pid = str(msg["player"])
            dcs_type, category = self.player_types.get(pid, (str(msg.get("type", "")), int(msg.get("category", 0))))
            if msg.get("type"):
                dcs_type = str(msg["type"])
            return [JoinRequest(pid, str(msg["flight"]), catalog.player_tasks(dcs_type, category))]
        if kind == "leave":
            return [LeaveRequest(str(msg["player"]))]
        if kind == "briefing":
            return [BriefingRequest(str(msg["player"]))]
        if kind == "player_event":
            try:
                return [PlayerEvent(str(msg["player"]), PlayerEventKind(msg["event"]))]
            except ValueError:
                return []
        if kind == "_disconnected":
            self.spawned.clear()
            self.known_players.clear()
            return [GameDisconnected(msg.get("reason", "connection closed"))]
        return []  # hello, pong, log ... are informational

    def _players(self, players: list[dict[str, Any]]) -> list[Report]:
        """Player reports, including players that disappeared."""
        out: list[Report] = []
        seen: set[str] = set()
        for p in players:
            side = COALITION_SIDE.get(int(p.get("coalition", 0)))
            if side is None:
                continue
            pid = str(p["id"])
            seen.add(pid)
            self.player_types[pid] = (str(p.get("type", "")), int(p.get("category", 0)))
            out.append(
                PlayerSeen(
                    player_id=pid,
                    name=str(p.get("name", pid)),
                    side=side,
                    kind=CATEGORY_KIND.get(int(p.get("category", 0)), PlayerKind.AIR),
                    position=from_dcs(p["x"], p["z"]),
                    altitude=float(p.get("alt", 0.0)),
                    aircraft_type=str(p.get("type", "")),
                )
            )
        out += [PlayerLeft(pid) for pid in sorted(self.known_players - seen)]
        self.known_players = seen
        return out

    def _aggregate(self, eid: str, units: list[dict[str, Any]]) -> tuple[Vec2 | None, dict[str, float]]:
        """Mean position and alive campaign elements of an entity's units."""
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
    """DCS ground route point."""
    x, y = to_dcs(p)
    return {
        "x": x,
        "y": y,
        "type": "Turning Point",
        "action": "On Road" if on_road else "Off Road",
        "speed": speed_kmh / 3.6,
        "speed_locked": True,
        "ETA": 0,
        "ETA_locked": False,
        "alt": 0,
        "alt_type": "BARO",
        "formation_template": "",
        "task": {"id": "ComboTask", "params": {"tasks": []}},
    }
