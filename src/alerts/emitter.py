"""Bridge inference results to the AnomalyEventLogger (Tasks 5.1+5.2 wire-up).

The Isolation Forest and LSTM autoencoder produce different shapes; this
module turns each into the unified ``AnomalyEvent`` and persists it through
``AnomalyEventLogger``. It owns:

* **emission policy** — *when* a result counts as an event. The LSTM has
  ``result.is_anomaly`` already; the IF labels rows ``-1`` (outlier) or
  ``1`` (inlier) plus the ``isolation_forest_missing_features`` flag for
  the dropout case. Both surface here as one method call.
* **dedupe** — back-to-back identical events from a noisy detector would
  spam the logger and the dashboard. A 1-second minimum gap per
  ``(source, model_version)`` is applied by default; turn it off with
  ``min_seconds_between_events=0`` for tests / replay.
* **counter accounting** — ``emitted`` / ``suppressed`` are observable so
  the daemon's `/status` endpoint (5.3) can surface them.
* **detector role** (A06.5) — an event from an ``evidence``-role detector
  (``models.roles``) is stored flagged ``role: evidence``, counted in
  ``emitted_evidence`` rather than ``emitted``, and never pushed.

Pure wiring; no torch / sklearn imports — kept narrow on purpose so the
daemon can construct one without touching the model layers.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

import pandas as pd

from src.alerts.event_logger import AnomalyEventLogger
from src.alerts.events import AnomalyEvent
from src.alerts.notifier import format_event_message
from src.alerts.tuning import AlertTuner

if TYPE_CHECKING:  # avoid importing torch-touching modules at import time
    from src.alerts.fusion import FusedResult
    from src.alerts.notifier import Notifier
    from src.config import DetectorRole
    from src.models.inference import SequenceInferenceResult

log = logging.getLogger(__name__)


@dataclass
class EmitterCounters:
    """Observable counts; rolling totals since the emitter was constructed."""

    emitted: int = 0
    suppressed_not_anomaly: int = 0
    suppressed_dedupe: int = 0
    #: Suppressed by an operator tuning rule (Task 6.25) — distinct from
    #: dedupe so the dashboard can show "tuning is muting N/hr" explicitly.
    suppressed_tuning: int = 0
    #: Over-threshold ticks withheld by the persistence gate (Phase 7.5) —
    #: the detector fired but hadn't fired for min_consecutive_ticks yet.
    suppressed_persistence: int = 0
    #: IF/LSTM ticks withheld because ``classify_vehicle_state`` said the
    #: engine wasn't running (Phase 1.5) — e.g. key-out, KOEO, no data.
    #: Behavior surfaces are unaffected; they keep their own mode-gating.
    skipped_vehicle_state: int = 0
    #: IF/sequence ticks withheld because the active capture profile (or,
    #: during a replay, the replayed session's) is not corpus-eligible
    #: (R03.12, operator decision 2026-09-24: tuning logs are not in the
    #: anomaly detectors). Counted only when vehicle state allowed the tick,
    #: so the two counters never double-count one tick.
    skipped_capture_profile: int = 0
    #: A06.5 — events stored from an ``evidence``-role detector. Counted
    #: apart from ``emitted`` because they are not alerts: the dashboard's
    #: "N emitted" and the alert budget both mean alerts.
    emitted_evidence: int = 0

    def reset(self) -> None:
        self.emitted = 0
        self.emitted_evidence = 0
        self.suppressed_not_anomaly = 0
        self.suppressed_dedupe = 0
        self.suppressed_tuning = 0
        self.suppressed_persistence = 0
        self.skipped_vehicle_state = 0
        self.skipped_capture_profile = 0

    def snapshot(self) -> EmitterCounters:
        """Independent copy — the base for :meth:`restore` (Phase 7.1)."""
        return replace(self)

    def restore(self, snapshot: EmitterCounters) -> None:
        """Load ``snapshot``'s values in place, preserving object identity
        (other code may hold a reference to this exact instance, e.g.
        ``GET /status``)."""
        self.emitted = snapshot.emitted
        self.emitted_evidence = snapshot.emitted_evidence
        self.suppressed_not_anomaly = snapshot.suppressed_not_anomaly
        self.suppressed_dedupe = snapshot.suppressed_dedupe
        self.suppressed_tuning = snapshot.suppressed_tuning
        self.suppressed_persistence = snapshot.suppressed_persistence
        self.skipped_vehicle_state = snapshot.skipped_vehicle_state
        self.skipped_capture_profile = snapshot.skipped_capture_profile

    def to_dict(self) -> dict[str, int]:
        """JSON-safe snapshot — what ``GET /status`` exposes per detector."""
        return {
            "emitted": self.emitted,
            "emitted_evidence": self.emitted_evidence,
            "suppressed_not_anomaly": self.suppressed_not_anomaly,
            "suppressed_dedupe": self.suppressed_dedupe,
            "suppressed_tuning": self.suppressed_tuning,
            "suppressed_persistence": self.suppressed_persistence,
            "skipped_vehicle_state": self.skipped_vehicle_state,
            "skipped_capture_profile": self.skipped_capture_profile,
        }


class AnomalyEventEmitter:
    """Convert per-detector results into ``AnomalyEvent`` and log them.

    Construct one per running detector (or share across both — the dedupe
    key is ``(source, model_version)`` so a single emitter feeds both
    safely). Not thread-safe; wrap with a lock if the LSTM and IF run on
    different threads, which they currently don't.
    """

    def __init__(
        self,
        logger: AnomalyEventLogger,
        *,
        min_seconds_between_events: float = 1.0,
        tuner: AlertTuner | None = None,
        notifier: Notifier | None = None,
        notify_min_severity: float = 0.8,
        notify_max_pushes_per_session: int = 3,
        roles: Mapping[str, DetectorRole] | None = None,
    ) -> None:
        self._logger = logger
        #: A06.5 — role per event source (``DetectorRolesConfig.by_source``).
        #: A source not named here is ``alert``. Applied in :meth:`_emit`, the
        #: one path every event takes, so no emit method can forget it. Held
        #: by reference, not copied: the daemon passes its
        #: ``DetectorRoleGate``, whose *effective* roles change when a
        #: hot-swap serves an artifact without an accepted alert-budget verdict.
        self._roles: Mapping[str, DetectorRole] = roles if roles is not None else {}
        self._min_seconds_between_events = min_seconds_between_events
        self._tuner = tuner
        self._last_emit: dict[tuple[str, str], float] = {}
        self.counters = EmitterCounters()
        #: Pre-replay snapshots, populated by :meth:`enter_replay_mode` and
        #: consumed by :meth:`exit_replay_mode` (Phase 7.1).
        self._pre_replay_counters: EmitterCounters | None = None
        self._pre_replay_last_emit: dict[tuple[str, str], float] | None = None
        #: Task 5.3 critical-alert hook. ``notifier`` is best-effort/optional
        #: (``None`` = notifications entirely off, the default); it never
        #: gates or delays emission — it only gets a fire-and-forget push
        #: after an event has already cleared every gate below.
        self._notifier = notifier
        self._notify_min_severity = notify_min_severity
        self._notify_max_pushes_per_session = notify_max_pushes_per_session
        self._notify_count = 0
        #: True for the duration of a replay (Phase 7.1's enter/exit_replay_mode)
        #: — the SAME replay-awareness the dedupe/counters snapshot uses, not a
        #: second mechanism. Replay must never push a live-looking notification
        #: for events that already happened.
        self._replaying = False

    def emit_lstm(
        self,
        result: SequenceInferenceResult,
        *,
        operating_mode: str | None = None,
    ) -> AnomalyEvent | None:
        """Emit an event from a streaming sequence-detector result, if anomalous + not deduped."""
        if not result.is_anomaly:
            self.counters.suppressed_not_anomaly += 1
            return None
        event = AnomalyEvent.from_lstm_result(result, operating_mode=operating_mode)
        return self._emit(event)

    def emit_isolation_forest_row(
        self,
        row: pd.Series,
        *,
        model_version: str,
        timestamp: str,
        affected_channels: tuple[str, ...] = (),
        session: str | None = None,
        score_column: str | None = None,
        label_column: str | None = None,
        missing_features_column: str | None = None,
        calibrated_anomaly_column: str | None = None,
        calibrated_threshold_column: str | None = None,
        mode_column: str = "operating_mode",
        feature_contributors: Mapping[str, float] | None = None,
        feature_contributors_reference: str | None = None,
    ) -> AnomalyEvent | None:
        """Emit an event from one row of a scored Isolation Forest frame.

        ``feature_contributors_reference`` (A04.2) says what the occlusion
        attribution replaced each feature with — ``mode_median`` or, on an
        artifact without recorded medians, ``pooled_mean`` — and is written
        beside the contributors as ``detail.feature_contributors_reference``.

        Anomaly rule (Phase 7.2): prefer the per-mode *calibrated* verdict
        (``isolation_forest_calibrated_anomaly``) when the row's mode was
        calibrated; otherwise fall back to the legacy contamination label
        (``-1``). The missing-features dropout flag forces anomalous in both
        cases. Rows that hit neither ``suppressed_not_anomaly`` and return
        ``None`` without persisting.

        A column argument left ``None`` means the Isolation Forest's own
        column name. They are resolved here rather than as default values
        because importing ``src.models.isolation_forest`` pulls in the whole
        feature pipeline, and this module is on the daemon's path to
        recording (S01.3); by the time a scored row exists it is loaded.
        """
        from src.models import isolation_forest as _if

        score_column = score_column or _if.SCORE_COLUMN
        label_column = label_column or _if.LABEL_COLUMN
        missing_features_column = missing_features_column or _if.MISSING_FEATURES_COLUMN
        calibrated_anomaly_column = calibrated_anomaly_column or _if.CALIBRATED_ANOMALY_COLUMN
        calibrated_threshold_column = calibrated_threshold_column or _if.CALIBRATED_THRESHOLD_COLUMN
        missing = bool(row.get(missing_features_column, False))
        calibrated = row.get(calibrated_anomaly_column)
        if calibrated is not None and pd.notna(calibrated):
            is_anomaly = missing or bool(calibrated)
        else:
            label = row.get(label_column)
            is_anomaly = missing or (label is not None and pd.notna(label) and int(label) == -1)
        if not is_anomaly:
            self.counters.suppressed_not_anomaly += 1
            return None
        raw_threshold = row.get(calibrated_threshold_column)
        threshold = (
            float(raw_threshold) if raw_threshold is not None and pd.notna(raw_threshold) else None
        )
        event = AnomalyEvent.from_isolation_forest_row(
            row,
            model_version=model_version,
            timestamp=timestamp,
            score_column=score_column,
            mode_column=mode_column,
            affected_channels=affected_channels,
            session=session,
            threshold=threshold,
            feature_contributors=feature_contributors,
        )
        if feature_contributors and feature_contributors_reference is not None:
            detail = dict(event.detail)
            detail["feature_contributors_reference"] = feature_contributors_reference
            event = event.model_copy(update={"detail": detail})
        if missing:
            # tag dropout vs ordinary outlier so the dashboard can render
            # the two cases differently — same source/model, different reason.
            detail = dict(event.detail)
            detail["missing_features"] = True
            event = event.model_copy(update={"detail": detail})
        return self._emit(event)

    def emit_fused(
        self,
        result: FusedResult,
        *,
        model_version: str,
        severity_threshold: float,
    ) -> AnomalyEvent | None:
        """Emit the fused event (Phase 7.2), if anomalous + not deduped/tuned.

        Goes through the same ``_emit`` gate as the per-source events, so
        dedupe and the operator tuner now also operate on the fused event —
        its ``(source="fused", model_version)`` dedupe key is independent of
        the per-detector keys, so keeping both streams doesn't cross-suppress.
        """
        if not result.is_anomaly:
            self.counters.suppressed_not_anomaly += 1
            return None
        event = AnomalyEvent.from_fused(
            result,
            model_version=model_version,
            severity_threshold=severity_threshold,
        )
        return self._emit(event)

    def emit_behavior(
        self,
        *,
        model: str,
        kind: str,
        channel: str,
        timestamp: str,
        severity_band: str,
        score: float,
        threshold: float,
        actual: float | None,
        expected: float | None,
        deviation: float | None,
        operating_mode: str | None = None,
        override_reason: str | None = None,
    ) -> AnomalyEvent | None:
        """Emit a behavior/physics residual event (Phase 7.4).

        First-class detector, kept alongside the per-source + fused events.
        Only ``warning`` / ``critical`` bands fire (``ok`` ⇒
        ``suppressed_not_anomaly``); dedupe keys on
        ``(source="behavior", "behavior/<model>")`` so each physical model
        debounces independently and never cross-suppresses another.

        ``override_reason`` (2.2) — set when an active accepted-baseline
        override recentered/graded this tick — passes straight through to
        ``AnomalyEvent.from_behavior``'s ``detail.override_reason``. An
        override whose ``better_direction: ignore`` side suppresses what
        would otherwise have been a symmetric-band alert never reaches this
        method at all (its severity is graded ``"ok"`` by
        ``behavior_decoder.deviation`` before the caller ever calls
        ``emit_behavior``) — there's no event to attach a reason to, so
        that suppression is logged instead, same convention as tuning
        suppression (see ``AlertTuner`` debug logging).
        """
        if severity_band not in ("warning", "critical"):
            self.counters.suppressed_not_anomaly += 1
            return None
        event = AnomalyEvent.from_behavior(
            model=model,
            kind=kind,
            channel=channel,
            timestamp=timestamp,
            severity_band=severity_band,
            score=score,
            threshold=threshold,
            actual=actual,
            expected=expected,
            deviation=deviation,
            operating_mode=operating_mode,
            override_reason=override_reason,
        )
        return self._emit(event)

    def emit_dtc(
        self,
        *,
        code: str,
        description: str,
        system: str,
        status: str,
        kind: Literal["new", "escalated"],
        timestamp: str,
        mil_on: bool | None = None,
        session: str | None = None,
    ) -> AnomalyEvent | None:
        """Emit a new/escalated DTC event (Phase 1.6).

        Deliberately skips the persistence gate every IF/LSTM/behavior
        result goes through: a DTC is already ECM-debounced (it only shows
        up here after the ECM's own confirm window), so there's nothing left
        to wait out — a genuinely new/escalated code goes straight to
        ``_emit`` (tuning + dedupe only), same low-level path as every other
        source.
        """
        event = AnomalyEvent.from_dtc(
            code=code,
            description=description,
            system=system,
            status=status,
            kind=kind,
            timestamp=timestamp,
            mil_on=mil_on,
            session=session,
        )
        return self._emit(event)

    def emit_misfire(
        self,
        *,
        cylinder: int,
        count: int,
        previous_count: int,
        timestamp: str,
        session: str | None = None,
    ) -> AnomalyEvent | None:
        """Emit a per-cylinder misfire-count-increase event (Task 3.1).

        Deliberately skips the persistence gate, same reasoning as
        ``emit_dtc``: the ECM has already debounced the Mode 06 counter by
        the time it shows up in a poll response, so a genuine increase goes
        straight to ``_emit`` (tuning + dedupe only).
        """
        event = AnomalyEvent.from_misfire(
            cylinder=cylinder,
            count=count,
            previous_count=previous_count,
            timestamp=timestamp,
            session=session,
        )
        return self._emit(event)

    def emit_trend(
        self,
        *,
        stat: str,
        session: str,
        timestamp: str,
        latest_value: float,
        reference_mean: float,
        reference_std: float,
        cusum_sh: float,
        cusum_sl: float,
        cusum_h: float,
        direction: Literal["upper", "lower"],
        mann_kendall_trend: str | None = None,
        mann_kendall_p_value: float | None = None,
    ) -> AnomalyEvent | None:
        """Emit a sustained-CUSUM-alarm trend event (Task 4.6).

        Same low-level path as every other source (tuning + dedupe via
        ``_emit``, no persistence gate — a CUSUM alarm is already the
        "sustained" signal, there's nothing left to debounce at the tick
        level here since this only runs once per session). The caller
        (``src.control.trends.evaluate_and_alert_trends``) is responsible
        for only calling this once per session per stat in the first place.
        """
        event = AnomalyEvent.from_trend(
            stat=stat,
            session=session,
            timestamp=timestamp,
            latest_value=latest_value,
            reference_mean=reference_mean,
            reference_std=reference_std,
            cusum_sh=cusum_sh,
            cusum_sl=cusum_sl,
            cusum_h=cusum_h,
            direction=direction,
            mann_kendall_trend=mann_kendall_trend,
            mann_kendall_p_value=mann_kendall_p_value,
        )
        return self._emit(event)

    def _emit(self, event: AnomalyEvent) -> AnomalyEvent | None:
        # Operator tuning gate (6.25) runs before dedupe: a tuned-out event
        # should never reach the logger/dashboard at all. Suppression-only by
        # contract — the tuner can mute, never manufacture.
        if self._tuner is not None:
            decision = self._tuner.decide(event, now=_seconds(event.timestamp))
            if not decision.emit:
                self.counters.suppressed_tuning += 1
                log.debug("tuning suppressed event", extra={"reason": decision.reason})
                return None
        if self._is_deduped(event):
            self.counters.suppressed_dedupe += 1
            return None
        event = self._apply_role(event)
        self._logger.log(event)
        if event.role == "evidence":
            self.counters.emitted_evidence += 1
        else:
            self.counters.emitted += 1
        self._last_emit[_dedupe_identity(event)] = _seconds(event.timestamp)
        if self._tuner is not None:
            self._tuner.notify_emitted(event, now=_seconds(event.timestamp))
        if event.role == "alert":
            self._maybe_push_notification(event)
        return event

    def _apply_role(self, event: AnomalyEvent) -> AnomalyEvent:
        """A06.5 — stamp the configured role. The stricter of the event's own
        role and the configured one wins: nothing promotes evidence to an
        alert."""
        if event.role == "evidence" or self._roles.get(event.source, "alert") == "alert":
            return event
        return event.model_copy(update={"role": "evidence"})

    def _maybe_push_notification(self, event: AnomalyEvent) -> None:
        """Task 5.3: fire-and-forget critical-alert push, AFTER dedupe/tuning
        gates above already decided the event is worth logging.

        Replay never notifies (same replay-awareness ``enter_replay_mode``/
        ``exit_replay_mode`` already give the dedupe/counters); a qualifying
        event must be at/above ``notify_min_severity`` and under the
        per-session push cap. A notifier failure can't reach here at all —
        ``Notifier.notify`` itself never raises.
        """
        if self._notifier is None or self._replaying:
            return
        if event.score < self._notify_min_severity:
            return
        if self._notify_count >= self._notify_max_pushes_per_session:
            return
        title, body = format_event_message(event)
        self._notifier.notify(title, body)
        self._notify_count += 1

    def reset_notification_count(self) -> None:
        """Reset the per-session push cap. Called at trip end (Task 5.4/5.5)
        so the next drive gets its own ``max_pushes_per_session`` budget."""
        self._notify_count = 0

    def _is_deduped(self, event: AnomalyEvent) -> bool:
        if self._min_seconds_between_events <= 0:
            return False
        prior = self._last_emit.get(_dedupe_identity(event))
        if prior is None:
            return False
        now = _seconds(event.timestamp)
        return (now - prior) < self._min_seconds_between_events

    def enter_replay_mode(self) -> None:
        """Snapshot live dedupe/counters, then start replay with a clean
        slate (Phase 7.1).

        Unlike :class:`PersistenceGate`/``LiveFeatureSource`` (a hard
        reset both ways), the emitter's counters are a running operational
        total the dashboard reads continuously — dropping them to zero for
        the replay's duration and then losing the live history entirely
        would misrepresent live operation. Snapshot + restore instead:
        replay gets its own clean dedupe/counter view, and live picks back
        up exactly where it left off once the replay ends.
        """
        self._pre_replay_counters = self.counters.snapshot()
        self._pre_replay_last_emit = dict(self._last_emit)
        self.counters.reset()
        self._last_emit = {}
        self._replaying = True

    def exit_replay_mode(self) -> None:
        """Restore the pre-replay live dedupe/counters (Phase 7.1).

        A no-op (beyond clearing replay's own state) if called without a
        matching :meth:`enter_replay_mode` — defensive, mirrors the writer/
        anomaly-logger ``exit_replay_mode`` best-effort contract.
        """
        if self._pre_replay_counters is not None:
            self.counters.restore(self._pre_replay_counters)
            self._pre_replay_counters = None
        if self._pre_replay_last_emit is not None:
            self._last_emit = self._pre_replay_last_emit
            self._pre_replay_last_emit = None
        self._replaying = False


def _dedupe_identity(event: AnomalyEvent) -> tuple[str, str]:
    """Key the dedupe/refractory window on (6.4): ``dedupe_key`` when the
    event sets one (currently only ``fused``, whose ``model_version`` embeds
    both backing models' versions and shouldn't churn the dedupe identity on
    a hot-swap), else the pre-6.4 ``(source, model_version)`` shape."""
    return (event.source, event.dedupe_key or event.model_version)


def _seconds(timestamp: str) -> float:
    """Coerce an event timestamp into an epoch-seconds float.

    Accepts the ISO-8601 strings the adapters produce (``..."+00:00"`` or
    ``..."Z"``); raises ``ValueError`` on garbage so the emitter doesn't
    silently accept timestamps that won't compare meaningfully.
    """
    cleaned = timestamp.replace("Z", "+00:00") if timestamp.endswith("Z") else timestamp
    dt = datetime.fromisoformat(cleaned)
    return dt.timestamp()


def __getattr__(name: str) -> Any:
    """``IsolationForestInferenceEngine`` moved to :mod:`src.models.inference`
    (R01.8), beside ``SequenceInferenceEngine``. Re-exported lazily so this
    module stays pure wiring with no model-layer import at load time."""
    if name == "IsolationForestInferenceEngine":
        from src.models.inference import IsolationForestInferenceEngine

        return IsolationForestInferenceEngine
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AnomalyEventEmitter",
    "EmitterCounters",
]
