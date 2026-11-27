"""Fixed installations that can be struck: depots, airbase facilities, command posts.

A structure is a handful of buildings (``elements`` in
``data/catalog/structures.toml``); its **health** is the share still standing.
What it is worth to its owner scales with it (see
:mod:`dynacamp.systems.structures`):

* a depot produces supply at ``health`` times its rate, and every building
  destroyed burns part of the stock;
* airbase facilities (hangars, fuel, shelters) turn the squadrons' aircraft
  around ``1 / health`` times slower; with none left nobody takes off;
* a command post lets its faction plan ``health`` times the packages a cycle.

Destroyed buildings are rebuilt after ``repair_h`` hours. The owner is whoever
holds the province (depots, command posts) or the airbase. Structures do not
move and everyone knows where the enemy's are, so they are not subject to the
fog of war.

In the game each building is a DCS static object (``[<kind>.dcs]`` of the
catalogue), named by :func:`dynacamp.adapters.dcs.protocol.static_name`; the
game reports the ones destroyed there.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from . import catalog
from .geo import Vec2

if TYPE_CHECKING:
    from .state import CampaignState


class StructureKind(StrEnum):
    """Kind of structure (an entry of ``structures.toml``)."""
    #: A supply depot's stores and fuel.
    DEPOT = "depot"
    #: An airbase's hangars, fuel and shelters.
    AIRBASE = "airbase"
    #: A command post.
    COMMAND = "command"


@dataclass(frozen=True)
class StructureType:
    """A kind of structure, from the catalogue.

    Attributes:
        id: The kind.
        name: Display name.
        elements: Buildings it is made of.
        repair_h: Hours to rebuild a destroyed building.
    """
    id: str
    name: str
    elements: int
    repair_h: float


#: The kinds of structure of the catalogue, by kind.
TYPES: dict[StructureKind, StructureType] = {
    StructureKind(k): v for k, v in catalog.build("structures", StructureType).items()
}

#: Spacing of the buildings of a structure, metres.
ELEMENT_SPACING_M = 60.0
#: Buildings per row of a structure's layout.
ELEMENTS_PER_ROW = 3
#: Prefix of the ids of the structures made for a scenario's depots...
DEPOT_PREFIX = "dep_"
#: ...and airbases.
AIRBASE_PREFIX = "ab_"
#: A depot's structure stands this far north-east of the province centre (out of the town).
DEPOT_OFFSET = Vec2(1_500.0, 1_500.0)
#: Airbase facilities stand this far from the airfield reference point (off the runway).
AIRBASE_OFFSET = Vec2(800.0, -800.0)


@dataclass
class Structure:
    """A fixed installation.

    Attributes:
        id: Structure id.
        name: Display name.
        kind: What it is.
        province: Province it stands in.
        position: Centre of its buildings.
        airbase: The airbase it serves (airbase facilities), else "".
        destroyed: Building index -> campaign time it is rebuilt.
    """
    id: str
    name: str
    kind: StructureKind
    province: str
    position: Vec2
    airbase: str = ""
    destroyed: dict[int, float] = field(default_factory=dict)

    @property
    def type(self) -> StructureType:
        """Its catalogue entry."""
        return TYPES[self.kind]

    @property
    def health(self) -> float:
        """Share of its buildings standing, 0-1."""
        return 1.0 - len(self.destroyed) / self.type.elements

    def standing(self) -> list[int]:
        """Indices of the buildings standing.

        Returns:
            The indices, in order.
        """
        return [i for i in range(self.type.elements) if i not in self.destroyed]

    def owner(self, state: CampaignState) -> str:
        """Faction that owns it: the airbase's owner, else the province's controller.

        Args:
            state: The campaign.

        Returns:
            The faction id.
        """
        if self.airbase and self.airbase in state.airbases:
            return state.airbases[self.airbase].owner(state.theater) or state.theater.provinces[self.province].controller
        return state.theater.provinces[self.province].controller

    def element_position(self, i: int) -> Vec2:
        """Where building ``i`` stands: a small grid around the centre.

        Args:
            i: Building index.

        Returns:
            Its position.
        """
        rows = (self.type.elements + ELEMENTS_PER_ROW - 1) // ELEMENTS_PER_ROW
        col, row = i % ELEMENTS_PER_ROW, i // ELEMENTS_PER_ROW
        return self.position + Vec2((col - (ELEMENTS_PER_ROW - 1) / 2) * ELEMENT_SPACING_M,
                                    (row - (rows - 1) / 2) * ELEMENT_SPACING_M)
