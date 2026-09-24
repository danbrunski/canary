"""B03 — permitted-use policy, unconditional demotions, compatibility, status, rollback.

B03.2: one typed policy (``PermittedUse`` via ``serving_status``) read by the
loader, the IF trainer's column selection and the daemon's detector.
B03.3: the review's demotions live in profile data (``behavior_policy.yaml``)
and survive refit, carry-over, hot reload and restoring an old artifact.
B03.4: a demotion withdraws *selection* of a deviation column, never its
production. B03.8: status surfaces carry identity, permitted use, validation
scope and abstain reason. B03.10: the prior artifact set stays recoverable and
rolling back to it keeps the policy in force.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from src.analysis.behavior_inventory import build_inventory, compare_deployed
from src.analysis.refit import default_recipes, refit_all
from src.config import CanaryConfig
from src.daemon import _observe_behavior, poll_behavior_models
from src.daemon.inference import BehaviorDetector
from src.dashboard.behavior_live import BehaviorLiveState
from src.dashboard.readers import read_behavior_models
from src.decode.behavior_decoder import (
    append_deviation_columns,
    withheld_learned_feature_columns,
)
from src.decode.behavior_models import (
    BEHAVIOR_POLICY_FILENAME,
    Axis1D,
    BehaviorModel1D,
    BehaviorModels,
    BehaviorPolicy,
    DeviationConfig,
    DriveSplitRecord,
    EngineStateRequirement,
    LifecyclePolicyEntry,
    PermittedUse,
    PolynomialFit,
    ServingEligibility,
    ValidationEvidence,
    apply_behavior_policy,
    artifact_fingerprint,
    contract_fingerprint,
    dump_behavior_models,
    load_behavior_models,
    policy_unmatched_entries,
    serving_status,
)
from src.models.spec_producibility import (
    behavior_feature_conflicts,
    behavior_fingerprint_mismatches,
    behavior_fingerprint_provenance,
    manifest_behavior_fingerprints,
)
from src.preprocessing.feature_pipeline import build_feature_spec, consumed_behavior_fingerprints
from src.preprocessing.live import LiveFeatureSource

REPO = Path(__file__).resolve().parents[1]
PROFILE_DIR = REPO / "data" / "nissan-xterra-2008"
SHIPPED = PROFILE_DIR / "behavior_models.yaml"
SHIPPED_POLICY = PROFILE_DIR / BEHAVIOR_POLICY_FILENAME

sys.path.insert(0, str(REPO / "scripts"))
import refit_behavior_models as refit_cli  # noqa: E402


@pytest.fixture
def config() -> CanaryConfig:
    return CanaryConfig.from_yaml(REPO / "canary.example.yaml")


def _entry(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "model": "steady_timing",
        "lifecycle": "demoted",
        "permitted_uses": ["display"],
        "reason": "test",
        "decided": "2026-09-21",
        "evidence": "tests",
    }
    fields.update(overrides)
    return fields


def _validated(name: str = "steady_timing") -> BehaviorModel1D:
    """A schema-2 model with passed held-out evidence — ``active`` on its own."""
    model = BehaviorModel1D(
        name=name,
        kind="surface_1d",
        output="obd:TIMING_ADVANCE",
        axis=Axis1D(name="rpm", channel="obd:RPM", domain=(400.0, 7000.0)),
        eligibility=ServingEligibility(engine_state=EngineStateRequirement(state="any")),
        fit=PolynomialFit(
            kind="poly",
            coefficients=[10.0],
            sample_count=100,
            fitted_at="2026-09-21T00:00:00Z",
            validation_sample_count=50,
            calibration_sessions=["2026-09-20T100000"],
        ),
        deviation=DeviationConfig(kind="absolute", warning_band=5.0, critical_band=15.0),
    )
    evidence = ValidationEvidence(
        evaluator_version=1,
        evaluated_at="2026-09-21T00:00:00Z",
        verdict="passed",
        contract_fingerprint=contract_fingerprint(model),
        split=DriveSplitRecord(policy="test", join_gap_seconds=600.0),
    )
    return model.model_copy(update={"evidence": evidence})


def _write_profile(tmp_path: Path, models: BehaviorModels, policy: str | None) -> Path:
    path = tmp_path / "behavior_models.yaml"
    dump_behavior_models(models, path)
    if policy is not None:
        (tmp_path / BEHAVIOR_POLICY_FILENAME).write_text(policy)
    return path


def _policy_yaml(*entries: dict[str, Any]) -> str:
    import yaml

    return yaml.safe_dump({"schema_version": 1, "entries": list(entries)})


# ---------------------------------------------------------------------------
# B03.2 — the typed policy
# ---------------------------------------------------------------------------


def test_policy_can_only_withdraw_uses() -> None:
    with pytest.raises(ValidationError, match="cannot grant 'alert'"):
        LifecyclePolicyEntry(**_entry(permitted_uses=["display", "alert"]))
    with pytest.raises(ValidationError, match="'display' must stay permitted"):
        LifecyclePolicyEntry(**_entry(permitted_uses=["learned_feature"]))
    with pytest.raises(ValidationError, match="pins one artifact"):
        LifecyclePolicyEntry(**_entry(lifecycle="quarantined"))
    with pytest.raises(ValidationError, match="only for quarantine"):
        LifecyclePolicyEntry(**_entry(artifact_fingerprint="sha256:x"))
    with pytest.raises(ValidationError, match="duplicate"):
        BehaviorPolicy(entries=[LifecyclePolicyEntry(**_entry())] * 2)


def test_validated_model_is_the_only_alerting_lifecycle() -> None:
    status = serving_status(_validated())
    assert status.state == "active" and status.alerting
    assert status.lifecycle == "validated"
    assert status.permitted_uses == frozenset(PermittedUse)
    # A policy entry demotes it regardless of the evidence.
    models = BehaviorModels(schema_version=2, models=[_validated()])
    apply_behavior_policy(models, BehaviorPolicy(entries=[LifecyclePolicyEntry(**_entry())]))
    demoted = serving_status(models.models[0])
    assert demoted.state == "diagnostic_only" and not demoted.alerting
    assert demoted.lifecycle == "demoted"
    assert demoted.permitted_uses == frozenset({PermittedUse.DISPLAY})
    assert demoted.reasons[0] == "policy:demoted"


def test_unvalidated_artifact_exposes_that_state() -> None:
    legacy = _validated().model_copy(update={"evidence": None, "eligibility": None})
    status = serving_status(legacy)
    assert status.lifecycle == "unvalidated"
    assert not status.alerting
    # Unvalidated still feeds existing detectors' feature columns.
    assert status.permits(PermittedUse.LEARNED_FEATURE)
    payload = status.to_dict()
    assert payload["lifecycle"] == "unvalidated"
    assert payload["permitted_uses"] == ["display", "learned_feature"]


# ---------------------------------------------------------------------------
# B03.3 — the unconditional demotions, on the shipped artifact
# ---------------------------------------------------------------------------


def test_shipped_demotions_apply_through_the_one_loader() -> None:
    models = load_behavior_models(SHIPPED)
    status = {model.name: serving_status(model) for model in models.models}
    assert status["timing_advance_vs_rpm_iat"].lifecycle == "demoted"
    assert status["cat_warmup"].lifecycle == "quarantined"
    assert status["timing_advance_vs_rpm_load"].lifecycle == "exploratory"
    for name in ("timing_advance_vs_rpm_iat", "cat_warmup", "timing_advance_vs_rpm_load"):
        assert status[name].permitted_uses == frozenset({PermittedUse.DISPLAY})
    assert not any(s.alerting for s in status.values())
    assert policy_unmatched_entries(models) == []
    # The raw fits are preserved: the historical artifact is still displayed.
    assert models.by_name("cat_warmup") is not None


def test_demotion_survives_a_refit_that_passes_validation(tmp_path: Path) -> None:
    # A future refit of the demoted name, even with passed evidence, stays demoted.
    refit = BehaviorModels(schema_version=2, models=[_validated("timing_advance_vs_rpm_iat")])
    path = _write_profile(tmp_path, refit, SHIPPED_POLICY.read_text())
    loaded = load_behavior_models(path)
    status = serving_status(loaded.models[0])
    assert status.lifecycle == "demoted" and not status.alerting


def test_quarantine_follows_the_artifact_through_carry_over(tmp_path: Path) -> None:
    shipped = load_behavior_models(SHIPPED)
    # A partial refit that refits nothing carries every prior over.
    carried, _ = refit_all([], pd.DataFrame(), existing=shipped, catalog=default_recipes())
    assert "cat_warmup" in {model.name for model in carried.models}
    path = _write_profile(tmp_path, carried, SHIPPED_POLICY.read_text())
    reloaded = load_behavior_models(path)
    cat = reloaded.by_name("cat_warmup")
    assert cat is not None
    assert artifact_fingerprint(cat) == artifact_fingerprint(shipped.by_name("cat_warmup"))  # type: ignore[arg-type]
    assert serving_status(cat).lifecycle == "quarantined"
    assert serving_status(reloaded.by_name("timing_advance_vs_rpm_iat")).lifecycle == "demoted"  # type: ignore[arg-type]


def test_a_new_catalyst_fit_is_not_covered_by_the_quarantine(tmp_path: Path) -> None:
    shipped = load_behavior_models(SHIPPED)
    old = shipped.by_name("cat_warmup")
    assert old is not None
    new_fit = old.fit.model_copy(update={"fitted_at": "2026-10-01T00:00:00+00:00"})
    replaced = BehaviorModels(
        schema_version=2, models=[old.model_copy(update={"fit": new_fit, "version": 5})]
    )
    path = _write_profile(tmp_path, replaced, SHIPPED_POLICY.read_text())
    loaded = load_behavior_models(path)
    status = serving_status(loaded.models[0])
    # Not quarantined — but not trusted either: it has no evidence.
    assert status.lifecycle == "unvalidated" and not status.alerting
    assert any(
        entry.startswith("cat_warmup:quarantined@") for entry in policy_unmatched_entries(loaded)
    )


def test_demoted_model_contributes_no_fusion_vote(
    config: CanaryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.daemon.inference as inference

    monkeypatch.setattr(inference, "gate_and_collect", lambda *a, **k: True)
    monkeypatch.setattr(inference, "_publish_score", lambda *a, **k: None)

    def run(models: BehaviorModels) -> tuple[list[str], list[Any], dict[str, Any]]:
        source = LiveFeatureSource(config=config, behavior_models=models)
        source.latest_operating_point = {"obd:RPM": 1500.0, "obd:TIMING_ADVANCE": 40.0}
        state = BehaviorLiveState()
        residuals = _observe_behavior(source, state)
        emitted: list[str] = []
        components = SimpleNamespace(
            config=SimpleNamespace(
                models=SimpleNamespace(fusion=SimpleNamespace(behavior_warning_severity=0.5))
            ),
            persistence_gate=None,
            alert_tuner=None,
            scores_broadcaster=None,
            emitter=SimpleNamespace(
                counters=None, emit_behavior=lambda **kw: emitted.append(kw["model"])
            ),
        )
        fusion = SimpleNamespace(
            behavior_source=lambda name, severity, *, channel: SimpleNamespace(
                fired=severity != "ok", severity=1.0
            )
        )
        detector = BehaviorDetector(components, fusion, source)  # type: ignore[arg-type]
        ctx = SimpleNamespace(now=datetime(2026, 9, 21, tzinfo=UTC), timestamp_str="t")
        fused = detector.apply(residuals, ctx)  # type: ignore[arg-type]
        return emitted, fused, state.snapshot(include_series=False)["models"]["steady_timing"]

    # Control: the validated model alerts and votes.
    emitted, fused, _ = run(BehaviorModels(schema_version=2, models=[_validated()]))
    assert emitted == ["steady_timing"] and len(fused) == 1

    demoted = BehaviorModels(schema_version=2, models=[_validated()])
    apply_behavior_policy(demoted, BehaviorPolicy(entries=[LifecyclePolicyEntry(**_entry())]))
    emitted, fused, live = run(demoted)
    assert emitted == [] and fused == []
    # Still shown, graded, and labelled with why it abstains.
    assert live["severity"] == "critical"
    assert live["lifecycle"] == "demoted"
    assert live["permitted_uses"] == ["display"]
    assert live["abstain_reason"] == "not_alerting:demoted"


async def test_hot_reload_applies_a_policy_edit_and_a_rollback(
    tmp_path: Path, config: CanaryConfig
) -> None:
    models = BehaviorModels(schema_version=2, models=[_validated()])
    path = _write_profile(tmp_path, models, None)
    prior = tmp_path / "_prior" / "behavior_models.yaml"
    prior.parent.mkdir()
    shutil.copy2(path, prior)
    live = LiveFeatureSource(config=config, behavior_models=load_behavior_models(path))
    stop = asyncio.Event()
    task = asyncio.create_task(poll_behavior_models(path, live, stop, interval_seconds=0.02))
    try:
        await asyncio.sleep(0.08)
        assert serving_status(live.behavior_models.models[0]).alerting  # type: ignore[union-attr]
        # A policy edit alone (artifact untouched) is picked up.
        (tmp_path / BEHAVIOR_POLICY_FILENAME).write_text(_policy_yaml(_entry()))
        await asyncio.sleep(0.15)
        assert serving_status(live.behavior_models.models[0]).lifecycle == "demoted"  # type: ignore[union-attr]
        # A replacement artifact, then a rollback to the prior with its
        # *older* mtime: the change is still seen and the policy still holds.
        dump_behavior_models(BehaviorModels(schema_version=2, models=[]), path)
        os.utime(path, (prior.stat().st_mtime + 50, prior.stat().st_mtime + 50))
        await asyncio.sleep(0.15)
        assert live.behavior_models is not None and live.behavior_models.models == []
        shutil.copy2(prior, path)
        await asyncio.sleep(0.15)
        restored = live.behavior_models
        assert restored is not None and [m.name for m in restored.models] == ["steady_timing"]
        assert serving_status(restored.models[0]).lifecycle == "demoted"
    finally:
        stop.set()
        await task


# ---------------------------------------------------------------------------
# B03.4 — compatibility: withdraw selection, never production
# ---------------------------------------------------------------------------


def test_demoted_deviation_column_is_still_produced_but_not_selectable() -> None:
    models = load_behavior_models(SHIPPED)
    n = 60
    frame = pd.DataFrame(
        {
            "session": ["s"] * n,
            "timestamp": pd.date_range("2026-09-21", periods=n, freq="1s", tz="UTC"),
            "obd:RPM": np.full(n, 1500.0),
            "obd:INTAKE_TEMP": np.full(n, 40.0),
            "obd:ENGINE_LOAD": np.full(n, 30.0),
            "obd:TIMING_ADVANCE": np.full(n, 20.0),
        }
    )
    out = append_deviation_columns(frame, models)
    for name in ("timing_advance_vs_rpm_iat", "timing_advance_vs_rpm_load", "cat_warmup"):
        assert f"deviation:{name}" in out.columns  # an existing detector keeps its input
    columns = [
        "deviation:timing_advance_vs_rpm_iat",
        "deviation:timing_advance_vs_rpm_iat__mean",
        "deviation:cat_warmup__roc",
        "deviation:air_charge_vs_rpm_throttle_did",
        "obd:RPM",
    ]
    withheld = withheld_learned_feature_columns(columns, models)
    assert set(withheld) == {
        "deviation:timing_advance_vs_rpm_iat",
        "deviation:timing_advance_vs_rpm_iat__mean",
        "deviation:cat_warmup__roc",
    }
    assert "demoted" in withheld["deviation:timing_advance_vs_rpm_iat"]
    # A manifest consuming a demoted column is exposed, not silently fed.
    conflicts = behavior_feature_conflicts(columns, models)
    assert set(conflicts) == set(withheld)
    assert behavior_feature_conflicts(columns, None) == {}


def test_tracked_manifests_consume_no_demoted_behavior_column(config: CanaryConfig) -> None:
    """B03.1 inventory fact the demotions rely on: nothing tracked is stranded."""
    from src.profiles import resolve_profile_for_tooling, scope_config

    scoped = scope_config(config, resolve_profile_for_tooling(config, "nissan-xterra-2008"))
    assert scoped.daemon.behavior_models.resolve() == SHIPPED
    inventory = build_inventory(scoped)
    manifests = inventory["dependent_manifests"]
    assert manifests, "no tracked IF/sequence manifests found"
    for manifest in manifests:
        assert "error" not in manifest
        assert manifest["behavior_feature_conflicts"] == {}


def _deviation_frame(*names: str) -> pd.DataFrame:
    n = 4
    return pd.DataFrame(
        {
            "session": ["s"] * n,
            "timestamp": pd.date_range("2026-09-22", periods=n, freq="1s", tz="UTC"),
            "obd:RPM": [800.0, 900.0, 1000.0, 1100.0],
            **{f"deviation:{name}": [0.1, 0.2, 0.3, 0.4] for name in names},
            **{f"deviation:{name}__mean": [0.1, 0.2, 0.3, 0.4] for name in names},
        }
    )


def test_fresh_spec_records_the_behavior_artifact_that_produced_each_consumed_column() -> None:
    """B03.4: a manifest names the artifact fingerprint behind every deviation column it uses."""
    steady = _validated("steady_timing")
    other = _validated("other_model")
    models = BehaviorModels(schema_version=2, models=[steady, other])
    frame = _deviation_frame("steady_timing", "other_model")
    columns = ["obd:RPM", "deviation:steady_timing", "deviation:steady_timing__mean"]

    spec = build_feature_spec(frame, columns, behavior_models=models)
    # Only the consumed model is pinned; ``other_model`` produces a column
    # this spec does not select, so its refit is not this detector's concern.
    assert spec["behavior_model_fingerprints"] == {"steady_timing": artifact_fingerprint(steady)}
    assert spec["behavior_model_columns"] == ["deviation:steady_timing"]
    # No artifact set given → the field is absent (unknown provenance), never
    # an empty mapping that would read as "consumes nothing".
    assert "behavior_model_fingerprints" not in build_feature_spec(frame, columns)
    # No deviation column consumed, artifact set known → recorded as none.
    assert (
        build_feature_spec(frame, ["obd:RPM"], behavior_models=models)[
            "behavior_model_fingerprints"
        ]
        == {}
    )
    # A consumed column whose producer is not in the recorded set is a stale
    # feature cache, not a fact to write down.
    with pytest.raises(ValueError, match="other_model.*stale feature cache"):
        consumed_behavior_fingerprints(
            ["deviation:other_model"], BehaviorModels(schema_version=2, models=[steady])
        )


def test_refit_behavior_artifact_is_flagged_against_the_consuming_manifest() -> None:
    """B03.4: a refit that changes a consumed column's meaning is a mismatch, never a pass."""
    steady = _validated("steady_timing")
    models = BehaviorModels(schema_version=2, models=[steady])
    columns = ["obd:RPM", "deviation:steady_timing", "deviation:steady_timing__roc"]
    recorded = {"steady_timing": artifact_fingerprint(steady)}

    same = behavior_fingerprint_provenance(columns, recorded, models)
    assert same["steady_timing"]["status"] == "match"
    assert behavior_fingerprint_mismatches(same) == []

    # Refit: new coefficients, same name/contract → different artifact.
    refit = steady.model_copy(
        update={"fit": steady.fit.model_copy(update={"coefficients": [12.0]})}
    )
    assert artifact_fingerprint(refit) != artifact_fingerprint(steady)
    assert contract_fingerprint(refit) == contract_fingerprint(steady)  # contract alone misses it
    after = behavior_fingerprint_provenance(
        columns, recorded, BehaviorModels(schema_version=2, models=[refit])
    )
    assert after["steady_timing"]["status"] == "mismatch"
    assert after["steady_timing"]["recorded"] == recorded["steady_timing"]
    assert after["steady_timing"]["current"] == artifact_fingerprint(refit)
    assert "retrain the detector" in after["steady_timing"]["reason"]
    assert behavior_fingerprint_mismatches(after) == ["steady_timing"]

    # The producing model retired altogether → absent (unproducible elsewhere).
    gone = behavior_fingerprint_provenance(columns, recorded, None)
    assert gone["steady_timing"]["status"] == "absent"
    # A manifest consuming no deviation column has nothing to say.
    assert behavior_fingerprint_provenance(["obd:RPM"], recorded, models) == {}


