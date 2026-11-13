"""Small typed-coercion helpers.

``optional_str`` (Task 4.4, refactoring.md) was defined identically three
times (``isolation_forest.py``, ``sequence_registry.py``, ``control/jobs.py``)
— a one-line "pass ``None`` through, ``str()`` everything else" coercion used
by every ``from_dict`` that has an optional ISO-timestamp-or-similar field.
``nonempty_str`` is the other rule: ``""`` also reads as absent. The
companion identity (``companion/discovery.py``) and the retraining feedback
window (``models/retraining_policy.py``) each had a private copy under the
same name as ``optional_str``, which hid the difference (operator,
2026-11-13: keep ``""`` as absent there).

``finite_float`` (R01.3, refactor review 2026-09-24): "a finite number, or
``None``" had about ten private copies with four behaviours. Some refused NaN
and +-inf, some let +-inf through, some let NaN through, and two refused
``bool``. A value that is not a finite number is not a measurement, and every
consumer (JSON payloads, operating points, event fields, parsed GPS/NMEA/CSV
fields) wants ``None`` for it, so there is one rule and no flags.
"""

from __future__ import annotations

import math
from typing import Any


def optional_str(value: Any) -> str | None:
    """``None`` stays ``None``; anything else is ``str()``'d."""
    if value is None:
        return None
    return str(value)


def nonempty_str(value: Any) -> str | None:
    """``None`` and ``""`` become ``None``; anything else is ``str()``'d."""
    return None if value in (None, "") else str(value)


def finite_float(value: object) -> float | None:
    """``value`` as a finite ``float``, else ``None``.

    ``None`` for ``None``, anything ``float()`` refuses (including ``""`` and
    ``pd.NA``), NaN, +-inf, and ``bool``. A ``bool`` is a state, not a
    number: ``float(True)`` is ``1.0``, which reads as a measurement. Numeric
    strings parse (``" 1.5 "`` -> ``1.5``), which is what the NMEA and CSV
    field parsers rely on.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)  # type: ignore[arg-type]  # except handles non-numeric
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


__all__ = ["finite_float", "nonempty_str", "optional_str"]
