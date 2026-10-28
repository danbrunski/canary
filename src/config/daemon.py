"""The ``dashboard:`` and ``daemon:`` sections: where the long-running daemon
reads and writes, and the tables that classify those paths (R21).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from src.config.base import _Strict
from src.paths import ANOMALIES_DIR, CONTROL_DIR, DATA_DIR


class DashboardConfig(_Strict):
    host: str
    port: int


class DaemonConfig(_Strict):
    """Where the long-running daemon reads / writes its sidecar artifacts.

    All fields are optional so existing ``canary.yaml`` files keep loading
    without edits; the defaults are sensible relative-to-cwd paths for the
    common WSL-dev case. Production deployments override them (the systemd
    unit will pin absolute paths under ``/var/lib/canary``).
    """

    anomalies_dir: Path = ANOMALIES_DIR
    models_dir: Path = Path("models")
    jobs_dir: Path = CONTROL_DIR / "jobs"
    audit_log: Path = CONTROL_DIR / "audit.jsonl"
    feedback_log: Path = CONTROL_DIR / "anomaly_feedback.jsonl"
    #: Vehicle maintenance / intervention ledger (Task 3.1,
    #: eval-longitudinal-and-dtc.md) — what physically happened to the
    #: truck, append-only, profile-scoped like the sibling paths below.
    vehicle_ledger: Path = CONTROL_DIR / "vehicle_ledger.jsonl"
    #: Frozen reference epoch for fingerprint drift trending (Task 4.3,
    #: eval-longitudinal-and-dtc.md) — per-stat mean/std/quantiles over a
    #: hand-picked set of healthy sessions. Written only by
    #: ``scripts/fingerprint.py freeze``; never auto-updated. Profile-scoped
    #: like the sibling ledger paths.
    reference_epoch: Path = CONTROL_DIR / "reference_epoch.json"
    training_ledger: Path = CONTROL_DIR / "training_runs.jsonl"
    tuning_config: Path = CONTROL_DIR / "tuning.yaml"
    tuning_audit: Path = CONTROL_DIR / "tuning_audit.jsonl"
    #: Persisted GPS trip odometer (cumulative distance + moving time). Survives
    #: daemon restarts so the System page shows lifetime "miles / time driven
    #: with the unit plugged in" rather than per-process totals.
    trip_state: Path = CONTROL_DIR / "trip_odometer.json"
    #: Task 4.6 idempotency marker — ``{stat: last_alerted_session_id}``. CUSUM
    #: state itself is recomputed fresh from the full fingerprint history on
    #: every evaluation (Task 4.4's design note: cheap at per-session
    #: frequency, avoids incremental-state staleness/corruption); this tiny
    #: file is the ONLY thing persisted, purely so a re-evaluation of the same
    #: already-alerted session never double-emits. Profile-scoped like the
    #: sibling ledger paths.
    trend_alert_state: Path = CONTROL_DIR / "trend_alert_state.json"
    #: Behavior-model surfaces (hand-edited / refit-written). A config field
    #: rather than a hardcoded path so it can be scoped per vehicle profile
    #: (see ``src/vehicle/profiles.py``). Rooted at the checkout like the sibling
    #: paths (R01.2), so a tool started from another directory reads the same
    #: file the daemon does.
    behavior_models: Path = DATA_DIR / "behavior_models.yaml"
    #: Directional, provenanced accepted-fault overrides (Task 1.3,
    #: online-training-strategy.md) — sibling to ``behavior_models`` above,
    #: same profile-scoping rationale (a second vehicle must never inherit
    #: this one's overrides). Optional: absent file ⇒ zero overrides
    #: (``load_accepted_baselines`` handles that gracefully).
    accepted_baselines: Path = DATA_DIR / "accepted_baselines.yaml"
    #: User-defined custom-PID formulas (Task 6.8, wired in by Task 2.6) —
    #: schema documented in ``custom_pids.example.yaml``. Optional: absent
    #: file ⇒ zero custom PIDs polled (``load_custom_pids`` handles that
    #: gracefully), same posture as ``behavior_models`` above.
    custom_pids: Path = DATA_DIR / "custom_pids.yaml"
    #: Operator-selectable collection modes, scoped per vehicle.
    capture_profiles: Path = DATA_DIR / "capture_profiles.yaml"
    #: Per-vehicle, per-tune AFR voltage transfer and target-table exports.
    afr_calibration: Path = DATA_DIR / "afr_calibration.yaml"
    #: Per-vehicle K-line facts (addresses, wake-up, framing, timing) for the
    #: CANary-HAT's native K-line path; absent means K-line is not configured.
    kline_profile: Path = DATA_DIR / "kline.yaml"
    #: Session catalog (Task 6.1, eval-longitudinal-and-dtc.md) — SQLite index
    #: over session sidecars. Rebuildable from disk at any time
    #: (``scripts/catalog.py rebuild``); never a second source of truth.
    #: Profile-scoped like the sibling ledger paths.
    catalog_db: Path = CONTROL_DIR / "catalog.db"
    #: Operational calibration state written from the dashboard: the flash
    #: registry ``append_flash`` extends, the verified RAM catalog and the last
    #: memory-verify outcomes. Left unset in YAML it resolves beside
    #: ``jobs_dir`` (``src.vehicle.profiles.operational_dir``), so a deployment that
    #: pins ``jobs_dir`` under ``/data/canary`` keeps this there too; the
    #: default below is where that lands for the default ``jobs_dir``.
    #: ``scope_config`` always sets it, per vehicle (R05.15: it used to be one
    #: directory inside ``jobs/`` shared by every profile).
    calibration_dir: Path = CONTROL_DIR / "calibration"
    #: Capture profiles saved or deleted on this machine, layered over the
    #: tracked ``capture_profiles`` catalog by name (S03.4). Operational state:
    #: the tracked catalog is never written at runtime.
    capture_profile_overrides: Path = CONTROL_DIR / "capture_profile_overrides.yaml"
    #: The ROM library (W08.3): plain flash images, content-addressed by their
    #: sha256 (``<sha256>.bin`` + ``<sha256>.json``), uploaded or read from the
    #: ECU. Proprietary bytes, so never in the checkout: under ``state_dir`` on
    #: the Pi, gitignored ``data/control/`` on the laptop, and per vehicle
    #: (``scope_config``).
    rom_library_dir: Path = CONTROL_DIR / "rom_library"
    #: Which ``rom_map.yaml`` describes a library image. ``active_profile``: the
    #: vehicle this daemon is scoped to, the only one a Pi in its own truck ever
    #: reads. ``calibration_id``: whichever profile's map lists the image's
    #: calibration ID, so a desk (``scripts/canary_desk.py``) or a companion
    #: opens another vehicle's ROM with that vehicle's map. Unset (``None``):
    #: ``calibration_id`` in companion mode, else ``active_profile``. Read it
    #: as ``CanaryConfig.rom_map_lookup``, which resolves that.
    rom_map_lookup: Literal["active_profile", "calibration_id"] | None = None
    #: Investigation memory (E01): evidence minted by analysis tools,
    #: investigations, predictions, interventions and outcomes, append-only
    #: (``src.evidence.ledger.MemoryLedger``). Per vehicle (``scope_config``);
    #: under ``state_dir`` on the Pi, gitignored ``data/control/`` on the laptop.
    memory_ledger: Path = CONTROL_DIR / "memory.jsonl"
    #: Sessions whose parquet footers the retention sweeper has read and found
    #: intact, each with a fingerprint of its chunks (S01.5). Persisted so a
    #: restart does not re-read every footer in the corpus: 70.5 s on the
    #: 2026-10-04 cold boot, inside a drive. A rebuildable cache, never a
    #: source of truth: missing or unreadable means every footer is read again.
    #: Per vehicle (``scope_config``).
    retention_footer_ledger: Path = CONTROL_DIR / "retention_footers.json"
    #: Root for everything the daemon writes at runtime (S03). When set, every
    #: path in :data:`DAEMON_STATE_LAYOUT` that the YAML does not set itself
    #: resolves under it, so nothing the Pi writes lands in the git checkout
    #: (where ``canary-update`` refuses tracked edits). ``None`` keeps the
    #: checkout-rooted defaults the laptop uses. The Pi sets ``/data/canary``.
    state_dir: Path | None = None
    #: Ceiling on the stored rows a whole-session *raw* read in the digest
    #: child may attempt (D03.6): a capture's tuning section (``read_channels``
    #: with no row filter) and the workbench jobs. Since P02.1 the rest of the
    #: digest reads part by part and is bounded by
    #: ``digest_span_budget_seconds`` instead. A session over the budget gets a
    #: ``digest_skipped.json`` naming the count and the budget instead of an
    #: attempt; ``0`` disables the check.
    #:
    #: ``None`` (the default) **derives** the budget from the child's
    #: address-space ceiling and the measured per-row peak, rather than
    #: stating a second number that can drift out of step with the first. A
    #: hand-picked 4 M sat against a 4 GiB ceiling that only covered ~2 M rows,
    #: so sessions in between passed the budget, spawned a child and died of
    #: ``MemoryError`` — safe, but it reported a memory anomaly where the
    #: honest answer was "this is simply larger than what fits".
    digest_row_budget: int | None = Field(default=None, ge=0)
    #: Ceiling, in seconds of recorded span (Parquet footer timestamps), on
    #: the session a digest or a ``classify`` job may attempt (P02.1). Their
    #: ``value_number`` chain holds one part of raw rows at a time plus the
    #: session's reduced cells and 1 Hz frames, so its peak follows the span,
    #: not the rows. ``None`` derives it from the child's address-space ceiling
    #: (``src.analysis.digest.digest_span_budget_seconds``: ~13.7 h for a
    #: 4 GiB child); ``0`` disables the check (the ceiling still applies).
    digest_span_budget_seconds: float | None = Field(default=None, ge=0)
    #: Hard ``RLIMIT_AS`` ceiling, in MiB, applied inside the digest child
    #: process before it touches any data. This is the backstop that holds
    #: even if the row budget is raised or the memory cost regresses: an
    #: over-budget digest then raises ``MemoryError`` *in the child*, which
    #: the parent reports, instead of the kernel picking an OOM victim (twice
    #: that victim was the daemon). ``None`` derives it from physical RAM —
    #: half of total, clamped to [1 GiB, 4 GiB] — which on the 8 GB Pi gives
    #: a 4 GiB child that still fits, beside the daemon's normal 150-650 MB,
    #: inside the unit's ``MemoryMax=6G``; the rlimit is the *catchable* half
    #: of that pair, since a cgroup kill is not an exception anyone can name.
    digest_address_space_mb: int | None = Field(default=None, gt=0)
    isolation_forest_artifact: str = "isolation_forest/model.joblib"
    lstm_artifact: str = "lstm_autoencoder/model.pt"
    tcn_artifact: str = "tcn_autoencoder/model.pt"
    model_poll_seconds: float = 30.0
    retention_sweep_seconds: float = 3600.0
    broadcaster_queue_size: int = 100
    control_token_env: str = "CANARY_CONTROL_TOKEN"

    @model_validator(mode="after")
    def _root_state_paths(self) -> Self:
        """Re-root every unset runtime path under ``state_dir`` (S03.1)."""
        if self.state_dir is None:
            return self
        for name, relative in DAEMON_STATE_LAYOUT.items():
            if name not in self.model_fields_set:
                setattr(self, name, self.state_dir / relative)
        return self


#: Where each runtime-written :class:`DaemonConfig` path goes under
#: ``state_dir`` (S03.1), before ``scope_config`` nests it per vehicle.
#: Trained artifacts sit with the models (operator decision 2026-10-04: they
#: are Pi-owned), control-plane state under ``control/``.
DAEMON_STATE_LAYOUT: dict[str, Path] = {
    "anomalies_dir": Path("anomalies"),
    "models_dir": Path("models"),
    "behavior_models": Path("models/behavior_models.yaml"),
    "jobs_dir": Path("control/jobs"),
    "audit_log": Path("control/audit.jsonl"),
    "feedback_log": Path("control/anomaly_feedback.jsonl"),
    "vehicle_ledger": Path("control/vehicle_ledger.jsonl"),
    "reference_epoch": Path("control/reference_epoch.json"),
    "training_ledger": Path("control/training_runs.jsonl"),
    "tuning_config": Path("control/tuning.yaml"),
    "tuning_audit": Path("control/tuning_audit.jsonl"),
    "trip_state": Path("control/trip_odometer.json"),
    "trend_alert_state": Path("control/trend_alert_state.json"),
    "catalog_db": Path("control/catalog.db"),
    "custom_pids": Path("control/custom_pids.yaml"),
    "capture_profile_overrides": Path("control/capture_profile_overrides.yaml"),
    "rom_library_dir": Path("control/rom_library"),
    "memory_ledger": Path("control/memory.jsonl"),
    "retention_footer_ledger": Path("control/retention_footers.json"),
}


#: :class:`DaemonConfig` paths that name version-controlled data the daemon
#: only reads. Every ``Path`` field is in exactly one of these two tables or
#: is ``state_dir``/``calibration_dir`` (``tests/test_state_dir.py``), so a new
#: field cannot default into the checkout unclassified.
DAEMON_TRACKED_PATHS: frozenset[str] = frozenset(
    {"accepted_baselines", "afr_calibration", "capture_profiles", "kline_profile"}
)
