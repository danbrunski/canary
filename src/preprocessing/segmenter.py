"""Rule-based operating-mode segmentation."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import pandas as pd

from src.columns import MODE_COLUMN, UNKNOWN_MODE, gap_column
from src.config import OperatingModeSpec
from src.decode.bindings import Bindings
from src.decode.engineering import assert_scalable, physical_series, physical_value

#: Signals whose coalesced value is nulled out on a stale (gap-flagged) row
#: before mode-rule evaluation, so a frozen sensor can't confidently mis-mode
#: a row — it falls to ``unknown`` instead. Narrowly scoped to coolant (the
#: signal that actually gates warmup/idle) rather than every signal: rpm/
#: speed/throttle are always-fast, always-gap-tracked already, and gating
#: them too would silently shrink segmentation coverage far beyond this
#: fix's intent.
_STALE_GATED_SIGNALS = frozenset({"coolant_temp"})

#: Derived signal name for the trailing-max ground speed (km/h) used to tell a
#: transient stop (recently driving) from a steady standing idle. Ephemeral:
#: computed here, consumed by the mode rules, never added to the output frame.
RECENT_SPEED_SIGNAL = "recent_speed"

#: Trailing window (s) for ``recent_speed``. Mirrors
#: ``PreprocessingConfig.idle_transient_lookback_seconds`` so direct callers
#: that don't thread config still segment like the configured default.
DEFAULT_RECENT_MOTION_WINDOW_SECONDS = 180.0

# Each signal maps to an ordered tuple of candidate wide columns; the segmenter
# coalesces them row-wise (first non-NaN in priority order). For ``speed`` the
# OBD wheel-speed PID (VSS) is the *primary* source: it reads a true 0 the
# instant the truck stops and tracks the drivetrain directly, so it's the
# reliable member. GPS ground-speed is the back-stop (used only when OBD is
# absent — e.g. rolling with the engine off, or a PID dropout): it has a
# stationary noise floor and over-reads on marginal fixes (observed 151 km/h vs
# OBD's 110), so it corroborates rather than gates. GPS↔OBD *disagreement* is
# surfaced separately as a sensor-committee residual (see signals.committees),
# not by which one wins the coalesce.
# The `decoded:` entries below are broadcast CAN, which arrives at ~98 Hz
# against Mode 01's 0.26-0.93 Hz. They sit *after* the OBD PID because the
# behaviour models are fitted on `obd:` columns and must keep seeing them —
# this is a coalesce, so OBD still wins every row where it is fresh.
#
# They matter because the resampler holds rpm/speed to the *fast*-channel
# staleness ceiling (2 s on a 1 Hz grid) while Mode 01 delivers them with a
# 6.6-32 s p99 gap. Every row where they went NaN fell through to the
# `unknown` mode: on the 2026-07-27 isolation-forest fit that bucket held
# 15464 of 21784 complete rows with a degenerate -0.0 threshold, flagging
# 100% of what landed in it. A broadcast signal that never goes stale is the
# fix, and it costs no bus time at all — the frames are already being read.
#
# A `decoded:` column holds RAW binding counts (the feature contract), while
# every mode threshold is in engineering units. The segmenter therefore never
# reads a candidate's column directly: `_coalesced_series` / `_row_value` send
# each one through `src.decode.engineering.physical_series`, which scales
# `decoded:` via bindings.yaml and refuses one with no pinned scale. That is
# what makes the 2026-08-06..09-18 bug unwritable — raw ECM_RPM (8x rpm),
# ECM_COOLANT_TEMP (degC + 40) and WHEEL_SPEED_6F7_C (16x km/h) were compared
# to rpm 900 / 70 degC / 80 km/h as they stood, so broadcast-labelled warm idle
# came out `unknown`, cold read as warm, and 60 km/h read as `highway`.
#
# Verified same-instant on 2026-08-06T162657 before being added here:
# obd:RPM vs decoded:ECM_RPM r2 = 0.9944 over 2557 paired seconds, and
# obd:COOLANT_TEMP vs decoded:ECM_COOLANT_TEMP r2 = 0.9929 over 584. Both
# fit slightly under unity (0.9939 / 0.9888) purely because the OBD side is
# stale between polls on a 1 s grid; against a 15 Hz DID the same RPM
# binding fits 1.0007 with 0.09% drift.
DEFAULT_SIGNAL_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "rpm": ("obd:RPM", "obd:rpm", "decoded:ECM_RPM"),
    # CAN wheel speed goes ahead of GPS because it is on the *same scale* as
    # the primary, not because it is more accurate — it is not. Against
    # obd:SPEED on 2026-08-06T162657 it fits r2 = 0.9992, slope 0.9945, drift
    # 1.00% (`stable`, n = 2458), while gps:speed sits ~8% higher.
    #
    # GPS is the one telling the truth there. This vehicle runs LT285/75R16
    # against a P265/75R16 stock fitment and 4.08 diffs in place of 3.69, and
    # both ECU-derived speeds read ~8% under true ground speed (per-row median
    # obd/gps 0.9203, flat across speed bands). Steady-cruise engine-rpm
    # clusters resolve the driveline exactly against GPS — gear ratios of
    # 1.000 (direct drive), 1.309 and 1.740 at FD 4.08 and a 2.609 m loaded
    # circumference — which a mis-scaled speed reference could not produce.
    # Tire size accounts for only ~4 points of the 8; the rest is unexplained
    # and is tracked on the 0x6F7 binding in bindings.yaml.
    #
    # Mode thresholds were set, and the behaviour models fitted, against
    # obd:SPEED. So the fallback must match *that* scale or a row that
    # coalesces onto it gets gated differently from its neighbours — an 8%
    # step is 6.4 km/h at the 80 km/h city/highway boundary, enough to flip
    # the label. GPS stays last: right about the road, wrong for this job,
    # and the only option left when rolling with the engine off.
    "speed": ("obd:SPEED", "obd:speed", "decoded:WHEEL_SPEED_6F7_C", "gps:speed"),
    "throttle": ("obd:THROTTLE_POS", "obd:throttle_pos"),
    "coolant_temp": ("obd:COOLANT_TEMP", "obd:coolant_temp", "decoded:ECM_COOLANT_TEMP"),
    # Phase 2a.7 — decimated IMU peaks (src.preprocessing.imu_decimation).
    # Absent from every frame recorded before Phase 2b hardware, so any mode
    # gating on these is simply skipped (same "missing candidate columns"
    # path every other signal already falls back to).
    "accel_lon": ("imu:ACCEL_LON_MAX",),
    "accel_lat": ("imu:ACCEL_LAT_MAX",),
}


@dataclass(frozen=True)
class SegmenterConfig:
    """Column mapping for operating-mode threshold inputs.

    Each signal maps either to a single wide column or to an ordered tuple of
    candidate columns coalesced row-wise (first non-NaN wins). A plain string is
    treated as a one-element candidate list, so single-source configs read the
    same as before.
    """

    signal_columns: Mapping[str, str | tuple[str, ...]]
    unknown_mode: str = UNKNOWN_MODE
    output_column: str = MODE_COLUMN
    #: Trailing window (s) for the derived ``recent_speed`` signal, and the
    #: columns its per-session rolling max needs. Computed only when a mode
    #: actually gates on ``recent_speed`` and a timestamp column is present;
    #: otherwise the signal is simply absent and recent-motion modes are skipped.
    recent_motion_window_seconds: float = DEFAULT_RECENT_MOTION_WINDOW_SECONDS
    session_column: str = "session"
    timestamp_column: str = "timestamp"
    #: Phase 5.2 — consecutive rows a candidate mode must hold before the
    #: label actually switches; the previous *effective* label persists
    #: during the dwell window. ``1`` (default) is the legacy immediate-switch
    #: behavior — every row's own raw threshold match wins, matching every
    #: caller/test that predates this field. Mirrors the shape of
    #: ``AlertsConfig.min_consecutive_ticks`` (also "N consecutive before it
    #: counts"), applied to mode labels instead of alert firing.
    mode_dwell_ticks: int = 1
    #: Catalog used to scale ``decoded:`` candidates to engineering units before
    #: the thresholds see them. ``None`` = the repo's ``data/bindings.yaml``
    #: (the file every production decode path loads). Not optional in effect:
    #: a ``decoded:`` candidate that cannot be scaled raises
    #: :class:`~src.decode.engineering.UnscaledDecodedChannelError`.
    bindings: Bindings | None = None

    def __post_init__(self) -> None:
        """Refuse a candidate table the repo catalog cannot convert to
        engineering units — at construction, not on the row that first needs it.

        Every threshold in ``operating_modes`` is an engineering value, so a
        ``decoded:`` candidate is only usable if the catalog pins its scale.
        Checking the *declared* table means a name no binding produces
        (``decoded:VEHICLE_SPEED``, which sat in three candidate lists for a
        year doing nothing) fails on the first segment call of any session,
        rather than only on the sessions where the primary PID went stale — the
        2026-08-06 regression was invisible for six weeks because fresh Mode 01
        masked it.

        Only against the repo catalog (``bindings is None``). An explicitly
        supplied catalog is a *scoping* choice — ``--bindings`` also decides
        which ``decoded:`` columns get produced at all, so a candidate it
        doesn't define is simply an absent column, not a declaration error.
        Those are caught at read time instead: ``_coalesced_series`` scales
        every candidate column *present in the frame*, so a stored ``decoded:``
        column this catalog cannot explain still refuses rather than being read
        as counts.
        """
        if self.bindings is not None:
            return
        assert_scalable(
            column
            for candidates in self.signal_columns.values()
            for column in ((candidates,) if isinstance(candidates, str) else candidates)
        )


def default_segmenter_config(frame: pd.DataFrame) -> SegmenterConfig:
    """Build a config from canonical wide columns present in frame.

    Keeps *every* present candidate per signal (in priority order) rather than
    just the first, so the row-wise coalesce can fall back from GPS speed to OBD
    speed when only some rows have a fix.
    """
    mapping: dict[str, str | tuple[str, ...]] = {}
    for signal, candidates in DEFAULT_SIGNAL_COLUMNS.items():
        present = tuple(candidate for candidate in candidates if candidate in frame.columns)
        if present:
            mapping[signal] = present
    return SegmenterConfig(signal_columns=mapping)


def segment_operating_modes(
    frame: pd.DataFrame,
    operating_modes: Mapping[str, OperatingModeSpec],
    *,
    config: SegmenterConfig | None = None,
    recent_motion_window_seconds: float | None = None,
    mode_dwell_ticks: int | None = None,
) -> pd.DataFrame:
    """Append an operating-mode label based on configured threshold rules.

    Modes are evaluated in mapping order; the first mode whose thresholds all
    match wins. Rows with missing required signals or no matching rule receive
    `unknown_mode`.

    ``recent_motion_window_seconds`` overrides ``config.recent_motion_window_seconds``
    so callers that don't build a ``SegmenterConfig`` (LSTM/labeling/replay) can
    still thread the operator-configured window and keep train/serve parity.
    ``mode_dwell_ticks`` overrides ``config.mode_dwell_ticks`` the same way
    (Phase 5.2 hysteresis). This is the one function both the batch loader
    (``resample_and_segment``) and live serve (``feature_pipeline.
    segment_or_constant``, re-run over the buffered window each tick) call —
    dwell lives here, not duplicated per call site, so train/serve can't drift.
    """
    seg_config = default_segmenter_config(frame) if config is None else config
    window_s = (
        seg_config.recent_motion_window_seconds
        if recent_motion_window_seconds is None
        else recent_motion_window_seconds
    )
    dwell_ticks = seg_config.mode_dwell_ticks if mode_dwell_ticks is None else mode_dwell_ticks
    result = frame.copy()
    labels = pd.Series(seg_config.unknown_mode, index=result.index, dtype="object")
    unassigned = pd.Series(True, index=result.index)

    # Coalesce each configured signal across its candidate columns once
    # (OBD-primary speed falls back to GPS per row). A signal whose candidate
    # columns are all absent from the frame is simply omitted, so any mode that
    # requires it is skipped — matching the prior "missing required signal"
    # behavior.
    coalesced: dict[str, pd.Series] = {}
    for signal in seg_config.signal_columns:
        series = _coalesced_series(
            result,
            _candidates(seg_config, signal),
            gap_masked=signal in _STALE_GATED_SIGNALS,
            bindings=seg_config.bindings,
        )
        if series is not None:
            coalesced[signal] = series

    # Derive ``recent_speed`` (trailing per-session max ground speed) only when a
    # mode actually gates on it — it distinguishes a transient stop (driving
    # moments ago) from a steady standing idle. Ephemeral: used for the labels
    # below, never written to ``result``, so it can't leak into the feature set.
    if RECENT_SPEED_SIGNAL in _all_required_signals(operating_modes) and "speed" in coalesced:
        recent = _recent_speed_max(result, coalesced["speed"], seg_config, window_s)
        if recent is not None:
            coalesced[RECENT_SPEED_SIGNAL] = recent

    for mode_name, spec in operating_modes.items():
        required_signals = _required_signals(spec)
        if not required_signals:
            continue
        if any(signal not in coalesced for signal in required_signals):
            continue

        mask = unassigned.copy()
        for signal in required_signals:
            mask &= _threshold_mask(coalesced[signal], spec, signal)

        labels.loc[mask] = mode_name
        unassigned &= ~mask

    if dwell_ticks > 1:
        labels = _apply_mode_dwell(labels, result, seg_config, dwell_ticks)

    result[seg_config.output_column] = labels
    return result


def mode_for_row(
    row: pd.Series,
    operating_modes: Mapping[str, OperatingModeSpec],
    *,
    config: SegmenterConfig,
) -> str:
    """Return the first matching mode for one row."""
    for mode_name, spec in operating_modes.items():
        required_signals = _required_signals(spec)
        if not required_signals:
            continue
        if any(signal not in config.signal_columns for signal in required_signals):
            continue
        values: dict[str, float] = {}
        complete = True
        for signal in required_signals:
            value = _row_value(row, _candidates(config, signal), bindings=config.bindings)
            if value is None:
                complete = False
                break
            values[signal] = value
        if complete and all(
            _value_matches(values[signal], spec, signal) for signal in required_signals
        ):
            return mode_name
    return config.unknown_mode


def _required_signals(spec: OperatingModeSpec) -> list[str]:
    signals: list[str] = []
    if spec.rpm_min is not None or spec.rpm_max is not None:
        signals.append("rpm")
    if spec.speed_min is not None or spec.speed_max is not None:
        signals.append("speed")
    if spec.throttle_min is not None or spec.throttle_max is not None:
        signals.append("throttle")
    if spec.coolant_temp_min is not None or spec.coolant_temp_max is not None:
        signals.append("coolant_temp")
    if spec.recent_speed_min is not None or spec.recent_speed_max is not None:
        signals.append(RECENT_SPEED_SIGNAL)
    if spec.accel_lon_min is not None or spec.accel_lon_max is not None:
        signals.append("accel_lon")
    if spec.accel_lat_min is not None or spec.accel_lat_max is not None:
        signals.append("accel_lat")
    return signals


def _all_required_signals(operating_modes: Mapping[str, OperatingModeSpec]) -> set[str]:
    """Union of signals any mode constrains — used to skip deriving unused ones."""
    signals: set[str] = set()
    for spec in operating_modes.values():
        signals.update(_required_signals(spec))
    return signals


def _candidates(config: SegmenterConfig, signal: str) -> tuple[str, ...]:
    """Normalize a signal's column spec to an ordered candidate tuple."""
    value = config.signal_columns[signal]
    return (value,) if isinstance(value, str) else tuple(value)


