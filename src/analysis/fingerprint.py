"""Per-session fingerprint: a small stat vector for long-horizon drift (Task 4.1,
eval-longitudinal-and-dtc.md).

Where :class:`~src.analysis.session_classifier.SessionClassification` answers
"what happened in this drive" (modes, events, disposition), the fingerprint
answers "how did this drive's engine behave, numerically" — a handful of
scalars (warm-up rate, idle stability, fuel trims, bank-balance/committee
residuals, behavior-surface deviations) trended session-over-session against
a frozen reference epoch (Task 4.3) to catch slow drift that no single
session looks anomalous enough to flag.

:func:`compute_fingerprint` reuses the exact same ``read → wide → resample →
segment`` loading path as :func:`~src.analysis.session_classifier.classify_session`
(:func:`~src.preprocessing.loader.resample_and_segment`) rather than
re-reading the session, then layers on the same derived-signal appenders the
offline feature pipeline uses (:func:`~src.analysis.derived.compute_anomaly_signals`,
:func:`~src.analysis.committees.append_committee_residuals`,
:func:`~src.decode.behavior_decoder.append_deviation_columns`) so the
bank-balance / committee / behavior-deviation stats are simple aggregations
over columns already computed elsewhere, not new physics.

Every stat is *omitted* (not zeroed or NaN'd) when its precondition doesn't
hold for a session — e.g. a session with no captured cold start has no
``warmup_time_constant_s`` key at all. A trend test over a ragged series of
dicts (Task 4.4) is expected to treat a missing key as "no observation this
session," not "zero."
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from src.analysis.committees import COMMITTEES, append_committee_residuals
from src.analysis.derived import compute_anomaly_signals
from src.collection.obd_registry import MISFIRE_COUNT_CHANNEL_PREFIX
from src.config import CanaryConfig
from src.decode.behavior_decoder import DEVIATION_COLUMN_PREFIX, append_deviation_columns
from src.decode.behavior_models import load_behavior_models
from src.preprocessing.loader import resample_and_segment
from src.preprocessing.resampler import TIMESTAMP_COLUMN
from src.preprocessing.segmenter import MODE_COLUMN
from src.timefmt import format_timestamp as _format_timestamp

FINGERPRINT_FILENAME = "fingerprint.json"

#: Increment when the stat set / semantics change, so a corpus-wide backfill
#: can detect and re-skip already-current sidecars (mirrors
#: ``session_classifier.CLASSIFIER_VERSION``).
FINGERPRINT_VERSION = "0.1.0"

SCHEMA_VERSION = 1

#: Coolant temperature (°C) below which a session is considered to have
#: captured an actual cold start. Matches
#: ``src.analysis.events.cold_start._COLD_COOLANT_C`` — same physical
#: definition of "cold", kept as a local constant rather than importing a
#: private name across modules.
_COLD_COOLANT_C = 50.0

#: Coolant temperature (°C) the warmup time constant is measured to —
#: roughly thermostat-open steady state (canary.example.yaml's operating
#: modes gate warm regimes at 70 °C; 80 °C is comfortably past that).
_OPERATING_TEMP_C = 80.0

#: Engine-running RPM threshold. Matches
#: ``src.dashboard.status._RPM_RUNNING_THRESHOLD`` (the vehicle_state
#: classifier's "ignition_only" vs "engine_running" cut).
_RUNNING_RPM = 400.0

_IDLE_MODE = "idle"
#: Steady-speed driving modes used as the "cruise" fuel-trim sample.
#: canary.example.yaml has no dedicated "cruise" mode; highway/suburban are
#: the closest analogue (sustained speed, not stop-and-go like "city").
_CRUISE_MODES = frozenset({"highway", "suburban"})

_RPM_COLUMN = "obd:RPM"
_COOLANT_COLUMN = "obd:COOLANT_TEMP"
_MAF_COLUMN = "obd:MAF"
_ENGINE_LOAD_COLUMN = "obd:ENGINE_LOAD"
_VOLTAGE_COLUMN = "obd:CONTROL_MODULE_VOLTAGE"
_STFT_COLUMNS = ("obd:SHORT_FUEL_TRIM_1", "obd:SHORT_FUEL_TRIM_2")
_LTFT_COLUMNS = ("obd:LONG_FUEL_TRIM_1", "obd:LONG_FUEL_TRIM_2")

#: ``derived:*`` bank-balance columns (src.analysis.derived.compute_anomaly_signals)
#: → the short stat-key suffix each rolls up to.
_BANK_BALANCE_COLUMNS: dict[str, str] = {
    "derived:STFT_BANK_BALANCE": "stft",
    "derived:LTFT_BANK_BALANCE": "ltft",
    "derived:AFR_BANK_BALANCE": "afr",
}


@dataclass(frozen=True)
class SessionFingerprint:
    """Full fingerprint sidecar payload for one session."""

    schema_version: int
    fingerprint_version: str
    computed_at: str
    session_id: str
    stats: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "fingerprint_version": self.fingerprint_version,
            "computed_at": self.computed_at,
            "session_id": self.session_id,
            "stats": dict(self.stats),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionFingerprint:
        return cls(
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            fingerprint_version=str(data["fingerprint_version"]),
            computed_at=str(data["computed_at"]),
            session_id=str(data["session_id"]),
            stats={str(k): float(v) for k, v in dict(data.get("stats") or {}).items()},
        )


def compute_fingerprint(session_dir: Path, config: CanaryConfig) -> dict[str, float]:
    """Compute the per-drive stat vector for one recorded session.

    Raises ``FileNotFoundError`` when the session has no telemetry parquet
    (mirrors :func:`~src.analysis.session_classifier.classify_session`;
    caller treats it as "skip", not "quarantine"). Returns an empty dict for
    an otherwise-empty/unsegmentable session rather than raising further.
    """
    chunks = sorted(session_dir.glob("telemetry-*.parquet"))
    if not chunks:
        raise FileNotFoundError(f"no telemetry parquet under {session_dir}")

    frame = resample_and_segment([session_dir], config, value="value_number")
    if frame.empty:
        return {}
    frame = frame.reset_index(drop=True)

    frame = compute_anomaly_signals(frame, config.vehicle.parameters)
    frame = append_committee_residuals(frame)
    frame = _append_behavior_deviations(frame, config)

    stats: dict[str, float] = {}
    _add_warmup_stat(frame, stats)
    _add_idle_operating_temp_stats(frame, stats)
    _add_fuel_trim_stats(frame, stats)
    _add_cranking_stat(frame, stats)
    _add_voltage_stats(frame, stats)
    _add_bank_balance_stats(frame, stats)
    _add_committee_stats(frame, stats)
    _add_behavior_deviation_stats(frame, stats)
    _add_misfire_stats(frame, stats)
    _add_cat_efficiency_stats(frame, stats)
    _add_o2_health_stats(frame, stats)
    return stats


def write_fingerprint(
    session_dir: Path,
    stats: dict[str, float],
    *,
    now: datetime | None = None,
) -> Path:
    """Write the ``fingerprint.json`` sidecar and return its path. Idempotent."""
    fingerprint = SessionFingerprint(
        schema_version=SCHEMA_VERSION,
        fingerprint_version=FINGERPRINT_VERSION,
        computed_at=_format_timestamp(datetime.now(UTC) if now is None else now),
        session_id=session_dir.name,
        stats=stats,
    )
    path = session_dir / FINGERPRINT_FILENAME
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(fingerprint.to_dict(), indent=2, sort_keys=True) + "\n")
    tmp.replace(path)
    return path


def read_fingerprint(session_dir: Path) -> SessionFingerprint | None:
    """Load the sidecar, or ``None`` if absent / unreadable."""
    path = session_dir / FINGERPRINT_FILENAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return SessionFingerprint.from_dict(data)
    except (KeyError, TypeError, ValueError):
        return None


def is_stale(fingerprint: SessionFingerprint | None) -> bool:
    """True when a sidecar is missing or its ``fingerprint_version`` is behind."""
    if fingerprint is None:
        return True
    return fingerprint.fingerprint_version != FINGERPRINT_VERSION


# --------------------------------------------------------------------------- #
# Frame enrichment
# --------------------------------------------------------------------------- #


def _append_behavior_deviations(frame: pd.DataFrame, config: CanaryConfig) -> pd.DataFrame:
    """Append ``deviation:*`` columns when a behavior-model file exists.

    Best-effort: a missing or unparseable ``behavior_models.yaml`` simply
    means the behavior-deviation stats are omitted, not a hard failure of
    the whole fingerprint.
    """
    models_path = config.daemon.behavior_models
    if not models_path.exists():
        return frame
    try:
        models = load_behavior_models(models_path)
    except Exception:  # noqa: BLE001 — a broken models file must not block fingerprinting
        return frame
    if not models.models:
        return frame
    return append_deviation_columns(frame, models)


# --------------------------------------------------------------------------- #
# Individual stats — each appends to ``stats`` only when its precondition holds
# --------------------------------------------------------------------------- #


def _numeric(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(dtype=float)
    return pd.to_numeric(frame[column], errors="coerce")


def _add_warmup_stat(frame: pd.DataFrame, stats: dict[str, float]) -> None:
    coolant = _numeric(frame, _COOLANT_COLUMN)
    first_valid = coolant.first_valid_index()
    if first_valid is None or coolant.loc[first_valid] > _COLD_COOLANT_C:
        return  # no cold start captured this session
    warm_mask = coolant >= _OPERATING_TEMP_C
    if not warm_mask.any():
        return  # never reached operating temp
    warm_idx = warm_mask.idxmax()
    ts = pd.to_datetime(frame[TIMESTAMP_COLUMN], utc=True)
    duration = (ts.loc[warm_idx] - ts.loc[first_valid]).total_seconds()
    if duration <= 0:
        return
    stats["warmup_time_constant_s"] = float(duration)


def _add_idle_operating_temp_stats(frame: pd.DataFrame, stats: dict[str, float]) -> None:
    if MODE_COLUMN not in frame.columns:
        return
    idle = frame[frame[MODE_COLUMN] == _IDLE_MODE]
    if idle.empty:
        return
    rpm = _numeric(idle, _RPM_COLUMN).dropna()
    if not rpm.empty:
        stats["idle_rpm_mean"] = float(rpm.mean())
        if len(rpm) > 1:
            stats["idle_rpm_std"] = float(rpm.std())
    maf = _numeric(idle, _MAF_COLUMN).dropna()
    if not maf.empty:
        stats["idle_maf_mean"] = float(maf.mean())
    load = _numeric(idle, _ENGINE_LOAD_COLUMN).dropna()
    if not load.empty:
        stats["idle_engine_load_mean"] = float(load.mean())


def _mean_over_columns(frame: pd.DataFrame, columns: Sequence[str]) -> float | None:
    present = [c for c in columns if c in frame.columns]
    if not present:
        return None
    values = pd.concat([_numeric(frame, c) for c in present], ignore_index=True).dropna()
    return float(values.mean()) if not values.empty else None


def _combined_trim(frame: pd.DataFrame) -> float | None:
    """Mean of per-bank (STFT + LTFT) across banks with both trims present."""
    parts: list[pd.Series] = []
    for stft_col, ltft_col in zip(_STFT_COLUMNS, _LTFT_COLUMNS, strict=True):
        if stft_col in frame.columns and ltft_col in frame.columns:
            parts.append(_numeric(frame, stft_col) + _numeric(frame, ltft_col))
    if not parts:
        return None
    combined = pd.concat(parts, ignore_index=True).dropna()
    return float(combined.mean()) if not combined.empty else None


def _add_fuel_trim_stats(frame: pd.DataFrame, stats: dict[str, float]) -> None:
    if MODE_COLUMN not in frame.columns:
        return
    idle = frame[frame[MODE_COLUMN] == _IDLE_MODE]
    cruise = frame[frame[MODE_COLUMN].isin(_CRUISE_MODES)]
    for label, subset in (("idle", idle), ("cruise", cruise)):
        if subset.empty:
            continue
        stft = _mean_over_columns(subset, _STFT_COLUMNS)
        if stft is not None:
            stats[f"stft_mean_{label}"] = stft
        ltft = _mean_over_columns(subset, _LTFT_COLUMNS)
        if ltft is not None:
            stats[f"ltft_mean_{label}"] = ltft
        combined = _combined_trim(subset)
        if combined is not None:
            stats[f"trim_combined_mean_{label}"] = combined


def _add_cranking_stat(frame: pd.DataFrame, stats: dict[str, float]) -> None:
    rpm = _numeric(frame, _RPM_COLUMN)
    first_valid = rpm.first_valid_index()
    if first_valid is None or rpm.loc[first_valid] >= _RUNNING_RPM:
        return  # no cranking transient captured (already running at first sample)
    running_mask = rpm >= _RUNNING_RPM
    if not running_mask.any():
        return  # engine never reached a running RPM this session
    running_idx = running_mask.idxmax()
    ts = pd.to_datetime(frame[TIMESTAMP_COLUMN], utc=True)
    duration = (ts.loc[running_idx] - ts.loc[first_valid]).total_seconds()
    if duration < 0:
        return
    stats["cranking_duration_s"] = float(duration)


def _add_voltage_stats(frame: pd.DataFrame, stats: dict[str, float]) -> None:
    voltage = _numeric(frame, _VOLTAGE_COLUMN)
    if voltage.empty:
        return
    rpm = _numeric(frame, _RPM_COLUMN)
    first_valid = rpm.first_valid_index()
    # Same precondition as _add_cranking_stat: only trust a "crank window" when
    # the session's telemetry genuinely starts mid-crank. Leading RPM NaNs from
    # round-robin OBD polling (resample_wide never back-fills) must not be
    # mistaken for a crank→running crossing.
    if first_valid is not None and rpm.loc[first_valid] < _RUNNING_RPM:
        running_mask = rpm >= _RUNNING_RPM
        if running_mask.any():
            running_idx = running_mask.idxmax()
            # Strictly the rows between the first RPM sample and the running
            # crossing — the cranking window (mirrors _add_cranking_stat).
            crank_mask = (voltage.index >= first_valid) & (voltage.index < running_idx)
            crank_voltage = voltage[crank_mask].dropna()
            if not crank_voltage.empty:
                stats["voltage_at_crank_v"] = float(crank_voltage.mean())
    if MODE_COLUMN in frame.columns:
        idle_voltage = voltage[frame[MODE_COLUMN] == _IDLE_MODE].dropna()
        if not idle_voltage.empty:
            stats["voltage_at_idle_v"] = float(idle_voltage.mean())


def _add_bank_balance_stats(frame: pd.DataFrame, stats: dict[str, float]) -> None:
    for column, suffix in _BANK_BALANCE_COLUMNS.items():
        values = _numeric(frame, column).dropna()
        if values.empty:
            continue
        stats[f"bank_balance_mean:{suffix}"] = float(values.mean())


def _add_committee_stats(frame: pd.DataFrame, stats: dict[str, float]) -> None:
    for committee in COMMITTEES:
        values = _numeric(frame, committee.output).dropna()
        if values.empty:
            continue
        stats[f"committee_residual_mean:{committee.name.lower()}"] = float(values.mean())


def _add_behavior_deviation_stats(frame: pd.DataFrame, stats: dict[str, float]) -> None:
    for column in frame.columns:
        if not str(column).startswith(DEVIATION_COLUMN_PREFIX):
            continue
        values = _numeric(frame, str(column)).dropna()
        if values.empty:
            continue
        name = str(column)[len(DEVIATION_COLUMN_PREFIX) :]
        stats[f"behavior_deviation_abs_mean:{name}"] = float(values.abs().mean())


def _add_misfire_stats(frame: pd.DataFrame, stats: dict[str, float]) -> None:
    """Task 3.2 — per-cylinder misfire count, end of session.

    Mode 06 misfire counters are cumulative within a drive cycle (never
    reset mid-session), so the session's max observed value is its ending
    count — the natural session-over-session trend stat (Task 3.1's poll
    lands one ``obd:MISFIRE_COUNT_CYLn`` numeric column per cylinder
    present; no cylinder count is hardcoded here, mirrors
    ``_add_behavior_deviation_stats``'s column-prefix scan above).
    """
    prefix = f"obd:{MISFIRE_COUNT_CHANNEL_PREFIX}"
    for column in frame.columns:
        if not str(column).startswith(prefix):
            continue
        values = _numeric(frame, str(column)).dropna()
        if values.empty:
            continue
        cylinder = str(column)[len(prefix) :]
        stats[f"misfire_count_cyl:{cylinder}"] = float(values.max())


def _add_cat_efficiency_stats(frame: pd.DataFrame, stats: dict[str, float]) -> None:
    """Task 4.4 — trend the catalyst-efficiency heuristic index per bank.

    ``derived:CAT_EFFICIENCY_B{1,2}``
    (``src.analysis.derived.compute_anomaly_signals``) is a bounded 0-1
    index, not a literal efficiency; the session mean is the natural
    session-over-session trend stat, same pattern as
    ``_add_bank_balance_stats`` above.
    """
    for bank in (1, 2):
        values = _numeric(frame, f"derived:CAT_EFFICIENCY_B{bank}").dropna()
        if values.empty:
            continue
        stats[f"cat_efficiency_mean:b{bank}"] = float(values.mean())


#: Task 4.5 — narrowband (0-1 V) O2 sensor rich/lean switch threshold. Same
#: physical constant as ``derived.py``'s catalyst-efficiency switch
#: counting, duplicated rather than imported: this is an independent
#: fingerprint-only stat, not the live ``derived:CAT_EFFICIENCY`` channel
#: (the task is explicit that O2 sensor health is fingerprint-stat
#: territory, not a new ``derived:`` channel).
_O2_SWITCH_THRESHOLD_V = 0.45


def _o2_run_durations(ts: pd.Series, state: pd.Series) -> tuple[float | None, float | None]:
    """Mean dwell duration (s) of ``state==True`` runs and ``state==False`` runs.

    A "run" is a maximal stretch of consecutive samples on one side of the
    rich/lean threshold; its duration is last-timestamp minus first-timestamp
    within the run — an undercount of true dwell time by one inter-sample
    gap, acceptable for a trend stat, not a precision timer.
    """
    run_id = state.ne(state.shift()).cumsum()
    grouped = pd.DataFrame({"ts": ts, "state": state, "run": run_id}).groupby("run")
    durations = (grouped["ts"].max() - grouped["ts"].min()).dt.total_seconds()
    run_state = grouped["state"].first()
    rich = durations[run_state].dropna()
    lean = durations[~run_state].dropna()
    rich_mean = float(rich.mean()) if not rich.empty else None
    lean_mean = float(lean.mean()) if not lean.empty else None
    return rich_mean, lean_mean


def _add_o2_health_stats(frame: pd.DataFrame, stats: dict[str, float]) -> None:
    """Task 4.5 — O2/AF sensor health. Fingerprint stats only, deliberately
    **not** a ``derived:`` live channel (the task's explicit framing).

    * Downstream narrowband sensor switching rate (Hz) + response asymmetry
      (mean rich-dwell / mean lean-dwell duration — a lazy sensor is slower
      to respond in one direction than the other; ~1 is symmetric/healthy).
      Inputs: ``obd:O2_B{1,2}S2``.
    * Upstream wideband λ noise (session std) and bias (session mean − 1.0,
      stoichiometric) restricted to steady idle — the only mode where λ is
      expected to sit near a stable setpoint rather than tracking a
      commanded excursion. Input: ``obd:O2_WR_LAMBDA_S1`` (bank-1 upstream
      wideband — the wideband PID this vehicle's ECM answers per Task 2.1).
    """
    if TIMESTAMP_COLUMN in frame.columns:
        ts_all = pd.to_datetime(frame[TIMESTAMP_COLUMN], utc=True)
        for bank in (1, 2):
            voltage = _numeric(frame, f"obd:O2_B{bank}S2")
            valid = voltage.notna()
            if valid.sum() < 2:
                continue
            v = voltage[valid]
            t = ts_all[valid]
            state = v > _O2_SWITCH_THRESHOLD_V
            transitions = int(state.ne(state.shift()).iloc[1:].sum())
            duration_s = (t.iloc[-1] - t.iloc[0]).total_seconds()
            if duration_s > 0:
                stats[f"o2_downstream_switch_rate_hz:b{bank}"] = transitions / duration_s
            rich_mean, lean_mean = _o2_run_durations(t, state)
            if rich_mean is not None and lean_mean is not None and lean_mean > 0:
                stats[f"o2_downstream_response_asymmetry:b{bank}"] = rich_mean / lean_mean

    if MODE_COLUMN in frame.columns:
        idle = frame[frame[MODE_COLUMN] == _IDLE_MODE]
        idle_lambda = _numeric(idle, "obd:O2_WR_LAMBDA_S1").dropna()
        if len(idle_lambda) > 1:
            stats["o2_upstream_wideband_lambda_noise_idle"] = float(idle_lambda.std())
            stats["o2_upstream_wideband_lambda_bias_idle"] = float(idle_lambda.mean() - 1.0)


__all__ = [
    "FINGERPRINT_FILENAME",
    "FINGERPRINT_VERSION",
    "SCHEMA_VERSION",
    "SessionFingerprint",
    "compute_fingerprint",
    "is_stale",
    "read_fingerprint",
    "write_fingerprint",
]
