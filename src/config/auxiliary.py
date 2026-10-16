"""The small independent sections: trends, notifications, online training,
non-volatile write interlocks, tuning readiness, profiles, auto-tagging,
LLM assist, plausibility, committees and the live twin (R21).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field

from src.config.base import _Strict
from src.paths import ANALYSES_DIR, CONTROL_DIR


class TrendsConfig(_Strict):
    """Task 4.4 trend-test parameters (``src.analysis.trends``). All defaulted
    to the textbook values so configs predating this task keep loading."""

    #: Tabular-CUSUM slack ``k``, in reference-std units — half the shift
    #: size worth detecting.
    cusum_k: float = Field(default=0.5, ge=0.0)
    #: Tabular-CUSUM decision interval ``h``, in reference-std units — the
    #: alarm threshold. 4-5 sigma is the usual textbook range.
    cusum_h: float = Field(default=5.0, gt=0.0)
    #: Mann-Kendall two-tailed significance level.
    mann_kendall_alpha: float = Field(default=0.05, gt=0.0, lt=1.0)


class NotificationsConfig(_Strict):
    """Push notifications via ntfy (Phase 5, eval-longitudinal-and-dtc.md).

    Ships disabled with no real topic baked in (operator-approved decision,
    see OVERNIGHT-QUEUE.md Q12) — a made-up ``topic`` would either silently
    no-op against ntfy.sh or, worse, land on a stranger's public topic.
    Turning this on for the operator's actual ntfy instance is an explicit
    morning task, not something to flip during unattended work. All fields
    default so configs predating this section keep loading with
    notifications off.
    """

    enabled: bool = False
    #: Base ntfy server URL (self-hosted or the public ``ntfy.sh``).
    ntfy_url: str = "https://ntfy.sh"
    #: ntfy topic to POST to. Blank until the operator picks a real one.
    topic: str = ""
    #: Minimum ``AnomalyEvent.score`` (same larger-is-worse scale each source
    #: already uses) that reaches a push (5.3's critical-alert hook).
    min_severity: float = Field(default=0.8, ge=0.0)
    #: Hard cap on pushes per recording session — a flapping detector must
    #: never turn into a phone full of notifications.
    max_pushes_per_session: int = Field(default=3, ge=0)
    #: End-of-drive digest push (5.5) — independent toggle from the
    #: per-event critical-alert hook above.
    digest_enabled: bool = False


class OnlineTrainingConfig(_Strict):
    """Config-gated scheduled retrain (Task 3.2, online-training-strategy.md).

    **Ships disabled** — a hard pre-made decision (queue Round-2): enabling
    unattended scheduled retrains against the real corpus is an explicit
    operator action, never an unattended default. When enabled, the daemon's
    ``run_online_training_scheduler`` background task (mirrors
    ``run_retention_sweeper``'s asyncio-task-on-a-cadence shape) submits a
    ``retrain_all`` job through the existing ``JobController`` every
    ``interval_seconds`` — the same subprocess-isolated mechanism the
    dashboard's manual "Retrain" button already uses, so a scheduled retrain
    never runs on the daemon's own event loop or blocks real-time capture.
    The job is windowed to the trailing ``window_days`` of sessions
    (``--since``, Task 3.1) — a genuine sliding window, not a fixed corpus —
    and the scheduler never adds ``--no-calibration-holdout``: a window too
    small to spare a calibration holdout is refused by ``retrain.py`` itself
    (Task 3.1) and logged as a skip, never degraded to in-sample calibration.
    """

    enabled: bool = False
    #: Seconds between scheduled-retrain attempts.
    interval_seconds: float = Field(default=86400.0, gt=0.0)
    #: Rolling-window length in days passed to ``retrain.py --since``.
    window_days: int = Field(default=30, gt=0)
    #: How often the scheduler re-checks a submitted job's status while
    #: waiting for it to finish (a cheap in-memory dict lookup — this is not
    #: additional training, just how promptly the loop notices completion).
    poll_interval_seconds: float = Field(default=5.0, gt=0.0)


class NonvolatileWriteConfig(_Strict):
    """Interlocks for the future ECU-programming path (W07).

    These defaults intentionally refuse uncertain or marginal supply/state
    conditions. No programming transport exists yet; the config is consumed
    by the mandatory authorization gate that must precede one.
    """

    authorization_ttl_seconds: float = Field(default=120.0, gt=0.0, le=600.0)
    max_signal_age_seconds: float = Field(default=5.0, gt=0.0, le=10.0)
    stationary_speed_max_kph: float = Field(default=0.5, ge=0.0, le=2.0)
    min_battery_voltage: float = Field(default=12.0, gt=0.0)
    min_ups_soc_percent: float = Field(default=40.0, ge=0.0, le=100.0)
    ups_runtime_margin_seconds: float = Field(default=120.0, ge=0.0)


class TuningReadinessConfig(_Strict):
    """Floors for the ``/status.tuning_readiness`` chips (W02.4).

    Status only: nothing refuses an action on these. The flash interlocks
    keep their own floors in ``nonvolatile_write``.
    """

    min_ups_soc_percent: float = Field(default=40.0, ge=0.0, le=100.0)


class ProfilesConfig(_Strict):
    """Multi-vehicle profile resolution (see ``src/vehicle/profiles.py``).

    When the Pi is plugged into a vehicle, the daemon reads the VIN
    (OBD-II Mode 09 PID 0x02) at startup and resolves it to a profile slug,
    then scopes every vehicle-specific path under ``<base>/<slug>/`` so two
    vehicles never share a training corpus or model set. All fields default so
    configs predating profiles keep loading; with the probe disabled or no VIN
    available the ``vehicle:`` block is used as the fallback/default profile.
    """

    enabled: bool = True
    #: VIN → profile registry (a daemon-managed *data* file, not canary.yaml).
    #: Not scoped per vehicle — it is the index that maps VIN to slug.
    registry_path: Path = Path("profiles.yaml")
    #: What this machine learned about its vehicles (display names, VINs seen,
    #: calibration ID / CVN), merged over ``registry_path`` by slug (S03.2).
    #: The tracked registry holds the laptop-authored vehicle parameters and is
    #: never written at runtime. Under ``daemon.state_dir`` when that is set.
    registry_overlay_path: Path = CONTROL_DIR / "profiles.yaml"
    #: Persist a freshly-seen VIN to the registry automatically (with a blank
    #: display name for the operator to fill in via the dashboard).
    auto_register: bool = True
    #: How long the startup VIN probe waits for the ECU's Mode 09 response.
    vin_probe_timeout_seconds: float = 2.0


class AutoTagConfig(_Strict):
    """Thresholds for the motion-aware auto-tagger (Phase 2a.5/2a.6,
    docs/roadmap/imu-gps-integration.md). One taxonomy, one config block:
    both the offline pass (``scripts/autotag.py``) and the live daemon hook
    evaluate the exact same thresholds, so tuning them (as real drives
    accumulate) never needs a redeploy — same philosophy as the behavior
    models' deviation bands.

    ``live_enabled`` is a **hard pre-made decision** (Round-2
    OVERNIGHT-QUEUE): ships ``False`` until Phase 2b hardware validation,
    independent of whether the offline pass is useful today (it is, for the
    GPS-only ``stopped``/``highway_cruise`` tags against existing sessions).
    """

    live_enabled: bool = False
    #: g — decimated ``imu:ACCEL_LON_MAX`` threshold, disambiguated into
    #: accel vs. brake by ``d(obd:SPEED)/dt``'s sign (IMU + OBD, no GPS).
    hard_accel_g: float = 0.35
    hard_brake_g: float = 0.40
    #: g — decimated ``imu:ACCEL_LAT_MAX`` threshold for ``cornering``.
    cornering_g: float = 0.30
    #: km/h — a launch's starting speed must be at/below this ("near-stop").
    launch_speed_max_kph: float = 5.0
    #: seconds a qualifying hard_accel must sustain from near-stop to count
    #: as ``launch`` rather than an ordinary hard_accel.
    launch_sustain_seconds: float = 1.5
    #: km/h — ``gps:speed`` at/below this counts as ``stopped`` (GPS-only,
    #: bus-independent — works even with no OBD/CAN fix at all).
    stopped_speed_max_kph: float = 3.0
    #: km/h — ``gps:speed`` at/above this, sustained, is a highway_cruise
    #: candidate.
    highway_speed_min_kph: float = 90.0
    #: seconds ``gps:speed`` must hold >= ``highway_speed_min_kph`` before
    #: ``highway_cruise`` tags.
    highway_sustain_seconds: float = 20.0
    #: g² — max variance of lon/lat accel allowed during the sustained-speed
    #: window (keeps a fast, bumpy/swervy stretch from reading as "cruise").
    #: Only applied when IMU accel columns are present; ignored otherwise.
    highway_max_accel_variance_g2: float = 0.02


class LLMAssistConfig(_Strict):
    """Feature flag for the optional LLM-assist tier (Task S.4,
    docs/roadmap/llm-assist-and-compute-roadmap.md). Default ``False``: no
    producer exists yet (the roadmap's Sequencing step 1 is seams-only), and
    nothing on the capture path reads this block — it's read only by the
    dashboard's read-only analysis routes deciding whether to surface
    ``src.analysis.llm_assist`` cards or the disabled/absent state.
    """

    enabled: bool = False
    #: Where a future producer appends cards and the dashboard endpoint
    #: reads them from (``src.analysis.llm_assist.store.LlmAssistCardStore``).
    cards_path: Path = ANALYSES_DIR / "llm_assist_cards.jsonl"


class PlausibilityConfig(_Strict):
    """Deterministic plausibility checks in the live tick (A05.9).

    Off by default: the per-tick cost has not been measured on the Pi
    against the missed-tick budget yet. When on, the vehicle's
    ``plausibility.yaml`` (profile data directory) is required — without it
    the evaluator is withheld and ``/status`` says why, rather than running on
    guessed tier intervals and confirmation policy. Detections are evidence
    records in ``<anomalies_dir>/plausibility-evidence.jsonl`` (replays:
    ``<anomalies_dir>/_replay/<id>.plausibility-evidence.jsonl``).
    """

    enabled: bool = False


class CommitteesConfig(_Strict):
    """Sensor committees in the live tick (H04.2).

    On by default: with no ``committees.yaml`` in the profile data directory
    every committee reports ``not_configured`` and nothing flags, so the file
    — not this switch — is what arms a vehicle. ``enabled: false`` removes the
    evaluator entirely and ``/status`` says so.
    """

    enabled: bool = True


class TwinLiveConfig(_Strict):
    """The target-phi twin on the live tick (E04.1).

    Off by default: E04.3 measures its per-tick cost on the Pi against the
    missed-tick and record-loss budget before live enablement. When on, the
    vehicle's ``target_phi_twin.yaml`` is required -- without it ``/status``
    says ``not_declared``. It never alerts and writes nothing.
    """

    enabled: bool = False
