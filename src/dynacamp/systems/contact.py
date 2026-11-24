"""Where forces actually meet.

A battle is fought where the two sides are within reach of each other's sensors
and weapons, not between two province centres tens of kilometres apart:

* the defender deploys on a forward defence line, ``defense_progress`` metres
  out of its province centre along the road the attack comes from;
* the attacker drives along that road and stops at its assault position: the
  first point from which the defence line is within its contact range (straight
  line, as sensors and guns see it): a few km for direct-fire units, further
  back for artillery, which fires from depth.

Everything stays on the road network (``Theater.point_on_link``), so units never
cut across the sea or through a forest, and spawn on roads in the game.
"""

from __future__ import annotations

from ..forces import Division
from ..state import CampaignState

DEFENSE_DEPTH_M = 6_000.0  # forward defence line at most this far out along the road...
DEFENSE_DEPTH_FRACTION = 0.3  # ...and never beyond this fraction of the link
DIRECT_CONTACT_M = 3_000.0  # tanks, IFVs, infantry: direct fire / visual range
INDIRECT_CONTACT_M = 12_000.0  # tube and rocket artillery firing from depth
INDIRECT_SHARE = 0.4  # a division counts as artillery above this share of guns
SEARCH_STEP_M = 100.0

_assault_cache: dict[tuple, float] = {}


def deploy_progress(state: CampaignState, province: str, toward: str) -> float:
    """The defence line as progress along the road province -> toward."""
    length = state.theater.link(province, toward).length
    return min(DEFENSE_DEPTH_M, length * DEFENSE_DEPTH_FRACTION)


def contact_range(div: Division) -> float:
    """Engagement distance of a division: indirect for artillery-heavy ones."""
    guns = div.elements.get("artillery", 0.0) + div.elements.get("mlrs", 0.0)
    combat = sum(n for k, n in div.elements.items() if k != "truck")
    return INDIRECT_CONTACT_M if combat and guns / combat >= INDIRECT_SHARE else DIRECT_CONTACT_M


def assault_progress(state: CampaignState, div: Division, target: str) -> float:
    """Progress along the road (location -> target) where ``div`` stops to attack."""
    t = state.theater
    rng = contact_range(div)
    key = (t.name, div.location, target, rng)
    if key not in _assault_cache:
        length = t.link(div.location, target).length
        line_s = length - deploy_progress(state, target, div.location)  # defence line, from our end
        line = t.point_on_link(div.location, target, line_s)
        s = line_s
        while s > 0 and t.point_on_link(div.location, target, s).distance_to(line) < rng:
            s -= SEARCH_STEP_M
        _assault_cache[key] = max(0.0, s)
    return _assault_cache[key]


def at_assault_position(state: CampaignState, div: Division) -> bool:
    """True if the division reached its assault position on its attack road."""
    mv = div.movement
    if not mv or not mv.path or mv.back:
        return False
    return mv.progress >= assault_progress(state, div, mv.path[0]) - 1.0
