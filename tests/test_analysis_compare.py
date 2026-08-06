"""The comparison API must refuse exactly where aggregates look convincing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from src.analysis.compare import (
    ComparisonRefusedError,
    align_pairs,
    compare_channels,
    regress,
    scale_stability,
)
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord

BINDINGS = Path("data/bindings.yaml")
DIDS = Path("data/dids.yaml")
T0 = datetime(2026, 8, 6, 12, 0, 0, tzinfo=UTC)


def _session(tmp_path: Path, records: list[TelemetryRecord]) -> Path:
    out = tmp_path / "2026-08-06T120000"
    write_records_to_parquet(records, out)
    return out


def _obd(channel: str, values: list[float], *, step_s: float = 1.0) -> list[TelemetryRecord]:
    return [
        TelemetryRecord(
            timestamp=T0 + timedelta(seconds=index * step_s),
            source="obd",
            channel=channel,
            value=value,
        )
        for index, value in enumerate(values)
    ]


def test_a_constant_channel_is_refused_not_fitted(tmp_path: Path) -> None:
    """This is the ECM_CALC_LOAD_B failure, reduced to its essence.

    That binding was accepted because a byte's per-window *mean* matched
    another byte's mean. A signal that barely moves has no scale to recover —
    every scale fits it — so any number produced here describes the session
    rather than the signals. Refusing is the only honest answer.
    """
    rng = np.random.default_rng(20260806)
    moving = list(rng.uniform(500.0, 4000.0, 200))
    flat = [128.0] * 200
    session = _session(tmp_path, _obd("RPM", moving) + _obd("SPEED", flat))

    with pytest.raises(ComparisonRefusedError, match="effectively constant"):
        compare_channels(session, "obd:RPM", "obd:SPEED", bindings_path=BINDINGS, dids_path=DIDS)


def test_too_few_aligned_pairs_is_refused_with_the_reason(tmp_path: Path) -> None:
    """ "No overlap" and "no relationship" are different answers; say which."""
    rng = np.random.default_rng(1)
    session = _session(
        tmp_path,
        _obd("RPM", list(rng.uniform(500.0, 4000.0, 10)))
        + _obd("SPEED", list(rng.uniform(0.0, 100.0, 10))),
    )

    with pytest.raises(ComparisonRefusedError, match="aligned pairs"):
        compare_channels(session, "obd:RPM", "obd:SPEED", bindings_path=BINDINGS, dids_path=DIDS)


def test_non_overlapping_series_never_pair(tmp_path: Path) -> None:
    """Binning inner-joins; it must not carry a neighbour's value forward.

    Two channels recorded an hour apart have nothing to say about each other,
    and a forward-filled join would happily report a relationship.
    """
    rng = np.random.default_rng(2)
    early = _obd("RPM", list(rng.uniform(500.0, 4000.0, 200)))
    late = [
        TelemetryRecord(
            timestamp=T0 + timedelta(hours=1, seconds=index),
            source="obd",
            channel="SPEED",
            value=float(value),
        )
        for index, value in enumerate(rng.uniform(0.0, 100.0, 200))
    ]
    session = _session(tmp_path, early + late)

    joined = align_pairs(session, "obd:RPM", "obd:SPEED", bindings_path=BINDINGS, dids_path=DIDS)
    assert joined.empty
    with pytest.raises(ComparisonRefusedError, match="aligned pairs"):
        compare_channels(session, "obd:RPM", "obd:SPEED", bindings_path=BINDINGS, dids_path=DIDS)


def test_a_real_linear_relationship_is_recovered_with_its_scale(tmp_path: Path) -> None:
    rng = np.random.default_rng(3)
    rpm = list(rng.uniform(800.0, 4000.0, 300))
    derived = [value * 0.25 + 3.0 for value in rpm]
    session = _session(tmp_path, _obd("RPM", rpm) + _obd("SPEED", derived))

    result = compare_channels(
        session, "obd:RPM", "obd:SPEED", bindings_path=BINDINGS, dids_path=DIDS
    )

    assert result.n_pairs == 300
    assert result.r_squared > 0.9999
    assert result.slope == pytest.approx(0.25, rel=1e-6)
    assert result.offset == pytest.approx(3.0, abs=1e-6)
    assert result.scale_verdict == "stable"


def test_n_pairs_counts_pairs_not_samples(tmp_path: Path) -> None:
    """The count that matters is what actually lined up.

    Reporting either input's length is how a comparison backed by a handful
    of real pairings gets quoted as if it had hundreds.
    """
    rng = np.random.default_rng(4)
    fast = list(rng.uniform(800.0, 4000.0, 400))
    # SPEED lands on every 4th second, so only a quarter of the bins pair.
    slow = [value * 0.25 for value in fast[::4]]
    session = _session(
        tmp_path,
        _obd("RPM", fast) + _obd("SPEED", slow, step_s=4.0),
    )

    result = compare_channels(
        session, "obd:RPM", "obd:SPEED", bindings_path=BINDINGS, dids_path=DIDS
    )

    assert result.n_pairs == 100
    assert result.n_pairs < len(fast)


def test_scale_stability_separates_unresolvable_from_unstable() -> None:
    """Three verdicts, because two of them have opposite consequences."""
    rng = np.random.default_rng(5)

    x = rng.uniform(0.0, 100.0, 300)
    assert scale_stability(x, x * 2.0 + 1.0)[1] == "stable"

    # Scale genuinely changes partway through, well beyond fitting noise.
    drift_x = np.concatenate([rng.uniform(0.0, 100.0, 150), rng.uniform(0.0, 100.0, 150)])
    drift_y = np.concatenate([drift_x[:150] * 2.0, drift_x[150:] * 6.0])
    assert scale_stability(drift_x, drift_y)[1] == "drifting"

    # Flat in every sub-window: no slope is identifiable, so no verdict is owed.
    flat = np.ones(300)
    assert scale_stability(flat, flat * 2.0)[1] == "unresolved"


def test_regress_refuses_a_constant_side() -> None:
    rng = np.random.default_rng(6)
    assert regress(np.ones(100), rng.uniform(0.0, 10.0, 100)) is None
    assert regress(rng.uniform(0.0, 10.0, 100), np.ones(100)) is None
