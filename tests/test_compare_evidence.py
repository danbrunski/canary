"""E01.2: the channel comparison mints evidence, and recomputes supersede."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.analysis.compare import PairingPolicy, compare_channels
from src.analysis.compare_evidence import ALL_CONDITIONS, mint_channel_comparison
from src.evidence import Evidence, MemoryLedger
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord
from src.vehicle.tune_revisions import TuneRevision, TuneRevisionRegistry

BINDINGS = Path("data/bindings.yaml")
DIDS = Path("data/dids.yaml")
T0 = datetime(2026, 8, 6, 12, 0, 0, tzinfo=UTC)
LOOSE = PairingPolicy.within("1s")
REGISTRY = TuneRevisionRegistry(
    revisions=[
        TuneRevision(revision="ZS44A-old", flashed_at=datetime(2026, 7, 1, tzinfo=UTC)),
        TuneRevision(revision="ZS44A-new", flashed_at=datetime(2026, 8, 1, tzinfo=UTC)),
        TuneRevision(revision="ZS44A-later", flashed_at=datetime(2026, 9, 1, tzinfo=UTC)),
    ]
)


def _session(tmp_path: Path, *, offset: float = 0.0, with_speed: bool = True) -> Path:
    records: list[TelemetryRecord] = []
    for second in range(60):
        stamp = T0 + timedelta(seconds=second)
        rpm = 800.0 + 20.0 * second
        records.append(
            TelemetryRecord(timestamp=stamp, source="obd", channel="RPM", value=rpm, unit="rpm")
        )
        if with_speed:
            records.append(
                TelemetryRecord(
                    timestamp=stamp, source="obd", channel="SPEED", value=rpm + offset, unit="rpm"
                )
            )
    out = tmp_path / "2026-08-06T120000"
    write_records_to_parquet(records, out)
    return out


def _mint(
    ledger: MemoryLedger,
    session: Path,
    registry: TuneRevisionRegistry | None = REGISTRY,
    policy: PairingPolicy = LOOSE,
) -> str:
    return mint_channel_comparison(
        ledger,
        session,
        "obd:RPM",
        "obd:SPEED",
        policy=policy,
        vin=None,
        program_id=None,
        tune_registry=registry,
        bindings_path=BINDINGS,
        dids_path=DIDS,
    )


def test_the_tool_persists_evidence_and_returns_its_id(tmp_path: Path) -> None:
    session = _session(tmp_path)
    ledger = MemoryLedger(tmp_path / "memory.jsonl", vehicle="truck")
    evidence_id = _mint(ledger, session)
    record = ledger.resolve_evidence(evidence_id).record
    assert isinstance(record, Evidence)
    comparison = compare_channels(
        session, "obd:RPM", "obd:SPEED", policy=LOOSE, bindings_path=BINDINGS, dids_path=DIDS
    )
    assert record.values["r_squared"].value == pytest.approx(comparison.r_squared)
    assert record.values["difference_bias"].value == pytest.approx(comparison.difference_bias)
    assert record.values["offset"].unit == "rpm"
    assert record.labels["scale_verdict"] == comparison.scale_verdict
    assert record.quality.n_observations == comparison.n_pairs == 60
    (source,) = record.sources
    assert source.session == session.name
    assert source.window_start == T0
    assert source.window_end == T0 + timedelta(seconds=59)
    assert set(record.input_hashes) == {"session_files", "bindings", "dids", "pairs"}
    assert record.policy["pairing"] == LOOSE.as_dict()
    assert record.provenance.producer == "compare_channels"
    assert record.provenance.code_version
    assert record.result.status == "evaluated"
    # Re-running on unchanged inputs is the same record, not a duplicate.
    assert _mint(ledger, session) == evidence_id
    assert len(ledger.entries()) == 1


def test_a_recompute_on_changed_data_supersedes_and_links_its_inputs(tmp_path: Path) -> None:
    ledger = MemoryLedger(tmp_path / "memory.jsonl", vehicle="truck")
    first = _mint(ledger, _session(tmp_path / "a"))
    second = _mint(ledger, _session(tmp_path / "b", offset=5.0))
    assert second != first
    new = ledger.entry(second).record
    old = ledger.entry(first)
    assert new.supersedes == (first,)
    assert old.superseded_by == (second,)
    assert old.record.input_hashes["pairs"] != new.input_hashes["pairs"]  # type: ignore[union-attr]


def test_a_different_policy_is_a_different_claim_not_a_recompute(tmp_path: Path) -> None:
    ledger = MemoryLedger(tmp_path / "memory.jsonl", vehicle="truck")
    session = _session(tmp_path)
    loose = _mint(ledger, session)
    tight = _mint(ledger, session, policy=PairingPolicy.within("20ms"))
    assert tight != loose
    assert ledger.entry(loose).current
    assert ledger.entry(tight).current
    assert ledger.entry(tight).record.supersedes == ()


def test_an_uncaptured_input_is_unevaluable_evidence_not_a_number(tmp_path: Path) -> None:
    ledger = MemoryLedger(tmp_path / "memory.jsonl", vehicle="truck")
    evidence_id = _mint(ledger, _session(tmp_path, with_speed=False))
    record = ledger.resolve_evidence(evidence_id).record
    assert isinstance(record, Evidence)
    assert record.result.status == "unevaluable"
    assert record.result.reason == "missing_input"
    assert record.result.residual is None
    assert record.values == {}
    assert "obd:SPEED" in record.result.detail


def test_the_tool_states_its_own_conditions_and_calibration(tmp_path: Path) -> None:
    """A caller cannot stamp an unfiltered comparison with a regime or a tune."""
    ledger = MemoryLedger(tmp_path / "memory.jsonl", vehicle="truck")
    session = _session(tmp_path)
    with pytest.raises(TypeError):
        mint_channel_comparison(  # type: ignore[call-arg]
            ledger,
            session,
            "obd:RPM",
            "obd:SPEED",
            policy=LOOSE,
            vin=None,
            program_id=None,
            tune_registry=REGISTRY,
            conditions={"regime": "WOT: throttle>=85"},
            bindings_path=BINDINGS,
            dids_path=DIDS,
        )
    record = ledger.resolve_evidence(_mint(ledger, session)).record
    assert isinstance(record, Evidence)
    assert record.conditions.regime == ALL_CONDITIONS
    assert record.vehicle.calibration == "ZS44A-new"  # the flash in force on 2026-08-06
    assert record.vehicle.profile == "truck"
    other = MemoryLedger(tmp_path / "other.jsonl", vehicle="truck")
    unknown = other.resolve_evidence(_mint(other, session, registry=None)).record
    assert unknown.vehicle.calibration is None


def test_the_cli_mints_into_the_scoped_ledger(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from src.config import CanaryConfig
    from src.evidence import memory_ledger_for
    from src.vehicle.profiles import resolve_profile_for_tooling, scope_config
    from tests.configs import write_isolated_config
    from tools.mint_comparison import main

    config_path = write_isolated_config(tmp_path, "example")
    base = CanaryConfig.from_yaml(config_path)
    config = scope_config(base, resolve_profile_for_tooling(base, "truck"))
    _session(config.collection.storage_path)
    argv = [
        "--config",
        str(config_path),
        "--profile",
        "truck",
        "--session",
        "2026-08-06T120000",
        "--tolerance",
        "1s",
        "obd:RPM",
        "obd:SPEED",
        "--json",
    ]
    assert main(argv) == 0
    printed = json.loads(capsys.readouterr().out)
    stored = memory_ledger_for(config).resolve_evidence(printed["id"]).record
    assert isinstance(stored, Evidence)
    assert stored.provenance.producer == "compare_channels"
    assert stored.conditions.regime == ALL_CONDITIONS
    assert memory_ledger_for(config).path.is_relative_to(tmp_path)
