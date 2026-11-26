"""The whole campaign state plus the small helpers every system needs."""

from __future__ import annotations

import random
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from .geo import Vec2
from .air import Airbase, Flight, Package, Squadron
from .air_defense import AirDefenseUnit
from .events import EventKind, EventLog
from .forces import Convoy, Division, MobileEntity, Movement, Order, OrderKind
from .world import Theater
from .units import UnitDomain, Units

@dataclass(slots=True)
class Objective:
    """A province a faction wants to hold.

    Attributes:
        province: Province id.
        priority: Weight for the AI and the scoreboard.
    """
    province: str
    priority: int = 1


@dataclass
class Faction:
    """A side of the war.

    Attributes:
        id: Faction id.
        name: Display name.
        bullseye: The side's fixed reference point for bearing/range calls
            (``BULLS 045/32``, see :mod:`dynacamp.bullseye`).
        side: Game-facing side (``"blue"``/``"red"``), mapped to coalitions by adapters.
        enemies: Factions it is at war with.
        objectives: Provinces it wants.
    """
    id: str
    name: str
    bullseye: Vec2
    side: str  # game-facing side, e.g. "blue" / "red"; adapters map it to coalitions
    enemies: set[str] = field(default_factory=set)
    objectives: list[Objective] = field(default_factory=list)


@dataclass
class Battle:
    """A battle: an assault on a province, or a meeting engagement on the road.

    Attributes:
        id: Battle id.
        province: Province fought for (assault), or the nearest one to where
            the forces met (meeting engagement: terrain and labels).
        attacker_faction: Attacking faction.
        defender_faction: Defending faction.
        attackers: Attacking division ids.
        defenders: Defending division ids.
        started: Campaign time it began.
        attack_from: Attacker id -> province it attacks from.
        kind: ``"assault"`` (on a province's defence line) or ``"meeting"``
            (two forces ran into each other: no defensive line, no river crossing).
        point: Where a meeting engagement happens (None for assaults).
    """
    id: str
    province: str
    attacker_faction: str
    defender_faction: str
    attackers: list[str]
    defenders: list[str]
    started: float
    # attacker id -> province it attacks from (needed for river penalties / retreat)
    attack_from: dict[str, str] = field(default_factory=dict)
    kind: str = "assault"
    point: Vec2 | None = None


