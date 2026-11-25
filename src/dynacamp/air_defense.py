"""Ground-based air defence: SAM sites, early-warning radars and the IADS.

Each air defence unit is a mobile entity like a brigade: it lives in a
province, moves along roads, and the player bubble hands it to the game. Its
``elements`` are the actual site components (radars, launchers, command post),
one game unit each.

Life cycle of a site: ``moving`` (radar off, packed) -> ``setting_up`` (setup
time of the system) -> ``active`` (radar on, engages). SEAD can force it into
``suppressed`` (radar off for a while); losing all radars or all launchers
destroys it.

Several sites in one province do not stand on top of each other: each one at
rest gets its own **emplacement** (:func:`place_sites`), the province centre
for the first and then points ``SITE_SPACING_M`` apart along the roads out of
the province (on land, also in coastal provinces), friendly roads first. The
emplacement is kept until the site moves again, so it does not jump around
when other sites come and go or a neighbouring province changes hands.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from .forces import Authority, MobileEntity
from .geo import Vec2
from .world import Theater

SITE_SPACING_M = 3_000.0  # between sites sharing a province


class SamRole(StrEnum):
    """Class of air defence system."""
    LONG = "long"
    MEDIUM = "medium"
    SHORT = "short"
    EWR = "ewr"


@dataclass(frozen=True, slots=True)
class SamSystem:
    """An air defence system type (game-agnostic).

    Attributes:
        id: System id, e.g. ``"s300ps"``.
        name: NATO reporting / common name.
        role: Long, medium, short range or early warning.
        range_m: Engagement range.
        detection_m: Own radar detection range.
        ceiling_m: Maximum engagement altitude.
        missiles: Ready missiles on the launchers.
        pk: Nominal single-shot kill probability.
        speed_kmh: Road speed.
        setup_s: Time to go into action after moving.
        elements: Site composition (radar, search_radar, launcher, cp, ...).
        radar: Emits (vulnerable to anti-radiation missiles).
        reload_per_hour: Missiles reloaded per hour when supplied.
    """
    id: str
    name: str  # NATO reporting name or common name
    role: SamRole
    range_m: float  # engagement range
    detection_m: float  # own radar detection range
    ceiling_m: float  # max engagement altitude
    missiles: int  # ready missiles on the launchers
    pk: float  # single-shot kill probability (nominal)
    speed_kmh: float
    setup_s: float  # time to go into action after moving
    elements: dict[str, int]  # site composition: radar / launcher / cp / ...
    radar: bool = True  # emits (vulnerable to anti-radiation missiles)
    reload_per_hour: float = 4.0  # missiles reloaded per hour when supplied

    @property
    def engages(self) -> bool:
        """False for early-warning radars, which only detect."""
        return self.role is not SamRole.EWR


def _s(*a, **k) -> SamSystem:
    """Shorthand constructor for the catalogue."""
    return SamSystem(*a, **k)


SYSTEMS: dict[str, SamSystem] = {
    s.id: s
    for s in (
        # long range
        _s("patriot", "Patriot", SamRole.LONG, 70_000, 160_000, 24_000, 16, 0.45, 40, 45 * 60,
           {"radar": 1, "launcher": 4, "cp": 1, "power": 1}, reload_per_hour=4),
        _s("s300ps", "SA-10 Grumble", SamRole.LONG, 75_000, 160_000, 27_000, 16, 0.4, 40, 30 * 60,
           {"search_radar": 1, "radar": 1, "cp": 1, "launcher": 4}, reload_per_hour=4),
        # medium range
        _s("hawk", "Hawk", SamRole.MEDIUM, 40_000, 90_000, 14_000, 12, 0.35, 40, 30 * 60,
           {"search_radar": 1, "radar": 1, "cp": 1, "launcher": 4}),
        _s("nasams", "NASAMS", SamRole.MEDIUM, 25_000, 75_000, 15_000, 12, 0.4, 60, 15 * 60,
           {"radar": 1, "cp": 1, "launcher": 3}),
        _s("buk", "SA-11 Gadfly", SamRole.MEDIUM, 35_000, 80_000, 22_000, 16, 0.4, 50, 5 * 60,
           {"search_radar": 1, "cp": 1, "launcher": 4}),
        _s("kub", "SA-6 Gainful", SamRole.MEDIUM, 25_000, 70_000, 12_000, 12, 0.3, 50, 10 * 60,
           {"radar": 1, "launcher": 4}),
        # short range
        _s("tor", "SA-15 Gauntlet", SamRole.SHORT, 12_000, 25_000, 6_000, 16, 0.4, 60, 3 * 60,
           {"launcher": 2}, reload_per_hour=8),
        _s("osa", "SA-8 Gecko", SamRole.SHORT, 10_000, 30_000, 5_000, 12, 0.3, 60, 5 * 60,
           {"launcher": 3}, reload_per_hour=8),
        _s("avenger", "Avenger", SamRole.SHORT, 8_000, 12_000, 3_800, 16, 0.3, 70, 60,
           {"launcher": 4}, radar=False, reload_per_hour=8),
        # early warning
        _s("ewr_1l13", "1L13 Box Spring", SamRole.EWR, 0, 250_000, 0, 0, 0.0, 30, 30 * 60, {"radar": 1}),
        _s("ewr_55g6", "55G6 Tall Rack", SamRole.EWR, 0, 300_000, 0, 0, 0.0, 30, 30 * 60, {"radar": 1}),
        _s("ewr_fps117", "AN/FPS-117", SamRole.EWR, 0, 300_000, 0, 0, 0.0, 30, 30 * 60,
           {"radar": 1, "dome": 1, "cp": 1}),
    )
}

RADAR_ELEMENTS = ("radar", "search_radar")


class SamStatus(StrEnum):
    """Operational state of a site."""
    ACTIVE = "active"
    SETTING_UP = "setting_up"
    MOVING = "moving"
    SUPPRESSED = "suppressed"


@dataclass(eq=False, kw_only=True)
class AirDefenseUnit(MobileEntity):
    """A SAM site or early-warning radar on the map.

    Attributes:
        name: Display name.
        system: System type.
        elements: Components still alive.
        missiles: Ready missiles.
        ready_at: End of setup or suppression.
        status: Active, setting up, moving or suppressed.
        engage_ready: Next time it can fire a salvo.
        scoot: It was targeted and should relocate.
        site: Emplacement at rest, ``(province, neighbour, metres along the road
            to it)``; neighbour ``""`` is the province centre. None (or another
            province) until :func:`place_sites` gives it one.
    """
    name: str
    system: SamSystem
    elements: dict[str, float] = field(default_factory=dict)
    missiles: float = -1.0  # -1 = full on creation
    ready_at: float = 0.0  # setting up / suppressed until this campaign time
    status: SamStatus = SamStatus.ACTIVE
    engage_ready: float = 0.0  # next time it can fire a salvo
    scoot: bool = False  # was targeted: relocate at the next chance ("shoot and scoot")
    site: tuple[str, str, float] | None = None  # emplacement at rest (see place_sites)
    kind: str = "sam"

    def __post_init__(self) -> None:
        """Fill components and missiles from the system on creation."""
        if not self.elements:
            self.elements = {k: float(v) for k, v in self.system.elements.items()}
        if self.missiles < 0:
            self.missiles = float(self.system.missiles)

    @property
    def speed_ms(self) -> float:
        """Road speed in m/s."""
        return self.system.speed_kmh / 3.6

    @property
    def operational(self) -> bool:
        """Has what it needs to fight: a radar (if the system uses one) and a launcher."""
        has_radar = not any(k in self.system.elements for k in RADAR_ELEMENTS) or any(
            self.elements.get(k, 0) >= 1 for k in RADAR_ELEMENTS
        )
        if self.system.role is SamRole.EWR:
            return has_radar
        launchers = self.elements.get("launcher", 0) >= 1
        return has_radar and launchers

    @property
    def emitting(self) -> bool:
        """Radar on (active, radar-guided system)."""
        return self.system.radar and self.status is SamStatus.ACTIVE

    @property
    def strength(self) -> float:
        """Share of components still alive."""
        nominal = sum(self.system.elements.values())
        return sum(self.elements.values()) / nominal if nominal else 0.0

    def position(self, theater: Theater) -> Vec2:
        """Current position: on the road while moving, else its emplacement.

        Args:
            theater: The map.
        """
        if self.movement or (self.game_position is not None and self.authority is not Authority.CAMPAIGN):
            return super().position(theater)
        if self.site is None or self.site[0] != self.location or not self.site[1]:
            return theater.provinces[self.location].center
        return theater.point_on_link(self.location, self.site[1], self.site[2])

    @property
    def bubble_reach(self) -> float:
        """A player inside the engagement range brings the site into the game."""
        return self.system.range_m


def place_sites(units: Iterable[AirDefenseUnit], theater: Theater) -> None:
    """Give every site at rest without an emplacement in its province a free one.

    The province centre if nobody holds it, else the nearest free point
    ``SITE_SPACING_M`` apart along the roads out of the province: roads to
    provinces of the site's faction first, then the others, each no further
    than half the road. Sites already placed keep their emplacement.

    Args:
        units: All the air defence units (the placed ones block their spots).
        theater: The map.
    """
    units = list(units)
    taken: dict[str, set[tuple[str, float]]] = {}
    for u in units:
        if not u.movement and u.site is not None and u.site[0] == u.location:
            taken.setdefault(u.location, set()).add((u.site[1], u.site[2]))
    for u in units:
        if u.movement:
            u.site = None  # packed up: a new emplacement where it stops
            continue
        if u.site is not None and u.site[0] == u.location:
            continue
        used = taken.setdefault(u.location, set())
        roads = sorted(theater.neighbors(u.location), key=lambda n: (theater.provinces[n].controller != u.faction, n))
        spots = [("", 0.0)]
        for k in range(1, 6):
            spots += [(n, k * SITE_SPACING_M) for n in roads if k * SITE_SPACING_M <= theater.link(u.location, n).length / 2]
        spot = next((s for s in spots if s not in used), ("", 0.0))
        used.add(spot)
        u.site = (u.location, *spot)
