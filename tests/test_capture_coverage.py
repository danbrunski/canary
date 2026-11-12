"""Persisted-rows coverage and recompute-on-demand (roadmap C03.7 / C03.9 / C03.14).

Rows are written through the real Parquet writer path so the sidecar a
recompute produces is scored on the same bytes the daemon would have left.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from src.analysis.capture_coverage import (
    channel_lifetimes_from_rows,
    member_channels,
    read_quality_with_scope,
    recompute_capture_quality,
)
from src.analysis.channels import read_acquisition_frame
from src.collection.capture_quality import (
    ACQUISITION_GAP,
    TUPLE_INCOMPLETE,
    UNKNOWN_LOSS_COUNTERS,
)
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord
from tests.builders import (
    CAPTURE_T0,
    capture_obd_record,
    capture_rounds,
    write_capture_session,
)


def test_lifetimes_from_rows_match_the_ledger_shape() -> None:
    frame = read_acquisition_frame(
        __import__("pandas").DataFrame([r.parquet_row() for r in capture_rounds(240)])
    )
    rows = channel_lifetimes_from_rows(
        frame, ["A", "B", "C"], started_at=CAPTURE_T0, ended_at=CAPTURE_T0 + 60
    )
    assert rows["A"]["observations"] == 240
    assert rows["A"]["cadence_hz"] == pytest.approx(4.0)
    assert rows["A"]["missing_final_s"] == pytest.approx(60 - 239 * 0.25)
    assert rows["C"]["observations"] == 0 and rows["C"]["missing_initial_s"] == 60.0


def test_recompute_scores_a_complete_capture_from_its_rows(tmp_path: Path) -> None:
    session = write_capture_session(tmp_path, capture_rounds(240))
    quality = recompute_capture_quality(session, ended_at=CAPTURE_T0 + 60)
    assert quality.scope == "recompute"
    assert quality.tuples["source"] == "persisted_rows"
    assert quality.tuples["complete"] == 240
    assert quality.interval["elapsed_s"] == pytest.approx(60.0)
    # No epochs were ever recorded for this session: unknown, never zero.
    assert quality.verdict == "unverified"
    assert {f["clause"] for f in quality.failures} == {UNKNOWN_LOSS_COUNTERS}
    assert any("loss counters unknown" in note for note in quality.notes)


def test_recompute_without_a_recorded_end_says_the_tail_is_undetectable(tmp_path: Path) -> None:
    session = write_capture_session(tmp_path, capture_rounds(240))
    quality = recompute_capture_quality(session)
    assert quality.interval["elapsed_s"] == pytest.approx(239 * 0.25 + 0.003)
    assert any("missing tail cannot be detected" in note for note in quality.notes)


def test_recompute_finds_the_disjoint_round_case_in_real_parquet(tmp_path: Path) -> None:
    session = write_capture_session(tmp_path, capture_rounds(240, both=False))
    quality = recompute_capture_quality(session, ended_at=CAPTURE_T0 + 60)
    assert quality.tuples["complete"] == 120
    assert quality.tuples["member_misses"] == {"A": 0, "B": 120}
    # Rows before the writer existed: no round identity at all.
    legacy = write_capture_session(
        tmp_path / "legacy",
        [capture_obd_record(CAPTURE_T0 + i, "A", round_id=None) for i in range(30)],
        profile={
            "mode01": {"A": 1.0},
            "required_tuple": ["A"],
            "target_rate_hz": 1.0,
            "min_achieved_rate_hz": 0.5,
            "max_tuple_skew_s": 0.1,
        },
    )
    quality = recompute_capture_quality(legacy, ended_at=CAPTURE_T0 + 30)
    assert quality.tuples["source"] == "no_round_evidence"
    assert quality.verdict == "unverified"


def test_recompute_reuses_a_v2_sidecars_epochs_and_keeps_a_v1_sidecars_scope(
    tmp_path: Path,
) -> None:
    session = write_capture_session(tmp_path, capture_rounds(20, both=False))
    (session / "capture_quality.json").write_text(
        json.dumps({"schema_version": 1, "verdict": "pass", "profile": "fuel-map"})
    )
    stored = read_quality_with_scope(session)
    assert stored is not None and stored["scope"] == "trailing_snapshot"
    assert stored["verdict"] == "pass"
    recomputed = recompute_capture_quality(session, ended_at=CAPTURE_T0 + 10)
    assert TUPLE_INCOMPLETE not in {f["clause"] for f in recomputed.failures}
    assert recomputed.tuples["complete"] == 10  # odd rounds only

    v2 = recomputed.to_dict()
    v2["losses"] = {
        "recording": {"status": "verified", "lost": 0, "unverified": [], "counters": {}},
        "dashboard": {"status": "verified", "lost": 0, "unverified": [], "counters": {}},
    }
    # The evaluator persists the declared interval itself; nothing is injected.
    assert v2["interval"]["ended_wall"] == pytest.approx(CAPTURE_T0 + 10)
    (session / "capture_quality.json").write_text(json.dumps(v2))
    again = recompute_capture_quality(session)
    assert again.losses["recording"]["status"] == "verified"
    assert again.interval["elapsed_s"] == pytest.approx(10.0)
    # The stored sidecar is never rewritten by a recompute.
    assert json.loads((session / "capture_quality.json").read_text()) == v2


def test_recompute_refuses_a_session_that_is_not_a_capture(tmp_path: Path) -> None:
    plain = tmp_path / "2026-08-05T142720"
    write_records_to_parquet(capture_rounds(4), plain)
    with pytest.raises(ValueError, match="not a capture session"):
        recompute_capture_quality(plain)


def test_member_channels_resolve_dids_through_the_catalogue() -> None:
    members = member_channels(
        {"mode01": {"RPM": 1.0}, "required_tuple": ["RPM", "ECM_MAF_GPS", "NOT_A_DID"]}
    )
    assert members["RPM"] == "RPM"
    assert members["ECM_MAF_GPS"].startswith("DID_")
    assert members["NOT_A_DID"] == "NOT_A_DID"


def _target_rows(*, answered: bool) -> pd.DataFrame:
    """The rows the live ledger test sees: a value every 0.5 s except
    100-140 s, where a sentinel row (or nothing) stands in."""
    target = "mem:ECM_TARGET_EQUIV_RATIO"
    records = []
    for i in range(601):
        t = i * 0.5
        if 100.0 <= t < 140.0:
            if answered:
                records.append(
                    TelemetryRecord.obd(CAPTURE_T0 + t, f"{target}_SENTINEL", "fuel_cut", "")
                )
            continue
        records.append(TelemetryRecord.obd(CAPTURE_T0 + t, target, 1.0, ""))
    records.append(TelemetryRecord.obd(CAPTURE_T0 + 5, "DID_FOO_SENTINEL", "x", ""))
    return read_acquisition_frame(pd.DataFrame([r.parquet_row() for r in records]))


def test_a_sentinel_only_stretch_leaves_no_long_gap_in_the_rebuild() -> None:
    target = "mem:ECM_TARGET_EQUIV_RATIO"
    rows = channel_lifetimes_from_rows(
        _target_rows(answered=True),
        [target, f"{target}_SENTINEL", "DID_FOO", "DID_FOO_SENTINEL"],
        started_at=CAPTURE_T0,
        ended_at=CAPTURE_T0 + 300,
    )
    assert rows[target]["long_gaps"] == []
    assert rows[target]["observations"] == 601
    assert rows[target]["max_interarrival_s"] == pytest.approx(0.5)
    # The sentinel channel keeps its own rows; a non-memory name is untouched.
    assert rows[f"{target}_SENTINEL"]["observations"] == 80
    assert rows["DID_FOO"]["observations"] == 0
    assert rows["DID_FOO_SENTINEL"]["observations"] == 1


def test_a_real_silence_still_scores_as_a_gap_in_the_rebuild() -> None:
    target = "mem:ECM_TARGET_EQUIV_RATIO"
    rows = channel_lifetimes_from_rows(
        _target_rows(answered=False),
        [target],
        started_at=CAPTURE_T0,
        ended_at=CAPTURE_T0 + 300,
    )
    assert rows[target]["long_gaps"] == [[pytest.approx(99.5), pytest.approx(140.0)]]


def test_recompute_does_not_fail_acquisition_gap_for_a_fuel_cut(tmp_path: Path) -> None:
    target = "mem:ECM_TARGET_EQUIV_RATIO"
    records = []
    for r in range(601):
        t = CAPTURE_T0 + r * 0.5
        records.append(capture_obd_record(t, "A", round_id=r))
        records.append(capture_obd_record(t + 0.003, "B", round_id=r))
        name = f"{target}_SENTINEL" if 100.0 <= r * 0.5 < 140.0 else target
        records.append(capture_obd_record(t + 0.006, name, round_id=r))
    session = write_capture_session(
        tmp_path,
        records,
        profile={
            "memory": ["ECM_TARGET_EQUIV_RATIO"],
            "target_rate_hz": 2.0,
            "min_achieved_rate_hz": 1.5,
        },
    )
    quality = recompute_capture_quality(session, ended_at=CAPTURE_T0 + 300)
    gaps = [f for f in quality.failures if f["clause"] == ACQUISITION_GAP]
    assert not any(target in f.get("evidence", {}) for f in gaps), gaps
    row = next(c for c in quality.channels if c.transport_channel == target)
    assert row.coverage["fraction"] == pytest.approx(1.0, abs=0.01)