@dataclass
class CampaignState:
    """The whole campaign: map, factions, forces, air war, air defence and log.

    Systems (:mod:`dynacamp.systems`) mutate it; the engine and the API read it.

    Attributes:
        name: Campaign name.
        theater: The map.
        factions: Factions by id.
        seed: Random seed.
        time: Campaign time in seconds since day 1 00:00.
        units: Units of the texts for players: ``imperial`` (kts, ft, NM) or ``metric``.
        divisions: Brigades by id.
        convoys: Supply convoys by id.
        battles: Battles in progress by id.
        airbases: Airbases by id.
        squadrons: Squadrons by id.
        packages: ATO packages by id.
        flights: Flights by id.
        archive: Completed packages by id, oldest first (the last ``ARCHIVE_MAX``):
            what the event log talks about after they leave the ATO.
        archived_flights: The flights of the archived packages, by id.
        air_defense: SAM sites and early-warning radars by id.
        player_flights: Player id -> flight id of the seat they hold.
        events: The event log.
        rng: The campaign's random generator.
        dirty: Entities whose orders changed this step (for adapters).
        timers: Next run times of periodic systems and cooldowns.
    """
    name: str
    theater: Theater
    factions: dict[str, Faction]
    seed: int = 0
    time: float = 0.0  # campaign seconds since start
    units: Units = UnitDomain.IMPERIAL # of texts for players: "imperial" (kts/ft/NM) or "metric"
    divisions: dict[str, Division] = field(default_factory=dict)
    convoys: dict[str, Convoy] = field(default_factory=dict)
    battles: dict[str, Battle] = field(default_factory=dict)
    airbases: dict[str, Airbase] = field(default_factory=dict)
    squadrons: dict[str, Squadron] = field(default_factory=dict)
    packages: dict[str, Package] = field(default_factory=dict)
    flights: dict[str, Flight] = field(default_factory=dict)
    archive: dict[str, Package] = field(default_factory=dict)
    archived_flights: dict[str, Flight] = field(default_factory=dict)
    air_defense: dict[str, AirDefenseUnit] = field(default_factory=dict)
    player_flights: dict[str, str] = field(default_factory=dict)  # player id -> flight id
    sortie_log: dict[str, list[tuple[float, int]]] = field(default_factory=dict)  # faction -> (time, aircraft)
    sortie_tokens: dict[str, float] = field(default_factory=dict)  # faction -> ATO pacing bucket
    fires: list = field(default_factory=list)  # recent artillery fire missions (systems.artillery)
    # Destroyed brigades waiting to be re-formed: (ready time, faction, template, name).
    reforming: list = field(default_factory=list)
    reforming_ad: list = field(default_factory=list)  # air defence batteries waiting for replacement
    # Fog of war: faction -> entity id -> Contact (see systems.intel).
    intel: dict[str, dict] = field(default_factory=dict)
    # Players as last reported by the engine: id -> (side, position, kind).
    player_positions: dict[str, tuple] = field(default_factory=dict)
    events: EventLog = field(default_factory=EventLog)
    rng: random.Random = field(init=False)
    # Entities whose orders/route changed this step (adapters must hear about it).
    dirty: set[str] = field(default_factory=set)
    # Next campaign time each periodic system runs (e.g. "ai", "logistics").
    timers: dict[str, float] = field(default_factory=dict)
    _counter: int = 0

    def __post_init__(self) -> None:
        """Seed the random generator."""
        self.rng = random.Random(self.seed)

    # ------------------------------------------------------------ ids / events

    def new_id(self, prefix: str) -> str:
        """A new unique id.

        Args:
            prefix: Id prefix, e.g. ``"bt"``.

        Returns:
            The id, ``prefix`` followed by a campaign-wide counter.
        """
        self._counter += 1
        return f"{prefix}{self._counter}"

    def emit(self, kind: EventKind, **data: object) -> None:
        """Append an event at the current campaign time.

        Args:
            kind: Event type.
            **data: Payload.
        """
        self.events.emit(self.time, kind, **data)

    # ------------------------------------------------------------ queries

    def entities(self) -> Iterator[MobileEntity]:
        """Iterate over every mobile entity (divisions, convoys, flights, air defence).

        Yields:
            Divisions, convoys, flights and air defence units, in that order.
        """
        yield from self.divisions.values()
        yield from self.convoys.values()
        yield from self.flights.values()
        yield from self.air_defense.values()

    def entity(self, eid: str) -> MobileEntity | None:
        """Find any mobile entity by id.

        Args:
            eid: Entity id.

        Returns:
            The entity, or None.
        """
        return self.divisions.get(eid) or self.convoys.get(eid) or self.flights.get(eid) or self.air_defense.get(eid)

    def at_war(self, a: str, b: str) -> bool:
        """True if factions ``a`` and ``b`` are at war.

        Args:
            a: Faction id.
            b: Faction id.

        Returns:
            Whether either lists the other as an enemy.
        """
        return b in self.factions[a].enemies or a in self.factions[b].enemies

    def hostile_province(self, faction: str, pid: str) -> bool:
        """True if ``pid`` is held by a faction at war with ``faction``.

        Args:
            faction: Faction id.
            pid: Province id.

        Returns:
            Whether the province's controller is at war with ``faction``.
        """
        return self.at_war(faction, self.theater.provinces[pid].controller)

    def friendly_province(self, faction: str, pid: str) -> bool:
        """True if ``pid`` is not hostile to ``faction``.

        Args:
            faction: Faction id.
            pid: Province id.

        Returns:
            Whether the province is friendly or neutral to ``faction``.
        """
        return not self.hostile_province(faction, pid)

    def divisions_in(self, pid: str, faction: str | None = None) -> list[Division]:
        """Divisions holding ``pid``: located there and not on the road elsewhere.

        A division closing in to attack out of ``pid`` still holds it (it is
        between the enemy and the province); one marching or withdrawing does not.

        Args:
            pid: Province id.
            faction: Keep only this faction's divisions (None = all).

        Returns:
            The divisions holding the province.
        """
        return [
            d
            for d in self.divisions.values()
            if d.location == pid
            and not (
                d.movement
                and d.movement.progress > 0
                and not (d.order.kind is OrderKind.ATTACK and not d.movement.back)
            )
            and (faction is None or d.faction == faction)
        ]

    def hostile_divisions_in(self, faction: str, pid: str) -> list[Division]:
        """Divisions at war with ``faction`` holding province ``pid``.

        Args:
            faction: Faction id.
            pid: Province id.

        Returns:
            The enemy divisions holding the province.
        """
        return [d for d in self.divisions_in(pid) if self.at_war(faction, d.faction)]

    def front_provinces(self, faction: str) -> list[str]:
        """Provinces held by ``faction`` that border a hostile one.

        Args:
            faction: Faction id.

        Returns:
            Ids of the faction's front provinces.
        """
        t = self.theater
        return [
            pid
            for pid, p in t.provinces.items()
            if p.controller == faction and any(self.hostile_province(faction, n) for n in t.neighbors(pid))
        ]

    def faction_for_side(self, side: str) -> Faction | None:
        """The faction playing a game side.

        Args:
            side: ``"blue"`` or ``"red"``.

        Returns:
            The first faction of that side, or None.
        """
        return next((f for f in self.factions.values() if f.side == side), None)

    # ------------------------------------------------------------ orders

    def set_order(self, div: Division, order: Order) -> bool:
        """Give an order and plan the route. Returns False if no route exists.

        Args:
            div: The division.
            order: The new order (``HOLD`` or no target clears it).

        Returns:
            Whether the order was accepted (False if there is no route, or a non-attack order
            targets a hostile province).
        """
        if order.kind is OrderKind.HOLD or order.target is None:
            div.order = Order()
            if div.movement and div.movement.progress == 0:
                div.movement = None
            self.dirty.add(div.id)
            return True
        hostile_ok = order.kind is OrderKind.ATTACK
        path = self.theater.shortest_path(
            div.location,
            order.target,
            passable=lambda pid: self.friendly_province(div.faction, pid),
            penalty=self.road_penalty(div.faction),
        )
        if path is None or (not hostile_ok and self.hostile_province(div.faction, order.target)):
            return False
        # Keep progress if we keep travelling along the same link.
        progress = 0.0
        if div.movement and div.movement.path and path and div.movement.path[0] == path[0]:
            progress = div.movement.progress
        div.order = order
        div.movement = Movement(path, progress) if path else None
        if path:
            div.deployed_toward = None
        self.dirty.add(div.id)
        return True

    #: A road where a faction was ambushed is avoided this long, seconds.
    CONTESTED_ROAD_S = 6 * 3600.0
    #: ...by making it this much costlier (still used if it is the only way).
    CONTESTED_ROAD_PENALTY = 20.0

    def mark_contested(self, faction: str, a: str, b: str) -> None:
        """``faction`` ran into the enemy on the road ``a``-``b``: avoid it for a while.

        Args:
            faction: Faction that met the enemy.
            a: One end of the road.
            b: The other end.
        """
        self.timers[f"contested:{faction}:" + "|".join(sorted((a, b)))] = self.time + self.CONTESTED_ROAD_S

    def road_penalty(self, faction: str) -> Callable[[str, str], float]:
        """Link cost multiplier for ``faction``'s routes: roads it recently lost a
        fight or a convoy on are avoided when another way exists.

        Args:
            faction: Faction id.

        Returns:
            A function giving the cost multiplier of the link between two provinces.
        """
        prefix = f"contested:{faction}:"
        contested = {k[len(prefix):] for k, v in self.timers.items() if k.startswith(prefix) and v > self.time}

        def penalty(a: str, b: str) -> float:
            """Cost multiplier of the link a-b.

            Args:
                a: One end of the link.
                b: The other end.

            Returns:
                ``CONTESTED_ROAD_PENALTY`` on a contested road, else 1.
            """
            return self.CONTESTED_ROAD_PENALTY if "|".join(sorted((a, b))) in contested else 1.0

        return penalty

    def route_entity(self, ent: MobileEntity, target: str) -> bool:
        """Route a non-combat entity (e.g. a convoy) through friendly territory only.

        Args:
            ent: The entity.
            target: Destination province id.

        Returns:
            Whether a route was found (False also for a hostile target).
        """
        if self.hostile_province(ent.faction, target):
            return False
        path = self.theater.shortest_path(
            ent.location, target, passable=lambda pid: self.friendly_province(ent.faction, pid),
            penalty=self.road_penalty(ent.faction),
        )
        if path is None:
            return False
        progress = 0.0
        if ent.movement and ent.movement.path and path and ent.movement.path[0] == path[0]:
            progress = ent.movement.progress
        ent.movement = Movement(path, progress) if path else None
        self.dirty.add(ent.id)
        return True

    # ------------------------------------------------------------ snapshot

    def snapshot(self) -> dict:
        """Plain-data view of the campaign (for the API / web UI).

        Returns:
            Provinces, divisions and other campaign data as JSON-ready dictionaries.
        """
        t = self.theater
        return {
            "name": self.name,
            "time": self.time,
            "provinces": {
                pid: {
                    "name": p.name,
                    "pos": [p.center.x, p.center.y],
                    "controller": p.controller,
                    "owner": p.owner,
                    "terrain": p.terrain.value,
                    "depot": None if p.depot is None else {"stock": round(p.depot.stock), "trucks": p.depot.trucks},
                }
                for pid, p in t.provinces.items()
            },
            "divisions": {
                d.id: {
                    "name": d.name,
                    "faction": d.faction,
                    "location": d.location,
                    "pos": [round(v) for v in (d.position(t).x, d.position(t).y)],
                    "strength": round(d.strength, 3),
                    "org": round(d.org, 3),
                    "supply": round(d.supply_ratio, 3),
                    "order": [d.order.kind.value, d.order.target],
                    "authority": d.authority.value,
                    "battle": d.battle,
                }
                for d in self.divisions.values()
            },
            "convoys": {
                c.id: {
                    "faction": c.faction,
                    "pos": [round(v) for v in (c.position(t).x, c.position(t).y)],
                    "cargo": round(c.cargo, 1),
                    "trucks": round(c.trucks, 1),
                    "destination": c.home if c.returning else c.destination,
                    "authority": c.authority.value,
                }
                for c in self.convoys.values()
            },
            "packages": {
                p.id: {
                    "faction": p.faction,
                    "task": p.task.value,
                    "target": p.target_label,
                    "tot": p.tot,
                    "flights": list(p.flights),
                }
                for p in self.packages.values()
            },
            "flights": {
                f.id: {
                    "callsign": f.callsign,
                    "faction": f.faction,
                    "task": f.task.value,
                    "aircraft": f.aircraft_type.id,
                    "size": f.size,
                    "status": f.status.value,
                    "pos": [round(f.pos.x), round(f.pos.y)],
                    "players": [s.name for s in f.players.values()],
                    "authority": f.authority.value,
                }
                for f in self.flights.values()
            },
            "battles": {
                b.id: {
                    "province": b.province,
                    "attackers": list(b.attackers),
                    "defenders": list(b.defenders),
                }
                for b in self.battles.values()
            },
        }