def test_manifest_without_fingerprints_is_unknown_provenance_not_a_pass() -> None:
    """B03.4: today's tracked manifests predate the record — say so, don't judge."""
    steady = _validated("steady_timing")
    models = BehaviorModels(schema_version=2, models=[steady])
    columns = ["deviation:steady_timing"]

    legacy = {"schema": {"feature_columns": columns}}
    assert manifest_behavior_fingerprints(legacy) is None
    provenance = behavior_fingerprint_provenance(columns, None, models)
    assert provenance["steady_timing"]["status"] == "unknown"
    assert provenance["steady_timing"]["recorded"] is None
    assert provenance["steady_timing"]["current"] == artifact_fingerprint(steady)
    assert behavior_fingerprint_mismatches(provenance) == []  # unknown never fails
    # ``{}`` (recorded, consumes none) and ``None`` (no record) stay distinct.
    assert (
        manifest_behavior_fingerprints(
            {"schema": {"feature_columns": columns, "behavior_model_fingerprints": {}}}
        )
        == {}
    )
    # Recorded for one model but not another → unknown only for the other.
    mixed = behavior_fingerprint_provenance(
        ["deviation:steady_timing", "deviation:other_model"],
        {"steady_timing": artifact_fingerprint(steady)},
        BehaviorModels(schema_version=2, models=[steady, _validated("other_model")]),
    )
    assert {name: entry["status"] for name, entry in mixed.items()} == {
        "steady_timing": "match",
        "other_model": "unknown",
    }


