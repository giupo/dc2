"""Radio frequencies of the ATO, clear of the theatre's airfields.

Every package gets a UHF frequency (package common) and every flight a VHF one
(intra-flight). They must not step on what is already in use on the map:

* the **ATC frequencies of every airfield** of the theatre (HF, low VHF, VHF,
  UHF), from ``dynacamp/data/radio.json``, generated from DCS' own airfield
  data by ``dynacamp radio`` (needs pydcs);
* the **guard** (emergency) frequencies, 121.5 and 243.0 MHz;
* the frequencies of the **other live packages and flights**.

"Not step on" means no frequency within ``SEPARATION_MHZ`` (adjacent channel
included). The allocation is deterministic (it starts from a hash of the id),
done once when the package/flight is planned, and stored on it.
"""

from __future__ import annotations

import json
import zlib
from collections.abc import Iterable
from functools import cache
from pathlib import Path

DATA = Path(__file__).parent / "data" / "radio.json"

GUARD_MHZ = (121.5, 243.0)
SEPARATION_MHZ = 0.05  # 25 kHz channels: keep at least one channel in between
STEP_MHZ = 0.025

# Package common frequency: military UHF, above the airfields' UHF ATC block and
# within every radio's UHF range (the MiG-21's R-832 stops at 390 MHz).
PACKAGE_BAND = (260.0, 389.975)
# Intra-flight: VHF AM, above the ATC/approach block and within the VHF range
# of both western (116-151.975) and eastern (100-149.975, MiG-21 118-140) radios.
FLIGHT_BAND = (128.0, 139.975)


@cache
def _data() -> dict:
    """The radio file (empty if it has not been generated)."""
    if not DATA.exists():
        return {}
    return json.loads(DATA.read_text(encoding="utf-8"))["theaters"]


def airfield_frequencies(theater: str) -> dict[str, list[float]]:
    """ATC frequencies (MHz) of every airfield of a theatre, by airfield name."""
    return {a["name"]: a["mhz"] for a in _data().get(theater, [])}


@cache
def reserved(theater: str) -> tuple[float, ...]:
    """Frequencies (MHz) no flight may use in a theatre: airfields and guard."""
    out = set(GUARD_MHZ)
    for mhz in airfield_frequencies(theater).values():
        out.update(mhz)
    return tuple(sorted(out))


def clear(mhz: float, taken: Iterable[float], separation: float = SEPARATION_MHZ) -> bool:
    """True if ``mhz`` is at least ``separation`` away from every taken frequency."""
    return all(abs(mhz - t) >= separation - 1e-9 for t in taken)


def allocate(key: str, band: tuple[float, float], taken: Iterable[float]) -> float:
    """A clear frequency in ``band`` for ``key`` (deterministic).

    Starts from a channel chosen by hashing ``key`` and walks up the band (25 kHz
    steps, wrapping) to the first channel clear of everything in ``taken``.

    Args:
        key: Stable id (package or flight id).
        band: ``(low, high)`` MHz.
        taken: Frequencies in use (MHz).

    Raises:
        RuntimeError: If the band is full.
    """
    taken = sorted(set(taken))
    lo, hi = band
    n = int(round((hi - lo) / STEP_MHZ)) + 1
    start = zlib.crc32(key.encode()) % n
    for k in range(n):
        mhz = round(lo + ((start + k) % n) * STEP_MHZ, 3)
        if clear(mhz, taken):
            return mhz
    raise RuntimeError(f"no clear frequency left in {lo}-{hi} MHz")


def fmt(mhz: float, band: str) -> str:
    """``"UHF 305.250 AM"``."""
    return f"{band} {mhz:.3f} AM"