def _coalesced_series(
    frame: pd.DataFrame,
    candidates: tuple[str, ...],
    *,
    gap_masked: bool = False,
    bindings: Bindings | None = None,
) -> pd.Series | None:
    """Row-wise first-non-NaN over the candidate columns present in ``frame``,
    in engineering units (``decoded:`` candidates scaled via ``bindings``).

    Returns ``None`` when none of the candidates is a column in the frame, so
    callers can treat the signal as missing.

    ``gap_masked`` treats a candidate's gap-flagged (stale/forward-filled)
    rows as absent for *that* candidate before the coalesce picks a value —
    so a frozen reading doesn't confidently drive a mode rule. If a
    ``<candidate>__gap`` column isn't present (segmenter called pre-1.2, or
    on a frame the resampler never touched), the candidate is treated as
    always-fresh — no gap column means no known staleness, not the reverse.
    """
    present = [column for column in candidates if column in frame.columns]
    if not present:
        return None

    def _values(column: str) -> pd.Series:
        values = physical_series(frame[column], column, bindings)
        if gap_masked:
            gap_col = gap_column(column)
            if gap_col in frame.columns:
                stale = frame[gap_col].fillna(True).astype(bool)
                values = values.where(~stale)
        return values

    series = _values(present[0])
    for column in present[1:]:
        series = series.fillna(_values(column))
    return series


