"""Engineering values for ``decoded:`` channels — the one sanctioned reader.

``decoded:<SIGNAL>`` channels hold the **raw integer** from a binding's byte
range, by contract: the models' feature columns are trained and served on those
counts (:mod:`src.decode.frame_decoder`, :meth:`src.preprocessing.live.
LiveFeatureSource.decode_for_telemetry`, :func:`src.preprocessing.decode.
decode_can_rows`). That contract is right for features and wrong for anything
that treats the value as a physical quantity. ``decoded:ECM_RPM`` is 8 counts
per rpm, ``decoded:ECM_COOLANT_TEMP`` is degC + 40, ``decoded:WHEEL_SPEED_6F7_C``
is 16 counts per km/h.

From 2026-08-06 (998668a) to 2026-09-18 the operating-mode segmenter fell back
to those columns when Mode 01 was stale and compared the counts directly to
``rpm_max: 900`` / ``coolant_temp_min: 70`` / ``speed_min: 80``: every warm
idle it labelled from broadcast rpm came out ``unknown``, every cold engine
read as warm, and 60 km/h read as ``highway``. The live trip/idle integrator
had the same bug, and ``dashboard/status.py`` had grown a private
``_ECM_RPM_SCALE`` to paper over it in one place.

So any code that consumes a ``decoded:`` column as a physical quantity goes
through :func:`physical_series` / :func:`physical_value` here. They are no-ops
for every other namespace (``obd:``/``gps:`` are already engineering units),
scale ``decoded:`` through :func:`src.decode.bindings.scaling_factors` (the
same linear map :func:`~src.decode.bindings.apply_scaling` uses), and **refuse**
— :class:`UnscaledDecodedChannelError` — a ``decoded:`` column whose binding is
missing or has no pinned scale, rather than hand back counts that look like a
measurement.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pandas as pd

from src.decode.bindings import (
    Bindings,
    Signal,
    apply_scaling,
    load_bindings,
    scaling_factors,
)
from src.paths import BINDINGS_YAML

DECODED_PREFIX = "decoded:"

#: Repo-root-anchored, the same file the daemon (``daemon.inference.
#: build_live_feature_source``), the storage decoder, and the training feature
#: pipeline load — so a mode rule scales a column with the binding that
#: produced it.
DEFAULT_BINDINGS_PATH = BINDINGS_YAML

_CACHE: dict[Path, tuple[int, Bindings]] = {}


class UnscaledDecodedChannelError(ValueError):
    """A ``decoded:`` column was about to be read as a physical quantity with
    no binding scale to convert it — the raw counts would have been taken at
    face value (the 2026-08-06..09-18 segmenter bug)."""


def is_decoded_column(column: str) -> bool:
    """``True`` for a ``decoded:`` channel (raw binding counts by contract)."""
    return column.startswith(DECODED_PREFIX)


def repo_bindings(path: Path = DEFAULT_BINDINGS_PATH) -> Bindings | None:
    """The bindings catalog at ``path``, re-read when its mtime changes.

    ``None`` when the file is absent (fresh checkout, foreign profile) — the
    caller then gets :class:`UnscaledDecodedChannelError` for any ``decoded:``
    column, which is correct: there is nothing to scale it with.
    """
    try:
        mtime = path.stat().st_mtime_ns
    except FileNotFoundError:
        return None
    cached = _CACHE.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    bindings = load_bindings(path)
    _CACHE[path] = (mtime, bindings)
    return bindings


def _signal(column: str, bindings: Bindings | None) -> Signal:
    name = column.removeprefix(DECODED_PREFIX)
    catalog = bindings if bindings is not None else repo_bindings()
    if catalog is None:
        raise UnscaledDecodedChannelError(
            f"{column} holds raw binding counts and no bindings catalog is loaded "
            "to scale it; refusing to read counts as a physical quantity"
        )
    for signal in catalog.signals:
        if signal.name == name:
            return signal
    raise UnscaledDecodedChannelError(
        f"{column} holds raw binding counts but {name!r} is not in the bindings "
        "catalog; refusing to read counts as a physical quantity"
    )


def decoded_factors(column: str, bindings: Bindings | None = None) -> tuple[float, float]:
    """``(scale, offset)`` turning ``column``'s raw counts into engineering units.

    Raises :class:`UnscaledDecodedChannelError` when the binding is absent or its
    scale is unpinned (``scale: null``).
    """
    factors = scaling_factors(_signal(column, bindings))
    if factors is None:
        raise UnscaledDecodedChannelError(
            f"{column} has no pinned scale in the bindings catalog, so its raw "
            "counts have no engineering value; refusing to use it as one"
        )
    return factors


def assert_scalable(columns: Iterable[str], bindings: Bindings | None = None) -> None:
    """Refuse *now* if any ``decoded:`` name in ``columns`` can't be scaled.

    The eager counterpart of :func:`physical_series`, for a table of candidate
    columns (the segmenter's ``signal_columns``, a range/report mapping): it
    raises :class:`UnscaledDecodedChannelError` where the table is *declared*
    rather than on the one session whose Mode 01 PID happened to go stale, so
    ``decoded:VEHICLE_SPEED`` — a name no binding ever produced, which sat in
    three candidate lists for a year — cannot be written again.

    No catalog available at all (no ``data/bindings.yaml``, no ``bindings``)
    means there is nothing to check against; the read-time refusal in
    :func:`physical_series` still fires if such a column shows up.
    """
    catalog = bindings if bindings is not None else repo_bindings()
    if catalog is None:
        return
    for column in columns:
        if is_decoded_column(column):
            decoded_factors(column, catalog)


def physical_series(values: pd.Series, column: str, bindings: Bindings | None = None) -> pd.Series:
    """``values`` (column ``column``) as a float series in engineering units.

    Pass-through (numeric-coerced) for any non-``decoded:`` column; scaled for
    ``decoded:``. A binding's declared sentinel word reads as NaN, never as a
    value (the decoders already keep it out of ``decoded:`` columns; this
    covers counts from anywhere else). Never mutates ``values``.
    """
    numeric = pd.to_numeric(values, errors="coerce").astype(float)
    if not is_decoded_column(column):
        return numeric
    scale, offset = decoded_factors(column, bindings)
    sentinel = _signal(column, bindings).sentinel_raw
    if sentinel is not None:
        numeric = numeric.mask(numeric == sentinel)
    return numeric * scale + offset


def physical_value(
    value: float | None, column: str, bindings: Bindings | None = None
) -> float | None:
    """Scalar counterpart of :func:`physical_series` (``None`` passes through;
    a declared sentinel word reads as ``None``)."""
    if value is None:
        return None
    if not is_decoded_column(column):
        return float(value)
    signal = _signal(column, bindings)
    if signal.is_sentinel(float(value)):
        return None  # a declared no-measurement word, not a reading
    scaled = apply_scaling(signal, float(value))
    if scaled is None:
        raise UnscaledDecodedChannelError(
            f"{column} has no pinned scale in the bindings catalog, so its raw "
            "counts have no engineering value; refusing to use it as one"
        )
    return scaled


__all__ = [
    "DECODED_PREFIX",
    "DEFAULT_BINDINGS_PATH",
    "UnscaledDecodedChannelError",
    "assert_scalable",
    "decoded_factors",
    "is_decoded_column",
    "physical_series",
    "physical_value",
    "repo_bindings",
]
