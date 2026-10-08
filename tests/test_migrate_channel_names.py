"""tools/migrate_channel_names.py — one-time DID (2026-09-18) and memory-channel
(2026-10-05) rename migration."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml

from src.storage.parquet_writer import TELEMETRY_SCHEMA
from tools import migrate_channel_names as tool

REPO = Path(__file__).resolve().parents[1]
DIDS = REPO / "data" / "dids.yaml"
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


# --- memory channels (2026-10-05: 0xFFFF2842) ----------------------------------

KNOCK_MAP = REPO / "tools/channel_renames/2026-10-05-knock-trim-diff-flags.yaml"
OLD_MEM = "ECM_TIMING_TRIM_SELECT_FLAGS"
NEW_MEM = "ECM_KNOCK_TRIM_DIFF_FLAGS"

#: (source, channel before, channel after). Memory names are stored as the
#: reading, its sentinel state, and on K-line with and without a module.
_MEM_ROWS = [
    ("obd", f"mem:{OLD_MEM}", f"mem:{NEW_MEM}"),
    ("obd", f"mem:{OLD_MEM}_SENTINEL", f"mem:{NEW_MEM}_SENTINEL"),
    ("obd", "mem:ECM_PEDAL_ENRICH_FLAGS", "mem:ECM_PEDAL_ENRICH_FLAGS"),
    ("obd", "mem:flags_2841", "mem:flags_2841"),  # a window request, not a channel
    ("obd", f"mem:{OLD_MEM}X", f"mem:{OLD_MEM}X"),  # whole names only
    ("obd", f"mem:X{OLD_MEM}", f"mem:X{OLD_MEM}"),
    ("obd", f"mem:{OLD_MEM}_SENTINELS", f"mem:{OLD_MEM}_SENTINELS"),
    ("kline", f"ECM:MEM_{OLD_MEM}", f"ECM:MEM_{NEW_MEM}"),
    ("kline", f"MEM_{OLD_MEM}", f"MEM_{NEW_MEM}"),  # pre-K01.1, no module
    ("did", f"did:{OLD_MEM}", f"did:{OLD_MEM}"),  # a memory name is never a DID column
]


def _write_chunk(path: Path, channels: list[tuple[str, str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    t0 = datetime(2026, 10, 4, 20, 45, 57, tzinfo=UTC)
    table = pa.Table.from_pylist(
        [
            {
                "timestamp": t0.replace(microsecond=i),
                "source": source,
                "channel": channel,
                "value_number": 4.0,
                "value_text": None,
                "value_bool": None,
                "unit": "counts",
                "payload": b"\x00\x04",
                "metadata_json": None,
            }
            for i, (source, channel) in enumerate(channels)
        ],
        schema=TELEMETRY_SCHEMA,
    )
    pq.write_table(table, path, compression="snappy")
    return path


def _knock(*extra: str) -> list[str]:
    return ["--map", str(KNOCK_MAP), *extra]


def test_the_knock_trim_diff_map_agrees_with_the_memory_catalog() -> None:
    """The target is a memory channel and the old name resolves nowhere: no alias."""
    rename_map = tool.load_rename_map(KNOCK_MAP)
    assert rename_map.memory == {OLD_MEM: NEW_MEM}
    assert rename_map.channels == {} and rename_map.identifiers == {}
    assert tool.check_against_catalog(rename_map, DIDS) == []


def test_memory_readings_in_parquet_are_renamed_exactly(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    chunk = _write_chunk(
        sessions / "_capture-ram-study-2026-10-04T204557" / "telemetry-00000.parquet",
        [(source, before) for source, before, _ in _MEM_ROWS],
    )
    before = pq.read_table(chunk)

    assert tool.main(_knock("--sessions-root", str(sessions))) == tool.EXIT_OK
    assert pq.read_table(chunk).equals(before)  # dry run

    assert tool.main(_knock("--sessions-root", str(sessions), "--apply", "--daemon-stopped")) == 0
    after = pq.read_table(chunk)
    assert after.column("channel").to_pylist() == [expected for *_, expected in _MEM_ROWS]
    assert after.schema == before.schema
    for name in before.schema.names:
        if name != "channel":
            assert after.column(name).equals(before.column(name)), name


def test_a_saved_profile_memory_list_is_rewritten_and_then_accepted(tmp_path: Path) -> None:
    """The Pi's ``ram-study`` override lists the old name; once migrated, the
    daemon's resolver accepts it (before, it refuses the whole profile)."""
    from src.collection.capture_profiles import CaptureProfileOverrides
    from src.collection.obd_scheduler import profile_memory_union

    overrides = tmp_path / "capture_profile_overrides.yaml"
    overrides.write_text(
        "schema_version: 1\n"
        "profiles:\n"
        "- name: ram-study\n"
        f"  # 2842 ({OLD_MEM}); not {OLD_MEM}X, X{OLD_MEM} or {OLD_MEM}_SENTINELS\n"
        "  storage_prefix: _capture-ram-study\n"
        "  corpus_eligible: false\n"
        "  memory:\n"
        "  - ECM_PEDAL_ENRICH_FLAGS\n"
        f"  - {OLD_MEM}\n"
        "  - ECM_TARGET_EQUIV_RATIO\n"
        "deleted: []\n"
    )
    stale = CaptureProfileOverrides.model_validate(yaml.safe_load(overrides.read_text()))
    with pytest.raises(ValueError, match=OLD_MEM):
        profile_memory_union(stale.profiles)

    rc = tool.main(_knock("--file", str(overrides), "--apply", "--daemon-stopped"))
    assert rc == tool.EXIT_OK

    text = overrides.read_text()
    assert f"  - {NEW_MEM}\n" in text
    assert f"  # 2842 ({NEW_MEM}); not {OLD_MEM}X, X{OLD_MEM} or {OLD_MEM}_SENTINELS\n" in text
    migrated = CaptureProfileOverrides.model_validate(yaml.safe_load(text))
    assert migrated.profiles[0].memory == (
        "ECM_PEDAL_ENRICH_FLAGS",
        NEW_MEM,
        "ECM_TARGET_EQUIV_RATIO",
    )
    assert [c.name for c in profile_memory_union(migrated.profiles)] == [
        "ECM_TARGET_EQUIV_RATIO",
        "ECM_PEDAL_ENRICH_FLAGS",
        NEW_MEM,
    ]


