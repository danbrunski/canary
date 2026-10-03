"""Tests for the tuning section of a post-drive digest (roadmap Phase 5)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
    root: Path, name: str, samples: list[tuple[float, float, float, float, float]]
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
                # 2.170 V -> 14.72 AFR; 1.690 V -> 13.75 AFR via the vehicle table.
                TelemetryRecord(timestamp=ts, source="obd", channel="DID_1225", value="01b2"),
                TelemetryRecord(timestamp=ts, source="obd", channel="DID_1226", value="0152"),
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
    assert digest.measured_afr["banks"][0]["afr_median"] == pytest.approx(14.72)
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
    assert digest.wot.with_measured_afr == 3
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
