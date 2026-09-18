from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.analysis.afr import load_afr_calibration
from src.analysis.tuning_frame import (
    Provider,
    build_tuning_frame,
    load_provider_registry,
    section_capabilities,
)
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord


def test_all_did_frame_uses_confirmed_semantic_roles(tmp_path: Path) -> None:
    session = tmp_path / "_capture-tuning-did-2026-08-05T120000"
    timestamp = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    write_records_to_parquet(
        [
            TelemetryRecord(timestamp=timestamp, source="obd", channel="DID_1219", value="09c4"),
            TelemetryRecord(timestamp=timestamp, source="obd", channel="DID_1225", value="01b8"),
            TelemetryRecord(timestamp=timestamp, source="obd", channel="DID_113A", value="80"),
            TelemetryRecord(timestamp=timestamp, source="obd", channel="DID_1135", value="81"),
            TelemetryRecord(timestamp=timestamp, source="obd", channel="DID_1163", value="82"),
        ],
        session,
    )
    (session / "capture_profile.json").write_text(
        json.dumps(
            {
                "profile": "tuning-did",
                "started_at": timestamp.isoformat(),
                "tune_revision": "test-revision",
                "profile_snapshot": {"corpus_eligible": False},
            }
        )
    )

    frame = build_tuning_frame(
        session,
        providers_path=Path("data/nissan-xterra-2008/tuning_providers.yaml"),
        calibration=load_afr_calibration(Path("data/nissan-xterra-2008/afr_calibration.yaml")),
    )

    assert frame.capture_profile == "tuning-did"
    assert frame.tune_revision == "test-revision"
    assert frame.capability["rpm"]["available"] is True
    assert frame.values["rpm"].iloc[0] == pytest.approx(2500.0)
    assert frame.values["measured_afr_b1_raw_voltage"].iloc[0] == pytest.approx(2.2)
    assert frame.values["measured_afr_b1"].iloc[0] == pytest.approx(14.83625)
    assert bool(frame.values["measured_afr_b1_censored"].iloc[0]) is False
    assert frame.capability["cam_command"]["available"] is True
    assert frame.capability["cam_measured"]["available"] is True
    assert frame.capability["cam_measured_b"]["available"] is True
    sections = section_capabilities(frame.capability)
    assert sections["measured_afr"]["available"] is True
    assert sections["cam_tracking"]["available"] is True


def test_wot_criterion_rejects_an_out_of_order_threshold(tmp_path: Path) -> None:
    """A threshold at or below the closed position would mark the whole drive WOT.

    Ordering is a measured constraint, so it belongs in the schema rather than
    in a comment nobody reads while editing a vehicle's YAML.
    """
    path = tmp_path / "providers.yaml"
    path.write_text(
        "schema_version: 1\n"
        "providers: []\n"
        "wot_criterion:\n"
        "  unit: V\n"
        "  closed_value: 0.62\n"
        "  wide_open_value: 4.12\n"
        "  threshold_value: 0.5\n"
        "  reached_stop_tolerance: 0.25\n"
        "  rpm_floor: 2500.0\n"
    )
    with pytest.raises(ValidationError, match="closed_value < threshold_value"):
        load_provider_registry(path)


def test_a_vehicle_without_a_measured_stop_has_no_criterion(tmp_path: Path) -> None:
    """No stop measured for this vehicle means no inference, not a borrowed default."""
    path = tmp_path / "providers.yaml"
    path.write_text("schema_version: 1\nproviders: []\n")
    assert load_provider_registry(path).wot_criterion is None


def test_round_stamped_rows_are_placed_on_their_acquisition_clock(tmp_path: Path) -> None:
    """C02.9: the tuning frame reads ``acquired_time=True``, so a round stamp
    shared by two readings does not put them on one instant."""
    session = tmp_path / "_capture-tuning-did-2026-09-07T000000"
    stamp = datetime(2026, 9, 7, 0, 0, tzinfo=UTC)
    rows = []
    for k in range(3):
        round_stamp = stamp + timedelta(seconds=0.25 * k)
        for channel, value, offset in (("DID_1219", "09c4", 0.004), ("DID_1225", "01b8", 0.041)):
            rows.append(
                TelemetryRecord(
                    timestamp=round_stamp,
                    source="obd",
                    channel=channel,
                    value=value,
                    metadata={
                        "ts_schema": 2,
                        "acquired_at": round_stamp.timestamp() + offset,
                        "acquired_clock": "kernel",
                        "generation": 1,
                        "round_id": k,
                    },
                )
            )
    write_records_to_parquet(rows, session)
    (session / "capture_profile.json").write_text(
        json.dumps(
            {
                "profile": "tuning-did",
                "started_at": stamp.isoformat(),
                "tune_revision": None,
                "profile_snapshot": {"corpus_eligible": False, "round_timestamps": True},
            }
        )
    )

    frame = build_tuning_frame(
        session,
        providers_path=Path("data/nissan-xterra-2008/tuning_providers.yaml"),
        calibration=load_afr_calibration(Path("data/nissan-xterra-2008/afr_calibration.yaml")),
    )
    values = frame.values.set_index("timestamp")
    rpm_times = values["rpm"].dropna().index
    afr_times = values["measured_afr_b1"].dropna().index
    assert len(rpm_times) == 3 and len(afr_times) == 3
    # 37 ms apart on the acquisition clock — not merged onto the round stamp.
    for rpm_at, afr_at in zip(rpm_times, afr_times, strict=True):
        assert (afr_at - rpm_at).total_seconds() == pytest.approx(0.037, abs=1e-5)


