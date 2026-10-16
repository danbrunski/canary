"""R15: the detector evaluation's report, stdout, exit codes and per-sample scores are pinned.

``scripts/evaluate_detectors.py`` (3,054 lines) was split into libraries under
``src/models`` and ``src/analysis`` and left as a CLI. A pure move must not
change a score, a verdict, an eligibility decision, a report field or an exit
status, so this runs the real command, through ``main``, over deterministic
synthetic drives and compares what comes out with
``tests/fixtures/detector_evaluation_golden.json``, captured from the
unmodified script before any code moved.

The drives (seeded, 300 rows each; nothing here reads a real session):

* two training drives the Isolation Forest saw (skipped, naming the artifact
  and the role) and four held-out drives, one of which has a sibling tuning
  capture that bridges it but is not scored (the default-profile rule, R04.6);
* a drive made only of a tuning recording (skipped), one that straddles a
  hardware-boundary label (skipped), one recorded after a flash (scored, cross-era:
  no injections, no summary) and a directory with no readable telemetry;
* a labelled fault and a labelled novelty interval, so the label counts differ.

What is compared, per run: the exit status, the stdout and stderr lines (log
timestamps removed), the report JSON (generated time and command text removed;
floats to 1e-9 relative) and the Markdown. For the first run also every drive's
per-sample scores, verdicts, alerting flags and timestamps (hashed, with count,
NaN mask, sum and extremes), each injection outcome, the VUS slices and the
shift report, recorded by wrapping ``DriveScorer.score_drive`` (a class both
the script and the library expose). The runs: the full evaluation, an unchanged
re-run, a regression compare that passes, one that exits 3, ``--plan``, and a
two-worker (spawned) run that must equal the first.

Regenerate only on purpose, from the code whose behaviour is meant to be the
reference::

    .venv/bin/python -m tests.test_detector_evaluation_golden --write
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("sklearn")

REPO = Path(__file__).resolve().parents[1]
GOLDEN = Path(__file__).parent / "fixtures" / "detector_evaluation_golden.json"
EXAMPLE_YAML = REPO / "canary.example.yaml"
RELATIVE = 1e-9


# ---------------------------------------------------------------------------
# The deterministic drives
# ---------------------------------------------------------------------------


def _write_session(root: Path, name: str, start_iso: str, *, seed: int, scale: float = 1.0) -> Path:
    from src.storage.parquet_writer import write_records_to_parquet
    from src.telemetry import TelemetryRecord

    start = datetime.fromisoformat(start_iso).replace(tzinfo=UTC).timestamp()
    rng = np.random.default_rng(seed)
    records = []
    for i in range(300):
        t = start + i
        idle = (i // 60) % 2 == 0
        records.append(
            TelemetryRecord.obd(
                t, "RPM", float(rng.normal(800.0 if idle else 2200.0, 40.0)) * scale, "rpm"
            )
        )
        records.append(
            TelemetryRecord.obd(t, "SPEED", 0.0 if idle else float(rng.normal(60, 4)), "km/h")
        )
        records.append(TelemetryRecord.obd(t, "COOLANT_TEMP", float(rng.normal(90, 1)), "degC"))
        records.append(TelemetryRecord.obd(t, "SHORT_FUEL_TRIM_1", float(rng.normal(0, 2)), "%"))
        records.append(
            TelemetryRecord.obd(t, "MAF", float(rng.normal(4 if idle else 20, 0.5)), "g/s")
        )
    write_records_to_parquet(records, root / name, max_rows=2000)
    return root / name


def build_fixture(base: Path) -> dict[str, Path]:
    """Sessions, the IF artifact, labels, a flash registry and fault roles under ``base``."""
    import yaml

    from src.decode.dids import load_repo_did_map
    from src.features.feature_pipeline import featurize_wide_frame, load_resampled_session
    from src.models.isolation_forest import default_manifest_path, fit_isolation_forest
    from src.storage.capture_manifest import CAPTURE_MANIFEST_FILENAME
    from tests.builders import IFConfig
    from tests.configs import example_config

    sessions = base / "sessions"
    train1 = _write_session(sessions, "2026-05-10T000000", "2026-05-10T00:00:00", seed=1)
    train2 = _write_session(sessions, "2026-05-11T000000", "2026-05-11T00:00:00", seed=2)
    calibration = _write_session(sessions, "2026-05-12T000000", "2026-05-12T00:00:00", seed=3)
    _write_session(sessions, "2026-05-13T000000", "2026-05-13T00:00:00", seed=4)
    capture = _write_session(
        sessions, "_capture-tuning-2026-05-13T000100", "2026-05-13T00:01:00", seed=5, scale=2.0
    )
    (capture / CAPTURE_MANIFEST_FILENAME).write_text(
        json.dumps(
            {
                "profile": "tuning",
                "started_at": "2026-05-13T00:01:00Z",
                "profile_snapshot": {"name": "tuning", "corpus_eligible": False},
            }
        )
    )
    _write_session(sessions, "2026-05-14T000000", "2026-05-14T00:00:00", seed=6)
    _write_session(sessions, "_tuning-2026-05-16T000000", "2026-05-16T00:00:00", seed=8, scale=3.0)
    _write_session(sessions, "2026-05-17T235800", "2026-05-17T23:58:00", seed=9)
    _write_session(sessions, "2026-05-19T000000", "2026-05-19T00:00:00", seed=10)
    broken = sessions / "2026-05-20T000000"
    broken.mkdir()
    (broken / "telemetry-000000.parquet").write_bytes(b"not a parquet file")

    config = example_config(EXAMPLE_YAML)
    did_map = load_repo_did_map()

    def features_of(*names: Path) -> pd.DataFrame:
        return pd.concat(
            [
                featurize_wide_frame(
                    load_resampled_session(s, config), config, behavior_models=None, did_map=did_map
                ).features
                for s in names
            ],
            ignore_index=True,
        )

    forest = fit_isolation_forest(
        features_of(train1, train2),
        IFConfig(n_estimators=30, random_state=3),
        feature_columns=["obd:RPM", "obd:SPEED", "obd:COOLANT_TEMP", "obd:MAF"],
        fitted_at="2026-05-12T00:00:00Z",
        calibration_frame=features_of(calibration),
    )
    artifact = base / "models" / "isolation_forest" / "model.joblib"
    forest.save(artifact, default_manifest_path(artifact))

    labels = base / "labels.yaml"
    labels.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "events": [
                    {"id": "fault-h1", "kind": "fault", "title": "x",
                     "start": "2026-05-13T00:01:00Z", "end": "2026-05-13T00:02:00Z",
                     "end_basis": "measured", "provenance": "fixture"},
                    {"id": "novelty-h2", "kind": "novelty", "title": "y",
                     "start": "2026-05-14T00:00:10Z", "end": "2026-05-14T00:03:00Z",
                     "end_basis": "measured", "provenance": "fixture"},
                    {"id": "hw-swap", "kind": "intervention", "title": "z",
                     "start": "2026-05-18T00:00:00Z", "end": "2026-05-18T00:00:00Z",
                     "end_basis": "measured", "provenance": "fixture", "corpus_boundary": True},
                ],
            }
        )
    )  # fmt: skip
    revisions = base / "revisions.yaml"
    revisions.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "revisions": [
                    {"revision": "stock", "flashed_at": "2026-01-01T00:00:00Z"},
                    {"revision": "tune-b", "flashed_at": "2026-05-16T12:00:00Z"},
                ],
            }
        )
    )
    roles = base / "roles.yaml"
    roles.write_text(yaml.safe_dump({"roles": {}}))
    return {
        "sessions": sessions,
        "artifact": artifact,
        "labels": labels,
        "revisions": revisions,
        "roles": roles,
    }


def _argv(
    fx: dict[str, Path], run: Path, *, workers: str = "1", out: str = "report.json"
) -> list[str]:
    return [
        "--config", str(EXAMPLE_YAML), "--profile", "nissan-xterra-2008",
        "--sessions-root", str(fx["sessions"]), "--if-model", str(fx["artifact"]),
        "--no-sequence", "--baselines", "robust_z", "pca", "plausibility",
        "--labels", str(fx["labels"]), "--tune-revisions", str(fx["revisions"]),
        "--fault-roles", str(fx["roles"]),
        "--faults", "thermostat", "bank_imbalance", "--injections-per-drive", "2",
        "--lead-seconds", "30", "--window-seconds", "60", "--grace-seconds", "10",
        "--shift-rows", "120", "--seed", "7",
        "--cache-dir", str(run / "cache"), "--derived-dir", str(run / "derived"),
        "--workers", workers, "--out", str(run / out),
    ]  # fmt: skip


# ---------------------------------------------------------------------------
# What a run is reduced to
# ---------------------------------------------------------------------------


def _short(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _digest(array: Any) -> dict[str, Any]:
    """An array as count, dtype, element types, NaN mask digest, sum, extremes and ends.

    Floats are not hashed: a bit of BLAS noise must not fail the pin, while any
    real change moves the sum, an extreme, an end or the NaN mask."""
    values = np.asarray(array)
    cells = values.tolist()
    kinds = sorted({type(v).__name__ for v in cells})
    out: dict[str, Any] = {"n": len(cells), "dtype": str(values.dtype), "types": kinds}
    if values.dtype == object:
        out["values"] = _short(repr(cells).encode())
        return out
    if values.dtype.kind == "M":
        out["values"] = _short(values.astype("datetime64[ns]").tobytes())
        return out
    numeric = values.astype("float64")
    nan = np.isnan(numeric)
    out["nan_mask"] = _short(nan.tobytes())
    out["nan_count"] = int(nan.sum())
    finite = numeric[~nan]
    if finite.size:
        out.update(sum=float(finite.sum()), min=float(finite.min()), max=float(finite.max()))
        out["first"], out["last"] = float(finite[0]), float(finite[-1])
    return out


def _scored_view(task: Any, result: Any) -> dict[str, Any]:
    return {
        "scores": {
            session: {
                name: {
                    "timestamps": _digest(score.timestamps.to_numpy()),
                    "verdict": _digest(score.verdict),
                    "score": _digest(score.score),
                    "alerting": _digest(score.alerting),
                }
                for name, score in sorted(by_detector.items())
            }
            for session, by_detector in sorted(result.scores.items())
        },
        "outcomes": [
            {k: (v.to_dict() if hasattr(v, "to_dict") else v) for k, v in outcome.items()}
            for outcome in result.outcomes
        ],
        "slices": [
            {k: _digest(v) if isinstance(v, np.ndarray) else v for k, v in piece.items()}
            for piece in result.slices
        ],
        "shift": result.shift,
        "skipped": result.skipped,
        "stream_reads": result.stream_reads,
    }


def _clean_lines(text: str, run: Path) -> list[str]:
    """Output lines with the run directory named. A JSON log line is reduced to its level
    and message (no timestamp, and no logger name: the loggers moved with the code), and
    the log lines are summarised by their count and a digest of the sorted messages."""
    plain, logged = [], []
    for line in _named(text, run).splitlines():
        try:
            record = json.loads(line)
            logged.append(f"{record['level']}|{record['message']}")
        except (ValueError, KeyError, TypeError):
            plain.append(line)
    if logged:
        digest = _short("\n".join(sorted(logged)).encode())
        plain.append(f"[{len(logged)} log line(s), digest {digest}]")
    return plain


def _named(text: str, run: Path) -> str:
    """``text`` with the run directory, the fixture directory above it and the checkout named."""
    named = text.replace(str(run), "<RUN>").replace(str(run.parent), "<TMP>")
    return named.replace(str(REPO), "<REPO>")


def _normalise_report(path: Path, run: Path) -> Any:
    if not path.exists():
        return None
    report = json.loads(_named(path.read_text(), run))
    for key in ("generated_at", "command"):
        report.pop(key, None)
    # The artifact is refit on each run of this test: its bytes are not the pin.
    for artifact in report["artifacts"].values():
        artifact["sha256"] = artifact["manifest_sha256"] = "<hash>"
    return report


def _markdown(path: Path, run: Path) -> list[str] | None:
    if not path.exists():
        return None
    lines = _named(path.read_text(), run).splitlines()[1:]
    return [re.sub(r"sha256 `[0-9a-f]{64}`", "sha256 `<hash>`", line) for line in lines]


def run_all(base: Path) -> dict[str, Any]:
    """The pinned runs, reduced to JSON-able data (works against the pre-split script too).

    ``afr`` warns about each withheld-target reason once per process. The pinned log-line
    count was taken in a fresh process, so a worker that already ran another test would
    log one line fewer; this run starts from, and leaves, an empty record."""
    from src.signals import afr

    saved = afr._logged_refusals
    afr._logged_refusals = set()
    try:
        return _run_all(base)
    finally:
        afr._logged_refusals = saved


def _run_all(base: Path) -> dict[str, Any]:
    from scripts.evaluate_detectors import DriveScorer, main
    from tests.pinned_checkout_harness import pin_in_process

    fx = build_fixture(base)
    run = base / "run"
    run.mkdir()
    recorded: list[tuple[str, dict[str, Any]]] = []
    original = DriveScorer.score_drive

    def recording(self: Any, task: Any) -> Any:
        result = original(self, task)
        if os.getpid() == parent:
            recorded.append((task.drive.drive_id, _scored_view(task, result)))
        return result

    parent = os.getpid()

    def invoke(argv: list[str], record: bool = False) -> dict[str, Any]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if record:
                DriveScorer.score_drive = recording  # type: ignore[method-assign]
            try:
                code = main(argv)
            finally:
                DriveScorer.score_drive = original  # type: ignore[method-assign]
        out_path = Path(argv[argv.index("--out") + 1])
        return {
            "exit": code,
            "stdout": _clean_lines(out.getvalue(), run),
            "stderr": _clean_lines(err.getvalue(), run),
            "report": _normalise_report(out_path, run),
            "markdown": _markdown(out_path.with_suffix(".md"), run),
        }

    # The score cache keys on the working tree (src/, data/, the config), which other
    # sessions edit while this runs; hold those inputs at their first values (T01.20).
    with pytest.MonkeyPatch.context() as pinned:
        pin_in_process(pinned, base / "key-pins.json")
        result: dict[str, Any] = {}
        result["full"] = invoke(_argv(fx, run), record=True)
        result["full"]["scored"] = dict(recorded)
        full_report = result["full"]["report"]
        full_markdown = result["full"]["markdown"]
        result["rerun"] = invoke(_argv(fx, run, out="rerun.json"))
        first = json.loads((run / "report.json").read_text())
        result["compare_clean"] = invoke(
            [*_argv(fx, run, out="compare.json"), "--baseline", str(run / "report.json")]
        )
        graded = {"graded": 4, "detected": 4}
        first["recall"] = {"isolation_forest": {"thermostat": {"3.0": graded}}}
        (run / "tampered.json").write_text(json.dumps(first))
        result["compare_regressed"] = invoke(
            [*_argv(fx, run, out="regressed.json"), "--baseline", str(run / "tampered.json")]
        )
        result["plan"] = invoke([*_argv(fx, run, out="plan.json"), "--plan"])
        result["spawned"] = invoke(_argv(fx, run, workers="2", out="spawned.json"))
    # The five later runs hold only what differs from the full run's report.
    for name in ("rerun", "compare_clean", "compare_regressed", "spawned"):
        run_report = result[name]["report"]
        result[name]["report"] = {
            key: value for key, value in run_report.items() if full_report.get(key) != value
        }
        if result[name]["markdown"] == full_markdown:
            result[name]["markdown"] = "same as the full run's"
    return result


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def differences(expected: Any, actual: Any, path: str = "") -> list[str]:
    """Every place ``actual`` is not ``expected``: exact except floats (relative 1e-9)."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        out = (
            [f"{path}: keys differ: {sorted(set(expected) ^ set(actual))}"]
            if (expected.keys() != actual.keys())
            else []
        )
        for key in expected.keys() & actual.keys():
            out += differences(expected[key], actual[key], f"{path}/{key}")
        return out
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return [f"{path}: length {len(expected)} != {len(actual)}"]
        return [d for i, (a, b) in enumerate(zip(expected, actual, strict=True))
                for d in differences(a, b, f"{path}[{i}]")]  # fmt: skip
    if isinstance(expected, float) and isinstance(actual, float):
        if math.isnan(expected) and math.isnan(actual):
            return []
        if math.isclose(expected, actual, rel_tol=RELATIVE, abs_tol=1e-12):
            return []
        return [f"{path}: {expected!r} != {actual!r}"]
    if type(expected) is not type(actual) or expected != actual:
        return [f"{path}: {expected!r} != {actual!r}"]
    return []


