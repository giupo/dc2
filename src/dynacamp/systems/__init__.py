"""Simulation systems, run in this order every campaign step."""

from . import ai, air, air_defense, artillery, ato, combat, control, movement, supply

PIPELINE = (ai, ato, supply, movement, combat, artillery, air_defense, air)

__all__ = ["PIPELINE", "ai", "air", "air_defense", "artillery", "ato", "combat", "control", "movement", "supply"]
