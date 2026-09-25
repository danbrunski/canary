"""The drive-grouped evaluation protocol (A02.4) and its thresholds (A02.3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.analysis.tune_revisions import TuneRevisionRegistry
from src.models.evaluation import (
    Drive,
    Era,
    Fold,
    HoldoutSplit,
    PersistenceSpec,
    ScoreTrace,
    SessionSpan,
    alert_budget_threshold,
    alert_episodes,
    band_of,
    block_maxima,
    bootstrap_ratio_ci,
    engine_hours,
    engine_running,
    era_resolver,
    fault_attributable_detection,
    group_into_drives,
    grouped_folds,
    observed_median,
    session_span,
    sustained_alerts,
    vus_pr,
)
from tests.builders import write_fixture_session

T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
GATE = PersistenceSpec(required=3, window=5, window_seconds=8.0)


def _span(name: str, start_min: float, end_min: float) -> SessionSpan:
    return SessionSpan(name, T0 + timedelta(minutes=start_min), T0 + timedelta(minutes=end_min))


def _stamps(n: int, *, step: float = 1.0) -> pd.Series:
    return pd.Series(pd.date_range("2026-09-18T12:00:00Z", periods=n, freq=f"{step}s"))


# ---------------------------------------------------------------------------
# Drives, eras, folds, splits
# ---------------------------------------------------------------------------


def test_sessions_within_the_gap_are_one_drive_and_order_by_parsed_time() -> None:
    spans = [
        _span("2026-09-18T120000", 0, 30),
        # a capture that opened mid-drive: its prefix sorts first by name
        _span("_capture-tuning-2026-09-18T121000", 10, 40),
        _span("2026-09-18T124200", 42, 60),  # 2-min gap: a restart
        _span("2026-09-18T140000", 120, 150),  # an hour later: a new drive
    ]
    drives = group_into_drives(list(reversed(spans)))
    assert [d.session_names for d in drives] == [
        ("2026-09-18T120000", "_capture-tuning-2026-09-18T121000", "2026-09-18T124200"),
        ("2026-09-18T140000",),
    ]
    assert drives[0].drive_id == "drive-2026-09-18T120000"


def test_a_drive_across_a_flash_straddles_and_joins_no_fold() -> None:
    registry = TuneRevisionRegistry.model_validate(
        {
            "revisions": [
                {"revision": "stock", "flashed_at": "2000-01-01T00:00:00Z"},
                {"revision": "tuned", "flashed_at": "2026-09-18T12:20:00Z"},
            ]
        }
    )
    resolve = era_resolver(registry)
    drives = group_into_drives(
        [_span("a", 0, 30), _span("b", 60, 70), _span("c", 80, 90), _span("d", 200, 210)],
        era_of=resolve,
    )
    straddling = drives[0]
    assert straddling.era is None and straddling.straddles is not None
    assert all(d.era == Era("tuned", 0) for d in drives[1:])
    folds = grouped_folds(drives)
    assert all(straddling.drive_id not in (*f.calibration, *f.evaluation) for f in folds)
    assert len(folds) == 3  # leave-one-drive-out over the three tuned drives


def test_a_hardware_boundary_starts_a_new_era() -> None:
    resolve = era_resolver(None, [T0 + timedelta(minutes=100)])
    assert resolve(T0) == Era(None, 0)
    assert resolve(T0 + timedelta(minutes=101)) == Era(None, 1)


def test_a_fold_never_mixes_eras_or_sides() -> None:
    before = Drive("d1", (), T0, T0, Era("stock", 0))
    after = Drive("d2", (), T0, T0, Era("tuned", 0))
    fold = Fold(Era("stock", 0), calibration=("d1",), evaluation=("d2",))
    with pytest.raises(ValueError, match="never mixes"):
        fold.check_eras({"d1": before, "d2": after})
    with pytest.raises(ValueError, match="both the calibration and evaluation"):
        Fold(Era("stock", 0), calibration=("d1",), evaluation=("d1",))


def test_a_session_in_both_holdouts_is_refused() -> None:
    """A02.1: the gate may not score the sessions thresholds were cut from."""
    with pytest.raises(ValueError, match=r"\['s2'\].*both the calibration and the evaluation"):
        HoldoutSplit(calibration=("s1", "s2"), evaluation=("s2", "s3"))


def test_one_physical_drive_cannot_sit_on_both_sides() -> None:
    split = HoldoutSplit(calibration=("a",), evaluation=("b",))
    (drive,) = group_into_drives([_span("a", 0, 10), _span("b", 11, 20)])
    with pytest.raises(ValueError, match="split across"):
        split.check_drives([drive])


def test_session_span_reads_parquet_footers(tmp_path: Path) -> None:
    session = write_fixture_session(tmp_path, "2026-05-12T000000", seed=1, n=30)
    span = session_span(session)
    assert span is not None
    assert span.name == "2026-05-12T000000"
    assert (span.end - span.start).total_seconds() > 0
    assert session_span(tmp_path / "empty") is None


# ---------------------------------------------------------------------------
# Alerts, episodes, engine time, detection
# ---------------------------------------------------------------------------


def test_episodes_follow_the_persistence_gate_and_ignore_blind_ticks() -> None:
    verdicts = [True, True, True, None, True, False, False, True, True, True, True]
    stamps = _stamps(len(verdicts))
    alerting = sustained_alerts(stamps, verdicts, GATE)
    # 3-of-5: the third firing tick latches; the blind tick neither latches
    # nor breaks the run.
    assert list(alerting) == [False, False, True, False, True, False, False, True, True, True, True]
    episodes = alert_episodes(stamps, alerting, verdicts=verdicts)
    assert len(episodes) == 2
    assert episodes[0].start == stamps.iloc[2]
    assert episodes[0].end == stamps.iloc[4]


def test_a_time_gap_ends_an_episode() -> None:
    stamps = pd.Series(
        list(pd.date_range("2026-09-18T12:00:00Z", periods=3, freq="1s"))
        + list(pd.date_range("2026-09-18T12:01:00Z", periods=3, freq="1s"))
    )
    episodes = alert_episodes(stamps, [True] * 6)
    assert len(episodes) == 2


def test_engine_hours_count_only_running_rows_and_cap_gaps() -> None:
    stamps = pd.Series(
        list(pd.date_range("2026-09-18T12:00:00Z", periods=3601, freq="1s"))
        + [pd.Timestamp("2026-09-18T15:00:00Z")]
    )
    running = np.ones(len(stamps), dtype=bool)
    # 3600 one-second steps plus one 2-hour gap capped at 5 s.
    assert engine_hours(stamps, running) == pytest.approx((3600 + 5) / 3600)
    running[:1800] = False
    assert engine_hours(stamps, running) == pytest.approx((1800 + 5) / 3600)
    frame = pd.DataFrame({"obd:RPM": [0.0, 399.0, 400.0, np.nan]})
    assert list(engine_running(frame)) == [False, False, True, False]
    # Mode 01 disabled (a DID-only capture): the broadcast rpm, in rpm, fills in.
    frame["decoded:ECM_RPM"] = [np.nan, np.nan, np.nan, 8 * 800.0]
    assert list(engine_running(frame)) == [False, False, True, True]


def test_a_detection_is_an_alert_the_fault_caused_on_the_same_rows() -> None:
    stamps = _stamps(20)
    start, end = stamps.iloc[5], stamps.iloc[10]
    verdicts = [True] * 20
    quiet = np.zeros(20, dtype=bool)
    faulted = np.zeros(20, dtype=bool)
    faulted[8] = True
    detected = fault_attributable_detection(
        stamps,
        faulted_alerting=faulted,
        clean_alerting=quiet,
        faulted_verdicts=verdicts,
        start=start,
        end=end,
        grace_seconds=0,
    )
    assert detected.status == "detected"
    assert detected.time_to_detect_seconds == 3.0

    # Already alerting on the clean rows: masked — no credit for a flood or
    # for jitter near an existing alert (no point-adjust).
    flooding = np.zeros(20, dtype=bool)
    flooding[9] = True
    assert (
        fault_attributable_detection(
            stamps,
            faulted_alerting=np.ones(20, dtype=bool),
            clean_alerting=flooding,
            faulted_verdicts=verdicts,
            start=start,
            end=end,
            grace_seconds=0,
        ).status
        == "masked"
    )
    # Quiet on both: a miss.
    assert (
        fault_attributable_detection(
            stamps,
            faulted_alerting=quiet,
            clean_alerting=quiet,
            faulted_verdicts=verdicts,
            start=start,
            end=end,
            grace_seconds=0,
        ).status
        == "missed"
    )
    # No verdict anywhere in the window: withheld, not missed.
    blind = [None] * 20
    assert (
        fault_attributable_detection(
            stamps,
            faulted_alerting=quiet,
            clean_alerting=quiet,
            faulted_verdicts=blind,
            start=start,
            end=end,
            grace_seconds=0,
        ).status
        == "withheld"
    )


# ---------------------------------------------------------------------------
# A02.3 — thresholds from the alert budget over independent blocks
# ---------------------------------------------------------------------------


def _trace(hours: float, *, spikes: int, seed: int = 0) -> ScoreTrace:
    n = int(hours * 3600)
    rng = np.random.default_rng(seed)
    scores = rng.normal(1.0, 0.1, n)
    for position in rng.choice(np.arange(100, n - 100), size=spikes, replace=False):
        scores[position : position + 10] = 5.0 + rng.random()
    return ScoreTrace(_stamps(n), scores, np.ones(n, dtype=bool))


def test_block_maxima_take_one_sample_per_block() -> None:
    stamps = _stamps(90)
    maxima = block_maxima(stamps, np.arange(90, dtype=float), block_seconds=30)
    assert list(maxima) == [29.0, 59.0, 89.0]


def test_budget_threshold_clears_the_rare_excursions_it_must() -> None:
    # 4 engine-hours with 12 excursions: a 1/h budget allows 4, so the
    # threshold must sit above the lowest 8 excursions and the achieved rate
    # stays within the budget.
    trace = _trace(4.0, spikes=12)
    budget = alert_budget_threshold([trace], persistence=GATE, block_seconds=30)
    assert budget.resolvable
    assert budget.threshold is not None and budget.threshold > 5.0
    assert budget.achieved_per_engine_hour is not None
    assert budget.achieved_per_engine_hour <= 1.0
    assert budget.blocks >= 4 * 120 - 1


def test_budget_threshold_says_when_the_exposure_cannot_resolve_it() -> None:
    short = _trace(0.25, spikes=1)
    budget = alert_budget_threshold([short], persistence=GATE, block_seconds=30)
    assert not budget.resolvable
    assert "unresolvable" in budget.note
    assert budget.threshold is not None and budget.threshold >= 5.0

    blind = ScoreTrace(_stamps(10), np.full(10, np.nan), np.ones(10, dtype=bool))
    empty = alert_budget_threshold([blind], persistence=GATE, block_seconds=30)
    assert empty.threshold is None and empty.note.startswith("withheld")


# ---------------------------------------------------------------------------
# Uncertainty, threshold-free comparison, strata
# ---------------------------------------------------------------------------


def test_bootstrap_resamples_drives_and_reports_the_ratio_of_sums() -> None:
    estimate = bootstrap_ratio_ci([2, 0, 4], [1.0, 1.0, 2.0], seed=3)
    assert estimate.estimate == pytest.approx(6 / 4)
    assert estimate.low is not None and estimate.high is not None
    assert estimate.low <= estimate.estimate <= estimate.high
    assert estimate == bootstrap_ratio_ci([2, 0, 4], [1.0, 1.0, 2.0], seed=3)
    one = bootstrap_ratio_ci([1], [2.0])
    assert one.estimate == 0.5 and one.low is None and "one drive" in one.note
    assert bootstrap_ratio_ci([], []).note.startswith("withheld")


def test_vus_pr_orders_detectors_without_a_threshold() -> None:
    labels = np.zeros(400, dtype=bool)
    labels[100:130] = True
    labels[300:320] = True
    perfect = labels.astype(float)
    rng = np.random.default_rng(0)
    noise = rng.random(400)
    inverted = 1.0 - perfect
    perfect_score = vus_pr(perfect, labels)
    # Buffers widen the labels, so even a sharp oracle loses a little.
    assert perfect_score is not None and perfect_score > 0.8
    noise_score = vus_pr(noise, labels)
    inverted_score = vus_pr(inverted, labels)
    assert noise_score is not None and inverted_score is not None
    assert perfect_score > noise_score > inverted_score - 1e-9
    assert vus_pr(noise, np.zeros(400, dtype=bool)) is None


def test_strata_use_freshly_observed_values_only() -> None:
    frame = pd.DataFrame(
        {
            "obd:BAROMETRIC_PRESSURE": [65.0, 65.0, 65.0, 80.0],
            "obd:BAROMETRIC_PRESSURE__gap": [False, True, True, False],
        }
    )
    # The forward-filled 65s are one reading, not three.
    assert observed_median(frame, "obd:BAROMETRIC_PRESSURE") == pytest.approx(72.5)
    assert observed_median(frame, "obd:AMBIENT_AIR_TEMP") is None
    from src.models.evaluation import BARO_BANDS_KPA, UNMEASURED

    assert band_of(72.5, BARO_BANDS_KPA) == "70-77 kPa"
    assert band_of(None, BARO_BANDS_KPA) == UNMEASURED


# ---------------------------------------------------------------------------
# A04.1 — single-quantity shift known answer
# ---------------------------------------------------------------------------


def _shift_frame(n: int = 200, channels: int = 40) -> pd.DataFrame:
    from src.preprocessing.segmenter import MODE_COLUMN

    rng = np.random.default_rng(0)
    frame = pd.DataFrame({MODE_COLUMN: ["city"] * (n // 2) + ["idle"] * (n // 2)})
    for index in range(channels):
        frame[f"obd:C{index:02d}"] = rng.normal(0.0, 1.0, n)
        frame[f"obd:C{index:02d}__mean"] = rng.normal(0.0, 0.3, n)
    return frame


def _z_detector(reduce: str, limit: float):  # type: ignore[no-untyped-def]
    """Row z-scores against the clean frame's own spread, reduced by max
    (sees one channel) or mean (dilutes it across all of them)."""
    base = _shift_frame()
    columns = [c for c in base.columns if c.startswith("obd:")]
    center, spread = base[columns].mean(), base[columns].std()

    def score(frame: pd.DataFrame) -> dict[str, list[bool]]:
        z = ((frame[columns] - center) / spread).abs()
        value = z.max(axis=1) if reduce == "max" else z.mean(axis=1)
        return {reduce: list((value > limit).to_numpy())}

    return score


def test_a_max_z_detector_passes_the_single_quantity_shift() -> None:
    from src.models.evaluation import single_quantity_shift
    from src.preprocessing.segmenter import MODE_COLUMN

    report = single_quantity_shift(
        _z_detector("max", 6.0),
        _shift_frame(),
        {"trim_b1": ["obd:C03"], "coolant": ["obd:C07", "obd:C08"]},
        mode_column=MODE_COLUMN,
    )["max"]
    assert report.verdict == "pass", report.note
    at_ten = {o.quantity: o.flag_rate for o in report.outcomes if o.sigma == 10.0}
    assert at_ten == {"trim_b1": 1.0, "coolant": 1.0}
    assert report.control is not None and report.control.flag_rate == 1.0


def test_a_detector_that_dilutes_one_quantity_is_recorded_as_failing() -> None:
    """The deployed IF's shape: every feature moved 3 sigma flags all rows,
    one quantity moved 100 sigma flags none. That is a failure, recorded."""
    from src.models.evaluation import single_quantity_shift
    from src.preprocessing.segmenter import MODE_COLUMN

    report = single_quantity_shift(
        _z_detector("mean", 2.0),
        _shift_frame(),
        {"trim_b1": ["obd:C03"]},
        mode_column=MODE_COLUMN,
        sigmas=(3.0, 10.0),
    )["mean"]
    assert report.verdict == "fail"
    assert "trim_b1" in report.note
    assert report.control is not None and report.control.flag_rate == 1.0
    assert all(o.flag_rate == 0.0 for o in report.outcomes)


def test_a_detector_the_shift_never_reaches_is_invalid_not_failing() -> None:
    from src.models.evaluation import single_quantity_shift
    from src.preprocessing.segmenter import MODE_COLUMN

    def constant(frame: pd.DataFrame) -> dict[str, list[bool]]:
        return {"constant": [False] * len(frame)}

    report = single_quantity_shift(
        constant, _shift_frame(), {"trim_b1": ["obd:C03"]}, mode_column=MODE_COLUMN
    )["constant"]
    assert report.verdict == "invalid"

    def withholding(frame: pd.DataFrame) -> dict[str, list[None]]:
        return {"blind": [None] * len(frame)}

    assert (
        single_quantity_shift(
            withholding, _shift_frame(), {"trim_b1": ["obd:C03"]}, mode_column=MODE_COLUMN
        )["blind"].verdict
        == "withheld"
    )


def test_the_shift_is_graded_only_where_the_quantity_was_observed() -> None:
    """A trim polled every few seconds: rows between samples carry a
    forward-filled value (``__gap`` set). A detector reading only arrived
    samples cannot see a fault there, and is not graded there."""
    from src.models.evaluation import single_quantity_shift
    from src.preprocessing.segmenter import MODE_COLUMN

    frame = _shift_frame(n=200, channels=4)
    fresh = np.arange(len(frame)) % 4 == 0
    frame["obd:C01__gap"] = ~fresh
    base = frame.copy()

    def observed_only(f: pd.DataFrame) -> dict[str, list[bool]]:
        z = (f["obd:C01"] - base["obd:C01"].mean()) / base["obd:C01"].std()
        return {"observed_only": list((z.abs() > 6.0) & ~f["obd:C01__gap"])}

    report = single_quantity_shift(
        observed_only, frame, {"trim": ["obd:C01"]}, mode_column=MODE_COLUMN, sigmas=(10.0,)
    )["observed_only"]
    (cell,) = report.outcomes
    assert cell.rows == int(fresh.sum())
    assert cell.flag_rate == 1.0