def _round_trip(data: Any) -> Any:
    """What the golden file holds: the data through JSON (tuples to lists, int keys to strings)."""
    return json.loads(json.dumps(data, default=str))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def golden() -> dict[str, Any]:
    return json.loads(GOLDEN.read_text())  # type: ignore[no-any-return]


@pytest.fixture(scope="module")
def current(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return _round_trip(run_all(tmp_path_factory.mktemp("golden")))


@pytest.mark.slow
@pytest.mark.parametrize(
    "name", ["full", "rerun", "compare_clean", "compare_regressed", "plan", "spawned"]
)
def test_the_run_matches_the_pinned_one(
    name: str, golden: dict[str, Any], current: dict[str, Any]
) -> None:
    assert differences(golden[name], current[name], name) == []


@pytest.mark.slow
def test_the_pinned_runs_cover_the_cases_the_refactor_must_not_move(golden: dict[str, Any]) -> None:
    """The fixture exercises each rule (a golden that never reaches one pins nothing of it)."""
    report = golden["full"]["report"]
    assert report["detectors"] == ["isolation_forest", "robust_z", "pca", "plausibility"]
    assert report["baseline_fit_sessions"] == ["2026-05-10T000000", "2026-05-11T000000"]
    assert report["baseline_threshold_sessions"] == ["2026-05-12T000000"]
    assert set(report["baseline_thresholds"]) == {"robust_z", "pca"}
    drives = {d["drive"]: d for d in report["drives"]}
    # held-out drives are scored; the capture bridges its drive and is named, not scored
    assert set(drives) == {
        "drive-2026-05-13T000000",
        "drive-2026-05-14T000000",
        "drive-2026-05-19T000000",
    }
    assert list(drives["drive-2026-05-13T000000"]["excluded_sessions"]) == [
        "_capture-tuning-2026-05-13T000100"
    ]
    assert drives["drive-2026-05-19T000000"]["cross_era"] is True
    reasons = {e["drive"]: e["reason"] for e in report["skipped_drives"]}
    assert any(
        "seen by 2026-05-10T000000 (isolation_forest training" in r for r in reasons.values()
    )
    assert any(r.startswith("skipped: no default-profile rows") for r in reasons.values())
    assert any(r.startswith("skipped: no telemetry rows") for r in reasons.values())
    assert any("spans era" in r for r in reasons.values())
    # injections were accounted: some graded, some cross-era skipped, shift ran
    assert report["injections"]
    assert any(s["reason"].startswith("cross-era") for s in report["skipped_injections"])
    assert report["known_answers"]["single_quantity_shift"]["per_drive"]
    assert report["events"] and report["fault_events"] == 1
    # per-sample scores were recorded for every scored drive
    assert set(golden["full"]["scored"]) == set(drives)
    assert golden["compare_regressed"]["exit"] == 3
    assert golden["compare_clean"]["report"]["regressions"]["comparable"] is True
    assert any(line.startswith("score cache: ") for line in golden["rerun"]["stdout"])
    # re-running and spawning workers change nothing in the report
    assert golden["rerun"]["report"] == {} and golden["spawned"]["report"] == {}


def test_the_comparison_fails_on_a_real_change() -> None:
    """The comparison itself reports a moved float, a flipped verdict, a renamed skip
    reason, a dropped key and a changed exit status; and tolerates 1e-12 of float noise."""
    base = {
        "exit": 0,
        "report": {"x": 1.0, "ok": True, "reasons": ["skipped: seen", "skipped: straddles"]},
        "scored": {"d": {"score": {"sum": 12.5, "n": 300, "types": ["bool_", "NoneType"]}}},
    }
    assert differences(base, json.loads(json.dumps(base))) == []
    noisy = json.loads(json.dumps(base))
    noisy["report"]["x"] = 1.0 + 1e-12
    assert differences(base, noisy) == []
    for mutate in (
        lambda d: d["report"].__setitem__("x", 1.001),
        lambda d: d["report"].__setitem__("ok", False),
        lambda d: d["report"]["reasons"].__setitem__(0, "skipped: unseen"),
        lambda d: d["report"].pop("ok"),
        lambda d: d.__setitem__("exit", 3),
        lambda d: d["scored"]["d"]["score"].__setitem__("n", 299),
        lambda d: d["scored"]["d"]["score"]["types"].reverse(),
        lambda d: d["scored"]["d"]["score"].__setitem__("sum", 12.6),
    ):
        changed = json.loads(json.dumps(base))
        mutate(changed)
        assert differences(base, changed), mutate


def main_write() -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        data = _round_trip(run_all(Path(tmp)))
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    print(f"wrote {GOLDEN} ({GOLDEN.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    if "--write" in sys.argv:
        sys.exit(main_write())
    sys.exit("run with --write to regenerate the golden, or under pytest")