def _row_value(
    row: pd.Series, candidates: tuple[str, ...], *, bindings: Bindings | None = None
) -> float | None:
    """First present, non-NaN candidate value in ``row`` (priority order), in
    engineering units."""
    for column in candidates:
        if column in row.index and pd.notna(row[column]):
            try:
                raw = float(row[column])
            except (TypeError, ValueError):
                return None
            return physical_value(raw, column, bindings)
    return None


def _threshold_mask(values: pd.Series, spec: OperatingModeSpec, signal: str) -> pd.Series:
    minimum, maximum = _bounds(spec, signal)
    mask = values.notna()
    if minimum is not None:
        mask &= values >= minimum
    if maximum is not None:
        mask &= values <= maximum
    return mask


def _value_matches(value: float, spec: OperatingModeSpec, signal: str) -> bool:
    minimum, maximum = _bounds(spec, signal)
    if minimum is not None and value < minimum:
        return False
    if maximum is not None and value > maximum:
        return False
    return True


def _bounds(spec: OperatingModeSpec, signal: str) -> tuple[float | None, float | None]:
    if signal == "rpm":
        return spec.rpm_min, spec.rpm_max
    if signal == "speed":
        return spec.speed_min, spec.speed_max
    if signal == "throttle":
        return spec.throttle_min, spec.throttle_max
    if signal == "coolant_temp":
        return spec.coolant_temp_min, spec.coolant_temp_max
    if signal == RECENT_SPEED_SIGNAL:
        return spec.recent_speed_min, spec.recent_speed_max
    if signal == "accel_lon":
        return spec.accel_lon_min, spec.accel_lon_max
    if signal == "accel_lat":
        return spec.accel_lat_min, spec.accel_lat_max
    raise ValueError(f"unknown operating-mode signal {signal!r}")


