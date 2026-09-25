"""R05.13 — WOT floors and rpm bands are profile data, not constants.

``tuning_digest`` carried ``WOT_RPM_FLOOR = 2500`` / ``WOT_LOAD_FLOOR = 85``
and ``tuning.DEFAULT_RPM_BANDS`` ended at 6500 rpm — the dev vehicle's VQ40
redline — while ``WotCriterion`` promised such values are "never in code".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.analysis.tuning_digest import _wot_inventory
from src.analysis.tuning_frame import LoadWotCriterion, load_provider_registry
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord

T0 = datetime(2026, 8, 5, 14, 0, 0, tzinfo=UTC)


def _mode01(root: Path, rows: list[tuple[float, float]]) -> Path:
    records: list[TelemetryRecord] = []
    for i, (rpm, load) in enumerate(rows):
        ts = T0 + timedelta(seconds=i)
        records += [
            TelemetryRecord(timestamp=ts, source="obd", channel="RPM", value=rpm),
            TelemetryRecord(timestamp=ts, source="obd", channel="ENGINE_LOAD", value=load),
        ]
    out = root / "_capture-fuel-map-2026-08-05T140000"
    write_records_to_parquet(records, out)
    return out


def test_the_floors_and_bands_come_from_the_profile(tmp_path: Path) -> None:
    session = _mode01(tmp_path, [(3200.0, 80.0)] * 4 + [(3200.0, 92.0)] * 3)
    xterra = load_provider_registry(Path("data/nissan-xterra-2008/tuning_providers.yaml"))
    assert xterra.mode01_wot is not None
    shipped = _wot_inventory(
        session, rpm_spec=None, did_continuous=False, load_wot=xterra.mode01_wot
    )
    assert shipped.seconds == 3  # load > 85 %
    lower = LoadWotCriterion(load_floor_pct=75.0, rpm_floor=2000.0, rpm_bands=(2000.0, 8000.0))
    other = _wot_inventory(session, rpm_spec=None, did_continuous=False, load_wot=lower)
    assert other.seconds == 7
    assert other.by_band == [{"rpm_band": "(2000.0, 8000.0]", "seconds": 7}]
    assert other.criterion is not None and other.criterion["threshold_value"] == 75.0


def test_a_vehicle_without_mode01_wot_says_so(tmp_path: Path) -> None:
    session = _mode01(tmp_path, [(3200.0, 92.0)] * 3)
    inventory = _wot_inventory(session, rpm_spec=None, did_continuous=False, load_wot=None)
    assert inventory.source == "unavailable"
    assert inventory.reason is not None and "mode01_wot" in inventory.reason


def test_rpm_bands_must_ascend() -> None:
    with pytest.raises(ValidationError, match="ascending"):
        LoadWotCriterion(load_floor_pct=85.0, rpm_floor=2500.0, rpm_bands=(3000.0, 2500.0))


def test_min_load_defaults_to_the_profiles_wot_floor() -> None:
    """Operator decision 2026-09-25: the MCP/CLI ``min_load`` default is the
    profile's ``mode01_wot.load_floor_pct``, not a restated 85."""
    import inspect

    from scripts import tuning_report
    from src.analysis.tuning_context import TuningContext
    from src.mcp_server.tools import tuning_analysis

    xterra = load_provider_registry(Path("data/nissan-xterra-2008/tuning_providers.yaml"))
    context = TuningContext(afr_stoich=14.7, providers=xterra)
    assert xterra.mode01_wot is not None
    assert context.resolve_min_load(None) == xterra.mode01_wot.load_floor_pct == 85.0
    assert context.resolve_min_load(70.0) == 70.0
    with pytest.raises(ValueError, match="mode01_wot"):
        TuningContext(afr_stoich=14.7).resolve_min_load(None)

    signature = inspect.signature(tuning_analysis.get_delivery_error)
    assert signature.parameters["min_load"].default is None
    args = tuning_report.build_parser().parse_args(["delivery-error", "--session", "x"])
    assert args.min_load is None
