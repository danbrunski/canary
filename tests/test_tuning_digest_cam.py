"""R05.3 — the digest's cam section pairs samples; it never correlates grid cells.

``_cam_tracking`` used to run ``np.corrcoef`` over ``read_aligned(grid="1s")``
cells — output whose own docstring says it "is not pairing evidence" — and
``except Exception: return None`` hid which kind of empty it was.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.analysis.tuning_digest import CAM_PAIRING, _cam_tracking
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord

T0 = datetime(2026, 9, 17, 18, 48, 21, tzinfo=UTC)
COMMAND = "obd:TIMING_ADVANCE"
MEASURED = "obd:SHORT_FUEL_TRIM_1"


def _session(tmp_path: Path, pairs: list[tuple[float, float]], *, measured: bool = True) -> Path:
    records: list[TelemetryRecord] = []
    for i, (command, actual) in enumerate(pairs):
        ts = T0 + timedelta(milliseconds=100 * i)
        records.append(
            TelemetryRecord(timestamp=ts, source="obd", channel="TIMING_ADVANCE", value=command)
        )
        if measured:
            records.append(
                TelemetryRecord(
                    timestamp=ts + timedelta(milliseconds=2),
                    source="obd",
                    channel="SHORT_FUEL_TRIM_1",
                    value=actual,
                )
            )
    out = tmp_path / "_capture-tuning-2026-09-17T184821"
    write_records_to_parquet(records, out)
    return out


def test_tracking_error_is_per_pair_and_the_fit_carries_its_verdict(tmp_path: Path) -> None:
    command = [float(i % 30) for i in range(90)]
    session = _session(tmp_path, [(c, c + 0.5) for c in command])
    cam = _cam_tracking(session, command_spec=COMMAND, measured_spec=MEASURED)
    assert cam["available"] is True
    assert cam["n_pairs"] == 90
    assert cam["mean_tracking_error_deg"] == 0.5
    assert cam["max_tracking_error_deg"] == 0.5
    assert cam["policy"] == CAM_PAIRING.as_dict()
    assert cam["fit"]["available"] is True
    assert cam["fit"]["scale_verdict"] in {"stable", "drifting", "unresolved"}
    # No grid-cell correlation survives.
    assert "correlation" not in cam


def test_a_constant_command_still_reports_tracking_and_says_why_no_fit(tmp_path: Path) -> None:
    """Idle: the command does not move, so no scale is identifiable — but the
    tracking error is still an answer."""
    session = _session(tmp_path, [(10.0, 10.0 + (i % 3) * 0.1) for i in range(60)])
    cam = _cam_tracking(session, command_spec=COMMAND, measured_spec=MEASURED)
    assert cam["available"] is True
    assert cam["fit"]["available"] is False
    assert "constant" in cam["fit"]["reason"]


def test_an_uncaptured_side_says_not_captured(tmp_path: Path) -> None:
    session = _session(tmp_path, [(1.0, 1.0)] * 10, measured=False)
    cam = _cam_tracking(session, command_spec=COMMAND, measured_spec=MEASURED)
    assert cam["available"] is False
    assert cam["kind"] == "not_captured"
    assert MEASURED in cam["reason"]


def test_the_not_captured_kind_comes_from_the_refusal_type_not_its_text() -> None:
    """R05.3: ``compare`` raises a typed refusal for an absent side."""
    import pandas as pd

    from src.analysis.compare import InputNotCapturedError, _refuse_if_absent

    with pytest.raises(InputNotCapturedError) as caught:
        _refuse_if_absent("did:X", pd.DataFrame(columns=["timestamp", "value"]))
    assert caught.value.spec == "did:X"


def test_a_crashed_tuning_section_says_so_rather_than_reading_as_no_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R05.12: ``None`` means "not a tuning capture"; a crash is its own answer."""
    from src.analysis import digest, tuning_digest
    from src.config import CanaryConfig

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("providers file unreadable")

    monkeypatch.setattr(tuning_digest, "compute_tuning_digest", boom)
    config = CanaryConfig.from_yaml(Path("canary.example.yaml"))
    section = digest._tuning_section(tmp_path / "_capture-tuning-2026-09-17T184821", config)
    assert section == {
        "available": False,
        "kind": "failed",
        "error": "RuntimeError: providers file unreadable",
    }