# --- base_fuel_schedule: a candidate scale provider carries its provenance ---

PROVIDERS = Path("data/nissan-xterra-2008/tuning_providers.yaml")


def test_a_candidate_scale_provider_cannot_omit_its_envelope_or_evidence() -> None:
    """The 0x1208 -> BFS scale is transitive and steady-state only; a bare
    candidate factor would be quoted against transients and WOT edges."""
    base = {
        "role": "base_fuel_schedule",
        "spec": "did:ECM_AIR_CHARGE",
        "transform": "scale",
        "factor": 1 / 2040,
        "unit": "ms",
        "confidence": "candidate",
    }
    with pytest.raises(ValidationError, match="conditions and evidence"):
        Provider.model_validate(base)
    with pytest.raises(ValidationError, match="evidence"):
        Provider.model_validate({**base, "conditions": "steady state"})
    with pytest.raises(ValidationError, match="conditions"):
        Provider.model_validate({**base, "evidence": "report.md"})
    ok = Provider.model_validate({**base, "conditions": "steady state", "evidence": "report.md"})
    assert ok.factor == pytest.approx(1 / 2040)


@pytest.mark.parametrize("factor", [None, 0.0, -0.001])
def test_scale_requires_a_positive_factor(factor: float | None) -> None:
    with pytest.raises(ValidationError, match="positive factor"):
        Provider(role="rpm", spec="did:ECM_RPM", transform="scale", factor=factor, unit="rpm")


def test_scale_requires_a_unit_and_factor_requires_scale() -> None:
    with pytest.raises(ValidationError, match="requires a unit"):
        Provider(role="rpm", spec="did:ECM_RPM", transform="scale", factor=2.0)
    with pytest.raises(ValidationError, match="only meaningful"):
        Provider(role="rpm", spec="did:ECM_RPM", factor=2.0)


def test_the_shipped_registry_declares_bfs_as_a_candidate_with_its_envelope() -> None:
    registry = load_provider_registry(PROVIDERS)
    bfs = next(p for p in registry.providers if p.role == "base_fuel_schedule")
    assert bfs.spec == "did:ECM_AIR_CHARGE"
    assert bfs.transform == "scale"
    assert bfs.confidence == "candidate"
    assert bfs.unit == "ms"
    assert bfs.factor == pytest.approx(1 / 2040, rel=1e-5)
    assert bfs.conditions and "steady state" in bfs.conditions.lower()
    assert bfs.evidence and "bfs1208.md" in bfs.evidence


def _air_charge_session(tmp_path: Path) -> Path:
    session = tmp_path / "_capture-fuel-pw-research-2026-09-18T200000"
    stamp = datetime(2026, 9, 18, 20, 0, tzinfo=UTC)
    rows = [
        TelemetryRecord(
            timestamp=stamp + timedelta(seconds=k),
            source="obd",
            channel="DID_1208",
            value=f"{raw:04x}",
        )
        for k, raw in enumerate((6000, 20400, 40800))
    ]
    rows.append(TelemetryRecord(timestamp=stamp, source="obd", channel="DID_1219", value="09c4"))
    write_records_to_parquet(rows, session)
    return session


def test_bfs_is_raw_air_charge_over_2040_only_when_candidates_are_allowed(
    tmp_path: Path,
) -> None:
    session = _air_charge_session(tmp_path)
    calibration = load_afr_calibration(Path("data/nissan-xterra-2008/afr_calibration.yaml"))

    default = build_tuning_frame(session, providers_path=PROVIDERS, calibration=calibration)
    assert default.capability["base_fuel_schedule"]["available"] is False
    assert "candidate" in str(default.capability["base_fuel_schedule"]["reason"])
    assert "base_fuel_schedule" not in default.values.columns

    opted = build_tuning_frame(
        session, providers_path=PROVIDERS, calibration=calibration, allow_candidates=True
    )
    cap = opted.capability["base_fuel_schedule"]
    assert cap["available"] is True
    assert cap["confidence"] == "candidate"
    assert cap["conditions"] and cap["evidence"]
    got = opted.values.dropna(subset=["base_fuel_schedule"])
    assert list(got["base_fuel_schedule_raw"]) == [6000, 20400, 40800]
    assert list(got["base_fuel_schedule"]) == pytest.approx([6000 / 2040, 10.0, 20.0], rel=1e-5)
