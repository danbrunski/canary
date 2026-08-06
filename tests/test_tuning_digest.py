"""Tests for the tuning section of a post-drive digest (roadmap Phase 5)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.analysis.tuning_digest import compute_tuning_digest
from src.config import CanaryConfig
from src.profiles import resolve_profile_for_tooling, scope_config
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord

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
    assert digest.wot_seconds == 10
    assert digest.wot_with_afr_pair == 10
    assert {row["rpm_band"] for row in digest.wot_by_band} == {
        "(2500.0, 3000.0]",
        "(3500.0, 4000.0]",
    }


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
    assert digest.wot_with_afr_pair == 0
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
                "profile_snapshot": {"corpus_eligible": False},
            }
        )
    )

    digest = compute_tuning_digest(session, config)

    assert digest is not None
    assert digest.wot_seconds == 0
    assert digest.delivery is None
    assert any("pull inventory unavailable" in note for note in digest.notes)
    assert any("all-DID capture lacks confirmed semantic roles" in note for note in digest.notes)


def test_digest_reproduces_the_reference_capture(config: CanaryConfig) -> None:
    """End-to-end against the real 2026-08-05 capture and its hand-checked numbers."""
    session = Path("sessions/nissan-xterra-2008/_tuning-2026-08-05T142720")
    if not session.exists():  # pragma: no cover - corpus not present in CI
        pytest.skip("reference capture not present locally")
    digest = compute_tuning_digest(session, config)
    assert digest is not None
    assert digest.tune_revision == "ZS44A-k106"
    assert digest.wot_seconds == 27
    assert digest.delivery is not None
    assert digest.delivery["n"] == 16
    # The closed-loop gate is what makes this number meaningful at all.
    assert digest.delivery["gates"]["filtered"]["closed_loop"] > 1000


def test_previous_capture_orders_by_time_not_directory_name(
    tmp_path: Path, config: CanaryConfig
) -> None:
    """Capture dirs carry different prefixes, so string order is not time order.

    `_capture-fuel-map-...` always sorts before `_tuning-...` regardless of
    date, so a capture a month *later* compared as "earlier" and could be
    picked as the previous run — silently comparing a drive against its own
    future.
    """
    from src.analysis.tune_revisions import load_tune_revisions
    from src.analysis.tuning_digest import _previous_capture

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