def _recent_speed_max(
    frame: pd.DataFrame,
    speed: pd.Series,
    config: SegmenterConfig,
    window_seconds: float,
) -> pd.Series | None:
    """Trailing (causal) max ground speed over ``window_seconds``, per session.

    Returns ``None`` when there's no timestamp column to window over, so the
    caller treats ``recent_speed`` as absent and recent-motion modes are skipped.
    Grouped by session so motion never bleeds across sessions; computed only on
    the past, so it's identical offline and in the live ring buffer.
    """
    ts_col = config.timestamp_column
    if ts_col not in frame.columns:
        return None
    timestamps = pd.to_datetime(frame[ts_col], utc=True, errors="coerce")
    if timestamps.isna().all():
        return None
    window = f"{max(int(round(window_seconds)), 1)}s"
    values = pd.to_numeric(speed, errors="coerce")
    work = pd.DataFrame({"v": values.to_numpy(), "ts": timestamps.to_numpy()}, index=frame.index)
    if config.session_column in frame.columns:
        groups: Iterable[tuple[object, pd.DataFrame]] = work.groupby(
            frame[config.session_column].to_numpy(), sort=False
        )
    else:
        groups = [(None, work)]
    out = pd.Series(float("nan"), index=frame.index, dtype="float64")
    for _, sub in groups:
        ordered = sub.sort_values("ts", kind="stable")
        rolled = ordered.set_index("ts")["v"].rolling(window).max()
        out.loc[ordered.index] = rolled.to_numpy()
    return out


