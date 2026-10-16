"""Primitives every configuration schema shares (R21).

``_Strict`` (unknown keys raise, so a typo is an error and never silent
drift) and the constants and the one validator that more than one package
imports. Imports nothing from ``src.config``: every other module in this
package depends on this one, and this one on none of them.
"""

from __future__ import annotations

import math
from typing import Final

from pydantic import BaseModel, ConfigDict

# Broadcast CAN change-only storage (P02.10): the stale timeout's default and
# floor, and the re-write lead derived from it. Defined in this base module
# (R21), not beside the ``collection.broadcast_stale_timeout_seconds`` field
# they validate (R09.2): ``src.storage`` and ``src.preprocessing`` import them
# too, and the field itself is in ``src.config.collection``.
# ``src.storage.broadcast_storage`` states the rule that uses them.

#: The operator's default (2026-09-26).
DEFAULT_BROADCAST_STALE_TIMEOUT_SECONDS: Final = 10.0
#: A shorter timeout is refused: the re-write happens one second before the
#: timeout, so under two seconds an unchanged id would be re-written more
#: often than once a second and the filter would save nothing it can measure.
MIN_BROADCAST_STALE_TIMEOUT_SECONDS: Final = 2.0
#: How long before the timeout an unchanged id is re-written: derived from the
#: timeout, never configured. It covers ids whose frame period is under a
#: second (the re-write fires on the first frame at least ``timeout - 1`` s
#: after the last stored one, so the stored value is at most ``timeout - 1 +
#: period`` old). A slower id could lapse that way (a 7 s id at 9 + 7 s), so
#: :class:`~src.storage.broadcast_storage.CanChangeFilter` also keeps a frame
#: when the next one, one inter-arrival later, would find the stored value past
#: the timeout.
REWRITE_LEAD_SECONDS: Final = 1.0


def validate_broadcast_stale_timeout(seconds: float) -> float:
    """``seconds`` as a timeout, or ``ValueError`` naming why it cannot be one."""
    value = float(seconds)
    if not math.isfinite(value) or value < MIN_BROADCAST_STALE_TIMEOUT_SECONDS:
        raise ValueError(
            f"broadcast stale timeout {seconds!r} s is under the "
            f"{MIN_BROADCAST_STALE_TIMEOUT_SECONDS:g} s floor (P02.10, operator 2026-09-26): "
            f"an unchanged CAN id is re-written {REWRITE_LEAD_SECONDS:g} s before the "
            "timeout, so a shorter one would re-write it more than once a second"
        )
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


#: Default ``CollectionConfig.obd_priority_channels`` — the OBD channels that
#: keep their configured poll rate when the request plan oversubscribes the
#: bus. Lives here rather than in ``collection.obd_scheduler`` (which imports
#: it) only to keep the config default and the scheduler constant from
#: drifting apart. See the scheduler's ``DEFAULT_PRIORITY_CHANNELS`` re-export
#: for why this channel specifically.
DEFAULT_PRIORITY_CHANNELS: tuple[str, ...] = ("RPM", "THROTTLE_POS")
