"""The comparison API must refuse exactly where aggregates look convincing."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.analysis.compare import (
    ComparisonRefusedError,
    PairingPolicy,
    align_pairs,
    binned_summary,
    compare_channels,
    regress,
    scale_stability,
)
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord

BINDINGS = Path("data/bindings.yaml")
DIDS = Path("data/dids.yaml")
T0 = datetime(2026, 8, 6, 12, 0, 0, tzinfo=UTC)
CATALOGS: dict[str, Any] = {"bindings_path": BINDINGS, "dids_path": DIDS}
#: Rows written by the batch writer carry ``metadata_json`` with no
#: ``acquired_at``, so their basis is ``stored`` and any tolerance is allowed.
STRICT = PairingPolicy.within("0ms")
LOOSE = PairingPolicy.within("1s")


def _session(tmp_path: Path, records: list[TelemetryRecord]) -> Path:
    out = tmp_path / "2026-08-06T120000"
    write_records_to_parquet(records, out)
    return out


def _obd(
    channel: str,
    values: list[float],
    *,
    step_s: float = 1.0,
    offset_s: float = 0.0,
    acquired_offset_s: float | None = None,
) -> list[TelemetryRecord]:
    records = []
    for index, value in enumerate(values):
        stamp = T0 + timedelta(seconds=index * step_s + offset_s)
        metadata: dict[str, Any] | None = None
        if acquired_offset_s is not None:
            metadata = {"acquired_at": stamp.timestamp() + acquired_offset_s}
        records.append(
            TelemetryRecord(
                timestamp=stamp,
                source="obd",
                channel=channel,
                value=value,
                metadata=metadata or {},
            )
        )
    return records


def _counterexample(tmp_path: Path) -> Path:
    """Two samples per second; every same-instant residual is exactly 100.

    A = 1000 + s + 50 then 1000 + s - 50; B the reverse. Their one-second
    means are identical, which is how the binned comparison reported r2=1,
    slope=1, offset~0, stable (roadmap C01).
    """
    records: list[TelemetryRecord] = []
    for second in range(60):
        for half, (delta_a, delta_b) in enumerate(((50.0, -50.0), (-50.0, 50.0))):
            stamp = T0 + timedelta(seconds=second + 0.5 * half)
            records.append(
                TelemetryRecord(
                    timestamp=stamp, source="obd", channel="RPM", value=1000.0 + second + delta_a
                )
            )
            records.append(
                TelemetryRecord(
                    timestamp=stamp, source="obd", channel="SPEED", value=1000.0 + second + delta_b
                )
            )
    return _session(tmp_path, records)


# --- C01.1 / C01.13: the counterexample -------------------------------------


def test_the_counterexample_cannot_be_reported_as_a_one_to_one_agreement(tmp_path: Path) -> None:
    session = _counterexample(tmp_path)

    result = compare_channels(session, "obd:RPM", "obd:SPEED", policy=STRICT, **CATALOGS)

    assert result.n_pairs == 120  # every real sample, not 60 bins
    assert not (result.r_squared > 0.99 and abs(result.slope - 1.0) < 0.05), result.describe()
    assert result.r_squared < 0.7
    assert result.slope < 0.0
    # The residuals are reported in engineering units, and they are not small:
    # every pair differs by exactly 100, so the 1:1 residual spread *is* 100.
    assert result.residual_spread > 20.0
    assert result.difference_spread == pytest.approx(100.0)
    assert result.difference_bias == pytest.approx(0.0, abs=1e-9)


def test_the_counterexample_pairs_disagree_by_exactly_100_at_every_instant(tmp_path: Path) -> None:
    aligned = align_pairs(
        _counterexample(tmp_path), "obd:RPM", "obd:SPEED", policy=STRICT, **CATALOGS
    )
    assert aligned.n_pairs == 120
    assert np.all(np.abs(aligned.pairs["a"] - aligned.pairs["b"]) == 100.0)
    assert np.all(aligned.pairs["separation_s"] == 0.0)


def test_binned_summary_is_where_the_false_agreement_lives_and_says_so(tmp_path: Path) -> None:
    """The bins still agree — which is why they are a picture, not evidence."""
    summary = binned_summary(
        _counterexample(tmp_path), "obd:RPM", "obd:SPEED", grid="1s", **CATALOGS
    )
    assert len(summary) == 60
    assert np.allclose(summary["a_mean"], summary["b_mean"])
    assert set(summary.columns) == {"bin", "a_mean", "n_a", "b_mean", "n_b"}
    assert (summary["n_a"] == 2).all()


def test_identical_round_stamps_do_not_pair_when_acquired_apart(tmp_path: Path) -> None:
    """A shared logical stamp is not simultaneity; ``acquired_at`` is."""
    rng = np.random.default_rng(7)
    rpm = list(rng.uniform(800.0, 4000.0, 120))
    session = _session(
        tmp_path,
        _obd("RPM", rpm, acquired_offset_s=0.0)
        + _obd("SPEED", [v * 0.25 for v in rpm], acquired_offset_s=0.8),
    )
    with pytest.raises(ComparisonRefusedError, match="aligned pairs"):
        compare_channels(
            session, "obd:RPM", "obd:SPEED", policy=PairingPolicy.within("100ms"), **CATALOGS
        )
    # At a tolerance that admits 800 ms the same rows pair, and the result
    # says how far apart they really were.
    result = compare_channels(session, "obd:RPM", "obd:SPEED", policy=LOOSE, **CATALOGS)
    assert result.n_pairs == 120
    assert result.timestamp_basis_a == result.timestamp_basis_b == "acquired"
    assert result.separation_median_s == pytest.approx(0.8)
    assert result.separation_max_s == pytest.approx(0.8)


# --- C01.3: clock provenance ------------------------------------------------


def _round_manifest(session: Path) -> None:
    (session / "capture_profile.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile": "tuning",
                "started_at": T0.isoformat(),
                "profile_snapshot": {"round_timestamps": True},
            }
        )
    )


def test_round_stamps_without_acquisition_refuse_a_tolerance_inside_the_round(
    tmp_path: Path,
) -> None:
    rng = np.random.default_rng(8)
    rpm = list(rng.uniform(800.0, 4000.0, 120))
    session = _session(tmp_path, _obd("RPM", rpm) + _obd("SPEED", [v * 0.25 for v in rpm]))
    _round_manifest(session)

    with pytest.raises(ComparisonRefusedError, match="'round'"):
        compare_channels(
            session, "obd:RPM", "obd:SPEED", policy=PairingPolicy.within("500ms"), **CATALOGS
        )
    result = compare_channels(session, "obd:RPM", "obd:SPEED", policy=LOOSE, **CATALOGS)
    assert result.timestamp_basis_a == result.timestamp_basis_b == "round"
    assert result.n_pairs == 120


def test_missing_metadata_falls_back_to_stored_and_a_frame_without_any_is_unknown(
    tmp_path: Path,
) -> None:
    rng = np.random.default_rng(9)
    rpm = list(rng.uniform(800.0, 4000.0, 120))
    session = _session(tmp_path, _obd("RPM", rpm) + _obd("SPEED", [v * 0.25 for v in rpm]))
    result = compare_channels(session, "obd:RPM", "obd:SPEED", policy=STRICT, **CATALOGS)
    assert result.timestamp_basis_a == result.timestamp_basis_b == "stored"

    frame = pd.read_parquet(next(session.glob("telemetry-*.parquet"))).drop(
        columns=["metadata_json"]
    )
    with pytest.raises(ComparisonRefusedError, match="metadata_json"):
        compare_channels(
            frame, "obd:RPM", "obd:SPEED", policy=PairingPolicy.within("999ms"), **CATALOGS
        )
    unknown = compare_channels(frame, "obd:RPM", "obd:SPEED", policy=LOOSE, **CATALOGS)
    assert unknown.timestamp_basis_a == "unknown"


# --- C01.5: the grid migration ----------------------------------------------


def test_grid_is_refused_by_name_not_reinterpreted(tmp_path: Path) -> None:
    session = _counterexample(tmp_path)
    with pytest.raises(TypeError, match="grid= is gone"):
        compare_channels(session, "obd:RPM", "obd:SPEED", policy=STRICT, grid="1s", **CATALOGS)
    with pytest.raises(TypeError, match="binned_summary"):
        align_pairs(session, "obd:RPM", "obd:SPEED", policy=STRICT, grid="1s", **CATALOGS)
    with pytest.raises(TypeError):
        compare_channels(session, "obd:RPM", "obd:SPEED", **CATALOGS)


# --- C01.9 / C01.10: pairing behaviour through the public path --------------


def test_exact_simultaneous_agreement_is_recovered_exactly(tmp_path: Path) -> None:
    rng = np.random.default_rng(3)
    rpm = list(rng.uniform(800.0, 4000.0, 300))
    derived = [value * 0.25 + 3.0 for value in rpm]
    session = _session(tmp_path, _obd("RPM", rpm) + _obd("SPEED", derived))

    result = compare_channels(session, "obd:RPM", "obd:SPEED", policy=STRICT, **CATALOGS)

    assert result.n_pairs == 300
    assert result.r_squared > 0.9999
    assert result.slope == pytest.approx(0.25, rel=1e-6)
    assert result.offset == pytest.approx(3.0, abs=1e-6)
    assert result.scale_verdict == "stable"
    assert result.unmatched_a == result.unmatched_b == 0
    assert result.separation_max_s == 0.0
    assert result.residual_bias == pytest.approx(0.0, abs=1e-9)
    assert result.residual_spread == pytest.approx(0.0, abs=1e-9)
    # The 1:1 residual is not zero here — SPEED = 0.25 * RPM + 3 is a real
    # scale, and b - a says so even though the fit absorbs it.
    assert abs(result.difference_bias) > 100.0


def test_unequal_rates_pair_each_sparse_sample_once_and_count_the_rest(tmp_path: Path) -> None:
    rng = np.random.default_rng(4)
    fast = list(rng.uniform(800.0, 4000.0, 400))
    slow = [value * 0.25 for value in fast[::4]]
    session = _session(tmp_path, _obd("RPM", fast) + _obd("SPEED", slow, step_s=4.0))

    result = compare_channels(session, "obd:RPM", "obd:SPEED", policy=STRICT, **CATALOGS)

    assert result.n_pairs == 100
    assert result.reference == "b"
    assert result.unmatched_a == 300
    assert result.unmatched_b == 0
    assert result.slope == pytest.approx(0.25, rel=1e-6)


def test_tolerance_boundary_pairs_at_exactly_the_tolerance_and_not_beyond(tmp_path: Path) -> None:
    rng = np.random.default_rng(10)
    rpm = list(rng.uniform(800.0, 4000.0, 100))
    session = _session(
        tmp_path, _obd("RPM", rpm) + _obd("SPEED", [v * 0.25 for v in rpm], offset_s=0.25)
    )
    at = align_pairs(
        session, "obd:RPM", "obd:SPEED", policy=PairingPolicy.within("250ms"), **CATALOGS
    )
    beyond = align_pairs(
        session, "obd:RPM", "obd:SPEED", policy=PairingPolicy.within("249ms"), **CATALOGS
    )
    assert at.n_pairs == 100
    assert np.all(at.pairs["separation_s"] == 0.25)
    assert beyond.n_pairs == 0
    assert beyond.unmatched_a == beyond.unmatched_b == 100


def test_ties_and_duplicate_stamps_are_deterministic(tmp_path: Path) -> None:
    """One SPEED sample between two equidistant RPM samples: earlier wins; twice."""
    records = [
        TelemetryRecord(timestamp=T0, source="obd", channel="RPM", value=1000.0),
        TelemetryRecord(
            timestamp=T0 + timedelta(seconds=1), source="obd", channel="RPM", value=2000.0
        ),
        TelemetryRecord(
            timestamp=T0 + timedelta(seconds=0.5), source="obd", channel="SPEED", value=10.0
        ),
        TelemetryRecord(
            timestamp=T0 + timedelta(seconds=0.5), source="obd", channel="SPEED", value=11.0
        ),
    ]
    session = _session(tmp_path, records)
    aligned = align_pairs(
        session, "obd:RPM", "obd:SPEED", policy=PairingPolicy.within("500ms"), **CATALOGS
    )
    assert aligned.n_pairs == 2
    # Reference is auto -> equal counts -> "a" (RPM). First RPM sample takes the
    # lower-index duplicate; the second RPM sample takes the remaining one.
    assert sorted(aligned.pairs["b"].tolist()) == [10.0, 11.0]
    assert len(set(aligned.pairs["index_b"])) == 2
    later = align_pairs(
        session,
        "obd:SPEED",
        "obd:RPM",
        policy=PairingPolicy.within("500ms", tie_break="later"),
        **CATALOGS,
    )
    assert later.pairs["b"].tolist() == [2000.0, 1000.0]


def test_unmatched_values_are_never_carried_forward_into_pairs(tmp_path: Path) -> None:
    """A stale neighbour is not a pair; every pair is inside tolerance."""
    rng = np.random.default_rng(11)
    rpm = list(rng.uniform(800.0, 4000.0, 200))
    # SPEED exists for the first 50 s only; afterwards RPM has no partner.
    session = _session(tmp_path, _obd("RPM", rpm) + _obd("SPEED", [v * 0.25 for v in rpm[:50]]))
    aligned = align_pairs(session, "obd:RPM", "obd:SPEED", policy=LOOSE, **CATALOGS)
    assert aligned.n_pairs == 50
    assert aligned.unmatched_a == 150
    assert (aligned.pairs["timestamp_a"] < pd.Timestamp(T0 + timedelta(seconds=50))).all()


def test_every_reported_pair_is_two_identifiable_observations_within_tolerance(
    tmp_path: Path,
) -> None:
    """C01.13 acceptance, checked against the rows the reader actually produced."""
    from src.analysis.channels import read_channels

    rng = np.random.default_rng(12)
    rpm = list(rng.uniform(800.0, 4000.0, 400))
    session = _session(
        tmp_path,
        _obd("RPM", rpm, step_s=0.1) + _obd("SPEED", [v * 0.25 for v in rpm[::7]], step_s=0.7),
    )
    policy = PairingPolicy.within("40ms")
    aligned = align_pairs(session, "obd:RPM", "obd:SPEED", policy=policy, **CATALOGS)
    long = read_channels(session, ["obd:RPM", "obd:SPEED"], acquired_time=True, **CATALOGS)

    assert aligned.n_pairs > 0
    assert aligned.pairs["index_a"].is_unique and aligned.pairs["index_b"].is_unique
    rows_a = long.loc[aligned.pairs["index_a"]]
    rows_b = long.loc[aligned.pairs["index_b"]]
    assert (rows_a["spec"] == "obd:RPM").all() and (rows_b["spec"] == "obd:SPEED").all()
    assert np.array_equal(rows_a["value"].to_numpy(), aligned.pairs["a"].to_numpy())
    assert np.array_equal(rows_b["value"].to_numpy(), aligned.pairs["b"].to_numpy())
    separation = (rows_a["timestamp"].to_numpy() - rows_b["timestamp"].to_numpy()) / np.timedelta64(
        1, "s"
    )
    assert np.all(np.abs(separation) <= policy.max_separation_s)
    result = compare_channels(session, "obd:RPM", "obd:SPEED", policy=policy, **CATALOGS)
    assert result.n_pairs == aligned.n_pairs
    assert result.separation_max_s <= policy.max_separation_s


def test_result_serialises_with_its_policy(tmp_path: Path) -> None:
    rng = np.random.default_rng(13)
    rpm = list(rng.uniform(800.0, 4000.0, 100))
    session = _session(tmp_path, _obd("RPM", rpm) + _obd("SPEED", [v * 0.25 for v in rpm]))
    result = compare_channels(session, "obd:RPM", "obd:SPEED", policy=STRICT, **CATALOGS)
    payload = json.loads(json.dumps(result.as_dict()))
    assert payload["policy"] == {
        "max_separation_s": 0.0,
        "reference": "auto",
        "tie_break": "earlier",
    }
    assert payload["n_pairs"] == 100
    assert payload["timestamp_basis_a"] == "stored"
    assert "policy=(within 0 ms" in result.describe()


# --- refusals that predate C01 and must survive it --------------------------


def test_a_channel_that_was_never_captured_is_named_not_blamed_on_the_clock(
    tmp_path: Path,
) -> None:
    """ "Never captured" and "clock cannot support it" demand opposite responses.

    A side with zero observations has no timestamp basis; falling through to
    the clock check blamed ``metadata_json`` for a PID the profile simply did
    not poll. The refusal must name the absent input.
    """
    rng = np.random.default_rng(7)
    session = _session(tmp_path, _obd("RPM", list(rng.uniform(500.0, 4000.0, 120))))

    for policy in (PairingPolicy.within("250ms"), LOOSE):
        with pytest.raises(ComparisonRefusedError) as excinfo:
            compare_channels(session, "obd:RPM", "obd:SPEED", policy=policy, **CATALOGS)
        message = str(excinfo.value)
        assert "obd:SPEED" in message
        assert "no observations" in message
        assert "metadata_json" not in message


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
        compare_channels(session, "obd:RPM", "obd:SPEED", policy=STRICT, **CATALOGS)


def test_too_few_aligned_pairs_is_refused_with_the_reason(tmp_path: Path) -> None:
    """ "No overlap" and "no relationship" are different answers; say which."""
    rng = np.random.default_rng(1)
    session = _session(
        tmp_path,
        _obd("RPM", list(rng.uniform(500.0, 4000.0, 10)))
        + _obd("SPEED", list(rng.uniform(0.0, 100.0, 10))),
    )

    with pytest.raises(ComparisonRefusedError, match="aligned pairs"):
        compare_channels(session, "obd:RPM", "obd:SPEED", policy=STRICT, **CATALOGS)


def test_non_overlapping_series_never_pair(tmp_path: Path) -> None:
    """Two channels recorded an hour apart have nothing to say about each other."""
    rng = np.random.default_rng(2)
    early = _obd("RPM", list(rng.uniform(500.0, 4000.0, 200)))
    late = _obd("SPEED", list(rng.uniform(0.0, 100.0, 200)), offset_s=3600.0)
    session = _session(tmp_path, early + late)

    aligned = align_pairs(session, "obd:RPM", "obd:SPEED", policy=LOOSE, **CATALOGS)
    assert aligned.empty
    assert aligned.unmatched_a == aligned.unmatched_b == 200
    with pytest.raises(ComparisonRefusedError, match="aligned pairs"):
        compare_channels(session, "obd:RPM", "obd:SPEED", policy=LOOSE, **CATALOGS)


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
