"""Quality balances quantity in the air: an arena of CAP against CAP.

Also a measuring tool (more seeds, any pairing):

    uv run python -m tests.test_air_quality f15c su27 2 3 1.2 1.0 [seeds]

blue type, red type, blue flights of 2, red flights of 2, blue quality, red quality.
"""

import math
import sys

from dynacamp.air import AIRCRAFT, Airbase, Squadron, Task
from dynacamp.engine import Engine
from dynacamp.events import EventKind
from dynacamp.geo import Vec2

from .conftest import line_scenario
from .test_air_combat import flight


def duel(blue: str, red: str, n_blue: int, n_red: int, q_blue: float, q_red: float, seeds: int) -> tuple[float, float]:
    """Two hours of CAP stations 20 km apart; returns the average share of each side shot down."""
    lost = [0.0, 0.0]
    for seed in range(seeds):
        state = line_scenario()
        state.timers["ai"] = state.timers["ato"] = state.timers["ada"] = state.timers["scramble"] = math.inf
        state.air_defense.clear()
        state.factions["blue"].quality, state.factions["red"].quality = q_blue, q_red
        state.airbases["ab"] = Airbase("ab", "Blue Field", Vec2(0, -10_000), province="a")
        state.airbases["rb"] = Airbase("rb", "Red Field", Vec2(80_000, 10_000), province="e")
        state.squadrons["bq"] = Squadron("bq", "B", "blue", "ab", AIRCRAFT[blue], 99, "B")
        state.squadrons["rq"] = Squadron("rq", "R", "red", "rb", AIRCRAFT[red], 99, "R")
        for i in range(n_blue):
            flight(state, f"b{i}", "bq", Task.CAP, Vec2(0, i * 3000.0), track=math.pi / 2, target=Vec2(20_000, i * 3000.0))
        for i in range(n_red):
            flight(state, f"r{i}", "rq", Task.CAP, Vec2(60_000, i * 3000.0), track=-math.pi / 2, target=Vec2(40_000, i * 3000.0))
        state.rng.seed(seed)
        engine = Engine(state)
        for _ in range(2 * 360):
            engine.step(10)
        for e in state.events.of_kind(EventKind.AIRCRAFT_LOST):
            lost[e.data["faction"] == "red"] += e.data["count"]
    return lost[0] / (2 * n_blue * seeds), lost[1] / (2 * n_red * seeds)


def test_one_israeli_phantom_is_worth_two_syrian_mig21s():
    """1973: four F-4E (IDF) against eight MiG-21 (Syrian AF) lose about the same share."""
    blue, red = duel("f4e", "mig21bis", 2, 4, 1.5, 0.75, 40)
    assert 0.6 < blue / red < 1.6
    even_blue, even_red = duel("f4e", "mig21bis", 2, 4, 1.0, 1.0, 40)
    assert even_blue > 1.5 * even_red  # without the quality, numbers win


def test_one_western_fighter_is_worth_one_and_a_half_eastern():
    """Today: four F-15C (US) against six Su-27 (Russia) lose about the same share."""
    blue, red = duel("f15c", "su27", 2, 3, 1.2, 1.0, 40)
    assert 0.6 < blue / red < 1.6


if __name__ == "__main__":
    b, r, nb, nr, qb, qr = sys.argv[1:7]
    shares = duel(b, r, int(nb), int(nr), float(qb), float(qr), int(sys.argv[7]) if len(sys.argv) > 7 else 200)
    print(f"{b} x{2 * int(nb)} (q {qb}) vs {r} x{2 * int(nr)} (q {qr}): lost {shares[0]:.0%} vs {shares[1]:.0%}")
