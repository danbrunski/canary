"""W04 — the Logs and Analysis pages' whole-session reads.

Each test pins one of the page's contracts: the freshness bound is counted
rather than forward-filled, a cap is stated rather than silent, the two kinds
of empty stay distinguishable, bins are rpm x airflow and never load, and every
whole-session route refuses while a capture is recording.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.analysis import channels
from src.analysis.channels import ChannelSpecError, concat_channel_frames, read_channels
from src.analysis.lambda_delivery import load_lambda_delivery
from src.analysis.noise_floor import ChannelFloor, NoiseFloor
from src.analysis.tuning import GateError
from src.analysis.tuning_frame import load_provider_registry
from src.analysis.workbench import (
    TRIM_SPECS,
    Unavailable,
    WorkbenchInputs,
    _read_optional,
    conditions_block,
    pooled_delivery_error,
    pull_comparison,
    revision_boundaries,
    session_frame,
    session_span,
    timing_surface,
    trim_surface,
)
from src.collection import tuning_profile
from src.config import CanaryConfig
from src.control import child_pool, workbench_jobs
from src.control.logs import list_logs, summarize_log
from src.dashboard.routes import workbench as workbench_routes
from src.preprocessing.loader import read_session
from src.signals.afr import AfrCalibration
from src.storage.capture_manifest import CAPTURE_MANIFEST_FILENAME, CAPTURE_QUALITY_FILENAME
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord
from src.vehicle.tune_revisions import TuneRevision, TuneRevisionRegistry

BINDINGS = Path("data/bindings.yaml")
#: The dev vehicle's mode01_wot.rpm_bands (profile data since R05.13).
RPM_BANDS = (2500.0, 3000.0, 3500.0, 4000.0, 4500.0, 5000.0, 6500.0)
DIDS = Path("data/dids.yaml")
T0 = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
PHI = AfrCalibration(afr_scale=14.7, commanded_equiv_ratio_convention="phi")
FLOOR = NoiseFloor(
    channels={
        "delivery_error_pct": ChannelFloor(
            unit="percent", conditions="WOT: throttle>=85", sd=1.49, n_cells=5
        ),
        "timing_advance_deg": ChannelFloor(unit="deg", conditions="WOT", sd=0.5, n_cells=5),
    }
)
PROFILE = Path("data/nissan-xterra-2008")
INPUTS = WorkbenchInputs(
    bindings_path=BINDINGS,
    dids_path=DIDS,
    calibration=PHI,
    # Delivery error runs on the vehicle's declared sources (R05.16).
    lambda_delivery=load_lambda_delivery(PROFILE / "lambda_delivery.yaml"),
    providers=load_provider_registry(PROFILE / "tuning_providers.yaml"),
    noise_floor=FLOOR,
    idle_spec={"rpm_min": 400.0, "rpm_max": 900.0, "speed_max": 0.0},
)


def _write(root: Path, name: str, rows: list[dict[str, float]], *, step_s: float = 1.0) -> Path:
    """One session from per-second dicts of ``CHANNEL: value`` (obd source)."""
    records: list[TelemetryRecord] = []
    for i, row in enumerate(rows):
        ts = T0 + timedelta(seconds=i * step_s)
        for channel, value in row.items():
            records.append(
                TelemetryRecord(timestamp=ts, source="obd", channel=channel, value=value)
            )
    out = root / name
    write_records_to_parquet(records, out)
    return out


def _enrich(rpm: float = 3200.0) -> dict[str, float]:
    return {
        "RPM": rpm,
        "ENGINE_LOAD": 90.0,
        "FUEL_STATUS": 4.0,
        "COMMANDED_EQUIV_RATIO": 1.2,
        "O2_WR_LAMBDA_S1": 0.83,
        "MAF": 120.0,
        "TIMING_ADVANCE": 20.0,
        "BAROMETRIC_PRESSURE": 80.0,
    }


def _closed(rpm: float = 2000.0, maf: float = 12.0, stft: float = 2.0) -> dict[str, float]:
    return {
        "RPM": rpm,
        "MAF": maf,
        "FUEL_STATUS": 2.0,
        "COMMANDED_EQUIV_RATIO": 1.0,
        "SHORT_FUEL_TRIM_1": stft,
        "LONG_FUEL_TRIM_1": 1.0,
        "SHORT_FUEL_TRIM_2": -1.0,
        "LONG_FUEL_TRIM_2": 0.5,
        "TIMING_ADVANCE": 30.0,
        "SPEED": 40.0,
    }


# --------------------------------------------------------------------------- frame


def test_frame_counts_cells_the_freshness_bound_dropped(tmp_path: Path) -> None:
    """Channels share a clock only where every one was sampled; the rest are counted."""
    rows = [_closed() for _ in range(6)]
    for row in rows[:2]:
        del row["MAF"]  # MAF missing in two cells
    session = _write(tmp_path, "2026-09-18T120000", rows)
    frame = session_frame(session, ["obd:RPM", "obd:MAF"], inputs=INPUTS)
    assert frame["rows_total"] == 4
    assert frame["freshness"] == {
        "cells_with_any": 6,
        "kept": 4,
        "dropped": 2,
        "rule": "a row exists only where every requested channel was sampled in the cell",
    }
    assert [c["spec"] for c in frame["columns"]] == ["obd:RPM", "obd:MAF"]
    assert len(frame["values"]["obd:MAF"]) == 4


def test_frame_regime_filter_uses_fuel_status_and_zero_phi(tmp_path: Path) -> None:
    cut = {**_closed(), "FUEL_STATUS": 4.0, "COMMANDED_EQUIV_RATIO": 0.0}
    rows = [_closed()] * 3 + [cut] * 2 + [_enrich()] * 4
    session = _write(tmp_path, "2026-09-18T120000", rows)
    closed = session_frame(session, ["obd:RPM"], inputs=INPUTS, regime="closed_loop")
    fuel_cut = session_frame(session, ["obd:RPM"], inputs=INPUTS, regime="fuel_cut")
    enrich = session_frame(session, ["obd:RPM"], inputs=INPUTS, regime="enrichment")
    assert (closed["rows_total"], fuel_cut["rows_total"], enrich["rows_total"]) == (3, 2, 4)
    assert fuel_cut["regimes"]["fuel_cut"]["source"] == "obd:COMMANDED_EQUIV_RATIO"
    assert closed["regime_report"]["removed"] == 6


def test_frame_names_an_unavailable_regime_instead_of_returning_empty(tmp_path: Path) -> None:
    """No FUEL_STATUS in the log: closed loop is 'not polled', not zero rows."""
    session = _write(tmp_path, "2026-09-18T120000", [{"RPM": 800.0}] * 3)
    frame = session_frame(session, ["obd:RPM"], inputs=INPUTS, regime="closed_loop")
    assert frame["regime_report"]["available"] is False
    assert "does not poll obd:FUEL_STATUS" in frame["regime_report"]["reason"]
    assert frame["rows_total"] == 0


def test_frame_cap_is_stated_and_pages_with_offset(tmp_path: Path) -> None:
    session = _write(tmp_path, "2026-09-18T120000", [_closed()] * 10)
    first = session_frame(session, ["obd:RPM"], inputs=INPUTS, max_rows=4)
    assert (first["rows_returned"], first["rows_total"], first["capped"]) == (4, 10, True)
    last = session_frame(session, ["obd:RPM"], inputs=INPUTS, max_rows=4, offset=8)
    assert (last["rows_returned"], last["capped"]) == (2, False)


def test_frame_reports_a_spec_the_log_never_held(tmp_path: Path) -> None:
    session = _write(tmp_path, "2026-09-18T120000", [_closed()] * 3)
    frame = session_frame(session, ["obd:RPM", "obd:INTAKE_TEMP"], inputs=INPUTS)
    assert frame["missing"] == [
        {
            "spec": "obd:INTAKE_TEMP",
            "reason": "This capture's profile does not poll obd:INTAKE_TEMP",
        }
    ]
    assert frame["rows_total"] == 0


def test_frame_carries_tags_and_revision_boundary(tmp_path: Path) -> None:
    session = _write(tmp_path, "2026-09-18T120000", [_closed()] * 10)
    (session / "tags.jsonl").write_text(
        json.dumps({"t": (T0 + timedelta(seconds=3)).timestamp(), "label": "pull_start"}) + "\n"
    )
    registry = TuneRevisionRegistry(
        revisions=[
            TuneRevision(revision="A", flashed_at=T0 - timedelta(days=1)),
            TuneRevision(revision="B", flashed_at=T0 + timedelta(seconds=5)),
        ]
    )
    inputs = WorkbenchInputs(bindings_path=BINDINGS, dids_path=DIDS, registry=registry)
    frame = session_frame(session, ["obd:RPM"], inputs=inputs)
    assert frame["tags"][0]["label"] == "pull_start"
    assert frame["revision"]["revision"] == "A"
    assert [b["revision"] for b in frame["revision_boundaries"]] == ["B"]


def test_revision_boundary_excludes_flashes_outside_the_span(tmp_path: Path) -> None:
    registry = TuneRevisionRegistry(revisions=[TuneRevision(revision="A", flashed_at=T0)])
    # Data starts at T0, so a flash at T0 is not inside (start, end].
    at_t0 = _write(tmp_path, "2026-09-18T120000", [_closed()] * 3)
    assert revision_boundaries(registry, at_t0, T0 + timedelta(hours=1)) == []
    later = TuneRevisionRegistry(
        revisions=[TuneRevision(revision="A", flashed_at=T0 + timedelta(seconds=1))]
    )
    assert revision_boundaries(later, at_t0, T0 + timedelta(seconds=1))[0]["revision"] == "A"


def test_a_session_named_before_its_data_is_bounded_by_its_data(tmp_path: Path) -> None:
    """P02.17: ``2026-08-03T163440`` is named 6 h 37 min before its first row,
    with the k106 flash in between. The label already followed the data
    (382dfa0); the boundary marker still started at the name, so the same
    session read as k106 *and* as crossing the k106 flash. Every caller that
    computes boundaries — the frame, the conditions block, the capture
    browser — now dates the session as the label does."""
    flash = T0 - timedelta(hours=2)
    registry = TuneRevisionRegistry(
        revisions=[
            TuneRevision(revision="k105", flashed_at=T0 - timedelta(days=3)),
            TuneRevision(revision="k106", flashed_at=flash),
        ]
    )
    # Named 4 h before its data (T0): the flash falls between name and data.
    named = (T0 - timedelta(hours=4)).strftime("%Y-%m-%dT%H%M%S")
    storage = tmp_path / "storage"
    storage.mkdir()
    session = _write(storage, named, [_closed()] * 10)
    inputs = WorkbenchInputs(bindings_path=BINDINGS, dids_path=DIDS, registry=registry)

    frame = session_frame(session, ["obd:RPM"], inputs=inputs)
    assert frame["revision"]["revision"] == "k106"
    assert frame["revision_boundaries"] == []

    block = conditions_block([session], inputs)
    assert block["revisions"] == {"k106": [named]}
    assert block["revision_boundaries"] == {}
    assert block["mixed_revisions"] is False

    (log,) = [summarize_log(session, registry)]
    assert log["revision"]["revision"] == "k106"
    assert log["revision_boundaries"] == []


def test_session_span_reads_footers(tmp_path: Path) -> None:
    session = _write(tmp_path, "2026-09-18T120000", [{"RPM": 800.0}] * 5)
    first, last, rows = session_span(session)
    assert rows == 5
    assert first == T0 and last == T0 + timedelta(seconds=4)


# --------------------------------------------------------------------------- delivery


def test_pooled_delivery_error_pools_and_names_not_captured(tmp_path: Path) -> None:
    # Each session's first read cannot show the enrichment rule's 0.6 s dwell
    # (R05.16: nothing earlier to judge it against).
    a = _write(tmp_path, "2026-09-18T120000", [_enrich(3200)] * 5)
    b = _write(tmp_path, "2026-09-18T130000", [_enrich(4200)] * 4)
    no_mode01 = _write(tmp_path, "2026-09-18T140000", [{"RPM": 3000.0}] * 3)
    result = pooled_delivery_error(
        [a, b, no_mode01], inputs=INPUTS, min_load=85.0, rpm_bands=RPM_BANDS
    )
    assert result["available"] is True
    assert result["n"] == 7
    assert [s["n"] for s in result["per_session"]] == [4, 3]
    skipped = result["unavailable"][0]
    assert skipped["session_id"] == "2026-09-18T140000"
    assert skipped["kind"] == "not_captured"
    assert "obd:COMMANDED_EQUIV_RATIO" in skipped["missing"]
    assert result["noise_floor"]["conditions"] == "WOT: throttle>=85"
    assert result["pairing"]["method"] == "acquisition-time match_ordered"
    assert result["engine_version"] == 2
    assert result["gates"]["filtered"] == {"target_transient": 2}
    assert [s["informed_constants"] for s in result["per_session"]] == [False, False]
    assert result["conditions"]["n_sessions"] == 2
    assert result["conditions"]["baro"]["min_kpa"] == 80.0


def test_pooled_delivery_error_gates_removed_everything_is_its_own_kind(tmp_path: Path) -> None:
    cut = {**_enrich(), "COMMANDED_EQUIV_RATIO": 0.0}
    session = _write(tmp_path, "2026-09-18T120000", [cut] * 4)
    result = pooled_delivery_error([session], inputs=INPUTS, rpm_bands=RPM_BANDS)
    assert result["available"] is False
    assert result["unavailable_kind"] == "gates_removed_everything"
    assert result["gates"]["filtered"] == {"fuel_cut": 4}


def test_pooled_delivery_error_all_not_captured(tmp_path: Path) -> None:
    session = _write(tmp_path, "2026-09-18T120000", [{"RPM": 3000.0}] * 3)
    result = pooled_delivery_error([session], inputs=INPUTS, rpm_bands=RPM_BANDS)
    assert result["available"] is False
    assert result["unavailable_kind"] == "not_captured"


def test_semantic_measured_source_refuses_a_load_floor(tmp_path: Path) -> None:
    session = _write(tmp_path, "2026-09-18T120000", [_enrich()] * 3)
    with pytest.raises(GateError, match="load"):
        pooled_delivery_error(
            [session],
            inputs=INPUTS,
            measured="semantic:measured_afr_b1",
            min_load=85.0,
            rpm_bands=RPM_BANDS,
        )


# --------------------------------------------------------------------------- surfaces


def test_trim_surface_is_closed_loop_by_rpm_airflow_with_per_session_split(
    tmp_path: Path,
) -> None:
    open_loop = {**_closed(stft=0.0), "FUEL_STATUS": 4.0}  # trims held, not correcting
    a = _write(tmp_path, "2026-09-18T120000", [_closed(stft=4.0)] * 6 + [open_loop] * 2)
    b = _write(tmp_path, "2026-09-18T130000", [_closed(stft=-4.0)] * 6)
    result = trim_surface([a, b], inputs=INPUTS)
    assert result["gates"]["filtered"]["not_closed_loop"] == 2
    pooled = result["banks"]["b1"]["cells"]
    assert len(pooled) == 1 and pooled[0]["n"] == 12
    assert pooled[0]["total"] == pytest.approx(1.0)  # (4+1 and -4+1) pooled
    per = {s["session_id"]: s["cells"]["b1"][0]["total"] for s in result["per_session"]}
    assert per == {"2026-09-18T120000": 5.0, "2026-09-18T130000": -3.0}
    assert result["banks"]["b2"]["cells"][0]["total"] == pytest.approx(-0.5)


def test_trim_surface_greys_sparse_cells(tmp_path: Path) -> None:
    session = _write(tmp_path, "2026-09-18T120000", [_closed()] * 3)
    result = trim_surface([session], inputs=INPUTS, min_n=5)
    assert result["banks"]["b1"]["cells"][0]["sparse"] is True


def test_surfaces_refuse_calculated_load_as_an_axis(tmp_path: Path) -> None:
    session = _write(tmp_path, "2026-09-18T120000", [_closed()] * 3)
    with pytest.raises(GateError, match="derived from BFS"):
        trim_surface([session], inputs=INPUTS, airflow="obd:ENGINE_LOAD")
    with pytest.raises(GateError, match="derived from BFS"):
        timing_surface([session], inputs=INPUTS, airflow="obd:ENGINE_LOAD")


def test_trim_surface_not_captured_names_the_channel(tmp_path: Path) -> None:
    session = _write(tmp_path, "2026-09-18T120000", [{"RPM": 2000.0, "MAF": 10.0}] * 3)
    result = trim_surface([session], inputs=INPUTS)
    assert result["unavailable_kind"] == "not_captured"
    assert "obd:FUEL_STATUS" in result["unavailable"][0]["missing"]


# ------------------------------------------------------- one scan per session (R26.1)


def _reference_read_optional(
    session: Path, specs: list[str], inputs: WorkbenchInputs
) -> tuple[pd.DataFrame, list[str]]:
    """``_read_optional`` as it was before R26.1: one scan per spec."""
    frames: list[pd.DataFrame] = []
    unresolved: list[str] = []
    for spec in dict.fromkeys(specs):
        try:
            frames.append(
                read_channels(
                    session,
                    [spec],
                    bindings_path=inputs.bindings_path,
                    dids_path=inputs.dids_path,
                    calibration=inputs.calibration,
                )
            )
        except (ChannelSpecError, ValueError, KeyError):
            unresolved.append(spec)
    if not frames:
        return pd.DataFrame(columns=["timestamp", "spec", "value", "unit"]), unresolved
    return concat_channel_frames(frames), unresolved


def _reference_not_captured(session: Path, required: list[str]) -> dict[str, Any] | None:
    """The ``Unavailable`` the old ``_aligned_or_missing`` gave ``trim_surface``
    for ``required``, from its own per-spec reads (its first of two)."""
    long, unresolved = _reference_read_optional(session, required, INPUTS)
    present = set(long["spec"].unique()) if not long.empty else set()
    missing = [spec for spec in required if spec not in present]
    if not missing:
        return None
    return Unavailable(
        session.name,
        "not_captured",
        "This capture's profile does not poll " + ", ".join(missing),
        missing=missing + [s for s in unresolved if s not in missing],
    ).as_dict()


@pytest.fixture
def scan_count(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """The session directories read from disk, one entry per scan."""
    scans: list[Path] = []
    real = read_session

    def counting(session: Any, *args: Any, **kwargs: Any) -> pd.DataFrame:
        scans.append(Path(session))
        return real(session, *args, **kwargs)

    monkeypatch.setattr(channels, "read_session", counting)
    return scans


def test_read_optional_reads_the_session_once_and_isolates_each_spec(
    tmp_path: Path, scan_count: list[Path]
) -> None:
    session = _write(tmp_path, "2026-09-18T120000", [_closed(stft=s) for s in (1.0, 2.0, 3.0)])
    unresolvable = "did:NO_SUCH_DID"  # the catalogs cannot resolve it
    absent = "obd:NEVER_POLLED"  # resolves, but this session has no rows for it
    specs = ["obd:RPM", unresolvable, absent, "obd:SHORT_FUEL_TRIM_1", "obd:RPM", "obd:MAF"]

    expected_long, expected_unresolved = _reference_read_optional(session, specs, INPUTS)
    reference_scans = len(scan_count)
    assert reference_scans == 4  # per distinct spec; the unresolvable one never reaches the disk
    scan_count.clear()

    long, unresolved = _read_optional(session, specs, INPUTS)

    assert len(scan_count) == 1
    assert unresolved == expected_unresolved == [unresolvable]
    pd.testing.assert_frame_equal(long, expected_long)
    assert set(long["spec"]) == {"obd:RPM", "obd:SHORT_FUEL_TRIM_1", "obd:MAF"}  # not `absent`


def test_trim_surface_not_captured_keeps_missing_order_and_reads_once(
    tmp_path: Path, scan_count: list[Path]
) -> None:
    """One axis unresolvable, one required spec resolvable but absent, the rest
    present: ``missing`` is in the required order, as it was with two reads."""
    session = _write(
        tmp_path,
        "2026-09-18T120000",
        [{"RPM": 2000.0, "SHORT_FUEL_TRIM_1": 1.0, "SHORT_FUEL_TRIM_2": 1.0}] * 3,
    )
    unresolvable = "did:NO_SUCH_DID"
    required = ["obd:RPM", unresolvable, "obd:FUEL_STATUS", *TRIM_SPECS["b1"]]
    # FUEL_STATUS and LONG_FUEL_TRIM_1 resolve but were never polled here.
    expected = _reference_not_captured(session, required)
    assert expected is not None and expected["missing"] == [
        unresolvable,
        "obd:FUEL_STATUS",
        "obd:LONG_FUEL_TRIM_1",
    ]
    scan_count.clear()

    result = trim_surface([session], inputs=INPUTS, airflow=unresolvable)

    assert result["unavailable_kind"] == "not_captured"
    assert result["unavailable"] == [expected]
    assert len(scan_count) == 1  # was one scan per spec, in two passes


def test_trim_surface_reads_each_session_once_including_the_barometer(
    tmp_path: Path, scan_count: list[Path]
) -> None:
    rows = [{**_closed(), "BAROMETRIC_PRESSURE": 80.0}] * 6
    a = _write(tmp_path, "2026-09-18T120000", rows)
    b = _write(tmp_path, "2026-09-18T130000", rows)

    result = trim_surface([a, b], inputs=INPUTS)

    assert sorted(scan_count) == [a, b]  # one scan each, nothing for conditions
    assert result["conditions"]["baro"]["min_kpa"] == 80.0
    assert result["conditions"]["baro"]["sessions_without"] == []
    assert result["n"] == 12


def test_session_frame_and_value_surfaces_read_each_session_once(
    tmp_path: Path, scan_count: list[Path]
) -> None:
    session = _write(tmp_path, "2026-09-18T120000", [_closed()] * 6)

    session_frame(session, ["obd:RPM", "obd:MAF"], inputs=INPUTS)
    assert scan_count == [session]  # the regime's channels shared the one scan

    scan_count.clear()
    result = timing_surface([session], inputs=INPUTS)
    assert result["available"] is True
    # The values (rpm, airflow, timing) in one scan, the barometer in another.
    assert scan_count == [session, session]


def test_pull_comparison_flags_a_drop_where_timing_was_advanced(tmp_path: Path) -> None:
    a = _write(tmp_path, "2026-09-18T120000", [_closed()] * 6)
    retarded = {**_closed(), "TIMING_ADVANCE": 26.0}
    b = _write(tmp_path, "2026-09-19T120000", [retarded] * 6)
    result = pull_comparison(
        [a],
        [b],
        revision_a="A",
        revision_b="B",
        inputs=INPUTS,
        floor_channel="timing_advance_deg",
    )
    cell = result["cells"][0]
    assert cell["delta"] == pytest.approx(-4.0)
    assert cell["dropped"] is True
    # R05.2: the cells span every regime; the timing floor was measured under
    # "WOT", so it is refused rather than quoted ~16x too tight.
    assert cell["delta_sigma"] is None
    assert result["significance"]["kind"] == "conditions_mismatch"
    assert result["significance"]["floor_conditions"] == "WOT"
    assert result["noise_floor"]["conditions"] == "WOT"


def test_pull_comparison_quotes_a_floor_measured_under_its_own_conditions(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    from src.analysis.workbench import ALL_CONDITIONS

    floor = NoiseFloor(
        channels={
            "timing_advance_deg": ChannelFloor(
                unit="deg", conditions=ALL_CONDITIONS, sd=0.5, n_cells=5
            )
        }
    )
    a = _write(tmp_path, "2026-09-18T120000", [_closed()] * 6)
    b = _write(tmp_path, "2026-09-19T120000", [{**_closed(), "TIMING_ADVANCE": 26.0}] * 6)
    result = pull_comparison(
        [a],
        [b],
        revision_a="A",
        revision_b="B",
        inputs=replace(INPUTS, noise_floor=floor),
        floor_channel="timing_advance_deg",
    )
    assert result["cells"][0]["delta_sigma"] == pytest.approx(-8.0)
    assert result["significance"]["available"] is True


# --------------------------------------------------------------------------- browser


def _config(tmp_path: Path) -> CanaryConfig:
    config = CanaryConfig.from_yaml(Path("canary.example.yaml"))
    profile = tmp_path / "profile"
    profile.mkdir(exist_ok=True)
    (profile / "afr_calibration.yaml").write_text(
        yaml.safe_dump({"afr_scale": 14.7, "commanded_equiv_ratio_convention": "phi"})
    )
    storage = tmp_path / "storage"
    storage.mkdir(exist_ok=True)
    return config.model_copy(
        update={
            "collection": config.collection.model_copy(update={"storage_path": storage}),
            "daemon": config.daemon.model_copy(
                update={
                    "afr_calibration": profile / "afr_calibration.yaml",
                    "jobs_dir": tmp_path / "control" / "jobs",
                }
            ),
        }
    )


def _capture(storage: Path, name: str, *, end_reason: str | None) -> Path:
    session = _write(storage, name, [_closed()] * 3)
    manifest: dict[str, Any] = {
        "profile": "tuning",
        "started_at": T0.isoformat(),
        "profile_snapshot": {},
        "end_reason": end_reason,
        "ended_at": (T0 + timedelta(seconds=90)).isoformat() if end_reason else None,
    }
    (session / CAPTURE_MANIFEST_FILENAME).write_text(json.dumps(manifest))
    (session / CAPTURE_QUALITY_FILENAME).write_text(
        json.dumps({"schema_version": 2, "verdict": "pass", "scope": "stop"})
    )
    return session


def test_capture_browser_orders_by_parsed_start_not_directory_name(tmp_path: Path) -> None:
    """`_capture-…` sorts before a date by name; the browser must not care."""
    config = _config(tmp_path)
    storage = config.collection.storage_path
    _capture(storage, "_capture-tuning-2026-09-10T120000", end_reason="daemon_shutdown")
    _capture(storage, "_capture-tuning-2026-09-20T120000", end_reason="operator_stop")
    _write(storage, "2026-09-15T120000", [_closed()] * 3)
    captures = list_logs(config)
    assert [log["session_id"] for log in captures["logs"]] == [
        "_capture-tuning-2026-09-20T120000",
        "_capture-tuning-2026-09-10T120000",
    ]
    newest = captures["logs"][0]
    assert newest["end_reason"] == "operator_stop"
    assert newest["quality"]["verdict"] == "pass"
    assert newest["row_count"] == 30
    assert newest["profile"] == "tuning"
    everything = list_logs(config, include_sessions=True)
    assert [log["session_id"] for log in everything["logs"]] == [
        "_capture-tuning-2026-09-20T120000",
        "2026-09-15T120000",
        "_capture-tuning-2026-09-10T120000",
    ]


# --------------------------------------------------------------------------- routes


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    config = _config(tmp_path)
    app = FastAPI()
    workbench_routes.register_workbench_routes(
        app,
        config=config,
        bindings_path=BINDINGS,
        dids_path=DIDS,
        resolved_control_token="secret",
    )
    # A thread pool stands in for the spawn child so the test stays fast; the
    # spawn path itself is exercised by test_run_off_loop_in_a_real_child.
    monkeypatch.setattr(child_pool, "spawn_pool", lambda: ThreadPoolExecutor(max_workers=1))
    monkeypatch.setattr(workbench_jobs, "limit_address_space", lambda nbytes: (nbytes, nbytes))
    with TestClient(app) as test_client:
        test_client.config = config  # type: ignore[attr-defined]
        yield test_client


AUTH = {"Authorization": "Bearer secret"}


def test_routes_require_auth(client: TestClient) -> None:
    assert client.get("/control/logs").status_code == 401
    assert client.get("/analysis/timing?sessions=x").status_code == 401


def test_frame_route_runs_through_the_child(client: TestClient) -> None:
    storage = client.config.collection.storage_path  # type: ignore[attr-defined]
    _write(storage, "2026-09-18T120000", [_closed()] * 3)
    response = client.get(
        "/control/sessions/2026-09-18T120000/frame?specs=obd:RPM,obd:MAF", headers=AUTH
    )
    assert response.status_code == 200, response.text
    assert response.json()["rows_total"] == 3


def test_every_whole_session_route_defers_while_a_capture_records(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = client.config.collection.storage_path  # type: ignore[attr-defined]
    _write(storage, "2026-09-18T120000", [_closed()] * 3)
    monkeypatch.setattr(tuning_profile, "is_active", lambda: True)
    monkeypatch.setattr(
        tuning_profile,
        "active_profile",
        lambda: type("P", (), {"name": "tuning"})(),
    )
    for path in (
        "/control/sessions/2026-09-18T120000/frame?specs=obd:RPM",
        "/analysis/delivery-error?sessions=2026-09-18T120000",
        "/analysis/trim-surface?sessions=2026-09-18T120000",
        "/analysis/timing?sessions=2026-09-18T120000",
        "/analysis/pull-comparison?revision_a=A&revision_b=B",
    ):
        response = client.get(path, headers=AUTH)
        assert response.status_code == 409, path
        detail = response.json()["detail"]
        assert detail["error"] == "analysis_deferred"
        assert detail["message"] == "analysis is deferred while 'tuning' is recording"
        assert "tar -cf -" in detail["laptop_path"]


def test_no_sessions_chosen_is_named(client: TestClient) -> None:
    response = client.get("/analysis/delivery-error", headers=AUTH)
    assert response.status_code == 400
    assert response.json()["detail"]["message"] == "Choose sessions or a revision"


def test_a_second_different_analysis_is_refused_as_busy(tmp_path: Path) -> None:
    config = _config(tmp_path)

    async def scenario() -> dict[str, Any]:
        workbench_jobs._IN_FLIGHT["other"] = asyncio.get_running_loop().create_future()  # type: ignore[assignment]
        try:
            with pytest.raises(workbench_jobs.WorkbenchRefusedError) as caught:
                await workbench_jobs.run_off_loop(
                    "timing", {"sessions": []}, config, bindings_path=BINDINGS, dids_path=DIDS
                )
            return caught.value.detail
        finally:
            workbench_jobs._IN_FLIGHT.pop("other", None)

    assert asyncio.run(scenario())["error"] == "analysis_busy"


def test_run_off_loop_in_a_real_child(tmp_path: Path) -> None:
    """The job function and its arguments survive ``spawn`` pickling."""
    config = _config(tmp_path)
    session = _write(config.collection.storage_path, "2026-09-18T120000", [_closed()] * 3)
    result = asyncio.run(
        workbench_jobs.run_off_loop(
            "frame",
            {"session": str(session), "specs": ["obd:RPM"]},
            config,
            bindings_path=BINDINGS,
            dids_path=DIDS,
            timeout=120.0,
        )
    )
    assert result["rows_total"] == 3


def test_frame_fuel_cut_comes_from_the_sentinel_channel(tmp_path: Path) -> None:
    """W04.4: where the log carries the pinned phi word, fuel cut is its _SENTINEL."""
    from src.analysis.tuning_frame import Provider, ProviderRegistry

    records: list[TelemetryRecord] = []
    for i in range(6):
        ts = T0 + timedelta(seconds=i)
        records.append(TelemetryRecord(timestamp=ts, source="obd", channel="RPM", value=2500.0))
        if i < 2:
            records.append(
                TelemetryRecord(
                    timestamp=ts,
                    source="obd",
                    channel="mem:ECM_TARGET_EQUIV_RATIO_SENTINEL",
                    value="fuel_cut",
                )
            )
        else:
            phi = 1.16 if i >= 4 else 1.0
            records.append(
                TelemetryRecord(
                    timestamp=ts, source="obd", channel="mem:ECM_TARGET_EQUIV_RATIO", value=phi
                )
            )
    session = tmp_path / "_capture-tuning-2026-09-18T120000"
    write_records_to_parquet(records, session)
    providers = ProviderRegistry(
        providers=[
            Provider(
                role="commanded_afr",
                spec="mem:ECM_TARGET_EQUIV_RATIO",
                transform="equiv_ratio_afr",
            )
        ]
    )
    inputs = WorkbenchInputs(bindings_path=BINDINGS, dids_path=DIDS, providers=providers)
    cut = session_frame(session, ["obd:RPM"], inputs=inputs, regime="fuel_cut")
    rich = session_frame(session, ["obd:RPM"], inputs=inputs, regime="enrichment")
    assert cut["rows_total"] == 2
    assert cut["regimes"]["fuel_cut"]["source"] == "mem:ECM_TARGET_EQUIV_RATIO_SENTINEL"
    assert rich["rows_total"] == 2
