"""Operating constants shared across detection, analysis and status (R01.7).

The engine-running floor was defined four times (``refit``, ``fingerprint``,
``cold_start_inventory``, ``dashboard.status``) and ``models.evaluation``
imported the 2,000-line ``analysis.refit`` only to read it. One home, a leaf
module, so any layer can import it.

Vehicle-specific operating points (WOT floors, rpm bands ending at a given
engine's redline) belong to the vehicle profile (R05.13), not here.
"""

from __future__ import annotations

#: RPM at or above which the engine is running. Below it with OBD fresh the
#: vehicle is ``ignition_only`` (key on, engine off): KOEO rows read RPM == 0
#: with off-map outputs (TIMING_ADVANCE ~63 deg vs ~17 deg at idle), so every
#: learned surface, statistic and state classifier gates on this. 400 sits well
#: below any running idle (~700-800) and well above sensor jitter.
ENGINE_RUNNING_MIN_RPM = 400.0

#: The operating mode the segmenter assigns at highway speed. Speed-band
#: decisions (``events.cruise``'s regime) read the segmenter's label rather
#: than restating its band: the cruise detector said >= 70 km/h while the
#: segmenter's highway band starts at 80.
HIGHWAY_MODE = "highway"

__all__ = ["ENGINE_RUNNING_MIN_RPM", "HIGHWAY_MODE"]
