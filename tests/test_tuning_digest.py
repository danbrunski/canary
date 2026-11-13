"""Tests for the tuning section of a post-drive digest (roadmap Phase 5)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.analysis.tuning_digest import compute_tuning_digest
from src.config import CanaryConfig
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord
from src.vehicle.profiles import resolve_profile_for_tooling, scope_config

T0 = datetime(2026, 8, 5, 14, 27, 20, tzinfo=UTC)


@pytest.fixture
def config() -> CanaryConfig:
    cfg = CanaryConfig.from_yaml(Path("canary.example.yaml"))
    return scope_config(cfg, resolve_profile_for_tooling(cfg, None))


def _capture(
    root: Path, name: str, samples: Sequence[tuple[float, float, float, float, float]]
) -> Path:
    records: list[TelemetryRecord] = []
    for i, (rpm, load, fs, ratio, lam) in enumerate(samples):
        ts = T0 + timedelta(seconds=i)
        records += [
            TelemetryRecord(timestamp=ts, source="obd", channel="RPM", value=rpm),
            TelemetryRecord(timestamp=ts, source="obd", channel="ENGINE_LOAD", value=load),
            TelemetryRecord(timestamp=ts, source="obd", channel="FUEL_STATUS", value=fs),
            TelemetryRecord(
                timestamp=ts, source="obd", channel="COMMANDED_EQUIV_RATIO", value=ratio
            ),
            TelemetryRecord(timestamp=ts, source="obd", channel="O2_WR_LAMBDA_S1", value=lam),
        ]
    out = root / name
    write_records_to_parquet(records, out)
    return out


def test_ordinary_drives_get_no_tuning_section(tmp_path: Path, config: CanaryConfig) -> None:
    """A normal drive must not sprout an empty tuning block.

    `None` distinguishes "not a tuning capture" from "a capture that found
    nothing", which are different things to a reader.
    """
    session = _capture(tmp_path, "2026-08-05T142720", [(3000, 90, 4, 1.2, 0.83)] * 4)
    assert compute_tuning_digest(session, config) is None


def test_capture_reports_wot_inventory_and_the_pair_count(
    tmp_path: Path, config: CanaryConfig
) -> None:
    """The pair count is the number that matters, not raw WOT seconds.

    WOT seconds without both a measured and a commanded AFR in the same
    second cannot answer anything — the July corpus had 25 such seconds
    against 4 usable ones.
    """
    session = _capture(
        tmp_path,
        "_capture-fuel-map-2026-08-05T142720",
        [(2600, 90, 4, 1.2, 0.83)] * 6 + [(3600, 92, 4, 1.2, 0.82)] * 4,
    )
    digest = compute_tuning_digest(session, config)
    assert digest is not None
    assert digest.wot.seconds == 10
    assert digest.wot.with_afr_pair == 10
    assert {row["rpm_band"] for row in digest.wot.by_band} == {
        "(2500.0, 3000.0]",
        "(3500.0, 4000.0]",
    }
    # R05.16: the delivery section is the acquisition-paired engine, and the
    # section says so, so a stored 1 s last-cell digest is distinguishable.
    payload = digest.to_dict()
    assert payload["schema_version"] == 2
    assert payload["delivery"]["engine_version"] == 2
    assert payload["delivery"]["pairing"]["target_source"] == "mode01_pid_0x44"
    # 10 target reads; the first cannot show the enrichment rule's 0.6 s dwell.
    assert payload["delivery"]["n"] == 9
    assert payload["delivery"]["gates"]["filtered"] == {"target_transient": 1}
    # The Mode 01 route (PID 0x44) models no transport delay.
    assert payload["delivery"]["pairing"]["transport_delay_modelled"] is False
    assert payload["delivery"]["pairing"]["session_informed_constants"] is False


def test_capture_resolves_its_tune_revision(tmp_path: Path, config: CanaryConfig) -> None:
    """Delivery error is meaningless without knowing which calibration produced it."""
    session = _capture(
        tmp_path, "_capture-fuel-map-2026-08-05T142720", [(3000, 90, 4, 1.2, 0.83)] * 5
    )
    digest = compute_tuning_digest(session, config)
    assert digest is not None
    assert digest.tune_revision == "ZS44A-k106"


def test_a_capture_with_no_usable_pairs_says_so(tmp_path: Path, config: CanaryConfig) -> None:
    """Silence about a failed capture is the failure mode this replaces."""
    records = [
        TelemetryRecord(timestamp=T0, source="obd", channel="RPM", value=3000.0),
        TelemetryRecord(timestamp=T0, source="obd", channel="ENGINE_LOAD", value=90.0),
    ]
    session = tmp_path / "_capture-fuel-map-2026-08-05T142720"
    write_records_to_parquet(records, session)
    digest = compute_tuning_digest(session, config)
    assert digest is not None
    assert digest.wot.with_afr_pair == 0
    assert any("cannot answer" in note or "no samples" in note for note in digest.notes)


def test_all_did_capture_never_falls_back_to_mode01(tmp_path: Path, config: CanaryConfig) -> None:
    """Mode-01 rows cannot make an all-DID capture appear analysable."""
    session = _capture(
        tmp_path,
        "_capture-tuning-did-2026-08-05T142720",
        [(3000, 90, 4, 1.2, 0.83)] * 4,
    )
    (session / "capture_profile.json").write_text(
        json.dumps(
            {
                "profile": "tuning-did",
                "started_at": T0.isoformat(),
                "profile_snapshot": {"corpus_eligible": False, "did_continuous": True},
            }
        )
    )
    digest = compute_tuning_digest(session, config)

    assert digest is not None
    assert digest.wot.seconds == 0
    # The point of the test: Mode-01 load rows are present and must not be
    # used. `unavailable` (not `inferred`) is what proves nothing was borrowed.
    assert digest.wot.source == "unavailable"
    assert digest.delivery is None
    assert any("pull inventory unavailable" in note for note in digest.notes)
    assert any("all-DID capture lacks confirmed semantic roles" in note for note in digest.notes)


# H07.2: the Logging page used to write pull_start/pull_end, which the digest
# did not count; both spellings must count as a tagged pull.
@pytest.mark.parametrize(
    "pull_labels",
    [("tuning_pull_start", "tuning_pull_end"), ("pull_start", "pull_end")],
    ids=["canonical", "logging-page-before-h07-2"],
)
def test_all_did_digest_carries_afr_lineage_rom_identity_and_polling_gaps(
    tmp_path: Path, config: CanaryConfig, pull_labels: tuple[str, str]
) -> None:
    """A partial all-DID drive still says exactly what its usable data means."""
    session = tmp_path / "_tuning-2026-08-05T142720"
    records: list[TelemetryRecord] = []
    for second in range(4):
        ts = T0 + timedelta(seconds=second)
        records.extend(
            [
                TelemetryRecord(timestamp=ts, source="obd", channel="DID_1219", value="0bb8"),
                # 2.170 V -> 14.719 AFR; 1.690 V -> 13.74 AFR via ECM table 0xE644.
                TelemetryRecord(timestamp=ts, source="obd", channel="DID_1225", value="01b2"),
                TelemetryRecord(timestamp=ts, source="obd", channel="DID_1226", value="0152"),
                # B06.7: the voltage route needs baro as of each read; 101 kPa
                # is inside the ECM's skip window, so the gain is 1.
                TelemetryRecord(
                    timestamp=ts, source="obd", channel="BAROMETRIC_PRESSURE", value=101.0
                ),
            ]
        )
    write_records_to_parquet(records, session)
    (session / "capture_profile.json").write_text(
        json.dumps(
            {
                "profile": "tuning-did",
                "started_at": T0.isoformat(),
                "profile_snapshot": {"corpus_eligible": False, "did_continuous": True},
            }
        )
    )
    (session / "tags.jsonl").write_text(
        "\n".join(
            json.dumps(tag)
            for tag in (
                {"t": T0.timestamp(), "label": pull_labels[0]},
                {"t": (T0 + timedelta(seconds=3)).timestamp(), "label": pull_labels[1]},
            )
        )
        + "\n"
    )

    digest = compute_tuning_digest(session, config)

    assert digest is not None
    assert digest.measured_afr["available"] is True
    assert digest.measured_afr["instrument"] == "factory A/F voltage transformed to AFR"
    assert digest.measured_afr["banks"][0]["samples"] == 4
    assert digest.measured_afr["banks"][0]["afr_median"] == pytest.approx(14.719)
    assert digest.pull_inventory == {
        "available": True,
        "method": "physical pull tags + semantic providers",
        "pulls": 1,
        "rpm_samples": 3,
        "rpm_seconds": 3,
        "rpm_floor": 2500.0,
        "rpm_over_floor_seconds": 3,
        "operator_demand_samples": None,
        "measured_afr_samples": 3,
        "fueled_samples": None,
        "commanded_afr_samples": None,
        "target_bearing_samples": None,
        "target_lookup_ready": False,
    }
    assert digest.rate_summary["did:ECM_RPM"] == {
        "samples": 4,
        "duration_seconds": 3.0,
        "observed_hz": 1.0,
        "median_interval_seconds": 1.0,
        "p95_interval_seconds": 1.0,
        "longest_gap_seconds": 1.0,
    }
    assert digest.target_map["rom_artifact"]["sha256"] == (
        "c5325776b08c3084edd49d8f68587dad4668e02b0e77c7686f7548fb6f7c8fa9"
    )
    assert digest.target_map["lookup_ready"] is False


def test_digest_reproduces_the_reference_capture(config: CanaryConfig) -> None:
    """End-to-end against the real 2026-08-05 capture and its hand-checked numbers."""
    session = Path("sessions/nissan-xterra-2008/_tuning-2026-08-05T142720")
    if not session.exists():  # pragma: no cover - corpus not present in CI
        pytest.skip("reference capture not present locally")
    digest = compute_tuning_digest(session, config)
    assert digest is not None
    assert digest.tune_revision == "ZS44A-k106"
    assert digest.wot.seconds == 27
    assert digest.delivery is not None
    # R05.16: acquisition-paired, judged by the enrichment rule (0.6 s /
    # 0.01 lambda dwell, 6 s post-cut holdoff, frozen 0.10 s delay). The 1 s
    # last-cell engine found 16; the closed-loop rule applied to enrichment
    # found 1; the enrichment rule finds 15 (band-avg +1.43 %).
    assert digest.delivery["engine_version"] == 2
    assert digest.delivery["n"] == 15
    filtered = digest.delivery["gates"]["filtered"]
    # Closed loop is still what makes the number meaningful at all: PID 0x44
    # is declared valid only for enrichment.
    assert filtered["source_not_valid_in_regime"] > 1000
    assert filtered["target_transient"] > 0 and filtered["post_fuel_cut"] > 0


def test_previous_capture_orders_by_time_not_directory_name(
    tmp_path: Path, config: CanaryConfig
) -> None:
    """Capture dirs carry different prefixes, so string order is not time order.

    `_capture-fuel-map-...` always sorts before `_tuning-...` regardless of
    date, so a capture a month *later* compared as "earlier" and could be
    picked as the previous run — silently comparing a drive against its own
    future.
    """
    from src.analysis.tuning_digest import _previous_capture
    from src.vehicle.tune_revisions import load_tune_revisions

    registry = load_tune_revisions(Path("data/nissan-xterra-2008/tune_revisions.yaml"))
    root = tmp_path / "vehicle"
    for name in (
        "_tuning-2026-08-06T100000",  # the session under test
        "_tuning-2026-08-05T100000",  # genuinely earlier
        "_capture-fuel-map-2026-09-01T100000",  # LATER, but sorts first by string
    ):
        path = root / name
        path.mkdir(parents=True)
        (path / "capture_profile.json").write_text(
            '{"profile":"tuning-did","started_at":"2026-08-05T10:00:00+00:00",'
            '"profile_snapshot":{"corpus_eligible":false}}\n'
        )

    found = _previous_capture(
        root / "_tuning-2026-08-06T100000",
        profile="tuning-did",
        revision="ZS44A-k106",
        revisions=registry,
    )
    assert found is not None
    assert found.name == "_tuning-2026-08-05T100000"


def _did_capture(root: Path, name: str, tps_volts: list[float], rpm: list[float]) -> Path:
    """An all-DID capture carrying only Mode 22 rows: rpm, TPS volts, both A/F banks."""
    records: list[TelemetryRecord] = []
    for second, (volts, revs) in enumerate(zip(tps_volts, rpm, strict=True)):
        ts = T0 + timedelta(seconds=second)
        records.extend(
            [
                TelemetryRecord(
                    timestamp=ts, source="obd", channel="DID_1219", value=f"{int(revs):04x}"
                ),
                TelemetryRecord(
                    timestamp=ts,
                    source="obd",
                    channel="DID_120F",
                    value=f"{round(volts / 0.005):04x}",
                ),
                TelemetryRecord(timestamp=ts, source="obd", channel="DID_1225", value="01b2"),
                TelemetryRecord(timestamp=ts, source="obd", channel="DID_1226", value="01b2"),
            ]
        )
    session = root / name
    write_records_to_parquet(records, session)
    (session / "capture_profile.json").write_text(
        json.dumps(
            {
                "profile": "tuning-did",
                "started_at": T0.isoformat(),
                "profile_snapshot": {"corpus_eligible": False, "did_continuous": True},
            }
        )
    )
    return session


def test_untagged_all_did_capture_infers_wot_from_the_throttle_stop(
    tmp_path: Path, config: CanaryConfig
) -> None:
    """An untagged capture still yields WOT seconds — labelled `inferred`.

    Before this, a 25-minute drive containing real full-throttle pulls
    reported `wot_seconds: 0` purely because nobody tagged it.
    """
    session = _did_capture(
        tmp_path,
        "_capture-tuning-did-2026-08-05T142720",
        tps_volts=[0.7, 0.7, 4.12, 4.12, 4.12, 0.7],
        rpm=[900, 1200, 3200, 3800, 4600, 1000],
    )

    digest = compute_tuning_digest(session, config)

    assert digest is not None
    assert digest.wot.source == "inferred"
    assert digest.wot.seconds == 3
    # B06.7: the measured AFR here is the A/F voltage route, which needs the
    # ECM's baro (Mode 01 PID 0x33; this ECM has no baro DID). The profile
    # disables Mode 01, so there is none -- refused by name, not at gain 1.
    assert digest.wot.with_measured_afr == 0
    assert digest.measured_afr["available"] is False
    assert all("baro_missing" in bank["refused"] for bank in digest.measured_afr["banks"])
    # A commanded target needs Mode 01, which this profile disables.
    assert digest.wot.with_afr_pair == 0
    assert digest.wot.criterion is not None
    assert digest.wot.criterion["provider"] == "did:ECM_THROTTLE_POS_SENSOR_1"
    assert {row["rpm_band"] for row in digest.wot.by_band} == {
        "(3000.0, 3500.0]",
        "(3500.0, 4000.0]",
        "(4500.0, 5000.0]",
    }
    assert any("INFERRED" in note for note in digest.notes)


def test_a_drive_that_never_opened_the_throttle_says_so(
    tmp_path: Path, config: CanaryConfig
) -> None:
    """Zero WOT because the throttle stayed shut is an ANSWER, not a gap.

    It must not be reported the same way as a capture that carried no demand
    channel at all — those two zeros call for opposite responses.
    """
    session = _did_capture(
        tmp_path,
        "_capture-tuning-did-2026-08-05T142720",
        tps_volts=[0.7, 1.4, 2.1, 1.8],
        rpm=[900, 2600, 3100, 2800],
    )

    digest = compute_tuning_digest(session, config)

    assert digest is not None
    assert digest.wot.seconds == 0
    assert digest.wot.source == "inferred"
    assert digest.wot.reason is not None
    assert "never opened" in digest.wot.reason
    assert digest.wot.criterion is not None


def test_tags_beat_the_inferred_criterion(tmp_path: Path, config: CanaryConfig) -> None:
    """Physical tags are stronger evidence and must win when both are present."""
    session = _did_capture(
        tmp_path,
        "_capture-tuning-did-2026-08-05T142720",
        tps_volts=[0.7, 0.7, 4.12, 4.12, 4.12, 0.7],
        rpm=[900, 3300, 3200, 3800, 4600, 3100],
    )
    (session / "tags.jsonl").write_text(
        json.dumps({"t": T0.timestamp(), "label": "tuning_pull_start"})
        + "\n"
        + json.dumps({"t": (T0 + timedelta(seconds=2)).timestamp(), "label": "tuning_pull_end"})
        + "\n"
    )

    digest = compute_tuning_digest(session, config)

    assert digest is not None
    assert digest.wot.source == "tagged"
    # Only the tagged window counts, and only its second above the rpm floor —
    # not the three seconds the throttle criterion would have selected.
    assert digest.wot.seconds == 1


def test_wot_source_travels_with_the_counts_in_the_serialised_digest(
    tmp_path: Path, config: CanaryConfig
) -> None:
    """Any reader that finds `wot_seconds` finds how it was established."""
    session = _did_capture(
        tmp_path,
        "_capture-tuning-did-2026-08-05T142720",
        tps_volts=[0.7, 4.12, 4.12],
        rpm=[900, 3200, 3800],
    )
    digest = compute_tuning_digest(session, config)
    assert digest is not None

    payload = digest.to_dict()
    assert payload["wot_seconds"] == payload["wot"]["seconds"] == 2
    assert payload["wot_source"] == payload["wot"]["source"] == "inferred"


def _manifest(session: Path, snapshot: dict[str, object]) -> None:
    (session / "capture_profile.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile": "fuel-map",
                "started_at": T0.isoformat(),
                "tune_revision": "ZS44A-k106",
                "profile_snapshot": snapshot,
            }
        )
    )


def test_rate_summary_covers_what_the_profile_asked_for_not_a_fixed_list(
    tmp_path: Path, config: CanaryConfig
) -> None:
    """The rate summary must include the channels that limited the analysis.

    It used to fall back to three hard-coded names — RPM, lambda, timing —
    because the semantic registry is DID-oriented and resolves nothing on a
    Mode-01 fuelling capture. Those three are the *fast* channels, so the
    2026-08-10 report showed 0.67-0.78 Hz and silently omitted every channel
    actually running at 0.28 Hz and gating the result.
    """
    session = _capture(
        tmp_path, "_capture-fuel-map-2026-08-05T142720", [(3000, 90, 4, 1.2, 0.83)] * 6
    )
    _manifest(
        session,
        {
            "name": "fuel-map",
            "mode01": {
                "RPM": 1.0,
                "ENGINE_LOAD": 1.0,
                "FUEL_STATUS": 1.0,
                "COMMANDED_EQUIV_RATIO": 1.0,
                "O2_WR_LAMBDA_S1": 1.0,
            },
            "required_tuple": [
                "RPM",
                "ENGINE_LOAD",
                "FUEL_STATUS",
                "COMMANDED_EQUIV_RATIO",
                "O2_WR_LAMBDA_S1",
            ],
        },
    )
    digest = compute_tuning_digest(session, config)
    assert digest is not None
    measured = set(digest.rate_summary)
    assert "obd:FUEL_STATUS" in measured
    assert "obd:COMMANDED_EQUIV_RATIO" in measured
    # p95 sits beside the median so a bad tail is visible without one
    # stop-and-restart dominating it the way `longest_gap_seconds` does.
    assert "p95_interval_seconds" in digest.rate_summary["obd:RPM"]


def test_complete_tuple_count_is_reported_and_says_how_it_knows(
    tmp_path: Path, config: CanaryConfig
) -> None:
    """The number that predicts the yield, and its evidence class."""
    session = _capture(
        tmp_path, "_capture-fuel-map-2026-08-05T142720", [(3000, 90, 4, 1.2, 0.83)] * 6
    )
    _manifest(
        session,
        {
            "name": "fuel-map",
            "mode01": {
                "RPM": 1.0,
                "ENGINE_LOAD": 1.0,
                "FUEL_STATUS": 1.0,
                "COMMANDED_EQUIV_RATIO": 1.0,
                "O2_WR_LAMBDA_S1": 1.0,
            },
            "required_tuple": [
                "RPM",
                "ENGINE_LOAD",
                "FUEL_STATUS",
                "COMMANDED_EQUIV_RATIO",
                "O2_WR_LAMBDA_S1",
            ],
        },
    )
    digest = compute_tuning_digest(session, config)
    assert digest is not None
    tuples = digest.complete_tuples
    assert tuples["available"] is True
    assert tuples["source"] == "declared"
    assert tuples["complete_cells"] == 6
    assert tuples["absent_members"] == []


def test_a_capture_predating_required_tuple_infers_it_and_labels_it(
    tmp_path: Path, config: CanaryConfig
) -> None:
    """Refusing outright would blind exactly the captures that motivated this.

    Substituting silently would present an inference as the profile's promise,
    so it is labelled `inferred` — the same discipline as WotInventory.source.
    """
    session = _capture(
        tmp_path, "_capture-fuel-map-2026-08-05T142720", [(3000, 90, 4, 1.2, 0.83)] * 6
    )
    _manifest(
        session,
        {
            "name": "fuel-map",
            "mode01": {
                "RPM": 1.0,
                "ENGINE_LOAD": 1.0,
                "FUEL_STATUS": 1.0,
                "COMMANDED_EQUIV_RATIO": 1.0,
                "O2_WR_LAMBDA_S1": 1.0,
            },
        },
    )
    digest = compute_tuning_digest(session, config)
    assert digest is not None
    assert digest.complete_tuples["source"] == "inferred"
    assert digest.complete_tuples["complete_cells"] == 6
    assert any("inferred, not declared" in note for note in digest.notes)


def test_a_missing_quality_sidecar_reads_as_unknown_never_as_a_pass(
    tmp_path: Path, config: CanaryConfig
) -> None:
    session = _capture(
        tmp_path, "_capture-fuel-map-2026-08-05T142720", [(3000, 90, 4, 1.2, 0.83)] * 5
    )
    digest = compute_tuning_digest(session, config)
    assert digest is not None
    assert digest.capture_quality is None
    assert any("not verified, and not a pass" in note for note in digest.notes)


def test_a_failed_capture_contract_is_carried_into_the_digest(
    tmp_path: Path, config: CanaryConfig
) -> None:
    """A rate-sensitive result must not be read without its capture's verdict."""
    session = _capture(
        tmp_path, "_capture-fuel-map-2026-08-05T142720", [(3000, 90, 4, 1.2, 0.83)] * 5
    )
    (session / "capture_quality.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "verdict": "fail",
                "profile": "fuel-map",
                "failures": [{"clause": "required_tuple_rate"}],
            }
        )
    )
    digest = compute_tuning_digest(session, config)
    assert digest is not None
    assert digest.capture_quality is not None
    assert digest.capture_quality["verdict"] == "fail"
    # A v1 sidecar keeps its limited scope: the note says so beside the clause.
    assert digest.capture_quality["scope"] == "trailing_snapshot"
    note = next(note for note in digest.notes if "required_tuple_rate" in note)
    assert "trailing 60 s" in note


