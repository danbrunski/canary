"""The one converter from analysis values to JSON-safe ones (R24.10).

Nine private copies (``_jsonable`` in four tools and ``validation.scenarios``,
``_json_default`` in ``digest`` and ``lambda_delivery_evidence``,
``artifact_lineage.json_default`` and ``fuel_comp_report.jsonable``) each
handled a different subset of numpy, dataclass and pydantic values, so one
number serialized differently across the reports under ``data/analyses/``.
There is one rule here:

- A float that is not finite (NaN, +-inf) is ``None``. Python's ``json`` would
  write ``NaN``/``Infinity``, which is not JSON; a value that is not a
  measurement is the same as no value (``coerce.finite_float`` says it too).
- ``ndigits`` is the caller's rounding for floats, never a default.
- Mapping keys are ``str(key)``; sets become sorted lists so a report does not
  change between runs.
- Anything this does not know is returned unchanged, so ``json.dumps`` refuses
  it by name, unless ``stringify`` asks for ``str(value)`` instead.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


def jsonable(value: Any, *, ndigits: int | None = None, stringify: bool = False) -> Any:
    """``value`` with numpy, pandas, dataclass and pydantic values made JSON-safe.

    ``ndigits`` rounds every float (numpy included); ``None`` leaves them
    exact. ``stringify`` turns a value of a type this does not know into
    ``str(value)`` where otherwise it is returned unchanged for ``json.dumps``
    to reject.
    """
    if isinstance(value, Enum):
        return jsonable(value.value, ndigits=ndigits, stringify=stringify)
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, np.datetime64 | np.timedelta64):
        # Before the integer branch: ``np.timedelta64`` is an ``np.signedinteger``.
        if np.isnat(value):
            return None
        if isinstance(value, np.datetime64):
            return pd.Timestamp(value).isoformat()
        return jsonable(pd.Timedelta(value).total_seconds(), ndigits=ndigits)
    if isinstance(value, bool | np.bool_):
        return bool(value)
    if isinstance(value, int | np.integer):
        return int(value)
    if isinstance(value, float | np.floating):
        number = float(value)
        if not math.isfinite(number):
            return None
        return number if ndigits is None else round(number, ndigits)

    def again(item: Any) -> Any:
        return jsonable(item, ndigits=ndigits, stringify=stringify)

    if isinstance(value, Mapping):
        return {str(k): again(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [again(v) for v in value]
    if isinstance(value, set | frozenset):
        items = [again(v) for v in value]
        try:
            return sorted(items)
        except TypeError:  # mixed types: still deterministic
            return sorted(items, key=str)
    if isinstance(value, np.ndarray):
        return again(value.tolist())
    if isinstance(value, pd.DataFrame):
        return again(value.to_dict("records"))
    if isinstance(value, pd.Series):
        return again(value.tolist())
    if value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, datetime | date):  # pd.Timestamp is a datetime
        return value.isoformat()
    if isinstance(value, timedelta):
        return jsonable(value.total_seconds(), ndigits=ndigits)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes | bytearray):
        return bytes(value).hex()
    if hasattr(value, "model_dump"):
        return again(value.model_dump(mode="json"))
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: again(getattr(value, f.name)) for f in dataclasses.fields(value)}
    return str(value) if stringify else value


def json_default(value: Any) -> Any:
    """``json.dumps(default=...)`` form of :func:`jsonable`.

    A type this does not know becomes ``str(value)``; ``json`` calls the
    default only for what it cannot encode itself, so it is a fallback, not a
    replacement for converting the whole report with :func:`jsonable`.
    """
    return jsonable(value, stringify=True)


__all__ = ["json_default", "jsonable"]
