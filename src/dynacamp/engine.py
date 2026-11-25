"""The campaign engine: runs the systems and mediates handoffs with a game adapter."""

from __future__ import annotations

import threading
import time as _time
from collections.abc import Callable
from dataclasses import dataclass, field

from .adapters.base import (
    Command,
    DematerializeAck,
    Dematerialize,
    EntitySnapshot,
    EntityState,
    GameAdapter,
    GameDisconnected,
    BriefingRequest,
    JoinRequest,
    LeaveRequest,
    Materialize,
    MaterializeAck,
    NotifyPlayer,
    PlayerEvent,
    PlayerEventKind,
    PlayerLeft,
    PlayerSeen,
    PublishAto,
    Report,
    UpdateRoute,
)
from . import callsigns
from .air import Flight, FlightStatus, Task, flight_altitude, waypoint_altitudes
from .air_defense import AirDefenseUnit
from .bubble import BubbleConfig, BubbleManager, Player
from .events import EventKind
from .forces import Authority, Convoy, Division, MobileEntity
from .state import CampaignState
from .systems import PIPELINE, air, ato, combat, movement



MIN_COMPRESSION = 1.0
MAX_COMPRESSION = 7200.0


@dataclass
class TimeControl:
    """Pause and speed of the real-time loop (see :mod:`dynacamp.runtime`).

    While a game simulates part of the campaign (:attr:`Engine.live`) time runs
    1:1 whatever is set here: the game can be neither paused nor accelerated
    from the campaign.

    Attributes:
        compression: Campaign seconds per real second when not live.
        paused: Campaign time stands still (the engine still talks to the game).
    """

    compression: float = 60.0
    paused: bool = False

    def set(self, paused: bool | None = None, compression: float | None = None) -> None:
        """Change pause and/or compression.

        Raises:
            ValueError: If the compression is outside
                [``MIN_COMPRESSION``, ``MAX_COMPRESSION``].
        """
        if compression is not None:
            if not MIN_COMPRESSION <= compression <= MAX_COMPRESSION:
                raise ValueError(f"compression must be between {MIN_COMPRESSION:g} and {MAX_COMPRESSION:g}")
            self.compression = float(compression)
        if paused is not None:
            self.paused = bool(paused)


@dataclass(frozen=True)
class EngineConfig:
    """Engine tuning.

    Attributes:
        max_substep_s: Systems never integrate more than this at once.
        handoff_timeout_s: Campaign seconds to wait for an adapter acknowledgement.
        arrival_radius_m: A delegated ground entity reached a province centre.
        waypoint_radius_m: A delegated flight reached a waypoint.
        bubble: Player bubble settings.
    """
    max_substep_s: float = 60.0  # systems never integrate more than this at once
    handoff_timeout_s: float = 30.0  # campaign seconds to wait for an adapter ack
    arrival_radius_m: float = 3_000.0  # delegated entity "reached" a province centre
    waypoint_radius_m: float = 5_000.0  # delegated flight "reached" a waypoint
    bubble: BubbleConfig = field(default_factory=BubbleConfig)


