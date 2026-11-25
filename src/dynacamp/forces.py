"""Forces: element types, division templates and the mobile entities on the map.

Units are aggregated (HOI4-style divisions/brigades). Element counts are *real*
counts (vehicles, infantry squads); a game adapter decides how many in-game
objects represent them when an entity is materialised inside a player bubble.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from . import catalog
from .geo import Vec2
from .world import Theater


# ---------------------------------------------------------------- elements


@dataclass(frozen=True, slots=True)
class ElementType:
    """A kind of combat or support element (one vehicle or infantry squad).

    Attributes:
        id: Element id, e.g. ``"mbt"``.
        soft_attack: Firepower against unarmoured targets.
        hard_attack: Firepower against armour.
        defense: Attacks it absorbs when defending.
        breakthrough: Attacks it absorbs when attacking.
        hardness: 0 = soft, 1 = fully armoured.
        combat: False for support elements such as trucks.
    """
    id: str
    soft_attack: float  # firepower vs unarmoured targets
    hard_attack: float  # firepower vs armour
    defense: float  # attacks it can absorb when defending
    breakthrough: float  # attacks it can absorb when attacking
    hardness: float  # 0 = soft, 1 = fully armoured
    combat: bool = True  # trucks, for instance, are not


ELEMENTS: dict[str, ElementType] = catalog.build("ground", ElementType, section="elements")
"""Every element type, from ``[elements]`` in ``data/catalog/ground.toml``."""


@dataclass(frozen=True, slots=True)
class DivisionTemplate:
    """Composition and logistics of a type of brigade/division.

    Attributes:
        id: Template id, e.g. ``"tank_brigade"``.
        name: Display name.
        elements: Nominal element counts.
        speed_kmh: Road march speed.
        supply_capacity: Supply points carried (about three days).
        supply_use_per_hour: Consumption at rest.
    """
    id: str
    name: str
    elements: dict[str, int]
    speed_kmh: float  # road march speed
    supply_capacity: float  # supply points carried (~3 days)
    supply_use_per_hour: float

    @property
    def nominal_size(self) -> float:
        """Total nominal number of elements."""
        return float(sum(self.elements.values()))


DEFAULT_TEMPLATES: dict[str, DivisionTemplate] = catalog.build(
    "ground", DivisionTemplate, section="templates", convert={"elements": catalog.counts}
)
"""The stock brigade templates, from ``[templates]`` in ``data/catalog/ground.toml``.
Scenarios may add their own."""


# ---------------------------------------------------------------- authority


class Authority(StrEnum):
    """Who simulates an entity right now.

    CAMPAIGN -> MATERIALIZING -> DELEGATED -> DEMATERIALIZING -> CAMPAIGN.
    In the two transitional states nobody simulates it: the entity is frozen
    until the game adapter acknowledges the handoff (or the handoff times out).
    """

    CAMPAIGN = "campaign"
    MATERIALIZING = "materializing"
    DELEGATED = "delegated"
    DEMATERIALIZING = "dematerializing"


class OrderKind(StrEnum):
    """What a division has been told to do."""
    HOLD = "hold"
    MOVE = "move"  # move through friendly territory
    ATTACK = "attack"  # move and attack the final (hostile) province


@dataclass(slots=True)
class Order:
    """A division's current order.

    Attributes:
        kind: Hold, move or attack.
        target: Target province id (None for hold).
    """
    kind: OrderKind = OrderKind.HOLD
    target: str | None = None


@dataclass(slots=True)
class Movement:
    """Progress of an entity along its route on the province graph.

    Attributes:
        path: Remaining provinces; ``path[0]`` is the next one.
        progress: Metres travelled along the link ``location -> path[0]``.
        back: Withdrawing back along the link to ``location``.
    """
    path: list[str]  # remaining provinces; path[0] is the next one
    progress: float = 0.0  # metres travelled along the link location -> path[0]
    # Withdrawing: drive back along the link to ``location`` (e.g. a failed attack).
    back: bool = False


# ---------------------------------------------------------------- entities


@dataclass(eq=False, kw_only=True)
class MobileEntity:
    """Base class of everything that moves on the map and can be handed to a game.

    Attributes:
        id: Unique id.
        faction: Owning faction id.
        location: Province it is in, or departed from while on a link.
        authority: Who simulates it now (campaign or game, see :class:`Authority`).
        movement: Route progress, or None when stationary.
        game_position: Last position reported by the game while delegated.
        handoff_started: Campaign time a handoff began (for timeouts).
        kind: Entity kind used by adapters (``"division"``, ``"convoy"``...).
    """
    id: str
    faction: str
    location: str  # province it is in, or departed from while on a link
    authority: Authority = Authority.CAMPAIGN
    movement: Movement | None = None
    # While delegated the game owns the position; we keep the last one reported.
    game_position: Vec2 | None = None
    handoff_started: float | None = None  # campaign time a handoff began

    kind: str = "entity"

    @property
    def simulated_by_campaign(self) -> bool:
        """True when the campaign (not a game) simulates it."""
        return self.authority is Authority.CAMPAIGN

    @property
    def materializable(self) -> bool:
        """Whether the game could take this entity over right now."""
        return True

    @property
    def bubble_reach(self) -> float:
        """Extra distance at which a player brings this entity into the game
        (e.g. a SAM site's engagement range)."""
        return 0.0

    def position(self, theater: Theater) -> Vec2:
        """Current position on the theater plane.

        While the game holds the entity this is the last reported position;
        otherwise the point on the road at the current movement progress (or the
        province centre).

        Args:
            theater: The map.
        """
        if self.game_position is not None and self.authority is not Authority.CAMPAIGN:
            return self.game_position
        if not self.movement or not self.movement.path:
            return theater.provinces[self.location].center
        return theater.point_on_link(self.location, self.movement.path[0], self.movement.progress)

    def heading(self, theater: Theater) -> float:
        """Direction of travel along the road (radians, 0 = north); 0 when stationary.

        Args:
            theater: The map.
        """
        if self.movement and self.movement.path:
            h = theater.heading_on_link(self.location, self.movement.path[0], self.movement.progress)
            return (h + 3.141592653589793) % (2 * 3.141592653589793) if self.movement.back else h
        return 0.0


@dataclass(eq=False, kw_only=True)
class Division(MobileEntity):
    """A manoeuvre unit (brigade-sized, HOI4-style).

    Attributes:
        name: Display name.
        template: Composition template.
        elements: Current element counts (real counts, float to allow attrition).
        org: Organisation / cohesion, 0..1.
        supply: Supply points carried.
        order: Current order.
        battle: Id of the battle it fights in, if any.
        deployed_toward: Neighbouring province its defensive line faces.
        deploy_m: Distance along that road of the defensive line.
    """
    name: str
    template: DivisionTemplate
    elements: dict[str, float] = field(default_factory=dict)
    org: float = 1.0  # organisation / cohesion, 0..1
    supply: float = -1.0  # supply points carried; -1 = full on creation
    order: Order = field(default_factory=Order)
    battle: str | None = None
    # Defensive deployment: standing ``deploy_m`` metres from the province centre
    # towards the neighbouring province ``deployed_toward`` (facing an attack).
    deployed_toward: str | None = None
    deploy_m: float = 0.0
    kind: str = "division"

    def __post_init__(self) -> None:
        """Fill elements and supply from the template on creation."""
        if not self.elements:
            self.elements = {k: float(v) for k, v in self.template.elements.items()}
        if self.supply < 0:
            self.supply = self.template.supply_capacity

    def position(self, theater: Theater) -> Vec2:
        """Position, including the forward defensive deployment when stationary.

        Args:
            theater: The map.
        """
        if self.deployed_toward and not self.movement and self.authority is Authority.CAMPAIGN:
            if self.deployed_toward in theater.provinces and self.deployed_toward in theater.neighbors(self.location):
                return theater.point_on_link(self.location, self.deployed_toward, self.deploy_m)
        return super().position(theater)

    @property
    def strength(self) -> float:
        """Fraction of nominal combat elements still alive."""
        nominal = sum(v for k, v in self.template.elements.items() if ELEMENTS[k].combat)
        alive = sum(v for k, v in self.elements.items() if ELEMENTS[k].combat)
        return alive / nominal if nominal else 0.0

    @property
    def supply_ratio(self) -> float:
        """Supply carried as a fraction of capacity."""
        return self.supply / self.template.supply_capacity

    @property
    def speed_ms(self) -> float:
        """March speed in m/s (halved when out of supply)."""
        factor = 0.5 if self.supply <= 0 else 1.0
        return self.template.speed_kmh / 3.6 * factor

    def firepower(self, enemy_hardness: float) -> float:
        """Total firepower against a target of the given hardness.

        Args:
            enemy_hardness: Target hardness 0..1.
        """
        soft = sum(ELEMENTS[k].soft_attack * n for k, n in self.elements.items())
        hard = sum(ELEMENTS[k].hard_attack * n for k, n in self.elements.items())
        return soft * (1.0 - enemy_hardness) + hard * enemy_hardness

    def defense(self) -> float:
        """Attacks the division can absorb when defending."""
        return sum(ELEMENTS[k].defense * n for k, n in self.elements.items())

    def breakthrough(self) -> float:
        """Attacks the division can absorb when attacking."""
        return sum(ELEMENTS[k].breakthrough * n for k, n in self.elements.items())

    def hardness(self) -> float:
        """Average hardness of its combat elements (0..1)."""
        combat = {k: n for k, n in self.elements.items() if ELEMENTS[k].combat}
        total = sum(combat.values())
        if not total:
            return 0.0
        return sum(ELEMENTS[k].hardness * n for k, n in combat.items()) / total

    def combat_power(self) -> float:
        """Rough single number used by the AI to compare forces."""
        return (self.firepower(0.3) + self.defense()) * (0.4 + 0.6 * self.org)

    def take_losses(self, fraction: float) -> None:
        """Lose the same fraction of every element.

        Args:
            fraction: Share lost, 0..1.
        """
        fraction = min(max(fraction, 0.0), 1.0)
        for k in self.elements:
            self.elements[k] *= 1.0 - fraction


@dataclass(eq=False, kw_only=True)
class Convoy(MobileEntity):
    """A column of trucks carrying supply between a depot and the front."""

    home: str  # depot province it returns to
    destination: str
    cargo: float
    trucks: float
    returning: bool = False
    kind: str = "convoy"
    speed_kmh: float = 40.0

    @property
    def speed_ms(self) -> float:
        """Driving speed in m/s."""
        return self.speed_kmh / 3.6
