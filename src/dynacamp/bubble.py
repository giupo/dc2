"""Player bubbles (Falcon 4 style).

Every human player projects a bubble around them. Whatever is inside a bubble
is simulated by the game; everything else by the campaign. Two refinements:

* hysteresis: an entity enters at ``radius`` but only leaves beyond
  ``radius + hysteresis``, so it does not flicker at the edge;
* battle closure: a battle is never split. If any participant (or the battle
  province) is in a bubble, all participants are delegated together.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .adapters.base import PlayerKind, Side
from .forces import Authority, MobileEntity
from .geo import Vec2
from .state import BattleKind, CampaignState


@dataclass(frozen=True)
class BubbleConfig:
    """Player bubble settings.

    Attributes:
        radius_m: Bubble radius per player kind (air, helicopter, ground).
        hysteresis_m: Extra distance before an entity leaves the bubble.
        player_timeout_s: Real seconds without news before a player is dropped.
    """
    radius_m: dict[PlayerKind, float] = field(
        default_factory=lambda: {
            PlayerKind.AIR: 60_000.0,
            PlayerKind.HELO: 30_000.0,
            PlayerKind.GROUND: 15_000.0,
        }
    )
    hysteresis_m: float = 10_000.0
    player_timeout_s: float = 30.0  # real seconds without news -> player gone


@dataclass
class Player:
    """A human player as seen by the engine.

    Attributes:
        id: Player id (adapter-defined).
        name: Display name.
        side: Game side.
        kind: Aircraft, helicopter or ground.
        position: Last reported position.
        altitude: Metres.
        last_seen: Wall-clock time of the last report.
    """
    id: str
    name: str
    side: Side
    kind: PlayerKind
    position: Vec2
    altitude: float
    last_seen: float  # wall-clock seconds


class BubbleManager:
    """Tracks players and decides which entities the game should simulate."""
    def __init__(self, config: BubbleConfig | None = None) -> None:
        """Start with no players and no bubble entities.

        Args:
            config: Bubble settings (defaults if None).
        """
        self.config = config or BubbleConfig()
        self.players: dict[str, Player] = {}

    # ------------------------------------------------------------ players

    def update_player(self, player: Player) -> None:
        """Add or refresh a player.

        Args:
            player: The player as last reported by the game.
        """
        self.players[player.id] = player

    def remove_player(self, player_id: str) -> None:
        """Forget a player.

        Args:
            player_id: Id of the player who left.
        """
        self.players.pop(player_id, None)

    def prune(self, now: float) -> list[str]:
        """Drop players not heard from within the timeout.

        Args:
            now: Current wall-clock time.

        Returns:
            Ids of the dropped players.
        """
        gone = [pid for pid, p in self.players.items() if now - p.last_seen > self.config.player_timeout_s]
        for pid in gone:
            del self.players[pid]
        return gone

    def clear(self) -> None:
        """Forget all players."""
        self.players.clear()

    @property
    def active(self) -> bool:
        """True if any player is present."""
        return bool(self.players)

    # ------------------------------------------------------------ geometry

    def inside(self, pos: Vec2, extra: float = 0.0) -> bool:
        """True if ``pos`` is inside any player's bubble.

        Args:
            pos: Point.
            extra: Extra radius (hysteresis, entity reach).

        Returns:
            Whether a player's bubble (plus ``extra``) contains the point.
        """
        for p in self.players.values():
            if pos.distance_to(p.position) <= self.config.radius_m[p.kind] + extra:
                return True
        return False

    def desired_delegation(self, state: CampaignState) -> set[str]:
        """Ids of the entities that should be simulated by the game right now.

        Args:
            state: The campaign.

        Returns:
            Ids of the entities inside a bubble, with every member of a battle that touches one.
        """
        if not self.players:
            return set()
        t = state.theater
        wanted: set[str] = set()
        for ent in state.entities():
            if not ent.materializable:
                continue
            extra = (self.config.hysteresis_m if _held_by_game(ent) else 0.0) + ent.bubble_reach
            if self.inside(ent.position(t), extra):
                wanted.add(ent.id)

        for battle in state.battles.values():
            members = battle.attackers + battle.defenders
            held = any(_held_by_game(state.divisions[i]) for i in members if i in state.divisions)
            extra = self.config.hysteresis_m if held else 0.0
            where = t.provinces[battle.province].center
            if battle.kind is BattleKind.MEETING:
                from .systems.combat import battle_point

                where = battle_point(state, battle)
            if any(i in wanted for i in members) or self.inside(where, extra):
                wanted.update(i for i in members if i in state.divisions)
        return wanted


def _held_by_game(ent: MobileEntity) -> bool:
    """True if the game holds or is taking over the entity.

    Args:
        ent: The entity.

    Returns:
        Whether its authority is ``MATERIALIZING`` or ``DELEGATED``.
    """
    return ent.authority in (Authority.MATERIALIZING, Authority.DELEGATED)
