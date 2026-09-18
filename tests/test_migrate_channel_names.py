"""tools/migrate_channel_names.py — one-time DID rename migration (2026-09-18)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.storage.parquet_writer import TELEMETRY_SCHEMA
from tools import migrate_channel_names as tool

DIDS = Path("data/dids.yaml")
#: The real /proc scan, captured before the autouse fixture stubs it out.
_RUNNING_DAEMONS = tool.running_daemons

_ROWS = [
    ("obd", "DID_1208", None, "6d2a"),
    ("did", "ECM_INJECTOR_PW_B1", 27946.0, None),
    ("obd", "RPM", 812.0, None),
    ("did", "did:ECM_INTAKE_PRESSURE_1206__mean", 410.0, None),
    ("did", "ECM_INJECTOR_PW_B1X", 1.0, None),  # not a mapped name
    ("did", "ECM_TARGET_EQUIV_RATIO_CANDIDATE", 1.0, None),
]


def _write_session(root: Path) -> Path:
    session = root / "_capture-fuel-pw-research-2026-09-18T200809"
    session.mkdir(parents=True)
    t0 = datetime(2026, 9, 18, 20, 8, 9, tzinfo=UTC)
    table = pa.Table.from_pylist(
        [
            {
                "timestamp": t0.replace(microsecond=i),
                "source": source,
                "channel": channel,
                "value_number": number,
                "value_text": text,
                "value_bool": None,
                "unit": None,
                "payload": None,
                "metadata_json": None,
            }
            for i, (source, channel, number, text) in enumerate(_ROWS)
        ],
        schema=TELEMETRY_SCHEMA,
    )
    pq.write_table(table, session / "telemetry-00000.parquet", compression="snappy")
    (session / "capture_quality.json").write_text(
        json.dumps(
            {
                "channels": [{"channel": "ECM_INJECTOR_PW_B1"}, {"channel": "ECM_RPM"}],
                "evidence": {"ECM_TARGET_EQUIV_RATIO_CANDIDATE": 3},
                "note": "ECM_INJECTOR_PW_B1 was polled",  # prose: untouched
            },
            indent=2,
        )
        + "\n"
    )
    (session / "tags.jsonl").write_text(
        json.dumps({"label": "pull", "channel": "did:ECM_INJECTOR_PW_B1"})
        + "\n"
        + json.dumps({"label": "ECM_INJECTOR_PW_B1 looked odd"})
        + "\n"
    )
    return session


def _write_models(root: Path) -> Path:
    model_dir = root / "nissan-xterra-2008" / "lstm_autoencoder"
    model_dir.mkdir(parents=True)
    manifest = {
        "schema": {
            "feature_columns": ["did:ECM_INJECTOR_PW_B1", "did:ECM_INJECTOR_PW_B1__mean", "obd:RPM"]
        },
        "scaler": {"means": {"did:ECM_INJECTOR_PW_B1": 1.0, "obd:RPM": 2.0}},
    }
    (model_dir / "model.manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return model_dir


def _write_profiles(path: Path) -> Path:
    path.write_text(
        "profiles:\n"
        "- name: fuel-pw-research\n"
        "  # polls ECM_INJECTOR_PW_B1 beside the pulse pair\n"
        "  mode22:\n"
        "  - ECM_INJECTOR_PW_B1\n"
        "  - ECM_INTAKE_PRESSURE_1206\n"
        "  - ECM_INTAKE_PRESSURE_1207\n"
        "  - ECM_RPM\n"
        "  mode22_rate_weights:\n"
        "    ECM_INTAKE_PRESSURE_1207: 0.5\n"
        "  behavior_model_recipes:\n"
        "  - injector_pw_vs_rpm_throttle_did\n"
    )
    return path


@pytest.fixture
def tree(tmp_path: Path) -> dict[str, Path]:
    sessions = tmp_path / "sessions"
    models = tmp_path / "models"
    session = _write_session(sessions)
    model_dir = _write_models(models)
    profiles = _write_profiles(tmp_path / "capture_profiles.yaml")
    return {
        "sessions": sessions,
        "session": session,
        "models": models,
        "manifest": model_dir / "model.manifest.json",
        "profiles": profiles,
    }


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(p): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _args(tree: dict[str, Path], *extra: str) -> list[str]:
    return [
        "--sessions-root",
        str(tree["sessions"]),
        "--models-dir",
        str(tree["models"]),
        "--file",
        str(tree["profiles"]),
        *extra,
    ]


@pytest.fixture(autouse=True)
def _no_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tool, "running_daemons", lambda: [])


def test_the_shipped_map_agrees_with_the_catalog() -> None:
    """Targets are current DID names and no old name still resolves — the
    operator's no-alias decision, checked rather than assumed."""
    rename_map = tool.load_rename_map(tool.DEFAULT_MAP)
    assert tool.check_against_catalog(rename_map, DIDS) == []
    assert rename_map.channels["ECM_INJECTOR_PW_B1"] == "ECM_AIR_CHARGE"


