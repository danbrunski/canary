"""The ``models:`` and ``alerts:`` sections: detectors, their training knobs,
fusion, roles and the emission policy they share (R21).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from src.config.base import _Strict


class IsolationForestConfig(_Strict):
    enabled: bool
    n_estimators: int
    contamination: float
    per_mode_contamination: dict[str, float] = Field(default_factory=dict)
    random_state: int = 42
    #: Phase 7.2 — fraction of each mode's clean training rows, time-ordered
    #: from the tail, held out (not fit on) and used purely to calibrate the
    #: per-mode score threshold. ``0`` disables calibration (legacy
    #: contamination-label decision only).
    calibration_holdout_fraction: float = Field(default=0.2, ge=0.0, lt=1.0)
    #: Target fraction of *nominal* held-out ticks the per-mode threshold is
    #: allowed to flag — the calibrated decision boundary is the
    #: ``1 - target_flag_rate`` quantile of held-out scores. This replaces the
    #: assumption that exactly ``contamination`` of nominal driving is anomalous.
    target_flag_rate: float = Field(default=0.001, gt=0.0, le=0.5)
    #: Task 2.2 — a mode with fewer than this many *fit* rows (post-holdout
    #: split) is skipped entirely rather than fitting a full forest on a
    #: handful of rows: no model persisted, listed in the manifest's
    #: ``skipped_modes`` instead.
    min_samples_per_mode: int = Field(default=100, ge=0)
    #: Parallel tree builders. A small fixed default avoids both the historical
    #: single-core fit and ``-1`` oversubscription on memory-constrained WSL.
    n_jobs: int = 4
    max_dropped_row_fraction: float = Field(default=0.75, ge=0.0, lt=1.0)
    allow_unknown_only: bool = False
    #: A04.4 — a mode model resting on fewer independent physical drives than
    #: this (training rows) is refused, and so is one whose threshold would
    #: rest on fewer calibration drives (``retrain.py --accept-thin-calibration
    #: REASON`` publishes it anyway and stamps the reason). The 2026-09-20
    #: city/highway models came from one day; idle/warmup thresholds from one
    #: session.
    min_training_drives_per_mode: int = Field(default=3, ge=0)
    min_calibration_drives_per_mode: int = Field(default=3, ge=0)
    #: A04.10 — extra seeds refit per mode to measure which calibration rows
    #: the threshold flags seed-to-seed (pairwise Jaccard, in the manifest).
    #: 0 disables the measurement.
    stability_seeds: int = Field(default=4, ge=0)

    @field_validator("n_jobs")
    @classmethod
    def _nonzero_n_jobs(cls, value: int) -> int:
        if value == 0:
            raise ValueError("n_jobs must not be zero")
        return value


class AlertsConfig(_Strict):
    """Emission policy shared by every detector (Phase 7.5; M-of-N 6.1).

    All defaulted so configs predating it keep loading.
    """

    #: A detector (IF / sequence / each behavior surface) must be over its
    #: threshold for at least this many ticks WITHIN the trailing
    #: ``persistence_window_ticks`` (M-of-N, 6.1) before it emits an event or
    #: counts as a firing fusion source. Real faults persist; a lone tick is
    #: the historical flood. 1 = legacy fire-on-first-tick behavior. Named
    #: ``min_consecutive_ticks`` from its pre-6.1 strict-N-consecutive
    #: shape; kept for config compatibility even though it's now the M in
    #: M-of-N (``persistence_window_ticks`` defaults to this same value,
    #: which reproduces the old strict-consecutive gate exactly).
    min_consecutive_ticks: int = Field(default=3, ge=1)
    #: N — the trailing window (in ticks) the M hits above are counted
    #: within. Must be >= min_consecutive_ticks. Default 5 against the
    #: default M=3 gives the roadmap's "3-of-5" shape out of the box —
    #: tolerates one fire-gap-fire flicker per window without weakening the
    #: 3 hits still required (flood protection unchanged). ``None`` falls
    #: back to ``min_consecutive_ticks`` itself (strict-consecutive, the
    #: exact pre-6.1 gate) for anyone who explicitly wants that shape back.
    persistence_window_ticks: int | None = Field(default=5, ge=1)
    #: T — wall-clock bound (seconds) on the same window: an entry older
    #: than this many seconds from "now" is evicted regardless of tick
    #: count, so a long data/replay gap can't let stale hits from before the
    #: gap count toward a streak once ticks resume. ``None`` disables the
    #: time bound (tick-count-only, the pre-6.1 shape).
    persistence_window_seconds: float | None = Field(default=8.0, gt=0.0)
    #: Per-(source, model) refractory window between emitted events. The old
    #: 1.0 s default was a no-op at the 1 Hz tick — a sustained deviation
    #: emitted ~3600 events/hour per source.
    min_seconds_between_events: float = Field(default=30.0, ge=0.0)

    @model_validator(mode="after")
    def _check_window(self) -> AlertsConfig:
        if (
            self.persistence_window_ticks is not None
            and self.persistence_window_ticks < self.min_consecutive_ticks
        ):
            raise ValueError(
                "persistence_window_ticks must be >= min_consecutive_ticks "
                f"({self.persistence_window_ticks} < {self.min_consecutive_ticks})"
            )
        return self


class FusionConfig(_Strict):
    """Phase 7.2 — per-tick fusion of IF + LSTM + behavior residuals into one
    agreement-aware calibrated severity. All defaulted so configs predating
    7.2 keep loading with fusion on.
    """

    enabled: bool = True
    #: Fused severity in [0, 1] at/above which a fused ``AnomalyEvent`` fires.
    severity_threshold: float = Field(default=0.5, gt=0.0, le=1.0)
    #: Added to the fused severity per *additional* agreeing source beyond the
    #: first (agreement-aware boost), before clamping to 1.0.
    agreement_boost: float = Field(default=0.25, ge=0.0, le=1.0)
    #: 6.2 (operator-approved decision, see OVERNIGHT-QUEUE.md "Fusion
    #: semantics"): the fused event is a real agreement gate, not
    #: OR-with-boost — ``is_anomaly`` requires at least this many
    #: *independent* firing sources (sibling surfaces sharing a ``group``
    #: still count as one), on top of ``severity_threshold``. A single
    #: source at any severity never sets the fused verdict alone; it still
    #: emits its own per-source event through the unrelated per-source path.
    min_agreement: int = Field(default=2, ge=1)
    #: Normalized severity a "warning" / "critical" behavior-deviation band
    #: contributes as a fusion source.
    behavior_warning_severity: float = Field(default=0.5, ge=0.0, le=1.0)
    behavior_critical_severity: float = Field(default=1.0, ge=0.0, le=1.0)


#: A06.5 — what a detector's events are allowed to do. ``alert``: the
#: historical behaviour (stored, shown as an active anomaly, eligible for an
#: ntfy push, and able to carry a fused event). ``evidence``: still scored and
#: stored, flagged ``role: evidence`` on the event record, and still counted
#: toward fusion agreement — but never pushed, never shown as an active
#: alert, and never the reason a fused event fires.
DetectorRole = Literal["alert", "evidence"]


class DetectorRolesConfig(_Strict):
    """A06.5 — per-detector role, the mechanism behind A06.1/A06.2.

    **Decided by the operator 2026-09-23** (A06.1, A06.2): both detectors
    are ``evidence``. The Isolation Forest scores and stores but never alerts
    on its own (A05's plausibility checks own gross single-channel faults);
    the sequence detector is a non-alerting novelty screen. The primary
    detector is the behavior models plus the deterministic layer (A06.4).
    Set a role to ``alert`` to restore the old behaviour for one detector.

    Enforced in two shared places, not by each detector: the
    ``AnomalyEventEmitter`` stamps the role on every event it stores and
    withholds pushes for ``evidence``, and ``FusionScorer`` lets an
    ``evidence`` source vote in agreement but requires an ``alert`` source
    before the fused verdict can fire. Behavior residuals and the
    deterministic sources (DTC, misfire, trend) are always ``alert``.
    """

    isolation_forest: DetectorRole = "evidence"
    #: The single sequence-detector slot, whichever backbone
    #: (``sequence_detector: lstm | tcn``) fills it — both publish the
    #: ``lstm_autoencoder`` event source.
    sequence: DetectorRole = "evidence"

    #: H04.2 sensor committees (``source: committee``). ``evidence`` until a
    #: committee has a confirmed real catch — the same bar the 2026-09-23
    #: decision set for the learned detectors; set ``alert`` to promote.
    #: Deterministic, so no artifact or alert-budget verdict applies: it is
    #: *not* in :meth:`by_source` (the artifact role gate would demote an
    #: ``alert`` with "no artifact loaded") but in :meth:`deterministic_by_source`.
    committee: DetectorRole = "evidence"

    def by_source(self) -> dict[str, DetectorRole]:
        """Role keyed by the ``AnomalyEvent.source`` / fusion-source name the
        detector publishes under — the only form the emitter and fusion need."""
        return {"isolation_forest": self.isolation_forest, "lstm_autoencoder": self.sequence}

    def deterministic_by_source(self) -> dict[str, DetectorRole]:
        """Roles of sources with no model artifact, applied as configured
        (never through the artifact role gate). Emitter-only: none of these is
        a fusion source."""
        return {"committee": self.committee}


class _SequenceTrainingConfig(_Strict):
    """What both sequence-detector backbones train, calibrate and score with.

    LSTM and TCN carried 17 identical copies of these fields (R03.10), so a
    new training knob could reach one backbone and not the other. Defaults
    are the TCN's; :class:`LSTMAutoencoderConfig` redeclares ``enabled``,
    ``sequence_length``, ``latent_dim`` and ``threshold_k`` as required.
    """

    enabled: bool = False
    sequence_length: int = 30
    latent_dim: int = 32
    dropout: float = 0.0
    batch_size: int = 64
    #: Overlapping training windows are highly redundant. Calibration remains
    #: dense by default so deployed thresholds retain one-tick resolution.
    training_stride: int = Field(default=5, ge=1)
    calibration_stride: int = Field(default=1, ge=1)
    #: ``None`` chooses a conservative platform default (zero on WSL). Worker
    #: processes can duplicate the full pandas-backed dataset.
    dataloader_workers: int | None = Field(default=None, ge=0)
    mixed_precision: bool = True
    compile_model: bool = False
    min_window_observed_fraction: float = Field(default=0.01, gt=0.0, le=1.0)
    min_usable_window_fraction: float = Field(default=0.9, gt=0.0, le=1.0)
    learning_rate: float = 0.001
    max_epochs: int = 100
    validation_fraction: float = 0.2
    early_stopping_patience: int = 10
    min_delta: float = 0.0
    random_state: int = 42
    threshold_k: float = 3.0
    #: When set (0–1), calibrate thresholds as this quantile of the
    #: reconstruction-error distribution instead of ``mean + k·σ``.
    #: Reconstruction errors are right-skewed and stride-1 windows heavily
    #: autocorrelated, so σ is a poor tail proxy; a quantile targets the
    #: alert budget directly (like the IF's ``target_flag_rate``). Task 3.2:
    #: defaults to 0.999 (the calibrated-FPR path) rather than ``None`` — the
    #: old ``None`` default silently meant ``mean + k·σ`` was every deploy's
    #: real threshold. Set explicitly to ``null`` in config to opt back into
    #: k·σ.
    threshold_quantile: float | None = Field(default=0.999, gt=0.0, lt=1.0)
    #: Feature columns to exclude from anomaly scoring. Each entry matches
    #: itself plus its rolling-derivative siblings (``__mean``, ``__std``,
    #: ``__roc``), so listing ``obd:FUEL_LEVEL`` silences all four. Applied
    #: at *train* time (excluded from ``feature_columns`` in the manifest,
    #: so the model never learns them) **and** at *serve* time (the live
    #: window's mask is forced to 0 at these indices, so a stale artifact
    #: that still includes them can't push the global score). Use for
    #: behavior signals — fuel level, HVAC blower, module voltage — that
    #: drift naturally over a drive without being a health signal.
    suppress_channels: tuple[str, ...] = ()


class LSTMAutoencoderConfig(_SequenceTrainingConfig):
    enabled: bool
    sequence_length: int
    latent_dim: int
    threshold_k: float
    hidden_size: int = 64
    num_layers: int = 1


class TCNAutoencoderConfig(_SequenceTrainingConfig):
    """Dilated causal TCN autoencoder — the alternate sequence-detector backbone.

    Fully defaulted (and ``enabled: false`` by default) so configs without a
    ``tcn_autoencoder`` block keep loading; selecting it requires both
    ``sequence_detector: tcn`` and ``enabled: true``.
    """

    # The training fields are ``_SequenceTrainingConfig``'s, shared with the
    # LSTM (R03.10). Not in the docstring: that is the JSON-schema description.
    channels: int = 64
    kernel_size: int = 3
    num_levels: int = 4
    #: Feed a bounded per-channel Δt decay as extra conv input channels (the
    #: GRU-D freshness gate). When false the TCN sees value+mask only.
    use_delta_decay: bool = True


class ModelsConfig(_Strict):
    isolation_forest: IsolationForestConfig
    lstm_autoencoder: LSTMAutoencoderConfig
    tcn_autoencoder: TCNAutoencoderConfig = Field(default_factory=TCNAutoencoderConfig)
    fusion: FusionConfig = Field(default_factory=FusionConfig)
    #: A06.5 — ``alert`` (default) or ``evidence`` per detector. The choice
    #: is the operator's (A06.1/A06.2); see :class:`DetectorRolesConfig`.
    roles: DetectorRolesConfig = Field(default_factory=DetectorRolesConfig)
    #: Which backbone fills the single sequence-detector slot. The selected
    #: backend's own ``enabled`` flag still gates whether it loads (so an
    #: existing ``lstm_autoencoder.enabled: false`` keeps the slot off, and
    #: ``tcn_autoencoder`` defaults to disabled). See
    #: ``src/models/sequence_registry.py``. 9.1 (operator-approved decision,
    #: see ``OVERNIGHT-QUEUE.md``): GRU-D LSTM is the shipped default — its
    #: GRU decoder reconstructs step-by-step from the latent, materially
    #: stronger temporal-shape modeling than the TCN decoder's single
    #: broadcast-latent reconstruction. TCN is documented as an NPU-export
    #: fallback, not an equal; no further bake-off gates this default.
    sequence_detector: Literal["lstm", "tcn"] = "lstm"
    #: A06.6 — forward-pass the sequence detector on every n-th tick. The
    #: window still advances every tick (it is state: an unappended row would
    #: be a hole in it), and the envelope check of the novelty screen still runs
    #: every tick; only the forward pass — the largest inference stage, a median
    #: 344 ms per tick on the Pi (2026-09-24) — is skipped in between. A skipped
    #: tick is a missing verdict, never a carried-over score. ``1`` is every
    #: tick. The cadence must let ``alerts.min_consecutive_ticks`` forward passes
    #: fit inside ``alerts.persistence_window_seconds`` (refused otherwise), and
    #: an artifact whose A02.3 alert level was chosen at another cadence refuses
    #: to serve.
    sequence_forward_every_n_ticks: int = Field(default=1, ge=1)
    #: Score IF + sequence in a child process rather than a worker thread.
    #: C08 moved scoring off the event loop into a thread, which is not the
    #: same as off the CPU: measured on the Pi 2026-09-09, the daemon missed
    #: 48 % of its 1 Hz ticks with 2.5 of 4 cores idle, because one Python
    #: process is GIL-bound to roughly one core and a slow forward pass
    #: starved the collector thread sharing it — collection stopped for ~100 s
    #: at a stretch.
    #:
    #: **Off by default, deliberately.** The child rebuilds its models from
    #: *this config's* artifact paths, so it only sees models that came from
    #: disk — a parent holding an injected or in-memory model (tests, and any
    #: caller that populates ``model_managers`` itself) would have the child
    #: score against something else, or nothing. Deployments where the models
    #: do come from disk opt in; nothing else changes behaviour. A host where
    #: the child cannot start falls back to in-process scoring, so this is a
    #: performance knob, never a correctness one.
    out_of_process_scoring: bool = False
    #: Build the live rolling feature row in one persistent spawned process.
    #: Unlike model scoring, this worker needs no artifact paths and is safe
    #: for every deployment: each request carries the exact config and
    #: behavior-model snapshot for that tick. Enabled by default so pandas'
    #: Python/GIL-heavy transform cannot starve collection or HTTP on the Pi.
    #: Set false for deterministic single-process debugging and unit fixtures.
    out_of_process_feature_build: bool = True