def _apply_mode_dwell(
    labels: pd.Series,
    frame: pd.DataFrame,
    config: SegmenterConfig,
    dwell_ticks: int,
) -> pd.Series:
    """Causal hysteresis (Phase 5.2): a candidate mode only takes effect once
    it has held for ``dwell_ticks`` consecutive rows (this row inclusive); the
    previous *effective* label persists until then, so a single flapping row
    at a mode boundary can't switch the label. Grouped by session and ordered
    by timestamp — same shape as ``_recent_speed_max`` — so dwell never bleeds
    across a session cut and is robust to out-of-order input. Pure function
    over the already-computed raw label series, so it runs identically whether
    the caller is the batch loader (full session) or live serve (the buffered
    window re-segmented each tick): one state machine, not two.
    """
    ts_col = config.timestamp_column
    work = pd.DataFrame({"label": labels.to_numpy()}, index=frame.index)
    if ts_col in frame.columns:
        work["ts"] = pd.to_datetime(frame[ts_col], utc=True, errors="coerce").to_numpy()
    if config.session_column in frame.columns:
        groups: Iterable[tuple[object, pd.DataFrame]] = work.groupby(
            frame[config.session_column].to_numpy(), sort=False
        )
    else:
        groups = [(None, work)]
    out = labels.copy()
    for _, sub in groups:
        ordered = sub.sort_values("ts", kind="stable") if "ts" in sub.columns else sub
        out.loc[ordered.index] = _debounce_sequence(list(ordered["label"]), dwell_ticks)
    return out


def _debounce_sequence(values: list[str], dwell_ticks: int) -> list[str]:
    """N-consecutive dwell state machine over one ordered label sequence.

    The first row has no prior state, so it always takes its own raw label.
    After that, a new candidate only becomes the effective label once it has
    repeated ``dwell_ticks`` times in a row (counting the row that first
    differed from the effective label); a shorter run reverts to the
    effective label unchanged, same as a single-row spike being absorbed.
    """
    if not values:
        return values
    effective = values[0]
    candidate = values[0]
    streak = 1
    out = [effective]
    for value in values[1:]:
        if value == candidate:
            streak += 1
        else:
            candidate = value
            streak = 1
        if candidate != effective and streak >= dwell_ticks:
            effective = candidate
        out.append(effective)
    return out