def test_a_capture_with_no_resolvable_revision_names_it_and_gets_no_map(
    tmp_path: Path, config: CanaryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P02.16: ``AfrCalibration.target_map`` has no ``active_revision``
    fallback, so a session whose revision does not resolve gets no target map
    and the digest says why, rather than labelling the active map as its own."""
    import src.analysis.tuning_digest as digest_module

    monkeypatch.setattr(
        digest_module,
        "resolve_session_revision",
        lambda _session, _registry: (None, "unknown", False),
    )
    session = _capture(
        tmp_path, "_capture-fuel-map-2026-08-05T142720", [(3600, 92, 4, 1.2, 0.82)] * 4
    )
    digest = compute_tuning_digest(session, config)
    assert digest is not None
    assert digest.tune_revision is None
    assert digest.target_map["available"] is False
    assert digest.target_map["revision"] is None
    assert digest.target_map["reason"].startswith("revision_unknown")


def test_a_bank_whose_every_sample_has_stale_baro_is_refused_with_counts(
    tmp_path: Path,
) -> None:
    """B06.7: never drop a bank silently. Every A/F read here is more than the
    calibration's 60 s after the one baro read, so the bank is refused by
    name, with its counts and stale fraction."""
    from src.analysis.tuning_digest import _measured_afr_summary
    from src.signals.afr import load_afr_calibration

    session = tmp_path / "_tuning-2026-08-05T142720"
    records = [
        TelemetryRecord(
            timestamp=T0 - timedelta(seconds=200),
            source="obd",
            channel="BAROMETRIC_PRESSURE",
            value=80.0,
        )
    ]
    for second in range(4):
        ts = T0 + timedelta(seconds=second)
        records.append(
            TelemetryRecord(timestamp=ts, source="obd", channel="DID_1225", value="01b2")
        )
    write_records_to_parquet(records, session)
    summary = _measured_afr_summary(
        session,
        calibration=load_afr_calibration(Path("data/nissan-xterra-2008/afr_calibration.yaml")),
        role_capabilities={
            "measured_afr_b1": {"available": True, "provider": "did:ECM_AF_SENSOR_V_B1"}
        },
    )
    assert summary["available"] is False
    [bank] = summary["banks"]
    assert bank["refused"].startswith("no convertible sample: 4 read, 0 baro_missing, 4 baro_stale")
    assert bank["baro_stale_fraction"] == 1.0


def test_the_digest_reads_its_capture_once_and_says_the_same(
    config: CanaryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P02.2: one filtered read for every section, and the same digest as one
    read per call (what it did before), byte for byte."""
    from contextlib import contextmanager

    import src.analysis.channels_plan as channels_plan
    import src.analysis.tuning_digest as tuning_digest
    import src.analysis.tuning_frame as tuning_frame
    from src.preprocessing.loader import read_session

    capture = Path("tests/fixtures/lambda_delivery/_capture-tuning-2026-09-21T223734")
    calls: list[Path] = []
    real = read_session

    def counting(path: Path, **kwargs: Any) -> Any:
        calls.append(Path(path))
        return real(path, **kwargs)

    monkeypatch.setattr(channels_plan, "read_session", counting)
    shared = compute_tuning_digest(capture, config)
    assert shared is not None
    assert calls.count(capture) == 1, calls

    @contextmanager
    def per_call(*args: Any, **kwargs: Any) -> Any:
        yield None

    monkeypatch.setattr(tuning_digest, "shared_session_read", per_call)
    monkeypatch.setattr(tuning_frame, "shared_session_read", per_call)
    calls.clear()
    per_call_digest = compute_tuning_digest(capture, config)
    assert per_call_digest is not None
    assert calls.count(capture) > 10
    assert json.dumps(shared.to_dict(), sort_keys=True, default=str) == json.dumps(
        per_call_digest.to_dict(), sort_keys=True, default=str
    )


# R26.2: a read that raised is not a fact about the drive.


def _raise_on(
    monkeypatch: pytest.MonkeyPatch, name: str, when: Any, error: BaseException
) -> list[Any]:
    """Make ``tuning_digest.<name>`` raise ``error`` when ``when(args, kwargs)``; log calls."""
    from src.analysis import tuning_digest

    real = getattr(tuning_digest, name)
    seen: list[Any] = []

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if when(args, kwargs):
            seen.append(args)
            raise error
        return real(*args, **kwargs)

    monkeypatch.setattr(tuning_digest, name, wrapper)
    return seen


def _is_pair_read(args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
    return len(args[1]) > 2


def _assert_says_it_could_not_read(digest: Any, cause: str) -> None:
    assert digest.wot.read_failed is True
    assert digest.wot.reason is not None
    assert "could not read" in digest.wot.reason
    assert cause in digest.wot.reason
    assert digest.to_dict()["wot"]["read_failed"] is True
    assert any("a read failed" in note and cause in note for note in digest.notes)
    # The failed read is not stated as a fact about the drive.
    assert not any("none carry both" in note for note in digest.notes)


def test_a_failed_mode01_pair_read_is_not_reported_as_no_pair(
    tmp_path: Path, config: CanaryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _capture(
        tmp_path,
        "_capture-fuel-map-2026-08-05T142720",
        [(2600, 90, 4, 1.2, 0.83)] * 6 + [(3600, 92, 4, 1.2, 0.82)] * 4,
    )
    seen = _raise_on(monkeypatch, "read_aligned", _is_pair_read, ValueError("corrupt chunk 7"))

    digest = compute_tuning_digest(session, config)

    assert digest is not None
    assert seen, "the pair read was never attempted"
    assert digest.wot.seconds == 10
    assert digest.wot.with_afr_pair == 0
    _assert_says_it_could_not_read(digest, "corrupt chunk 7")


def test_a_failed_mode01_pair_read_outside_the_read_failures_propagates(
    tmp_path: Path, config: CanaryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _capture(
        tmp_path, "_capture-fuel-map-2026-08-05T142720", [(2600, 90, 4, 1.2, 0.83)] * 6
    )
    _raise_on(monkeypatch, "read_aligned", _is_pair_read, RuntimeError("a bug, not a bad read"))

    with pytest.raises(RuntimeError, match="a bug"):
        compute_tuning_digest(session, config)


def _infer(session: Path) -> Any:
    """``_infer_wot`` over the throttle-stop capture, with a measured-AFR spec to read.

    Called directly: through the digest, this profile's measured AFR is refused
    for want of a baro, so the role is unavailable and the read is never tried.
    """
    from src.analysis.tuning_digest import _infer_wot
    from src.analysis.tuning_frame import WotCriterion

    return _infer_wot(
        session,
        criterion=WotCriterion(
            unit="V",
            closed_value=0.7,
            wide_open_value=4.12,
            threshold_value=3.0,
            reached_stop_tolerance=0.1,
            rpm_floor=1500.0,
        ),
        rpm_spec="did:ECM_RPM",
        demand_spec="did:ECM_THROTTLE_POS_SENSOR_1",
        measured_afr_specs=["did:ECM_AFR_B1"],
        rpm_bands=(1500.0, 8000.0),
    )


def _inferred_session(tmp_path: Path) -> Path:
    return _did_capture(
        tmp_path,
        "_capture-tuning-did-2026-08-05T142720",
        tps_volts=[0.7, 0.7, 4.12, 4.12, 4.12, 0.7],
        rpm=[900, 1200, 3200, 3800, 4600, 1000],
    )


def test_a_failed_inferred_afr_read_is_not_reported_as_no_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _inferred_session(tmp_path)
    seen = _raise_on(monkeypatch, "read_aligned", _is_pair_read, KeyError("DID_1225"))

    wot = _infer(session)

    assert seen, "the measured-AFR read was never attempted"
    assert wot.source == "inferred"
    assert wot.seconds == 3
    assert wot.read_failed is True
    assert wot.with_measured_afr == 0
    assert wot.reason is not None
    assert "could not read" in wot.reason
    assert "DID_1225" in wot.reason


def test_a_failed_inferred_afr_read_outside_the_read_failures_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _inferred_session(tmp_path)
    _raise_on(monkeypatch, "read_aligned", _is_pair_read, TypeError("a bug"))

    with pytest.raises(TypeError, match="a bug"):
        _infer(session)


def _tagged_did_capture(tmp_path: Path) -> Path:
    session = _did_capture(
        tmp_path,
        "_capture-tuning-did-2026-08-05T142720",
        tps_volts=[0.7, 0.7, 4.12, 4.12, 4.12, 0.7],
        rpm=[900, 3300, 3200, 3800, 4600, 3100],
    )
    (session / "tags.jsonl").write_text(
        json.dumps({"t": T0.timestamp(), "label": "tuning_pull_start"})
        + "\n"
        + json.dumps({"t": (T0 + timedelta(seconds=2)).timestamp(), "label": "tuning_pull_end"})
        + "\n"
    )
    return session


def _is_tagged_read(args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
    return bool(kwargs.get("acquired_time")) and len(args[1]) == 1


def test_a_failed_tagged_read_says_the_tags_could_not_be_used(
    tmp_path: Path, config: CanaryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _tagged_did_capture(tmp_path)
    seen = _raise_on(monkeypatch, "read_channels", _is_tagged_read, ValueError("bad rpm chunk"))

    digest = compute_tuning_digest(session, config)

    assert digest is not None
    assert seen, "the tagged read was never attempted"
    # It falls back to the throttle stop, and says it did so and why.
    assert digest.wot.source == "inferred"
    assert digest.wot.read_failed is True
    assert digest.wot.reason is not None
    assert "tagged pulls present" in digest.wot.reason
    assert "bad rpm chunk" in digest.wot.reason
    assert any("a read failed" in note and "bad rpm chunk" in note for note in digest.notes)


def test_a_failed_tagged_read_outside_the_read_failures_propagates(
    tmp_path: Path, config: CanaryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _tagged_did_capture(tmp_path)
    _raise_on(monkeypatch, "read_channels", _is_tagged_read, RuntimeError("a bug"))

    with pytest.raises(RuntimeError, match="a bug"):
        compute_tuning_digest(session, config)
