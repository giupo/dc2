"""Simulation systems, run in this order every campaign step."""

from . import (
    ai,
    air,
    air_defense,
    artillery,
    ato,
    combat,
    control,
    intel,
    movement,
    scramble,
    structures,
    supply
)

#: The systems run at every engine substep, in this order.
PIPELINE = (
    intel,
    scramble,
    ai,
    ato,
    structures,
    supply,
    movement,
    combat,
    artillery,
    air_defense,
    air
)

__all__ = [
    "PIPELINE",
    "ai",
    "air",
    "air_defense",
    "artillery",
    "ato",
    "combat",
    "control",
    "intel",
    "movement",
    "scramble",
    "structures",
    "supply"
]
