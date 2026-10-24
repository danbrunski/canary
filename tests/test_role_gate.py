"""A06.5 — the daemon serves an ``alert`` role only beside a passing verdict.

Operator decision 2026-09-23: an ``alert`` role in ``models.roles`` takes
effect only for an artifact whose sha256 has an accepted alert-budget verdict
(``<artifact>.alert-budget.json``, written by ``scripts/retrain.py``). Without
one the detector serves as ``evidence`` and ``/status`` names why. Before
this, flipping the Pi's config to ``alert`` put an unmeasured detector back
on the alarm path — the shape of the 2026-09-10 flood.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from src.alerts.emitter import AnomalyEventEmitter
from src.alerts.events import AnomalyEvent
from src.daemon.health import HealthMonitor
from src.models.alert_budget import (
    AlertBudgetCheck,
    DetectorBudget,
    file_sha256,
    verdict_path_for,
    verdict_record,
    write_verdict,
)
from src.models.fusion import isolation_forest_source
from src.models.role_gate import DetectorRoleGate, resolve_role
from tests.configs import isolate


def _artifact(directory: Path, content: bytes = b"weights-v1") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "model.joblib"
    path.write_bytes(content)
    return path


def _check(*, passed: bool, override: str | None = None) -> AlertBudgetCheck:
    detector = DetectorBudget(
        "isolation_forest",
        passed=passed,
        reason=(
            "0 false alarm(s) in 5.00 engine-h over 5 drive(s): 0.00/h, upper bound 0.74/h "
            "within 1/h"
            if passed
            else "12 false alarm(s) in 1.00 engine-h over 1 drive(s): 12.00/h, upper bound "
            "20.97/h over 1/h"
        ),
        episodes=0 if passed else 12,
        engine_hours=5.0 if passed else 1.0,
        upper=0.74 if passed else 20.97,
    )
    return AlertBudgetCheck(
        budget=1.0,
        alert_sources=("isolation_forest",),
        detectors=(detector,),
        override_reason=override,
        extra={"evaluation_drives": [{"drive": "drive-2026-09-23T142926", "engine_hours": 1.06}]},
    )


def _write(artifact: Path, check: AlertBudgetCheck, *, sha256: str | None = None) -> Path:
    return write_verdict(
        artifact,
        verdict_record(
            check,
            source="isolation_forest",
            artifact_path=artifact,
            sha256=sha256 or file_sha256(artifact),
            evaluated_at="2026-09-23T20:00:00Z",
        ),
    )


def test_configured_evidence_stays_evidence_without_reading_anything(tmp_path: Path) -> None:
    resolution = resolve_role("isolation_forest", "evidence", artifact_path=None)
    assert (resolution.effective, resolution.reason) == (
        "evidence",
        "configured evidence (models.roles)",
    )


def test_alert_without_a_verdict_serves_as_evidence_and_names_the_artifact(
    tmp_path: Path,
) -> None:
    artifact = _artifact(tmp_path)
    digest = file_sha256(artifact)

    resolution = resolve_role("isolation_forest", "alert", artifact_path=artifact)

    assert resolution.configured == "alert"
    assert resolution.effective == "evidence"
    assert resolution.reason == (
        f"alert role refused: no passing alert-budget verdict for {digest[:12]}"
    )
    assert resolution.artifact_sha256 == digest


def test_alert_with_a_passing_verdict_for_this_sha_serves_as_alert(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    verdict = _write(artifact, _check(passed=True))
    assert verdict == verdict_path_for(artifact) == tmp_path / "model.joblib.alert-budget.json"

    resolution = resolve_role("isolation_forest", "alert", artifact_path=artifact)

    assert resolution.effective == "alert"
    assert resolution.reason.startswith("alert: passing alert-budget verdict for")
    assert resolution.verdict is not None
    assert resolution.verdict["evaluation_drives"][0]["engine_hours"] == 1.06
    assert resolution.verdict["detectors"][0]["upper_per_engine_hour"] == 0.74


def test_alert_with_a_verdict_for_other_bytes_serves_as_evidence(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    _write(artifact, _check(passed=True), sha256="f" * 64)

    resolution = resolve_role("isolation_forest", "alert", artifact_path=artifact)

    assert resolution.effective == "evidence"
    assert "the verdict beside it is for ffffffffffff" in resolution.reason


def test_alert_with_a_failed_verdict_serves_as_evidence_with_its_numbers(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    _write(artifact, _check(passed=False))

    resolution = resolve_role("isolation_forest", "alert", artifact_path=artifact)

    assert resolution.effective == "evidence"
    assert "failed: isolation_forest: 12 false alarm(s)" in resolution.reason
    assert "upper bound 20.97/h over 1/h" in resolution.reason


def test_a_publish_override_is_not_a_passing_verdict(tmp_path: Path) -> None:
    """The operator's decision reads "a passing alert-budget verdict": an
    artifact published over budget with --accept-alert-budget still serves
    as evidence, and says why."""
    artifact = _artifact(tmp_path)
    _write(artifact, _check(passed=False, override="bench drive, operator 2026-09-23"))

    resolution = resolve_role("isolation_forest", "alert", artifact_path=artifact)

    assert resolution.effective == "evidence"
    assert "an override is not a passing verdict" in resolution.reason
    assert "'bench drive, operator 2026-09-23'" in resolution.reason


def test_an_unreadable_verdict_serves_as_evidence(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    verdict_path_for(artifact).write_text("{not json")

    resolution = resolve_role("isolation_forest", "alert", artifact_path=artifact)

    assert resolution.effective == "evidence"
    assert "unreadable alert-budget verdict" in resolution.reason


@dataclass
class _Loaded:
    artifact_path: Path
    serving_version: str


def test_a_hot_swap_to_an_artifact_without_a_verdict_demotes_at_the_swap(
    tmp_path: Path,
) -> None:
    first = _artifact(tmp_path / "v1")
    _write(first, _check(passed=True))
    second = _artifact(tmp_path / "v2", b"weights-v2")  # no verdict beside it
    serving: dict[str, Any] = {"isolation_forest": _Loaded(first, "if-v1")}
    published: list[dict[str, Any]] = []
    gate = DetectorRoleGate({"isolation_forest": "alert", "lstm_autoencoder": "evidence"})
    gate.bind(serving.get, on_change=published.append)

    assert gate["isolation_forest"] == "alert"
    assert gate["lstm_autoencoder"] == "evidence"
    assert gate.get("behavior:coolant", "alert") == "alert"  # not a gated source

    serving["isolation_forest"] = _Loaded(second, "if-v2")  # the ModelManager swapped

    assert gate["isolation_forest"] == "evidence"  # type: ignore[comparison-overlap]
    snapshot = published[-1]["isolation_forest"]
    assert (snapshot["configured"], snapshot["effective"]) == ("alert", "evidence")
    assert snapshot["model_version"] == "if-v2"
    assert snapshot["reason"].startswith("alert role refused: no passing alert-budget verdict")


def test_a_verdict_that_lands_after_its_binary_is_picked_up_by_refresh(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    serving = {"isolation_forest": _Loaded(artifact, "if-v1")}
    gate = DetectorRoleGate({"isolation_forest": "alert"})
    gate.bind(serving.get)
    assert gate["isolation_forest"] == "evidence"

    _write(artifact, _check(passed=True))  # scp'd a moment later
    assert gate["isolation_forest"] == "evidence"  # lookups never touch the disk
    gate.refresh()  # the model poller, off the event loop
    assert gate["isolation_forest"] == "alert"  # type: ignore[comparison-overlap]


def test_the_emitter_stamps_the_effective_role_after_a_swap(tmp_path: Path) -> None:
    first = _artifact(tmp_path / "v1")
    _write(first, _check(passed=True))
    serving: dict[str, Any] = {"isolation_forest": _Loaded(first, "if-v1")}
    gate = DetectorRoleGate({"isolation_forest": "alert"})
    gate.bind(serving.get)
    logged: list[AnomalyEvent] = []
    emitter = AnomalyEventEmitter(
        SimpleNamespace(log=logged.append),  # type: ignore[arg-type]
        min_seconds_between_events=0.0,
        roles=gate,
    )

    def _event(second: int) -> AnomalyEvent:
        return AnomalyEvent(
            timestamp=f"2026-09-23T12:00:{second:02d}Z",
            source="isolation_forest",
            model_version="if",
            score=1.0,
        )

    emitter._emit(_event(0))
    serving["isolation_forest"] = _Loaded(_artifact(tmp_path / "v2", b"weights-v2"), "if-v2")
    emitter._emit(_event(5))

    assert [e.role for e in logged] == ["alert", "evidence"]


def test_the_live_fusion_source_carries_the_effective_role(tmp_path: Path) -> None:
    from src.daemon.inference import _with_effective_role

    gate = DetectorRoleGate({"isolation_forest": "alert"})
    gate.bind(lambda _source: None)  # nothing loaded: refused
    components = SimpleNamespace(role_gate=gate)
    fired = isolation_forest_source(0.0, -0.0645, scale=0.05)
    assert fired.role == "alert"

    assert _with_effective_role(components, fired).role == "evidence"
    assert _with_effective_role(SimpleNamespace(role_gate=None), fired).role == "alert"


def test_status_shows_configured_and_effective_roles(tmp_path: Path) -> None:
    health = HealthMonitor()
    gate = DetectorRoleGate({"isolation_forest": "alert", "lstm_autoencoder": "evidence"})
    gate.bind(lambda _source: None, on_change=health.observe_detector_roles)

    roles = health.snapshot()["detector_roles"]

    assert roles["isolation_forest"]["configured"] == "alert"
    assert roles["isolation_forest"]["effective"] == "evidence"
    assert roles["isolation_forest"]["reason"] == "alert role refused: no artifact loaded"
    assert roles["lstm_autoencoder"]["effective"] == "evidence"


def test_the_daemon_wires_the_gate_into_components(tmp_path: Path) -> None:
    """``build_components`` resolves roles from the served artifacts: an
    ``alert``-configured IF with nothing verified behind it is evidence."""
    from src.config import CanaryConfig
    from src.daemon import build_components

    example = Path(__file__).resolve().parents[1] / "canary.example.yaml"
    text = example.read_text()
    old = "  roles:\n    isolation_forest: evidence\n    sequence: evidence\n"
    assert old in text
    path = tmp_path / "canary.yaml"
    path.write_text(
        text.replace(old, "  roles:\n    isolation_forest: alert\n    sequence: evidence\n")
    )
    # Every daemon state path under tmp_path, not only these two (R07.2).
    config = isolate(CanaryConfig.from_yaml(path), tmp_path)
    config.daemon.models_dir = tmp_path / "models"
    config.daemon.anomalies_dir = tmp_path / "anomalies"

    components = build_components(config, load_models=False)

    assert components.role_gate is not None
    # The emitter reads the gate for the learned detectors, and the configured
    # role for deterministic sources (H04.2 committees), never the gate's.
    assert components.emitter._roles.gate is components.role_gate  # type: ignore[attr-defined]
    assert components.emitter._roles["isolation_forest"] == "evidence"
    assert components.emitter._roles["committee"] == config.models.roles.committee
    roles = components.health_monitor.snapshot()["detector_roles"]
    assert (roles["isolation_forest"]["configured"], roles["isolation_forest"]["effective"]) == (
        "alert",
        "evidence",
    )
