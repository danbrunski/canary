"""Tests for the disposition-report CLI (Task 1.5, online-training-strategy.md)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from scripts.disposition_report import build_report_markdown, main
from src.analysis.training_disposition import ChannelDisposition, DispositionSummary
from src.decode.accepted_baselines import (
    AcceptedBaselineOverride,
    AcceptedBaselines,
    DirectionalBand,
    Provenance,
)
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord

# ---------------------------------------------------------------------------
# build_report_markdown — rendering
# ---------------------------------------------------------------------------


def test_report_includes_reject_counts() -> None:
    summary = DispositionSummary(groups=(), reject_counts={"obd:RPM": 3})
    report = build_report_markdown(summary, generated_at="T", sessions=[Path("s0")])
    assert "obd:RPM" in report
    assert "| Reject | 3 |" in report


def test_report_omits_channels_with_no_persistent_outliers() -> None:
    clean = ChannelDisposition(
        channel="obd:COOLANT_TEMP",
        operating_mode="idle",
        accept_recenter=0,
        quarantine=0,
        accept=100,
        baseline_low=70.0,
        baseline_high=95.0,
    )
    summary = DispositionSummary(groups=(clean,), reject_counts={})
    report = build_report_markdown(summary, generated_at="T", sessions=[Path("s0")])
    assert "No channel sits persistently outside" in report
    assert "obd:COOLANT_TEMP" not in report.split("## Persistently")[1].split("## Next")[0]


def test_report_lists_quarantined_channel_with_its_baseline_band() -> None:
    flagged = ChannelDisposition(
        channel="obd:SHORT_FUEL_TRIM_1",
        operating_mode="idle",
        accept_recenter=0,
        quarantine=12,
        accept=88,
        baseline_low=-5.0,
        baseline_high=5.0,
    )
    summary = DispositionSummary(groups=(flagged,), reject_counts={})
    report = build_report_markdown(summary, generated_at="T", sessions=[Path("s0")])
    section = report.split("## Persistently")[1].split("## Next")[0]
    assert "obd:SHORT_FUEL_TRIM_1" in section
    assert "12" in section


def test_report_never_authors_an_override() -> None:
    summary = DispositionSummary(groups=(), reject_counts={})
    report = build_report_markdown(summary, generated_at="T", sessions=[Path("s0")])
    assert "This report never authors one" in report


# ---------------------------------------------------------------------------
# main() end-to-end over a tiny synthetic corpus
# ---------------------------------------------------------------------------


def _write_session(
    root: Path,
    name: str,
    *,
    rows: int,
    start_epoch: float,
    fuel_trim_outliers: int = 0,
) -> Path:
    session = root / name
    records = []
    for i in range(rows):
        t = start_epoch + i
        # A clear, persistent fuel-trim outlier band for the first
        # `fuel_trim_outliers` rows — everything else sits near 0.
        fuel_trim = 30.0 if i < fuel_trim_outliers else (i % 3) * 0.1
        records.extend(
            [
                TelemetryRecord.obd(t, "RPM", 900.0 + (i % 20), "rpm"),
                TelemetryRecord.obd(t, "COOLANT_TEMP", 85.0, "C"),
                TelemetryRecord.obd(t, "SPEED", 0.0, "km/h"),
                TelemetryRecord.obd(t, "THROTTLE_POS", 8.0, "%"),
                TelemetryRecord.obd(t, "SHORT_FUEL_TRIM_1", fuel_trim, "%"),
            ]
        )
    # One physically-impossible RPM cell, mid-session.
    records.append(TelemetryRecord.obd(start_epoch + 1, "RPM", 16383.0, "rpm"))
    write_records_to_parquet(records, session, max_rows=100_000)
    return session


def test_main_writes_report_with_reject_and_totals(tmp_path: Path) -> None:
    session = _write_session(tmp_path, "s0", rows=200, start_epoch=1_780_000_000.0)
    out = tmp_path / "report.md"

    rc = main(
        [
            str(session),
            "--config",
            "canary.example.yaml",
            "--out",
            str(out),
            "--timestamp",
            "TEST",
        ]
    )

    assert rc == 0
    assert out.exists()
    text = out.read_text()
    assert "# Disposition report (TEST)" in text
    assert "obd:RPM" in text  # the rejected decode-rollover cell
    assert "No retrain occurred" in text


def test_main_no_sessions_found_returns_error(tmp_path: Path) -> None:
    rc = main(
        [
            "--sessions-root",
            str(tmp_path / "empty"),
            "--config",
            "canary.example.yaml",
            "--out",
            str(tmp_path / "report.md"),
        ]
    )
    assert rc == 1
    assert not (tmp_path / "report.md").exists()


def test_main_loads_accepted_baselines_override(tmp_path: Path, monkeypatch) -> None:
    """An active `accepted_baselines.yaml` override reclassifies what would
    otherwise be Quarantine as Accept-recenter in the written report."""

    session = _write_session(
        tmp_path, "s0", rows=200, start_epoch=1_780_000_000.0, fuel_trim_outliers=40
    )

    # Without an override file: the fuel-trim outlier band shows up as
    # Quarantine.
    out_no_override = tmp_path / "report_no_override.md"
    rc = main([str(session), "--config", "canary.example.yaml", "--out", str(out_no_override)])
    assert rc == 0
    no_override_text = out_no_override.read_text()
    assert "obd:SHORT_FUEL_TRIM_1" in no_override_text

    # Stub `load_accepted_baselines` to return a real, active override,
    # regardless of the (profile-scoped) path `main()` resolves — isolates
    # this test from the actual committed, empty
    # `data/nissan-xterra-2008/accepted_baselines.yaml`.
    import scripts.disposition_report as dr_module

    override = AcceptedBaselineOverride(
        channel="obd:SHORT_FUEL_TRIM_1",
        accepted_center=30.0,
        band=DirectionalBand(
            tolerance=3.0, worse_direction="positive", worse_warning=3.0, worse_critical=6.0
        ),
        provenance=Provenance(
            declared_by="test",
            declared_at=date(2020, 1, 1),
            review_by=date(2099, 1, 1),
            reason="test override",
        ),
    )
    stub_baselines = AcceptedBaselines(schema_version=1, overrides=[override])
    # The real committed `data/nissan-xterra-2008/accepted_baselines.yaml`
    # already exists (empty), so `main()`'s `.exists()` check passes without
    # needing a filesystem stub — only the parsed content needs stubbing.
    monkeypatch.setattr(dr_module, "load_accepted_baselines", lambda _path: stub_baselines)

    out_with_override = tmp_path / "report_with_override.md"
    rc = main([str(session), "--config", "canary.example.yaml", "--out", str(out_with_override)])
    assert rc == 0
    with_override_text = out_with_override.read_text()

    # Row shape: | channel | mode | accept_recenter | quarantine | accept | band |
    fuel_trim_line = next(
        line for line in with_override_text.splitlines() if "obd:SHORT_FUEL_TRIM_1" in line
    )
    cells = [c.strip() for c in fuel_trim_line.strip("|").split("|")]
    accept_recenter, quarantine = cells[2], cells[3]
    assert quarantine == "0"
    assert int(accept_recenter.replace(",", "")) > 0
