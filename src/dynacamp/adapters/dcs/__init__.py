"""DCS World adapter."""

from pathlib import Path

from .adapter import DcsAdapter
from .protocol import DcsTranslator

#: The Lua bridge script embedded in the missions (``lua/dynacamp_bridge.lua``).
LUA_BRIDGE = Path(__file__).parent / "lua" / "dynacamp_bridge.lua"

__all__ = ["LUA_BRIDGE", "DcsAdapter", "DcsTranslator"]