def test_dry_run_changes_nothing_but_reports(
    tree: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _snapshot(tmp_path)
    assert tool.main(_args(tree)) == tool.EXIT_OK
    assert _snapshot(tmp_path) == before
    out = capsys.readouterr().out
    assert "would rewrite" in out
    assert "dry run:" in out


def test_apply_rewrites_only_mapped_names(tree: dict[str, Path]) -> None:
    chunk = tree["session"] / "telemetry-00000.parquet"
    before = pq.read_table(chunk)

    assert tool.main(_args(tree, "--apply", "--daemon-stopped")) == tool.EXIT_OK

    after = pq.read_table(chunk)
    assert after.schema == before.schema
    assert after.num_rows == before.num_rows
    assert after.column("channel").to_pylist() == [
        "DID_1208",
        "ECM_AIR_CHARGE",
        "RPM",
        "did:ECM_INJECTOR_PULSE_1__mean",
        "ECM_INJECTOR_PW_B1X",
        "ECM_ENRICHMENT_TARGET_EQUIV_RATIO_CANDIDATE",
    ]
    for name in before.schema.names:
        if name != "channel":
            assert after.column(name).equals(before.column(name)), name

    quality = json.loads((tree["session"] / "capture_quality.json").read_text())
    assert [c["channel"] for c in quality["channels"]] == ["ECM_AIR_CHARGE", "ECM_RPM"]
    assert quality["evidence"] == {"ECM_ENRICHMENT_TARGET_EQUIV_RATIO_CANDIDATE": 3}
    assert quality["note"] == "ECM_INJECTOR_PW_B1 was polled"

    tags = [json.loads(line) for line in (tree["session"] / "tags.jsonl").read_text().splitlines()]
    assert tags[0]["channel"] == "did:ECM_AIR_CHARGE"
    assert tags[1]["label"] == "ECM_INJECTOR_PW_B1 looked odd"

    manifest = json.loads(tree["manifest"].read_text())
    assert manifest["schema"]["feature_columns"] == [
        "did:ECM_AIR_CHARGE",
        "did:ECM_AIR_CHARGE__mean",
        "obd:RPM",
    ]
    assert manifest["scaler"]["means"] == {"did:ECM_AIR_CHARGE": 1.0, "obd:RPM": 2.0}

    profiles = tree["profiles"].read_text()
    assert "ECM_INJECTOR_PW_B1" not in profiles
    assert "INTAKE_PRESSURE" not in profiles
    assert "  - ECM_INJECTOR_PULSE_1\n" in profiles
    assert "    ECM_INJECTOR_PULSE_2: 0.5\n" in profiles
    assert "  - air_charge_vs_rpm_throttle_did\n" in profiles
    assert "# polls ECM_AIR_CHARGE beside" in profiles  # comments survive, renamed


def test_apply_is_idempotent(tree: dict[str, Path], tmp_path: Path) -> None:
    assert tool.main(_args(tree, "--apply", "--daemon-stopped")) == tool.EXIT_OK
    once = _snapshot(tmp_path)
    assert tool.main(_args(tree, "--apply", "--daemon-stopped")) == tool.EXIT_OK
    assert _snapshot(tmp_path) == once


def test_apply_requires_the_daemon_stopped_flag(tree: dict[str, Path], tmp_path: Path) -> None:
    before = _snapshot(tmp_path)
    assert tool.main(_args(tree, "--apply")) == tool.EXIT_REFUSED
    assert _snapshot(tmp_path) == before


def test_apply_refuses_while_the_daemon_runs(
    tree: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tool, "running_daemons", lambda: ["pid 42: python -m src.main"])
    before = _snapshot(tmp_path)
    assert tool.main(_args(tree, "--apply", "--daemon-stopped")) == tool.EXIT_REFUSED
    assert _snapshot(tmp_path) == before


def test_running_daemons_reads_proc_cmdlines(tmp_path: Path) -> None:
    (tmp_path / "101").mkdir()
    (tmp_path / "101" / "cmdline").write_bytes(b"/x/.venv/bin/python\0-m\0src.main\0--config\0c\0")
    (tmp_path / "102").mkdir()
    (tmp_path / "102" / "cmdline").write_bytes(b"python\0-m\0src.mcp_server\0")
    found = _RUNNING_DAEMONS(tmp_path)
    assert len(found) == 1 and found[0].startswith("pid 101:")


def test_a_map_disagreeing_with_the_catalog_is_refused(
    tree: dict[str, Path], tmp_path: Path
) -> None:
    bad = tmp_path / "map.yaml"
    bad.write_text("channels:\n  ECM_RPM: ECM_NOT_A_DID\n")
    before = _snapshot(tree["sessions"])
    assert tool.main(["--map", str(bad), *_args(tree)]) == tool.EXIT_REFUSED
    assert _snapshot(tree["sessions"]) == before


def test_model_binary_payload_is_rewritten(tmp_path: Path) -> None:
    joblib = pytest.importorskip("joblib")
    models = tmp_path / "models" / "isolation_forest"
    models.mkdir(parents=True)
    artifact = models / "model.joblib"
    payload = {
        "artifact_format_version": 1,
        "model_type": "isolation_forest",
        "models": {"cruise": ["ECM_INJECTOR_PW_B1"]},  # estimators: never touched
        "scaler": {"columns": ["did:ECM_INJECTOR_PW_B1__std"]},
        "manifest": {"schema": {"feature_columns": ["did:ECM_INTAKE_PRESSURE_1207"]}},
    }
    joblib.dump(payload, artifact)

    rc = tool.main(["--models-dir", str(tmp_path / "models"), "--apply", "--daemon-stopped"])
    assert rc == tool.EXIT_OK

    loaded = joblib.load(artifact)
    assert loaded["scaler"]["columns"] == ["did:ECM_AIR_CHARGE__std"]
    assert loaded["manifest"]["schema"]["feature_columns"] == ["did:ECM_INJECTOR_PULSE_2"]
    assert loaded["models"] == {"cruise": ["ECM_INJECTOR_PW_B1"]}


def test_torch_checkpoint_manifest_is_rewritten_and_weights_kept(tmp_path: Path) -> None:
    torch = pytest.importorskip("torch")
    models = tmp_path / "models" / "lstm_autoencoder"
    models.mkdir(parents=True)
    artifact = models / "model.pt"
    weights = {"encoder.weight": torch.arange(4, dtype=torch.float32)}
    torch.save(
        {
            "artifact_format_version": 1,
            "model_type": "lstm_autoencoder",
            "state_dict": weights,
            "manifest": {"thresholds": {"channel_errors": {"did:ECM_INJECTOR_PW_B1": 0.3}}},
        },
        artifact,
    )

    rc = tool.main(["--models-dir", str(tmp_path / "models"), "--apply", "--daemon-stopped"])
    assert rc == tool.EXIT_OK

    loaded = torch.load(artifact, map_location="cpu", weights_only=False)
    assert loaded["manifest"]["thresholds"]["channel_errors"] == {"did:ECM_AIR_CHARGE": 0.3}
    assert torch.equal(loaded["state_dict"]["encoder.weight"], weights["encoder.weight"])
