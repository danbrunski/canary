"""The one wide -> model-ready feature transform, shared by batch and live.

Segment -> derived channels -> behavior deviations -> rolling features.
The offline builder, the live tick, the evaluation harness and the
perturbation fast path all call :func:`featurize_wide_frame`; it takes the
behavior models, DID catalog and config explicitly so no caller can inherit
a different default.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from src.behavior.behavior_decoder import append_deviation_columns
from src.behavior.behavior_models import BehaviorModels, load_behavior_models
from src.columns import MODE_COLUMN, base_channel
from src.config import CanaryConfig, require_profile_scoped
from src.decode.bindings import Bindings
from src.decode.dids import DidMap
from src.preprocessing.channel_units import append_did_views, channel_units_for
from src.preprocessing.features import FeatureConfig, compute_features, compute_features_last_row
from src.preprocessing.segmenter import SegmenterConfig, segment_operating_modes
from src.signals.afr import FlashHistory, append_afr_channels, load_afr_calibration
from src.signals.committees import append_committee_residuals, committee_channel_reason
from src.signals.derived import compute_anomaly_signals, compute_instantaneous_channels
from src.vehicle.profiles import active_flash_history

log = logging.getLogger(__name__)


#: Mode label used when no OBD PIDs are present and the threshold segmenter
#: can't run — everything becomes one mode and the model fits a single global
#: detector. Per-mode models materialize once OBD data exists.
ALL_MODE = "all"

#: OBD channels the operating-mode segmenter needs as inputs. ``coolant_temp``
#: is required for the cold-start / warm-idle split (see ``warmup`` /
#: ``idle`` modes in canary.example.yaml — without it the segmenter can't
#: tell a cold engine from a warm one and behavior models fit one surface
#: to both regimes, which is what caused the May 2026 R²=0.41 warmup mess.
#:
#: Deliberately NOT ``segmenter.DEFAULT_SIGNAL_COLUMNS``, despite the overlap:
#: ``have_inputs`` below requires *every* key here to resolve, and that table
#: carries optional signals (``imu:``) absent from all pre-Phase-2b data. Using
#: it would send every historical session to ``ALL_MODE``. This is the
#: *required* set; that one is the full candidate map. Keep the per-signal
#: candidate lists in step between them — a fallback added there and not here
#: silently does nothing on the training path.
SEGMENTER_SIGNAL_COLUMNS: dict[str, str | tuple[str, ...]] = {
    # Broadcast CAN sits last: ~98 Hz against Mode 01's 0.26-0.93 Hz, so it
    # fills the rows where the PID was stale rather than overriding it. See
    # segmenter.DEFAULT_SIGNAL_COLUMNS for the measured agreement and for why
    # those NaN rows mattered (they became the degenerate `unknown` mode).
    "rpm": ("obd:RPM", "decoded:ECM_RPM"),
    # OBD wheel-speed PID is the reliable primary; GPS is the back-stop when OBD
    # is absent, coalesced row-wise (see segmenter). GPS↔OBD disagreement is a
    # sensor-committee residual, not a coalesce winner.
    "speed": ("obd:SPEED", "decoded:WHEEL_SPEED_6F7_C", "gps:speed"),
    "throttle": "obd:THROTTLE_POS",
    "coolant_temp": ("obd:COOLANT_TEMP", "decoded:ECM_COOLANT_TEMP"),
}

#: Pre-R04.14 private name, kept for its importers.
_SEGMENTER_SIGNAL_COLUMNS = SEGMENTER_SIGNAL_COLUMNS


def segmenter_input_columns() -> tuple[str, ...]:
    """Every column :data:`SEGMENTER_SIGNAL_COLUMNS` names, flattened, in order,
    once each: what a bounded reader must load for mode labels to match the
    training path."""
    columns: list[str] = []
    for candidates in SEGMENTER_SIGNAL_COLUMNS.values():
        columns.extend((candidates,) if isinstance(candidates, str) else candidates)
    return tuple(dict.fromkeys(columns))


@dataclass(frozen=True)
class FeaturizedFrame:
    """Output of :func:`featurize_wide_frame`.

    ``features`` is the model-ready frame (rolling features appended).
    ``operating_frame`` is the *pre-feature* wide row set after segmentation
    and behavior-deviation columns but before ``compute_features`` — the live
    overlay reads its last row even during the feature-window warmup, so it is
    returned rather than recomputed.
    ``segmenter_fell_back`` (4.7) is ``True`` when :func:`segment_or_constant`
    used the constant ``"all"`` mode because the segmenter's *inputs* weren't
    present in this frame — every mode-gated surface silently never fires in
    that case. ``False`` both on normal segmentation and on an explicit
    ``preprocessing.single_operating_mode`` config (that's an intentional
    choice, not a diagnostic-worthy fallback).
    """

    features: pd.DataFrame
    operating_frame: pd.DataFrame
    segmenter_fell_back: bool = False


def configured_behavior_models(config: CanaryConfig) -> BehaviorModels | None:
    """The behavior models the daemon serves for ``config``: its scoped
    ``daemon.behavior_models``, or ``None`` when that file is absent (P02.12).

    The value every offline :func:`featurize_wide_frame` caller passes unless it
    means a different artifact set. Read once per call; hoist it out of loops.
    """
    require_profile_scoped(config, reader="behavior models (featurization)")
    path = config.daemon.behavior_models
    return load_behavior_models(path) if path.exists() else None


def featurize_wide_frame(
    wide: pd.DataFrame,
    config: CanaryConfig,
    *,
    behavior_models: BehaviorModels | None,
    did_map: DidMap | None,
    feature_config: FeatureConfig | None = None,
    bindings: Bindings | None = None,
    flash_history: FlashHistory | None = None,
    last_row_only: bool = False,
) -> FeaturizedFrame:
    """The single wide → model-ready feature transform (7.1 keystone).

    Both the offline batch path (:func:`build_feature_frame`, after
    read/decode/resample) and the online streaming path
    (``LiveFeatureSource.tick``, after observe/snapshot) call this so the
    schema-producing tail is *one* implementation, not two that must be kept
    byte-identical by hand. The head differs by necessity — batch reads and
    resamples a recorded session, streaming accumulates a current-value
    snapshot per tick — and the persisted feature spec plus
    :func:`reconcile_feature_frame_to_spec` are the contract that bridges any
    residual head divergence at serve time. This function is the part that
    must never diverge: ``segment_or_constant`` → ``append_deviation_columns``
    (when behavior models are loaded) → ``compute_features``.

    ``feature_config`` lets the streaming caller pass its once-built
    ``FeatureConfig`` instead of reconstructing it each tick; when omitted it
    is derived from ``config.preprocessing.rolling_window_seconds`` exactly as
    the batch path always did — the two are value-identical.

    ``bindings`` is forwarded to ``segment_or_constant`` so the ``decoded:``
    mode candidates are scaled with the catalog that decoded them; omitted, the
    accessor uses the repo ``data/bindings.yaml`` (identical to the daemon's
    ``dashboard.paths.DEFAULT_BINDINGS_PATH`` — asserted in
    tests/test_decoded_mode_signals.py, which is why the per-tick live path
    does not have to ship a catalog across the scoring-process boundary).

    B01.2 — behavior models are evaluated *before* ``compute_features``;
    ``append_deviation_columns`` builds the context their serving contracts
    read first (``__roc`` derivatives at this transform's rolling horizon,
    ``context:*`` engine-start evidence). The live path's
    :class:`~src.features.live.LiveFeatureSource` carries start
    evidence across ticks itself (its ring buffer is far shorter than a
    warmup) and has already written those columns into ``wide``.

    P02.11 — ``config`` must be profile-scoped while profiles are enabled
    (:func:`~src.config.require_profile_scoped`): the derived channels read the
    vehicle's ``afr_calibration.yaml``, which only the scoped config names.

    P02.12 — ``behavior_models`` has no default. A default of ``None`` let the
    offline callers featurize without the deviations the daemon appends, so
    each caller states its set: :func:`configured_behavior_models` for the
    served one, the live source's hot-reloaded copy, or ``None`` on purpose.

    P02.14 — ``did_map`` is the DID catalog ``wide`` was decoded with, and it
    has no default either. The DID views (``derived:BASE_FUEL_SCHEDULE``) and
    the target map's load-axis unit are judged against it. Read from the repo
    file instead, a frame decoded with a catalog that scales 0x1208 got a base
    fuel schedule scaled twice, silently. ``None``: no DID catalog decoded the
    frame, so no view and no DID-backed load axis.

    P02.16 — ``flash_history`` is where ``derived:TARGET_AFR`` reads each
    row's tune revision. ``None`` is the vehicle's own
    (:func:`~src.vehicle.profiles.active_flash_history`) at the row's stamp,
    right for every frame stamped when it was recorded. The live tick passes
    its source's, which reads a replayed row at its recorded instant.

    P02.9 — ``last_row_only`` rolls only the row ``compute_features`` would
    emit last (:func:`~src.preprocessing.features.compute_features_last_row`),
    so ``features`` has at most one row; ``operating_frame`` is unchanged. The
    live tick keeps one row of its ring buffer, and rolling the rest was about
    two fifths of the tick. Same windows, same columns and dtypes; ``__roc``
    bit-identical, mean/std within that function's declared tolerance.
    """
    require_profile_scoped(config, reader="featurize_wide_frame")
    framed, segmenter_fell_back = segment_or_constant(wide, config, bindings=bindings)
    framed = append_derived_channels(framed, config, did_map=did_map, flash_history=flash_history)
    cfg = resolve_feature_config(config, feature_config)
    if behavior_models is not None and behavior_models.models:
        framed = append_deviation_columns(
            framed, behavior_models, rate_window_seconds=cfg.window_seconds
        )
    roll = compute_features_last_row if last_row_only else compute_features
    features = roll(without_committee_columns(framed), config=cfg)
    return FeaturizedFrame(
        features=features, operating_frame=framed, segmenter_fell_back=segmenter_fell_back
    )


def resolve_feature_config(
    config: CanaryConfig, feature_config: FeatureConfig | None
) -> FeatureConfig:
    return feature_config or FeatureConfig(
        window_seconds=config.preprocessing.rolling_window_seconds
    )


def append_derived_channels(
    framed: pd.DataFrame,
    config: CanaryConfig,
    *,
    did_map: DidMap | None,
    flash_history: FlashHistory | None = None,
) -> pd.DataFrame:
    """Every derived channel :func:`featurize_wide_frame` appends between
    segmentation and the behavior deviations, in order: the one list both it
    and :func:`featurize_perturbed_frame` run."""
    # Derived anomaly signals (air mass per cycle, VE, bank-balance
    # asymmetries, total trim, catalyst efficiency, AFR/TPS cross-checks)
    # appended BEFORE behavior deviations + feature extraction so recipes can
    # target them as outputs and the rolling feature builder picks up their
    # derivatives. `vehicle` unlocks VE_PERCENT (Task 4.1), which needs the
    # configured displacement.
    # P02.11: the scoped config is the only one naming the vehicle's file; an
    # unscoped one reads as "no calibration" and drops channels silently.
    require_profile_scoped(config, reader="featurization (afr_calibration)")
    afr_calibration = (
        load_afr_calibration(config.daemon.afr_calibration)
        if config.daemon.afr_calibration.exists()
        else None
    )
    # P02.14: a vehicle's DIDs read in an engineering unit their catalog entry
    # does not carry (the Xterra's base fuel schedule in ms), before the
    # target lookup that reads one of them as its load axis.
    framed = append_did_views(framed, did_map=did_map)
    # P02.13: the load axis's unit comes from the channel catalogs, which
    # src.signals cannot import, so it is resolved here.
    # P02.16: each row's target map is the one flashed when the row was
    # recorded, from the vehicle's flash history. One resolver for the live
    # tick (rows stamped now: the running revision) and offline frames that
    # concatenate sessions from either side of a flash.
    framed = append_afr_channels(
        framed,
        config.vehicle.parameters,
        afr_calibration,
        channel_units=channel_units_for(did_map),
        flash_history=active_flash_history(config) if flash_history is None else flash_history,
    )
    framed = compute_anomaly_signals(framed, config.vehicle.parameters)
    # Performance channels (Task 1.1): fuel rate, instant economy,
    # boost/vacuum, estimated power/torque. Row-local only — the cumulative
    # trip integrals (FUEL_USED_L, TRIP_DISTANCE_KM, ...) are deliberately
    # NOT computed here, because this frame is a live ring-buffer *window*
    # on the streaming path, not a full session; a cumsum over it would
    # reset every time the window evicts old rows. See
    # ``compute_instantaneous_channels`` and ``LiveFeatureSource``'s
    # session-scoped accumulators (Task 1.3) for the integrated versions.
    # `calibration` is not optional decoration: PID 0x44 is φ on some ECMs and
    # λ on others, the two are reciprocals, and they agree at stoich — so
    # guessing is silent at idle and worst under enrichment, exactly where the
    # fuel estimate matters. Passing it lets the fuel math use the verified
    # convention instead of hard-coding λ.
    framed = compute_instantaneous_channels(
        framed, config.vehicle.parameters, calibration=afr_calibration
    )
    # Sensor-committee channels (H04.1): the member transforms and the spread
    # residuals of redundant sensors of one quantity (e.g. OBD↔GPS speed).
    # Committees are detectors, not features — they stay in the operating
    # frame for the committee evaluator and the fingerprint, and are never
    # rolled into the feature frame (see signals.committees).
    framed = append_committee_residuals(framed)
    return framed


def without_committee_columns(framed: pd.DataFrame) -> pd.DataFrame:
    """``framed`` without the sensor-committee columns: committees are
    detectors, not features, so they are never rolled (H04.1)."""
    committee_columns = [
        column
        for column in framed.columns
        if committee_channel_reason(base_channel(str(column))) is not None
    ]
    return framed.drop(columns=committee_columns)


def segment_or_constant(
    resampled: pd.DataFrame, config: CanaryConfig, *, bindings: Bindings | None = None
) -> tuple[pd.DataFrame, bool]:
    """Apply the operating-mode segmenter when its inputs exist, else add a
    constant ``operating_mode = "all"`` column so a single model is fit.

    Returns ``(frame, fell_back)`` — ``fell_back`` (4.7) is ``True`` only for
    the *runtime* fallback (segmenter input columns missing from this frame),
    not for an explicit ``single_operating_mode`` config, which is an
    intentional choice rather than a diagnostic-worthy degradation. Callers
    that care about the constant-mode outcome but not *why* can ignore the
    second element.

    ``bindings`` is the catalog the ``decoded:`` candidates in
    ``SEGMENTER_SIGNAL_COLUMNS`` are scaled with — pass the same one the
    frame was *decoded* with (``build_feature_frame`` does, so ``--bindings``
    reaches the mode rules too). Omitted, the shared accessor uses the repo's
    ``data/bindings.yaml``, which is what every production decode path loads.
    """
    if config.preprocessing.single_operating_mode:
        out = resampled.copy()
        out[MODE_COLUMN] = ALL_MODE
        return out, False
    seg_cfg = SegmenterConfig(
        signal_columns=dict(SEGMENTER_SIGNAL_COLUMNS),
        recent_motion_window_seconds=config.preprocessing.idle_transient_lookback_seconds,
        mode_dwell_ticks=config.preprocessing.mode_dwell_ticks,
        bindings=bindings,
    )
    # A signal is available when *any* of its candidate columns is present, so
    # the speed gate is satisfied by gps:speed or obd:SPEED.
    have_inputs = all(
        _signal_present(resampled, value) for value in seg_cfg.signal_columns.values()
    )
    if have_inputs:
        segmented = segment_operating_modes(
            resampled, config.preprocessing.operating_modes, config=seg_cfg
        )
        return segmented, False
    out = resampled.copy()
    out[MODE_COLUMN] = ALL_MODE
    log.info("no OBD PID columns present; using a single operating mode", extra={"mode": ALL_MODE})
    return out, True


def _signal_present(frame: pd.DataFrame, value: str | tuple[str, ...]) -> bool:
    """True when at least one of a segmenter signal's candidate columns exists."""
    columns = (value,) if isinstance(value, str) else value
    return any(column in frame.columns for column in columns)
