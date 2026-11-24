"""lat/lon -> DCS map coordinates, for drafting scenario files.

Usage:
    python tools/dcs_ll.py syria 34.73 36.71
    python tools/dcs_ll.py caucasus 42.27 42.70
Prints DCS x (north) / z (east) and the scenario ``pos = [east, north]``.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dynacamp.projection import ll_to_dcs  # noqa: E402

if __name__ == "__main__":
    theater, lat, lon = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
    x, z = ll_to_dcs(theater, lat, lon)
    print(f"DCS x={x:.0f} z={z:.0f}   core pos = [{z:.0f}, {x:.0f}]")
