"""Versioned scenario reports and the before/after markdown summary (C10.3/C10.8).

One JSON document per scenario run, ``schema_version`` 1, carrying the
provenance the task lists — revision, dependency and runtime versions,
hardware/adapter, vehicle identity, profile snapshot, model status,
power/thermal, workload, warmup, duration and the timestamp basis — beside
the evidence blocks from :mod:`src.validation.evidence`. Two report
directories (``before``/``after``) are diffed into a markdown table by
:func:`summarize_markdown`; the diff is by scenario name and by field, never
by position, so a scenario missing on one side is reported as missing.
"""

from __future__ import annotations

import dataclasses
import importlib.metadata
import json
import os
import platform
import socket
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.validation.evidence import analyse
from src.validation.harness import RunArtifacts

SCHEMA_VERSION = 1
DEPENDENCIES = ("pyarrow", "pandas", "numpy", "python-can", "scikit-learn", "torch", "pydantic")


def git_revision(cwd: Path | None = None) -> dict[str, Any]:
    def _git(*args: str) -> str | None:
        try:
            out = subprocess.run(
                ["git", *args], cwd=cwd, capture_output=True, text=True, check=False, timeout=10
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout.strip() if out.returncode == 0 else None

    return {
        "sha": _git("rev-parse", "HEAD"),
        "short": _git("rev-parse", "--short", "HEAD"),
        "describe": _git("describe", "--always", "--dirty"),
        "worktree": None if cwd is None else str(cwd),
    }


def dependency_versions() -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for name in DEPENDENCIES:
        try:
            out[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            out[name] = None
    return out


def provenance(artifacts: RunArtifacts) -> dict[str, Any]:
    config = artifacts.config
    vehicle = config.vehicle
    slug = config.collection.storage_path.parent.name
    if config.daemon.models_dir.name:
        slug = config.daemon.models_dir.name
    spec = artifacts.can_spec
    return {
        "git": git_revision(Path.cwd()),
        "runtime": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "hostname": socket.gethostname(),
            "cpu_count": os.cpu_count(),
            "dependencies": dependency_versions(),
        },
        "hardware": {
            "adapter": "synthetic" if artifacts.scenario.kind != "replay" else "replay (none)",
            "bus": "SyntheticEcu on the real clock; ECU latency is a sleep",
        },
        "vehicle": {
            "profile_slug": slug,
            "make_model_year": f"{vehicle.make} {vehicle.model} {vehicle.year}",
            "ecu": "ScriptedEcu (src/validation/scripted_ecu) extended by SyntheticEcu",
        },
        "profile_snapshot": artifacts.profile.model_dump(mode="json"),
        "profile_fields_unsupported_by_this_tree": artifacts.profile_fields_dropped,
        "spec_fields_unsupported_by_this_tree": artifacts.spec_fields_dropped,
        "collector_spec": (
            None
            if spec is None
            else {
                "poll_rate_hz": spec.poll_rate_hz,
                "pids": list(spec.pids),
                "dids": list(spec.dids),
                "batch_max_pids": spec.batch_max_pids,
                "did_batch_max_dids": getattr(spec, "did_batch_max_dids", "unavailable"),
                "did_response_widths_known": len(getattr(spec, "did_response_widths", {}) or {}),
                "request_timeout_seconds": spec.request_timeout_seconds,
                "did_session": None
                if spec.did_session is None
                else {
                    k: v
                    for k, v in spec.did_session.__dict__.items()
                    if not isinstance(v, (bytes, bytearray))
                },
                "priority_channels": list(spec.priority_channels),
                "dtc_poll_interval_seconds": spec.dtc_poll_interval_seconds,
            }
        ),
        "models": artifacts.models,
        "components": artifacts.components_enabled,
        "power_thermal": "unavailable (workstation)",
        "workload": artifacts.scenario.to_dict(),
        "warmup_s": artifacts.scenario.warmup_s,
        "duration_s": {
            "declared": artifacts.scenario.duration_s,
            "armed_to_end": round(artifacts.ended_wall - artifacts.armed_wall, 3),
            "start_to_end": round(artifacts.ended_wall - artifacts.started_wall, 3),
        },
        "timestamps": {
            "started_wall": artifacts.started_wall,
            "armed_wall": artifacts.armed_wall,
            "ended_wall": artifacts.ended_wall,
            "basis": {
                "obd_rows": "acquired_at (adapter handout stamp, 'kernel' when within 5 s of "
                "time.time()) with presentation timestamp = round wall_start under "
                "round_timestamps",
                "can_rows": "stored timestamp = adapter handout instant",
                "inference": "monotonic deadline grid; rows stamped datetime.now(UTC)",
                "replay": "rows rebased to emit time; acquired_at shifted by the same offset",
            },
        },
        "sessions": {
            "capture_dir": None if artifacts.capture_dir is None else str(artifacts.capture_dir),
            "all_session_dirs": [str(p) for p in artifacts.session_dirs],
            "bindings": str(artifacts.bindings_path),
            "replay_source": None
            if artifacts.replay_source is None
            else str(artifacts.replay_source),
            "replay_speed": artifacts.replay_speed,
            "replay_emission": None
            if artifacts.replay_emission is None
            else dataclasses.asdict(artifacts.replay_emission),
        },
    }


def build_report(artifacts: RunArtifacts) -> dict[str, Any]:
    evidence = analyse(artifacts)
    return {
        "schema_version": SCHEMA_VERSION,
        "report": "sustained_validation",
        "label": artifacts.label,
        "scenario": artifacts.scenario.name,
        "table_row": artifacts.scenario.table_row,
        "generated_at": datetime.now(UTC).isoformat(),
        "git_rev": git_revision(Path.cwd()).get("short"),
        "provenance": provenance(artifacts),
        "evidence": evidence,
        "headline": headline(
            artifacts.scenario.expected_failures,
            evidence,
            expected_any=artifacts.scenario.expected_failures_any,
        ),
    }


def headline(
    expected_failures: tuple[str, ...],
    evidence: Mapping[str, Any],
    *,
    expected_any: tuple[str, ...] = (),
) -> dict[str, Any]:
    quality = evidence.get("quality") or {}
    adapter = quality.get("with_adapter_ledger") or {}
    inference = (evidence.get("timing") or {}).get("inference")
    inference_summary: dict[str, Any] | str
    if isinstance(inference, Mapping):
        inference_summary = {
            "ticks_scheduled": inference.get("ticks_scheduled"),
            "ticks_completed": inference.get("ticks_completed"),
            "missed_ticks": inference.get("missed_ticks"),
            "lateness_p95_ms": (inference.get("lateness") or {}).get("p95_ms"),
            "lateness_max_ms": (inference.get("lateness") or {}).get("max_ms"),
            "feature_p95_ms": ((inference.get("stages") or {}).get("feature") or {}).get("p95_ms"),
        }
    else:
        inference_summary = str(inference)
    failures = list(quality.get("failures") or [])
    expected_seen = [name for name in expected_failures if name in failures]
    any_seen = [name for name in expected_any if name in failures]
    tuples = (adapter.get("tuples") or (quality.get("stop_sidecar") or {}).get("tuples")) or {}
    batching = (evidence.get("transport") or {}).get("batch_occupancy") or {}
    scheduler = batching.get("scheduler")
    return {
        "limiting_stage": (evidence.get("limiting_stage") or {}).get("stage"),
        "sequence_reconciliation": (evidence.get("sequence_reconciliation") or {}).get("status"),
        "sequence_missing": (evidence.get("sequence_reconciliation") or {}).get("missing_count"),
        "loss_identity_holds": (evidence.get("losses") or {}).get("identity_holds"),
        "storage_dropped": (evidence.get("losses") or {}).get("storage_dropped"),
        "quality_verdict": quality.get("verdict"),
        "quality_failures": failures,
        "quality_verdict_with_adapter_ledger": adapter.get("verdict"),
        "expected_failures": list(expected_failures),
        "expected_failures_any": list(expected_any),
        "expected_failures_seen": sorted({*expected_seen, *any_seen}),
        # Every named failure must appear; of the "any" set, at least one must.
        "expected_failures_met": (
            sorted(expected_seen) == sorted(expected_failures)
            and (bool(any_seen) or not expected_any)
        ),
        "tuples_complete": tuples.get("complete"),
        "tuples_attempted": tuples.get("attempted"),
        "tuple_skew_max_s": (tuples.get("skew") or {}).get("max_s"),
        "batch_occupancy": None
        if not isinstance(scheduler, Mapping)
        else {
            key: scheduler.get(key)
            for key in (
                "mean_pids_per_batch",
                "batched_requests",
                "batch_failures",
                "did_batched_requests",
                "did_mean_members_per_batch",
                "did_batch_failures",
            )
            if key in scheduler
        },
        "ecu_batches_seen": (batching.get("ecu_side") or {}),
        "inference": inference_summary,
        "known_answer": [
            {"pair": f"{k['a']} ~ {k['b']}", "outcome": k.get("outcome"), "slope": k.get("slope")}
            for k in evidence.get("known_answer") or []
        ],
        "heartbeat_p99_ms": (
            (evidence.get("timing") or {}).get("event_loop_heartbeat_overshoot") or {}
        ).get("p99_ms"),
    }


def write_report(report: Mapping[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True, default=_default) + "\n")
    return path


def _default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if hasattr(value, "item"):
        return value.item()
    return str(value)


# ---------------------------------------------------------------------------
# before / after summary
# ---------------------------------------------------------------------------


def load_reports(directory: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.json")):
        payload = json.loads(path.read_text())
        if payload.get("report") == "sustained_validation":
            out[str(payload["scenario"])] = payload
    return out


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.3g}"
    if isinstance(value, list):
        return ", ".join(_fmt(v) for v in value) or "none"
    if isinstance(value, Mapping):
        return "; ".join(f"{k}={_fmt(v)}" for k, v in value.items()) or "—"
    return str(value)


def _cell(before: Mapping[str, Any] | None, after: Mapping[str, Any] | None, key: str) -> str:
    b = None if before is None else before.get("headline", {}).get(key)
    a = None if after is None else after.get("headline", {}).get(key)
    if before is None:
        return f"(missing) → {_fmt(a)}"
    if after is None:
        return f"{_fmt(b)} → (missing)"
    return _fmt(a) if _fmt(a) == _fmt(b) else f"{_fmt(b)} → {_fmt(a)}"


SUMMARY_FIELDS: tuple[tuple[str, str], ...] = (
    ("limiting_stage", "limiting stage"),
    ("sequence_reconciliation", "sequence accounting"),
    ("sequence_missing", "sequences missing"),
    ("loss_identity_holds", "push identity"),
    ("storage_dropped", "storage drops"),
    ("quality_verdict", "C03 verdict"),
    ("quality_verdict_with_adapter_ledger", "verdict w/ adapter ledger"),
    ("quality_failures", "failures"),
    ("expected_failures_met", "expected failure met"),
    ("tuples_complete", "complete tuples"),
    ("tuple_skew_max_s", "tuple skew max s"),
    ("batch_occupancy", "batch occupancy"),
    ("ecu_batches_seen", "ECU batches seen"),
    ("inference", "inference"),
    ("heartbeat_p99_ms", "loop heartbeat p99 ms"),
    ("known_answer", "known answer"),
)


def summarize_markdown(before_dir: Path, after_dir: Path) -> str:
    before = load_reports(before_dir)
    after = load_reports(after_dir)
    names = sorted(set(before) | set(after))
    lines: list[str] = []
    for name in names:
        b = before.get(name)
        a = after.get(name)
        any_report = a or b
        assert any_report is not None
        lines.append(f"### `{name}` — {any_report.get('table_row')}")
        lines.append("")
        rev_b = None if b is None else b.get("git_rev")
        rev_a = None if a is None else a.get("git_rev")
        notes = _fmt(any_report.get("provenance", {}).get("workload", {}).get("notes"))
        lines.append(f"before `{rev_b}` → after `{rev_a}`; {notes}")
        lines.append("")
        lines.append("| field | before → after |")
        lines.append("|---|---|")
        for key, title in SUMMARY_FIELDS:
            lines.append(f"| {title} | {_cell(b, a, key)} |")
        lines.append("")
    return "\n".join(lines)


__all__ = [
    "SCHEMA_VERSION",
    "build_report",
    "headline",
    "load_reports",
    "summarize_markdown",
    "write_report",
]
