"""Per-mode training envelopes for sequence models (A03.8, R04.11).

Split out of :mod:`src.models.sequence_training`, which re-exports every name.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import pandas as pd

from src.columns import MODE_COLUMN, SESSION_COLUMN

# ---------------------------------------------------------------------------
# A03.8 — training envelope per operating mode
# ---------------------------------------------------------------------------

#: The driving conditions the envelope records, as generic J1979 Mode 01
#: channels (not a vehicle's catalog): name -> (column, histogram bin width in
#: the channel's own units). Values are the resampler's bounded-ffill cells
#: (NaN once stale), so hours at a condition count every fresh-held second.
ENVELOPE_QUANTITIES: dict[str, tuple[str, float]] = {
    "baro_kpa": ("obd:BAROMETRIC_PRESSURE", 1.0),
    "iat_c": ("obd:INTAKE_TEMP", 2.0),
    "speed_kph": ("obd:SPEED", 5.0),
}


#: A02.2 via A03.8: a calibration covers a mode's envelope on a quantity when
#: its [min, max] in that mode spans at least this share of the mode's
#: training rows. The 2026-09-20 calibration (71–73 kPa) would cover almost
#: none of a corpus trained mostly near 80 kPa.
MIN_ENVELOPE_COVERAGE = 0.8


def session_envelopes(frame: pd.DataFrame, *, interval_seconds: float) -> dict[str, dict[str, Any]]:
    """Per-session, per-mode envelope summaries of an *unscaled* frame.

    ``{session: {mode: {"rows": n, "quantities": {q: {"channel", "rows",
    "min", "max", "bin_width", "histogram": {bin_start: rows}}}}}}``. Kept
    per session so any subset (training minus validation, a calibration
    holdout) merges exactly: rows and histograms add, min/max combine.
    """
    if frame.empty or MODE_COLUMN not in frame.columns:
        return {}
    out: dict[str, dict[str, Any]] = {}
    keys = [frame[SESSION_COLUMN].astype(str), frame[MODE_COLUMN].astype(str)]
    for (session, mode), group in frame.groupby(keys, sort=True):
        quantities: dict[str, Any] = {}
        for name, (column, width) in ENVELOPE_QUANTITIES.items():
            if column not in group.columns:
                continue
            values = pd.to_numeric(group[column], errors="coerce").to_numpy(dtype=np.float64)
            values = values[np.isfinite(values)]
            if values.size == 0:
                continue
            bins = np.floor(values / width) * width
            labels, counts = np.unique(bins, return_counts=True)
            quantities[name] = {
                "channel": column,
                "rows": int(values.size),
                "min": float(values.min()),
                "max": float(values.max()),
                "bin_width": width,
                "histogram": {
                    f"{label:g}": int(count) for label, count in zip(labels, counts, strict=True)
                },
            }
        out.setdefault(str(session), {})[str(mode)] = {
            "rows": int(len(group)),
            "seconds": float(len(group)) * float(interval_seconds),
            "quantities": quantities,
        }
    return out


def merge_envelopes(
    per_session: Mapping[str, Mapping[str, Any]],
    sessions: Sequence[str] | None = None,
) -> dict[str, Any]:
    """The envelope of ``sessions`` (all when ``None``): per mode, the hours of
    data and each quantity's range, 5th/95th percentile and histogram. A
    quantity never measured in a mode is listed under ``unmeasured``."""
    names = sorted(per_session) if sessions is None else sorted(set(sessions) & set(per_session))
    modes: dict[str, dict[str, Any]] = {}
    for session in names:
        for mode, summary in per_session[session].items():
            merged = modes.setdefault(mode, {"rows": 0, "seconds": 0.0, "quantities": {}})
            merged["rows"] += int(summary["rows"])
            merged["seconds"] += float(summary["seconds"])
            for name, stats in summary["quantities"].items():
                into = merged["quantities"].setdefault(
                    name,
                    {
                        "channel": stats["channel"],
                        "rows": 0,
                        "min": float("inf"),
                        "max": float("-inf"),
                        "bin_width": stats["bin_width"],
                        "histogram": {},
                    },
                )
                into["rows"] += int(stats["rows"])
                into["min"] = min(into["min"], float(stats["min"]))
                into["max"] = max(into["max"], float(stats["max"]))
                for label, count in stats["histogram"].items():
                    into["histogram"][label] = into["histogram"].get(label, 0) + int(count)
    for merged in modes.values():
        merged["hours"] = round(merged.pop("seconds") / 3600.0, 4)
        for stats in merged["quantities"].values():
            stats["p05"], stats["p95"] = _histogram_quantiles(stats, (0.05, 0.95))
            stats["histogram"] = dict(
                sorted(stats["histogram"].items(), key=lambda item: float(item[0]))
            )
        merged["unmeasured"] = sorted(set(ENVELOPE_QUANTITIES) - set(merged["quantities"]))
    return {
        "sessions": names,
        "quantities": {name: column for name, (column, _w) in ENVELOPE_QUANTITIES.items()},
        "modes": dict(sorted(modes.items())),
    }


def _histogram_quantiles(stats: Mapping[str, Any], quantiles: Sequence[float]) -> list[float]:
    """Bin-start quantiles of a merged histogram (resolution: one bin)."""
    items = sorted((float(label), int(count)) for label, count in stats["histogram"].items())
    total = sum(count for _label, count in items)
    out: list[float] = []
    for q in quantiles:
        running = 0
        value = items[-1][0] if items else float("nan")
        for label, count in items:
            running += count
            if running >= q * total:
                value = label
                break
        out.append(value)
    return out


def envelope_coverage(
    training: Mapping[str, Any], calibration: Mapping[str, Any], *, mode: str, quantity: str
) -> float | None:
    """Share of ``mode``'s training rows (for ``quantity``) inside the
    calibration's [min, max] in that mode; ``None`` when training never
    measured it there. ``0.0`` when calibration has no such rows."""
    trained = training.get("modes", {}).get(mode, {}).get("quantities", {}).get(quantity)
    if not trained or not trained["rows"]:
        return None
    calibrated = calibration.get("modes", {}).get(mode, {}).get("quantities", {}).get(quantity)
    if not calibrated or not calibrated["rows"]:
        return 0.0
    width = float(trained["bin_width"])
    low = np.floor(float(calibrated["min"]) / width) * width
    high = float(calibrated["max"])
    inside = sum(
        int(count) for label, count in trained["histogram"].items() if low <= float(label) <= high
    )
    return inside / float(trained["rows"])


def envelope_coverage_violations(
    training: Mapping[str, Any],
    calibration: Mapping[str, Any],
    *,
    modes: Sequence[str],
    minimum: float = MIN_ENVELOPE_COVERAGE,
) -> list[str]:
    """A02.2's envelope refusal: for each scored mode and recorded quantity,
    the calibration must span :data:`MIN_ENVELOPE_COVERAGE` of the training
    rows. A mode the calibration never visited is the mode-coverage
    violation's to report, not this one's."""
    violations: list[str] = []
    for mode in sorted(modes):
        if mode not in calibration.get("modes", {}):
            continue
        trained_mode = training.get("modes", {}).get(mode, {})
        for quantity in ENVELOPE_QUANTITIES:
            share = envelope_coverage(training, calibration, mode=mode, quantity=quantity)
            if share is None or share >= minimum:
                continue
            trained = trained_mode["quantities"][quantity]
            calibrated = calibration["modes"][mode]["quantities"].get(quantity)
            span = (
                "never measured it"
                if not calibrated
                else f"spans {calibrated['min']:g}–{calibrated['max']:g}"
            )
            violations.append(
                f"mode {mode!r}: calibration {quantity} {span}, covering {share:.0%} of "
                f"{trained_mode.get('hours', 0):g} training hours "
                f"(training {trained['min']:g}–{trained['max']:g}; "
                f"{minimum:.0%} required)"
            )
    return violations


__all__ = [
    "ENVELOPE_QUANTITIES",
    "MIN_ENVELOPE_COVERAGE",
    "envelope_coverage",
    "envelope_coverage_violations",
    "merge_envelopes",
    "session_envelopes",
]
