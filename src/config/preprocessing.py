"""The ``preprocessing:`` section: resampling and operating-mode gates (R21)."""

from __future__ import annotations

from pydantic import Field

from src.config.base import _Strict


class OperatingModeSpec(_Strict):
    rpm_min: float | None = None
    rpm_max: float | None = None
    speed_min: float | None = None
    speed_max: float | None = None
    throttle_min: float | None = None
    throttle_max: float | None = None
    #: Coolant-temperature gates (°C). Lets the segmenter pull "cold engine
    #: warming up" out of the same RPM/speed band as warm steady-state idle —
    #: without this the behavior models fit one surface to both regimes and
    #: fire on every cold-start cycle. See ``warmup`` in
    #: ``canary.example.yaml`` for a representative threshold; the gating
    #: signal is ``obd:COOLANT_TEMP`` (degC).
    coolant_temp_min: float | None = None
    coolant_temp_max: float | None = None
    #: Recent-motion gates (km/h) on a derived ``recent_speed`` signal — the
    #: trailing max ground speed over ``idle_transient_lookback_seconds``. Lets
    #: the segmenter pull a *transient stop* (stopped now, but driving moments
    #: ago — a stop light) out of the same warm RPM/zero-speed band as a steady
    #: standing idle (driveway / pre-drive). Without it both collapse to one
    #: ``idle`` model that can't tell "stopped mid-drive" from "never moved".
    #: ``recent_speed`` is computed per session, so it never bleeds across
    #: sessions, and is ephemeral — it gates segmentation only, never a feature.
    recent_speed_min: float | None = None
    recent_speed_max: float | None = None
    #: Longitudinal/lateral accel gates (g) on the decimated
    #: ``imu:ACCEL_LON_MAX``/``imu:ACCEL_LAT_MAX`` channels (Phase 2a.3/2a.7,
    #: docs/roadmap/imu-gps-integration.md) — lets ``launch``/``hard_brake``/
    #: ``cornering`` modes gate on a transient IMU peak the same way other
    #: modes gate on RPM/speed/throttle. Inert (never required, never
    #: matched) until a real session actually has these columns — no IMU
    #: hardware exists yet, so every stock mode spec leaves these ``None``.
    accel_lon_min: float | None = None
    accel_lon_max: float | None = None
    accel_lat_min: float | None = None
    accel_lat_max: float | None = None


class PreprocessingConfig(_Strict):
    resample_hz: float
    rolling_window_seconds: int
    operating_modes: dict[str, OperatingModeSpec]
    #: Trailing window (s) for the segmenter's derived ``recent_speed`` signal
    #: (see ``OperatingModeSpec.recent_speed_*``). Sized to outlast a typical
    #: red light so a multi-minute stop stays ``idle_transient`` rather than
    #: decaying into standing ``idle``. The live ring buffer is grown to hold at
    #: least this many seconds so serve-time segmentation matches training.
    idle_transient_lookback_seconds: int = 180
    #: Skip per-mode segmentation entirely and fit/score a single model over a
    #: constant ``operating_mode = "all"``. Use when the corpus covers one
    #: regime, or when the OBD mode signals (RPM/SPEED/THROTTLE) are too sparse
    #: to segment reliably live — that sparsity otherwise produces train/serve
    #: skew where serving emits a mode the trainer never produced. See
    #: ``feature_transform.segment_or_constant``.
    single_operating_mode: bool = False
    #: Forward-fill ceiling (s) for slow channels (coolant/IAT/baro —
    #: ``resampler.DEFAULT_SLOW_CHANNEL_HINTS``) during resampling. Generous
    #: relative to fast channels' ``max_gap`` (interval × ``max_gap_factor``)
    #: because slow sensors legitimately update on the order of tens of
    #: seconds; this only catches a channel that's gone genuinely dead.
    #: Beyond the limit the resampled cell goes NaN and its ``__gap`` flag
    #: sets, same treatment fast channels already got.
    slow_channel_max_gap_seconds: float = 120.0
    #: A larger wall-clock discontinuity starts a new logical capture segment,
    #: even if both sides were accidentally written beneath one directory.
    #: Resampling across suspend/disconnect gaps manufactures thousands of
    #: all-stale rows and lets rolling features bridge unrelated drives.
    session_gap_seconds: float = Field(default=300.0, gt=0.0)
    #: Multiple of the resample interval a *fast* channel may go unobserved
    #: before its ffilled cell is NaN'd and its ``__gap`` flag sets.
    #:
    #: This is a claim about the achievable poll cadence, not the configured
    #: one. The default 2.0 (a 2 s ceiling at 1 Hz) assumes fast-tier PIDs
    #: really arrive at ~1 Hz; on a contended bus they do not — the 2026-07-26
    #: drive measured SPEED at 2.13 s and MAF at 4.46 s mean spacing against an
    #: 8 req/s budget carrying ~25 req/s of demand. Every cell past the ceiling
    #: becomes NaN, and because ``TrainedIsolationForest.score`` refuses any row
    #: with a NaN in any trained feature, that made the IF unable to score a
    #: single row (0 of 273 features reached 75% coverage; the Pi reported
    #: ``missing_feature_rate: 1.0``).
    #:
    #: Exposed as config (it was previously a hardcoded ``ResampleConfig``
    #: default) because it is preprocessing-parity-critical: it is recorded in
    #: the model manifest as ``max_gap_seconds`` and read by BOTH the offline
    #: resampler and ``LiveFeatureSource``, so train and serve move together.
    #: Changing it requires a retrain to take effect.
    #:
    #: The default stays at the historical 2.0 deliberately. Raising it was
    #: measured on 2026-07-26T164436 and does NOT rescue the IF: over 40 OBD
    #: channels, mean coverage went 11.7% (factor 2) → 20.9% (5) → 25.7% (10)
    #: → 30.0% (20), with **zero** channels reaching 50% at any setting. The
    #: shortfall is real poll cadence and Mode-01 suspension during DID
    #: windows, not an over-tight ceiling, so a looser ceiling only relabels
    #: stale data as fresh without making per-row scoring viable. Left tunable
    #: because it is a legitimate knob, not because raising it is the fix.
    max_gap_factor: float = 2.0
    #: Phase 5.2 — consecutive rows a candidate operating-mode label must hold
    #: before segmentation actually switches to it. ``1`` (default) is the
    #: legacy immediate-switch behavior. Without dwell, 1 Hz flapping at a
    #: mode boundary (e.g. city/highway right at the speed threshold)
    #: contaminates both models' training rows with a handful of mislabeled
    #: ticks each way. Mirrors ``AlertsConfig.min_consecutive_ticks``'s shape;
    #: see ``segmenter._apply_mode_dwell``.
    mode_dwell_ticks: int = Field(default=1, ge=1)
