"""Small typed-coercion helpers for manifest / record ``from_dict`` bodies
(Task 4.4, refactoring.md).

``optional_str`` was defined identically three times (``isolation_forest.py``,
``sequence_registry.py``, ``control/jobs.py``) — a one-line "pass ``None``
through, ``str()`` everything else" coercion used by every ``from_dict``
that has an optional ISO-timestamp-or-similar field. ``models/retraining_policy.py``
has a fourth, genuinely different version (``""`` also coerces to ``None``)
that stays where it is rather than being folded in here.
"""

from __future__ import annotations

from typing import Any


def optional_str(value: Any) -> str | None:
    """``None`` stays ``None``; anything else is ``str()``'d."""
    if value is None:
        return None
    return str(value)


__all__ = ["optional_str"]