# ---------------------------------------------------------------------------
# B03.8 — status surfaces: empty, demoted, failed, off-map, eligible
# ---------------------------------------------------------------------------


def test_behavior_endpoint_states(tmp_path: Path) -> None:
    empty = read_behavior_models(tmp_path / "missing" / "behavior_models.yaml")
    assert empty["models"] == [] and empty["policy"]["present"] is False

    failed = _validated("failed_model")
    assert failed.evidence is not None
    failed = failed.model_copy(
        update={
            "evidence": failed.evidence.model_copy(
                update={"verdict": "failed", "failures": ["skill_vs_baseline:0.02<0.3"]}
            )
        }
    )
    models = BehaviorModels(
        schema_version=2, models=[_validated("eligible_model"), _validated(), failed]
    )
    path = _write_profile(tmp_path, models, _policy_yaml(_entry()))
    payload = read_behavior_models(path)
    by_name = {entry["name"]: entry for entry in payload["models"]}

    eligible = by_name["eligible_model"]
    assert eligible["serving_status"]["lifecycle"] == "validated"
    assert "alert" in eligible["serving_status"]["permitted_uses"]
    assert eligible["artifact"]["fingerprint"].startswith("sha256:")
    assert eligible["artifact"]["version"] == 1
    assert eligible["validation_scope"]["verdict"] == "passed"
    assert eligible["validation_scope"]["contract_matches"] is True

    demoted = by_name["steady_timing"]
    assert demoted["serving_status"]["lifecycle"] == "demoted"
    assert demoted["serving_status"]["policy"]["reason"] == "test"

    failing = by_name["failed_model"]
    assert failing["serving_status"]["lifecycle"] == "unvalidated"
    assert failing["validation_scope"]["verdict"] == "failed"
    assert "validation_failed:skill_vs_baseline:0.02<0.3" in failing["serving_status"]["reasons"]
    assert payload["policy"]["present"] is True