class Engine:
    """Runs the simulation systems and mediates handoffs with a game adapter.

    Each :meth:`step` reads the adapter's reports (players, acknowledgements,
    observations, ATO requests), decides handoffs from the player bubbles, runs
    the systems in substeps, then sends commands (materialise, dematerialise,
    routes, ATO boards, notifications). The whole step holds :attr:`lock`.

    Attributes:
        state: The campaign.
        adapter: The game adapter, or None for a campaign without game.
        config: Engine settings.
        bubbles: Player bubbles.
        lock: Re-entrant lock held during steps (readers such as the API take it).
    """
    def __init__(
        self,
        state: CampaignState,
        adapter: GameAdapter | None = None,
        config: EngineConfig | None = None,
        wall_clock: Callable[[], float] = _time.monotonic,
    ) -> None:
        """Args:
            state: The campaign.
            adapter: Optional game adapter.
            config: Engine settings (defaults if None).
            wall_clock: Real-time clock (for player timeouts).
        """
        self.state = state
        self.adapter = adapter
        self.config = config or EngineConfig()
        self.bubbles = BubbleManager(self.config.bubble)
        self.wall_clock = wall_clock
        self._outbox: list[Command] = []
        self._in_game: set[str] = set()  # ids the game currently holds
        self._published_ato: dict[str, tuple] = {}  # side -> last board sent
        # Held for the whole of a step; readers (the HTTP API) take it to see a
        # consistent state between steps.
        self.lock = threading.RLock()
        # Pause / fast-forward of the real-time loop (the API changes it).
        self.control = TimeControl()

    # ------------------------------------------------------------ main loop

    @property
    def live(self) -> bool:
        """True while a game is simulating part of the campaign: time must run 1:1."""
        return self.bubbles.active or bool(self._in_game)

    @property
    def time_rate(self) -> float:
        """Campaign seconds per real second right now: 0 paused, 1 live, else the compression."""
        if self.live:
            return 1.0
        return 0.0 if self.control.paused else self.control.compression

    def step(self, dt: float) -> None:
        """Advance the campaign by ``dt`` campaign seconds (holds the lock).

        Args:
            dt: Campaign seconds.
        """
        with self.lock:
            self._step(dt)

    def _step(self, dt: float) -> None:
        """Body of :meth:`step`: reports, handoffs, systems in substeps, commands."""
        if self.adapter is not None:
            for report in self.adapter.poll():
                self._handle(report)

            for pid in self.bubbles.prune(self.wall_clock()):
                self.state.emit(EventKind.PLAYER_LEFT, player=pid, reason="timeout")

            self._handoffs()

        # Players are sensors too (see systems.intel).
        self.state.player_positions = {
            p.id: (p.side, p.position, "air" if p.kind.value in ("air", "helo") else "ground")
            for p in self.bubbles.players.values()
        }
        remaining = dt
        while remaining > 0:
            sub = min(remaining, self.config.max_substep_s)

            for system in PIPELINE:
                system.run(self.state, sub)

            self.state.time += sub
            remaining -= sub

        self._sync_game()

    def run_for(self, seconds: float, dt: float = 60.0) -> None:
        """Advance by ``seconds`` in steps of ``dt``.

        Args:
            seconds: Campaign seconds to simulate.
            dt: Step size.
        """
        end = self.state.time + seconds
        while self.state.time < end:
            self.step(min(dt, end - self.state.time))

    # ------------------------------------------------------------ handoffs

    def _handoffs(self) -> None:
        """Start/finish handoffs from the desired delegation set; time out stuck ones."""
        state, cfg = self.state, self.config
        wanted = self.bubbles.desired_delegation(state)
        for ent in list(state.entities()):
            a = ent.authority
            
            if a is Authority.CAMPAIGN and ent.id in wanted:
                ent.authority = Authority.MATERIALIZING
                ent.handoff_started = state.time
                self._outbox.append(Materialize(self._snapshot(ent)))
                self._in_game.add(ent.id)
                state.emit(EventKind.HANDOFF, entity=ent.id, to="game")

            elif a is Authority.DELEGATED and ent.id not in wanted:
                ent.authority = Authority.DEMATERIALIZING
                ent.handoff_started = state.time
                self._outbox.append(Dematerialize(ent.id))
                state.emit(EventKind.HANDOFF, entity=ent.id, to="campaign")

            elif a in (Authority.MATERIALIZING, Authority.DEMATERIALIZING):
                assert ent.handoff_started is not None
                if state.time - ent.handoff_started > cfg.handoff_timeout_s:
                    state.emit(EventKind.HANDOFF_TIMEOUT, entity=ent.id, state=a.value)
                    self._reclaim(ent)

    def _reclaim(self, ent: MobileEntity) -> None:
        """Take an entity back with its last known state, whatever the game says."""
        ent.authority = Authority.CAMPAIGN
        ent.game_position = None
        ent.handoff_started = None

        if ent.id in self._in_game and self.adapter is not None and self.adapter.connected:
            self._outbox.append(Dematerialize(ent.id))

        self._in_game.discard(ent.id)

    def _reclaim_all(self, reason: str) -> None:
        """The game is gone: take every entity back and forget the players."""
        for ent in self.state.entities():
            if ent.authority is not Authority.CAMPAIGN:
                ent.authority = Authority.CAMPAIGN
                ent.game_position = None
                ent.handoff_started = None

        self._in_game.clear()
        self._published_ato.clear()
        for pid in list(self.state.player_flights):
            ato.player_disconnected(self.state, pid)

        self.bubbles.clear()
        self.state.emit(EventKind.GAME_DISCONNECTED, reason=reason)

    # ------------------------------------------------------------ reports

    def _handle(self, r: Report) -> None:
        """Apply one report from the adapter."""
        state = self.state
        match r:
            case PlayerSeen():
                if r.player_id not in self.bubbles.players:
                    state.emit(EventKind.PLAYER_JOINED, player=r.player_id, name=r.name, side=r.side, player_kind=r.kind.value)
                self.bubbles.update_player(
                    Player(r.player_id, r.name, r.side, r.kind, r.position, r.altitude, self.wall_clock())
                )

            case PlayerLeft():
                if r.player_id in self.bubbles.players:
                    self.bubbles.remove_player(r.player_id)
                    state.emit(EventKind.PLAYER_LEFT, player=r.player_id, reason="left")
                ato.player_disconnected(state, r.player_id)

            case JoinRequest():
                player = self.bubbles.players.get(r.player_id)
                if player is None:
                    return
                capable = None if r.capable_tasks is None else frozenset(Task(t) for t in r.capable_tasks if t in Task.__members__)
                ok, text = ato.join(state, r.player_id, player.name, player.side, r.flight_id, capable)
                self._notify(r.player_id, text, with_marks=ok)

            case LeaveRequest():
                left = ato.leave(state, r.player_id)
                self._notify(r.player_id, "You left your flight." if left else "You are not assigned to a flight.")

            case BriefingRequest():
                found = ato.seat_of(state, r.player_id)
                if found is None:
                    self._notify(r.player_id, "You are not assigned to a flight. Pick one from the ATO menu.")

                else:
                    self._notify(r.player_id, ato.briefing_text(state, found[0]), with_marks=True)

            case PlayerEvent():
                if r.event is PlayerEventKind.TAKEOFF:
                    ato.player_took_off(state, r.player_id)

                elif r.event is PlayerEventKind.LANDED:
                    ato.player_landed(state, r.player_id)

                elif r.event is PlayerEventKind.LOST:
                    ato.player_lost(state, r.player_id)

            case MaterializeAck():
                ent = state.entity(r.entity_id)
                if ent is None or ent.authority is not Authority.MATERIALIZING:
                    return

                if r.ok:
                    ent.authority = Authority.DELEGATED
                    ent.handoff_started = None

                else:
                    state.emit(EventKind.HANDOFF_FAILED, entity=ent.id, error=r.error)
                    self._in_game.discard(ent.id)
                    ent.authority = Authority.CAMPAIGN
                    ent.handoff_started = None

            case EntityState():
                ent = state.entity(r.entity_id)
                if ent is not None and ent.authority is Authority.DELEGATED:
                    self._apply_observation(ent, r.position, r.elements)

            case DematerializeAck():
                self._in_game.discard(r.entity_id)
                ent = state.entity(r.entity_id)

                if ent is None or ent.authority is not Authority.DEMATERIALIZING:
                    return

                self._apply_observation(ent, r.position, r.elements)
                ent.authority = Authority.CAMPAIGN
                ent.game_position = None
                ent.handoff_started = None

            case GameDisconnected():
                self._reclaim_all(r.reason)

    def _notify(self, player_id: str, text: str, with_marks: bool = False) -> None:
        """Queue a message (and optionally the player's flight waypoints) for a player.

        Args:
            player_id: Player id.
            text: Message.
            with_marks: Attach the waypoints of the player's flight as map marks.
        """
        marks: tuple = ()
        if with_marks:
            found = ato.seat_of(self.state, player_id)

            if found is not None:
                marks = tuple(ato.waypoint_marks(self.state, found[0]))

        self._outbox.append(NotifyPlayer(player_id, text, marks))

    def notify(self, player_id: str, text: str) -> None:
        """Queue a message for a player in the game (dropped if they are not in it).

        Args:
            player_id: Player id.
            text: Message.
        """
        with self.lock:
            if player_id in self.bubbles.players:
                self._notify(player_id, text)

    def brief_players(self, flight_ids) -> int:
        """Send the current briefing and waypoint marks to the players seated in
        flights whose plan changed (see :mod:`dynacamp.systems.orders`).

        Args:
            flight_ids: The flights.

        Returns:
            How many players were briefed.
        """
        n = 0
        with self.lock:
            for fid in flight_ids:
                f = self.state.flights.get(fid)
                if f is None:
                    continue
                for seat in f.active_seats:
                    if seat.player_id in self.bubbles.players:
                        text = "FLIGHT PLAN CHANGED\n\n" + ato.briefing_text(self.state, f)
                        self._notify(seat.player_id, text, with_marks=True)
                        n += 1
        return n

    def _observe_flight(self, f: Flight, pos, elements: dict[str, float]) -> None:
        """Apply a game observation to a delegated flight (losses, position, waypoints)."""
        if "aircraft" in elements:
            alive = int(round(elements["aircraft"]))
            if alive < f.ai_count:
                air.lose_aircraft(self.state, f, f.ai_count - alive, "game")
        if pos is None:
            return
        f.pos = pos
        f.game_position = pos

        if f.status is FlightStatus.ON_STATION and self.state.time >= f.station_until:
            f.status = FlightStatus.RTB
            f.wp_index = f.target_index + 1

        if f.wp_index < len(f.waypoints) and pos.distance_to(f.waypoints[f.wp_index]) <= self.config.waypoint_radius_m:
            reached = f.wp_index
            if reached == f.target_index:
                f.attacked = True  # the game flew the attack
                
                if f.task.orbits:
                    f.status = FlightStatus.ON_STATION
                    return
                
                f.status = FlightStatus.RTB
                
            f.wp_index += 1
            
            if f.wp_index >= len(f.waypoints):
                f.ai_done = True

    def _apply_observation(self, ent: MobileEntity, pos, elements: dict[str, float]) -> None:
        """Apply a game observation to a delegated entity.

        Args:
            ent: The entity.
            pos: Reported position (None if nothing is alive).
            elements: Alive elements in campaign units.
        """
        t = self.state.theater
        if isinstance(ent, Flight):
            self._observe_flight(ent, pos, elements)
            return

        if isinstance(ent, Division) and elements:
            before = ent.strength
            for k in ent.elements:
                # The game can only kill; reinforcements are a campaign matter.
                ent.elements[k] = min(ent.elements[k], max(0.0, elements.get(k, 0.0)))

            combat.apply_reported_losses(ent, before - ent.strength)

            if before - ent.strength > 0:
                self.state.emit(EventKind.GAME_LOSSES, entity=ent.id, strength=round(ent.strength, 3))

        elif isinstance(ent, AirDefenseUnit) and elements:
            lost = False
            for k in ent.elements:
                alive = min(ent.elements[k], max(0.0, elements.get(k, 0.0)))
                lost |= alive < ent.elements[k]
                ent.elements[k] = alive
            if lost:
                self.state.emit(EventKind.GAME_LOSSES, entity=ent.id, strength=round(ent.strength, 3))

        elif isinstance(ent, Convoy) and elements:
            trucks = min(ent.trucks, elements.get("truck", 0.0))
            if trucks < ent.trucks:
                ent.cargo *= trucks / ent.trucks if ent.trucks else 0.0
                ent.trucks = trucks
                self.state.emit(EventKind.GAME_LOSSES, entity=ent.id, trucks=round(trucks, 1))

            if ent.trucks <= 0:
                self.state.convoys.pop(ent.id, None)
                self.state.emit(EventKind.CONVOY_DESTROYED, convoy=ent.id, faction=ent.faction, reason="game")
                return

        if pos is None:
            return

        ent.game_position = pos

        if ent.movement and ent.movement.path:
            nxt = t.provinces[ent.movement.path[0]].center
            if pos.distance_to(nxt) <= self.config.arrival_radius_m:
                movement.arrive(self.state, ent)
            else:
                ent.movement.progress = t.project_on_link(ent.location, ent.movement.path[0], pos)

    # ------------------------------------------------------------ outgoing

    def _snapshot(self, ent: MobileEntity) -> EntitySnapshot:
        """Describe an entity for the adapter (:class:`EntitySnapshot`)."""
        t = self.state.theater
        faction = self.state.factions[ent.faction]

        if isinstance(ent, Division):
            name, elements, speed = ent.name, dict(ent.elements), ent.template.speed_kmh

        elif isinstance(ent, Convoy):
            name, elements, speed = f"Convoy {ent.id}", {"truck": ent.trucks}, ent.speed_kmh

        elif isinstance(ent, Flight):
            return self._flight_snapshot(ent)

        elif isinstance(ent, AirDefenseUnit):
            return EntitySnapshot(
                id=ent.id, kind="sam", name=ent.name, faction=ent.faction, side=faction.side,
                position=ent.position(t), heading=ent.heading(t), elements=dict(ent.elements),
                waypoints=self._waypoints(ent), speed_kmh=ent.system.speed_kmh,
                attrs={"system": ent.system.id, "road_back": self._road_behind(ent), "deployed": ent.movement is None},
            )

        else:  # pragma: no cover
            raise TypeError(ent)

        return EntitySnapshot(
            id=ent.id,
            kind=ent.kind,
            name=name,
            faction=ent.faction,
            side=faction.side,
            position=ent.position(t),
            heading=ent.heading(t),
            elements=elements,
            waypoints=self._waypoints(ent),
            speed_kmh=speed,
            attrs={"road_back": self._road_behind(ent)},
        )

    def _road_behind(self, ent: MobileEntity, length: float = 3_000.0) -> list:
        """The road behind the entity (nearest point first), to line its vehicles up on."""
        t = self.state.theater

        if ent.movement and ent.movement.path:
            nxt = ent.movement.path[0]
            ahead = t.link_points_from(ent.location, nxt, ent.movement.progress)
            road = t.link_points(ent.location, nxt)
            behind = [ahead[0], *[p for p in road if p not in ahead][::-1]]
            if ent.movement.back:  # withdrawing: "behind" is towards the target
                behind = ahead

        else:
            toward = getattr(ent, "deployed_toward", None) or next(iter(t.neighbors(ent.location)), None)
            if toward is None:
                return []
            s = getattr(ent, "deploy_m", 0.0) if getattr(ent, "deployed_toward", None) else 0.0
            # Deployed units face the enemy: their column trails back towards home.
            road = t.link_points(ent.location, toward)
            here = t.point_on_link(ent.location, toward, s)
            behind = [here, *[p for p in road if t.project_on_link(ent.location, toward, p) < s][::-1]]
            if len(behind) < 2:
                behind = t.link_points_from(ent.location, toward, s)
        out, acc = [behind[0]], 0.0

        for p, q in zip(behind, behind[1:]):
            out.append(q)
            acc += p.distance_to(q)
            if acc >= length:
                break

        return out

    def _flight_snapshot(self, f: Flight) -> EntitySnapshot:
        """Snapshot of a flight's AI part."""
        faction = self.state.factions[f.faction]
        return EntitySnapshot(
            id=f.id,
            kind="flight",
            name=f.callsign,
            faction=f.faction,
            side=faction.side,
            position=f.pos,
            heading=f.heading(self.state.theater),
            elements={"aircraft": float(f.ai_count)},
            waypoints=self._waypoints(f),
            speed_kmh=f.aircraft_type.cruise_kmh,
            altitude=flight_altitude(f),
            attrs={
                # planned altitude at each waypoint sent (the briefing's profile)
                "altitudes": tuple(waypoint_altitudes(f)[self._route_start(f):]),
                "aircraft": f.aircraft_type.id,
                "task": f.task.value,
                "callsign": f.callsign,
                # DCS radio callsign (None for eastern sides); AI after the players' seats
                "radio_callsign": callsigns.radio_callsign(self.state, f),
                "first_unit": len(f.active_seats) + 1,
                "helo": f.aircraft_type.helo,
                "home": self.state.airbases[self.state.squadrons[f.squadron].airbase].name,
                "target": f.target_point,
                "target_index": f.target_index - f.wp_index,  # index within the waypoints sent
                "on_station_s": max(0.0, f.station_until - self.state.time) if f.task.orbits else 0.0,
                # AI talk on the package frequency: the one in the players' briefing
                "frequency_mhz": self.state.packages[f.package].uhf_mhz if f.package in self.state.packages else 0.0,
            },
        )

    @staticmethod
    def _route_start(f: Flight) -> int:
        """First waypoint of a flight still to send to the game (the station while on it)."""
        return f.target_index if f.status is FlightStatus.ON_STATION else f.wp_index

    def _waypoints(self, ent: MobileEntity):
        """Remaining route of an entity as points (province centres or flight waypoints)."""
        t = self.state.theater
        if isinstance(ent, Flight):
            return tuple(ent.waypoints[self._route_start(ent):])

        if not ent.movement:
            return ()

        return tuple(t.provinces[p].center for p in ent.movement.path)

    def _publish_ato(self) -> None:
        """Send the join boards to the game when they change."""
        if not self.bubbles.active:
            return

        for side in sorted({f.side for f in self.state.factions.values()}):
            board = tuple(ato.ato_board(self.state, side))
            if self._published_ato.get(side) != board:
                self._published_ato[side] = board
                self._outbox.append(PublishAto(side, board))

    def _sync_game(self) -> None:
        """Despawn vanished entities, send route updates and boards, flush commands."""
        state = self.state
        if self.adapter is None:
            state.dirty.clear()
            return

        # Entities that vanished from the campaign while the game held them.
        for eid in list(self._in_game):
            if state.entity(eid) is None:
                self._outbox.append(Dematerialize(eid))
                self._in_game.discard(eid)

        pending = set()
        for eid in sorted(state.dirty):
            ent = state.entity(eid)
            if ent is None:
                continue
            if ent.authority is Authority.MATERIALIZING:
                pending.add(eid)  # the spawn may carry the old route: resend once the game holds it
            elif ent.authority is Authority.DELEGATED:
                if isinstance(ent, Flight):
                    snap = self._flight_snapshot(ent)
                    self._outbox.append(UpdateRoute(eid, snap.waypoints, snap.speed_kmh, snapshot=snap))
                else:
                    speed = ent.template.speed_kmh if isinstance(ent, Division) else getattr(ent, "speed_kmh", 30.0)
                    self._outbox.append(UpdateRoute(eid, self._waypoints(ent), speed))

        state.dirty.clear()
        state.dirty |= pending
        self._publish_ato()

        if self._outbox and self.adapter.connected:
            self.adapter.send(self._outbox)

        self._outbox = []
