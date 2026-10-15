"""Is a trained model's feature spec still satisfiable by the live pipeline?

D02.5, from the 2026-09-10 drive. Lives here rather than in
:mod:`src.daemon.inference` because both the daemon (which writes the verdict)
and the status payload (which reads it) need it, and the daemon already
imports from the dashboard.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from src.features.feature_spec import FeatureSpecReconciliation


def structural_blindness(report: FeatureSpecReconciliation) -> dict[str, Any] | None:
    """Whether a model's feature spec is unsatisfiable by the live pipeline.

    D02.5. ``reconcile_feature_frame_to_spec`` fills an absent expected column
    with NaN, and ``TrainedIsolationForest.score`` withholds any row whose
    missing fraction exceeds ``MAX_MISSING_FEATURE_FRACTION``. So a column the
    pipeline *cannot produce at all* costs the same fraction on **every** row:
    once the structurally-absent count alone clears the ceiling, the detector
    can never score anything, for any input, until it is retrained.

    That is a different thing from ordinary sparsity, and it wants a different
    answer — retrain, not wait. On the 2026-09-10 drive it read as a bare
    ``missing_feature_rate: 1.0`` with no cause attached: six of the July
    artifact's 88 features (``decoded:ECM_AIRFLOW_PROXY_23D`` and
    ``decoded:ECM_CALC_LOAD_B``, plus their rolling derivatives) had been
    renamed and retracted out of ``data/bindings.yaml`` on 2026-08-04/05, and
    6/88 = 6.8 % clears the 5 % ceiling on every row forever.

    Returns ``None`` when the spec is satisfiable. Deliberately not a refusal:
    a degraded detector beats a stalled collector, and the fail-safe already
    suppresses the emits. What was missing was *saying so*.
    """
    from src.models.isolation_forest import MAX_MISSING_FEATURE_FRACTION

    expected = len(report.expected_feature_columns)
    absent = report.missing_feature_columns
    if not expected or not absent:
        return None
    fraction = len(absent) / expected
    return {
        "absent_feature_columns": list(absent),
        "absent_count": len(absent),
        "expected_count": expected,
        "absent_fraction": round(fraction, 4),
        "withhold_ceiling": MAX_MISSING_FEATURE_FRACTION,
        # The verdict, so nobody has to redo this arithmetic against a 1.0.
        "scores_nothing": fraction > MAX_MISSING_FEATURE_FRACTION,
    }


#: Latest structural-blindness verdict per model type, for `/status` (D02.5).
#: Written by :func:`observe_reconciliation` on every report; absent when the
#: model's latest report reconciled with nothing missing and no column has
#: been absent throughout.
_STRUCTURAL_BLINDNESS: dict[str, dict[str, Any]] = {}


def structural_blindness_snapshot() -> dict[str, dict[str, Any]]:
    """Per-model-type blindness verdicts, for the status payload."""
    return {name: dict(value) for name, value in sorted(_STRUCTURAL_BLINDNESS.items())}


#: A column is only *structurally* absent once it has been absent from every
#: reconciliation over a sustained window. The first inference window after a
#: daemon start routinely lacks slow-tier PIDs (baro, coolant, IAT) and GPS
#: (no fix yet); on 2026-09-18 that startup gap was frozen into a 17-of-88
#: "structural" verdict for the whole drive. Both floors must be met.
STRUCTURAL_MIN_OBSERVED_SECONDS = 120.0
STRUCTURAL_MIN_REPORTS = 10


@dataclass
class _BlindnessTracker:
    """Per-model-type evidence behind a structural-blindness verdict.

    ``always_absent`` is the intersection of the absent sets of every report
    since the model version loaded: one sighting of a column proves the
    pipeline *can* produce it, so it can never be structural again.
    """

    model_version: str
    expected: tuple[str, ...]
    first_seen: float
    last_seen: float
    always_absent: set[str]
    currently_absent: tuple[str, ...]
    reports: int = 1
    established: bool = False
    established_columns: tuple[str, ...] | None = None
    established_scores_nothing: bool | None = None
    #: A04.5 — the modes blind when the verdict was last established.
    established_blind_modes: tuple[str, ...] | None = None

    @property
    def observed_seconds(self) -> float:
        return max(0.0, self.last_seen - self.first_seen)

    def ready(self) -> bool:
        return (
            self.observed_seconds >= STRUCTURAL_MIN_OBSERVED_SECONDS
            and self.reports >= STRUCTURAL_MIN_REPORTS
        )


_TRACKERS: dict[str, _BlindnessTracker] = {}


@dataclass(frozen=True)
class BlindnessUpdate:
    """What one report changed, so the caller can log exactly once per change."""

    model_type: str
    verdict: dict[str, Any] | None
    #: ``"established"`` the first time the window fills, ``"changed"`` when an
    #: established structural set or ``scores_nothing`` later moves; else None.
    transition: str | None


def observe_reconciliation(
    model_type: str,
    model_version: str,
    report: FeatureSpecReconciliation,
    *,
    now: float | None = None,
) -> BlindnessUpdate:
    """Fold one reconciliation report into ``model_type``'s blindness evidence.

    Publishes the per-model verdict to :data:`_STRUCTURAL_BLINDNESS` (read by
    ``/status``). Until the window is sustained the verdict says
    ``status: "pending"`` and ``scores_nothing: None`` — "not known yet" is a
    different empty from "satisfiable" and from "blind".
    """
    from src.models.isolation_forest import MAX_MISSING_FEATURE_FRACTION

    ts = time.monotonic() if now is None else float(now)
    absent = tuple(str(column) for column in report.missing_feature_columns)
    tracker = _TRACKERS.get(model_type)
    if tracker is None or tracker.model_version != model_version:
        tracker = _BlindnessTracker(
            model_version=model_version,
            expected=tuple(report.expected_feature_columns),
            first_seen=ts,
            last_seen=ts,
            always_absent=set(absent),
            currently_absent=absent,
        )
        _TRACKERS[model_type] = tracker
    else:
        tracker.reports += 1
        tracker.last_seen = max(tracker.last_seen, ts)
        tracker.always_absent &= set(absent)
        tracker.currently_absent = absent
        if report.expected_feature_columns:
            tracker.expected = tuple(report.expected_feature_columns)

    expected_count = len(tracker.expected)
    # Keep the spec's column order so the list reads like the manifest.
    structural = tuple(c for c in tracker.expected if c in tracker.always_absent)
    transient = tuple(c for c in tracker.currently_absent if c not in tracker.always_absent)
    fraction = len(structural) / expected_count if expected_count else 0.0

    # A04.5: withholding is decided per mode model, so when the loaded model
    # registered its mode feature sets the verdict is counted per mode —
    # "scores nothing" means *every* mode model is blind, and a transition is
    # a change in which modes are blind. Without a registration (the sequence
    # detector, one feature set) the global fraction is the per-model answer.
    audit = _LOAD_AUDIT.get(model_type)
    by_mode: dict[str, dict[str, Any]] | None = (
        per_mode_blindness(structural, audit["mode_feature_columns"])
        if audit is not None and audit.get("model_version") == model_version
        else None
    )
    blind_modes: tuple[str, ...] | None = (
        None
        if by_mode is None
        else tuple(sorted(m for m, v in by_mode.items() if v["scores_nothing"]))
    )
    transition: str | None = None
    if tracker.ready():
        scores_nothing: bool | None = (
            fraction > MAX_MISSING_FEATURE_FRACTION
            if by_mode is None
            else bool(by_mode) and all(v["scores_nothing"] for v in by_mode.values())
        )
        if not tracker.established:
            tracker.established = True
            transition = "established"
        elif (
            structural != tracker.established_columns
            or scores_nothing != tracker.established_scores_nothing
            or blind_modes != tracker.established_blind_modes
        ):
            transition = "changed"
        tracker.established_columns = structural
        tracker.established_scores_nothing = scores_nothing
        tracker.established_blind_modes = blind_modes
        status = "established"
    else:
        scores_nothing = None
        status = "pending"

    if not structural and not transient:
        _STRUCTURAL_BLINDNESS.pop(model_type, None)
        verdict: dict[str, Any] | None = None
    else:
        verdict = {
            "model_version": model_version,
            "status": status,
            # Pending: absent in every report *so far* (candidates). Established:
            # absent in every report over the sustained window.
            "absent_feature_columns": list(structural),
            "absent_count": len(structural),
            "expected_count": expected_count,
            "absent_fraction": round(fraction, 4),
            "withhold_ceiling": MAX_MISSING_FEATURE_FRACTION,
            "scores_nothing": scores_nothing,
            # Absent from the latest report but seen present at least once:
            # sparsity (slow-tier poll, no GPS fix), not a retrain signal.
            "transient_absent_feature_columns": list(transient),
            "observed_seconds": round(tracker.observed_seconds, 1),
            "reports": tracker.reports,
            "min_observed_seconds": STRUCTURAL_MIN_OBSERVED_SECONDS,
            "min_reports": STRUCTURAL_MIN_REPORTS,
        }
        if by_mode is not None and blind_modes is not None:
            verdict["by_mode"] = by_mode
            verdict["blind_modes"] = list(blind_modes)
        _STRUCTURAL_BLINDNESS[model_type] = verdict
    return BlindnessUpdate(model_type=model_type, verdict=verdict, transition=transition)


def per_mode_blindness(
    structural: Sequence[str],
    mode_feature_columns: Mapping[str, Sequence[str]],
) -> dict[str, dict[str, Any]]:
    """Structural absence counted against each mode model's own features (A04.5).

    Withholding is decided per mode (``TrainedIsolationForest.score`` compares
    a row's missing fraction over the *scoring mode's* features), so a global
    ``absent / 295`` hides a mode that is blind: three missing base channels
    with four variants each blind a 255-feature mode model outright.
    """
    from src.models.isolation_forest import MAX_MISSING_FEATURE_FRACTION

    absent = set(structural)
    out: dict[str, dict[str, Any]] = {}
    for mode, columns in sorted(mode_feature_columns.items()):
        expected = len(columns)
        missing = [c for c in columns if c in absent]
        fraction = len(missing) / expected if expected else 0.0
        out[str(mode)] = {
            "absent_count": len(missing),
            "expected_count": expected,
            "absent_fraction": round(fraction, 4),
            "scores_nothing": fraction > MAX_MISSING_FEATURE_FRACTION,
        }
    return out


def describe_structural_verdict(verdict: Mapping[str, Any], transition: str) -> tuple[str, str]:
    """``(level, message)`` for a structural-blindness verdict, one wording for
    the daemon log (A04.5).

    With ``by_mode`` (a per-mode model), the message is about which mode
    models withhold every row, each mode's absent share against the ceiling —
    never the absent share of the feature union, which no withholding
    decision uses. ``level`` is ``"error"`` only when every mode is blind.
    """
    ceiling = float(verdict["withhold_ceiling"]) * 100.0
    absent = ", ".join(list(verdict["absent_feature_columns"])[:12])
    window = f"over {verdict['observed_seconds']:.0f} s / {verdict['reports']} reports"
    by_mode = verdict.get("by_mode")
    if isinstance(by_mode, Mapping) and by_mode:
        shares = "; ".join(
            f"{mode} {v['absent_count']}/{v['expected_count']} ({v['absent_fraction'] * 100:.1f}%)"
            for mode, v in sorted(by_mode.items())
        )
        blind = list(verdict.get("blind_modes") or [])
        if verdict.get("scores_nothing"):
            return (
                "error",
                f"model feature spec is unsatisfiable ({transition}): every mode model is "
                f"blind — its structurally absent features exceed the {ceiling:.1f}% withhold "
                f"ceiling ({shares}) {window} — so this detector withholds every row until "
                f"retrained; absent: {absent}",
            )
        if blind:
            return (
                "warning",
                f"model feature spec structural absence {transition}: mode model(s) "
                f"{', '.join(blind)} withhold every row (absent share over the "
                f"{ceiling:.1f}% ceiling); the other modes still score ({shares}) {window}; "
                f"absent: {absent}",
            )
        return (
            "warning",
            f"model feature spec structural absence {transition}: no mode model is over "
            f"the {ceiling:.1f}% withhold ceiling ({shares}) {window}; absent: {absent}",
        )
    if verdict.get("scores_nothing"):
        return (
            "error",
            f"model feature spec is unsatisfiable ({transition}): "
            f"{verdict['absent_count']} of {verdict['expected_count']} features absent from "
            f"every reconciliation {window} ({verdict['absent_fraction'] * 100:.1f}% > "
            f"{ceiling:.1f}% ceiling), so this detector will withhold every row until "
            f"retrained; absent: {absent}",
        )
    return (
        "warning",
        f"model feature spec structural absence {transition}: {verdict['absent_count']} of "
        f"{verdict['expected_count']} features absent from every reconciliation {window} "
        f"(under the {ceiling:.1f}% withhold ceiling)",
    )


#: A04.6/A04.8 — what the loader refused or flagged, per model type, for
#: ``/status`` (``detectors.<model>.load_audit``).
_LOAD_AUDIT: dict[str, dict[str, Any]] = {}


def record_load_audit(
    model_type: str,
    *,
    model_version: str,
    refused_modes: Mapping[str, str],
    policy_violations: Mapping[str, str],
    policy_checked: bool,
    mode_feature_columns: Mapping[str, Sequence[str]],
) -> None:
    """Publish a load's refusals and policy violations, and the per-mode
    feature sets the blindness verdict is counted against."""
    _LOAD_AUDIT[model_type] = {
        "model_version": model_version,
        "refused_modes": dict(sorted(refused_modes.items())),
        "policy_checked": policy_checked,
        "policy_violation_count": len(policy_violations),
        "policy_violations": dict(sorted(policy_violations.items())),
        "mode_feature_columns": {
            str(mode): list(columns) for mode, columns in mode_feature_columns.items()
        },
    }


def load_audit_snapshot() -> dict[str, dict[str, Any]]:
    """Per-model load audit for ``/status`` (per-mode feature lists omitted)."""
    return {
        name: {k: v for k, v in audit.items() if k != "mode_feature_columns"}
        for name, audit in sorted(_LOAD_AUDIT.items())
    }


def reset_blindness_tracking() -> None:
    """Forget all evidence (tests; a fresh daemon starts empty anyway)."""
    _TRACKERS.clear()
    _STRUCTURAL_BLINDNESS.clear()
    _LOAD_AUDIT.clear()
