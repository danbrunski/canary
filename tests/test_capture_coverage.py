"""Persisted-rows coverage and recompute-on-demand (roadmap C03.7 / C03.9 / C03.14).

Rows are written through the real Parquet writer path so the sidecar a
recompute produces is scored on the same bytes the daemon would have left.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.analysis.capture_coverage import (
    channel_lifetimes_from_rows,
    member_channels,
    read_quality_with_scope,
    recompute_capture_quality,
)
from src.analysis.channels import read_acquisition_frame
from src.collection.capture_quality import TUPLE_INCOMPLETE, UNKNOWN_LOSS_COUNTERS
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord

T0 = 1_700_000_000.0
STARTED = datetime.fromtimestamp(T0, tz=UTC)


def _obd(t: float, channel: str, *, round_id: int | None, generation: int = 1) -> TelemetryRecord:
    metadata = None
    if round_id is not None:
        metadata = {
            "ts_schema": 2,
            "acquired_at": t,
            "acquired_clock": "kernel",
            "generation": generation,
            "round_id": round_id,
        }
    return TelemetryRecord.obd(t, channel, 1.0, "", metadata=metadata)


def _session(root: Path, records: list[TelemetryRecord], *, profile: dict | None = None) -> Path:
    session = root / "_capture-fuel-map-2026-08-05T142720"
    write_records_to_parquet(records, session)
    snapshot = {
        "name": "fuel-map",
        "mode01": {"A": 1.0, "B": 1.0},
        "mode22": [],
        "target_rate_hz": 4.0,
        "required_tuple": ["A", "B"],
        "min_achieved_rate_hz": 3.0,
        "round_timestamps": True,
        "max_tuple_skew_s": 0.125,
        **(profile or {}),
    }
    (session / "capture_profile.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile": "fuel-map",
                "started_at": STARTED.isoformat(),
                "tune_revision": None,
                "profile_snapshot": snapshot,
            }
        )
    )
    return session


def _rounds(n: int, *, both: bool = True) -> list[TelemetryRecord]:
    records: list[TelemetryRecord] = []
    for r in range(n):
        t = T0 + r * 0.25
        records.append(_obd(t, "A", round_id=r))
        if both or r % 2:
            records.append(_obd(t + 0.003, "B", round_id=r))
    return records


def test_lifetimes_from_rows_match_the_ledger_shape() -> None:
    frame = read_acquisition_frame(
        __import__("pandas").DataFrame([r.parquet_row() for r in _rounds(240)])
    )
    rows = channel_lifetimes_from_rows(frame, ["A", "B", "C"], started_at=T0, ended_at=T0 + 60)
    assert rows["A"]["observations"] == 240
    assert rows["A"]["cadence_hz"] == pytest.approx(4.0)
    assert rows["A"]["missing_final_s"] == pytest.approx(60 - 239 * 0.25)
    assert rows["C"]["observations"] == 0 and rows["C"]["missing_initial_s"] == 60.0


def test_recompute_scores_a_complete_capture_from_its_rows(tmp_path: Path) -> None:
    session = _session(tmp_path, _rounds(240))
    quality = recompute_capture_quality(session, ended_at=T0 + 60)
    assert quality.scope == "recompute"
    assert quality.tuples["source"] == "persisted_rows"
    assert quality.tuples["complete"] == 240
    assert quality.interval["elapsed_s"] == pytest.approx(60.0)
    # No epochs were ever recorded for this session: unknown, never zero.
    assert quality.verdict == "unverified"
    assert {f["clause"] for f in quality.failures} == {UNKNOWN_LOSS_COUNTERS}
    assert any("loss counters unknown" in note for note in quality.notes)


def test_recompute_without_a_recorded_end_says_the_tail_is_undetectable(tmp_path: Path) -> None:
    session = _session(tmp_path, _rounds(240))
    quality = recompute_capture_quality(session)
    assert quality.interval["elapsed_s"] == pytest.approx(239 * 0.25 + 0.003)
    assert any("missing tail cannot be detected" in note for note in quality.notes)


def test_recompute_finds_the_disjoint_round_case_in_real_parquet(tmp_path: Path) -> None:
    session = _session(tmp_path, _rounds(240, both=False))
    quality = recompute_capture_quality(session, ended_at=T0 + 60)
    assert quality.tuples["complete"] == 120
    assert quality.tuples["member_misses"] == {"A": 0, "B": 120}
    # Rows before the writer existed: no round identity at all.
    legacy = _session(
        tmp_path / "legacy",
        [_obd(T0 + i, "A", round_id=None) for i in range(30)],
        profile={
            "mode01": {"A": 1.0},
            "required_tuple": ["A"],
            "target_rate_hz": 1.0,
            "min_achieved_rate_hz": 0.5,
            "max_tuple_skew_s": 0.1,
        },
    )
    quality = recompute_capture_quality(legacy, ended_at=T0 + 30)
    assert quality.tuples["source"] == "no_round_evidence"
    assert quality.verdict == "unverified"


def test_recompute_reuses_a_v2_sidecars_epochs_and_keeps_a_v1_sidecars_scope(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path, _rounds(20, both=False))
    (session / "capture_quality.json").write_text(
        json.dumps({"schema_version": 1, "verdict": "pass", "profile": "fuel-map"})
    )
    stored = read_quality_with_scope(session)
    assert stored is not None and stored["scope"] == "trailing_snapshot"
    assert stored["verdict"] == "pass"
    recomputed = recompute_capture_quality(session, ended_at=T0 + 10)
    assert TUPLE_INCOMPLETE not in {f["clause"] for f in recomputed.failures}
    assert recomputed.tuples["complete"] == 10  # odd rounds only

    v2 = recomputed.to_dict()
    v2["losses"] = {
        "recording": {"status": "verified", "lost": 0, "unverified": [], "counters": {}},
        "dashboard": {"status": "verified", "lost": 0, "unverified": [], "counters": {}},
    }
    # The evaluator persists the declared interval itself; nothing is injected.
    assert v2["interval"]["ended_wall"] == pytest.approx(T0 + 10)
    (session / "capture_quality.json").write_text(json.dumps(v2))
    again = recompute_capture_quality(session)
    assert again.losses["recording"]["status"] == "verified"
    assert again.interval["elapsed_s"] == pytest.approx(10.0)
    # The stored sidecar is never rewritten by a recompute.
    assert json.loads((session / "capture_quality.json").read_text()) == v2


def test_recompute_refuses_a_session_that_is_not_a_capture(tmp_path: Path) -> None:
    plain = tmp_path / "2026-08-05T142720"
    write_records_to_parquet(_rounds(4), plain)
    with pytest.raises(ValueError, match="not a capture session"):
        recompute_capture_quality(plain)


def test_member_channels_resolve_dids_through_the_catalogue() -> None:
    members = member_channels(
        {"mode01": {"RPM": 1.0}, "required_tuple": ["RPM", "ECM_MAF_GPS", "NOT_A_DID"]}
    )
    assert members["RPM"] == "RPM"
    assert members["ECM_MAF_GPS"].startswith("DID_")
    assert members["NOT_A_DID"] == "NOT_A_DID"