def test_live_abstain_reasons(config: CanaryConfig) -> None:
    source = LiveFeatureSource(
        config=config, behavior_models=BehaviorModels(schema_version=2, models=[_validated()])
    )
    state = BehaviorLiveState()
    # Eligible and alerting: no abstain reason.
    source.latest_operating_point = {"obd:RPM": 1500.0, "obd:TIMING_ADVANCE": 11.0}
    _observe_behavior(source, state)
    entry = state.snapshot(include_series=False)["models"]["steady_timing"]
    assert entry["abstain_reason"] is None and entry["lifecycle"] == "validated"
    assert entry["artifact_version"] == 1
    # Off-map: the operating point is outside the fitted domain.
    source.latest_operating_point = {"obd:RPM": 9000.0, "obd:TIMING_ADVANCE": 11.0}
    _observe_behavior(source, state)
    entry = state.snapshot(include_series=False)["models"]["steady_timing"]
    assert entry["expected"] is None
    assert entry["abstain_reason"] == entry["reason"] and entry["reason"] is not None


# ---------------------------------------------------------------------------
# B03.1 inventory and B03.10 recoverable prior
# ---------------------------------------------------------------------------


def test_inventory_records_deployed_identity_as_unknown_without_a_snapshot() -> None:
    models = load_behavior_models(SHIPPED)
    assert compare_deployed(models, None)["state"] == "unknown"
    snapshot = read_behavior_models(SHIPPED)
    observed = compare_deployed(models, snapshot)
    assert observed["state"] == "observed"
    assert all(row["match"] for row in observed["models"].values())
    # A pre-B03.8 daemon (no artifact block) is compared by version/fitted_at.
    for entry in snapshot["models"]:
        entry.pop("artifact")
    by_name = {entry.get("name"): entry for entry in snapshot["models"]}
    by_name["air_charge_vs_rpm_throttle_did"]["version"] = 99  # by name, not position
    legacy = compare_deployed(models, snapshot)["models"]
    assert legacy["air_charge_vs_rpm_throttle_did"]["match"] is False
    assert "predates B03.8" in legacy["warm_idle_rpm_baseline"]["reason"]


def test_refit_preserves_the_prior_artifact_and_its_report(tmp_path: Path) -> None:
    out = tmp_path / "behavior_models.yaml"
    report = tmp_path / "behavior_models.yaml.report.json"
    assert refit_cli.preserve_prior_artifact(out, report) is None
    shutil.copy2(SHIPPED, out)
    report.write_text("{}\n")
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    target = refit_cli.preserve_prior_artifact(out, report, now=now)
    assert target == tmp_path / "_prior" / "2026-09-21T120000"
    assert (target / out.name).read_bytes() == SHIPPED.read_bytes()
    assert (target / report.name).read_text() == "{}\n"
    # The preserved prior loads with the same identities it served with.
    restored = load_behavior_models(target / out.name)
    assert [artifact_fingerprint(m) for m in restored.models] == [
        artifact_fingerprint(m) for m in load_behavior_models(SHIPPED).models
    ]
