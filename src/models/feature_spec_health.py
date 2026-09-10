"""Is a trained model's feature spec still satisfiable by the live pipeline?

D02.5, from the 2026-09-10 drive. Lives here rather than in
:mod:`src.daemon.inference` because both the daemon (which writes the verdict)
and the status payload (which reads it) need it, and the daemon already
imports from the dashboard.
"""

from __future__ import annotations

from typing import Any

from src.preprocessing.feature_pipeline import FeatureSpecReconciliation


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
#: Written when a spec proves unsatisfiable; cleared when a later version of
#: the same model type reconciles cleanly.
_STRUCTURAL_BLINDNESS: dict[str, dict[str, Any]] = {}


def structural_blindness_snapshot() -> dict[str, dict[str, Any]]:
    """Per-model-type blindness verdicts, for the status payload."""
    return {name: dict(value) for name, value in sorted(_STRUCTURAL_BLINDNESS.items())}