def test_yaml_specs_naming_a_memory_reading_or_its_sentinel_are_rewritten(
    tmp_path: Path,
) -> None:
    providers = tmp_path / "tuning_providers.yaml"
    providers.write_text(
        f"spec: 'mem:{OLD_MEM}'\n"
        f"sentinel_spec: 'mem:{OLD_MEM}_SENTINEL'\n"
        f"feature: obd:mem:{OLD_MEM}__mean\n"
        f"other: mem:{OLD_MEM}_B\n"
    )
    assert tool.main(_knock("--file", str(providers), "--apply", "--daemon-stopped")) == 0
    assert providers.read_text() == (
        f"spec: 'mem:{NEW_MEM}'\n"
        f"sentinel_spec: 'mem:{NEW_MEM}_SENTINEL'\n"
        f"feature: obd:mem:{NEW_MEM}__mean\n"
        f"other: mem:{OLD_MEM}_B\n"
    )


def test_json_sidecars_rename_exact_memory_names_only(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    session = sessions / "_capture-ram-study-2026-10-04T204557"
    session.mkdir(parents=True)
    (session / "capture_profile.json").write_text(
        json.dumps(
            {
                "profile": "ram-study",
                "profile_snapshot": {
                    "memory": ["ECM_PEDAL_ENRICH_FLAGS", OLD_MEM],
                    "kline": [OLD_MEM],
                    "description": f"reads {OLD_MEM}",  # prose: untouched
                },
            },
            indent=2,
        )
        + "\n"
    )
    (session / "capture_quality.json").write_text(
        json.dumps(
            {
                "channels": [
                    {"channel": f"mem:{OLD_MEM}", "profile_channel": OLD_MEM},
                    {"channel": f"kline:{OLD_MEM}"},
                    {"channel": f"mem:{OLD_MEM}X"},
                ],
                "achieved_hz": {f"mem:{OLD_MEM}": 0.12, f"mem:X{OLD_MEM}": 1.0},
            },
            indent=2,
        )
        + "\n"
    )
    (session / "digest.json").write_text(
        json.dumps({"columns": [f"obd:mem:{OLD_MEM}", f"obd:mem:{OLD_MEM}__std"]})
    )

    assert tool.main(_knock("--sessions-root", str(sessions), "--apply", "--daemon-stopped")) == 0

    profile = json.loads((session / "capture_profile.json").read_text())
    snapshot = profile["profile_snapshot"]
    assert snapshot["memory"] == ["ECM_PEDAL_ENRICH_FLAGS", NEW_MEM]
    assert snapshot["kline"] == [NEW_MEM]
    assert snapshot["description"] == f"reads {OLD_MEM}"
    quality = json.loads((session / "capture_quality.json").read_text())
    assert quality["channels"] == [
        {"channel": f"mem:{NEW_MEM}", "profile_channel": NEW_MEM},
        {"channel": f"kline:{NEW_MEM}"},
        {"channel": f"mem:{OLD_MEM}X"},
    ]
    assert quality["achieved_hz"] == {f"mem:{NEW_MEM}": 0.12, f"mem:X{OLD_MEM}": 1.0}
    digest = json.loads((session / "digest.json").read_text())
    assert digest["columns"] == [f"obd:mem:{NEW_MEM}", f"obd:mem:{NEW_MEM}__std"]

    once = _snapshot(sessions)
    assert tool.main(_knock("--sessions-root", str(sessions), "--apply", "--daemon-stopped")) == 0
    assert _snapshot(sessions) == once  # idempotent


@pytest.mark.parametrize(
    ("entry", "problem"),
    [
        # the source is still a memory channel: the rename would strand it
        ("ECM_PEDAL_ENRICH_FLAGS: ECM_KNOCK_TRIM_DIFF_FLAGS", "memory source"),
        # ... or still a DID: memory and DID names share one namespace
        ("ECM_AIR_CHARGE: ECM_KNOCK_TRIM_DIFF_FLAGS", "memory source"),
        # the target is not catalogued at all
        (f"{OLD_MEM}: ECM_NOT_A_MEMORY_CHANNEL", "memory target"),
        # the target is a DID, not a memory channel
        (f"{OLD_MEM}: ECM_AIR_CHARGE", "memory target"),
    ],
)
def test_a_memory_map_disagreeing_with_the_catalog_is_refused(
    tmp_path: Path, entry: str, problem: str, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "map.yaml"
    bad.write_text(f"memory_channels:\n  {entry}\n")
    target = tmp_path / "profiles.yaml"
    target.write_text(f"memory:\n- {OLD_MEM}\n- ECM_PEDAL_ENRICH_FLAGS\n")
    before = target.read_bytes()

    rc = tool.main(["--map", str(bad), "--file", str(target), "--apply", "--daemon-stopped"])

    assert rc == tool.EXIT_REFUSED
    assert target.read_bytes() == before
    assert problem in capsys.readouterr().err


def test_a_name_in_two_sections_or_a_chain_is_refused(tmp_path: Path) -> None:
    both = tmp_path / "both.yaml"
    both.write_text(f"channels:\n  {OLD_MEM}: ECM_AIR_CHARGE\nmemory_channels:\n  {OLD_MEM}: B\n")
    with pytest.raises(tool.MapError, match="more than one section"):
        tool.load_rename_map(both)
    chain = tmp_path / "chain.yaml"
    chain.write_text(f"channels:\n  A: {OLD_MEM}\nmemory_channels:\n  {OLD_MEM}: B\n")
    with pytest.raises(tool.MapError, match="both renamed and rename targets"):
        tool.load_rename_map(chain)
