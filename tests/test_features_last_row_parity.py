"""P02.9: the live tick rolls only its newest row, and that row must be the one
``compute_features`` would have produced over the same frame.

``compute_features_last_row`` replaces "roll the whole ring buffer, keep one
row". Every passthrough column and every ``__roc`` runs the batch code and is
asserted bit-identical. ``__mean``/``__std`` come from a second (numpy) kernel
held to the declared ``LAST_ROW_RTOL`` contract (``last_row_mismatches``:
pandas' sliding kernel carries rounding from the frame's first row, so the
batch value itself moves as the buffer slides). The cases
are the ones the item names: NaN and gaps, window endpoints, session restart
(several sessions in one frame), replay/reset, and every feature family.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.columns import ROLLING_SUFFIXES, mean_column, roc_column, std_column
from src.config import CanaryConfig
from src.features.feature_pipeline import featurize_wide_frame
from src.features.live import LiveFeatureSource, PreparedLiveTick, build_prepared_live_tick
from src.preprocessing.features import (
    FeatureConfig,
    compute_features,
    compute_features_last_row,
    last_row_mismatches,
)
from src.telemetry import TelemetryRecord
from tests.configs import example_config

T0 = pd.Timestamp("2026-10-01T12:00:00", tz="UTC")
#: A channel the drive slice must carry for the real-data check to mean much.
ROLLING_INPUT_PROBE = "obd:RPM"


def assert_row_parity(
    frame: pd.DataFrame,
    full: pd.DataFrame,
    row: pd.DataFrame,
) -> None:
    """``row`` equals ``full``'s last row (the batch over ``frame``) under the
    declared contract; every column but mean/std bit for bit."""
    expected = full.iloc[[-1]].reset_index(drop=True)
    assert last_row_mismatches(frame, expected, row) == []
    rolled = [c for c in expected.columns if str(c).endswith(("__mean", "__std"))]
    exact = [c for c in expected.columns if c not in rolled]
    pd.testing.assert_frame_equal(row[exact], expected[exact], check_exact=True)


def assert_last_row_parity(frame: pd.DataFrame, **kwargs: Any) -> None:
    assert_row_parity(
        frame,
        compute_features(frame, **kwargs),
        compute_features_last_row(frame, **kwargs),
    )


def _frame(
    seconds: np.ndarray, *, session: object = "s", rng: np.random.Generator | None = None
) -> pd.DataFrame:
    rng = np.random.default_rng(0) if rng is None else rng
    n = len(seconds)
    frame = pd.DataFrame(
        {
            "timestamp": T0 + pd.to_timedelta(seconds, unit="s"),
            "session": session,
            "operating_mode": "cruise",
            "obd:RPM": rng.normal(2000, 300, n),
            "obd:RPM__gap": rng.random(n) < 0.1,
            # Mostly missing: NaN rows inside and at the window's edges.
            "obd:STFT": np.where(rng.random(n) < 0.4, np.nan, rng.normal(-2, 1.5, n)),
            "obd:COOLANT": np.full(n, 88.0),
            "did:COUNT": rng.integers(0, 40, n),
            "derived:NARROW": rng.normal(0, 1, n).astype(np.float32),
            # A level far above its spread: the ill-conditioned std case.
            "derived:BIG": 1e5 + rng.normal(0, 1e-4, n),
            "derived:WITH_INF": np.where(rng.random(n) < 0.1, np.inf, rng.normal(5, 1, n)),
            "deviation:model": rng.normal(0, 0.2, n),
            "context:start": 1.0,
        }
    )
    return frame


@pytest.mark.parametrize("seed", range(40))
def test_random_frames_match_the_batch_last_row(seed: int) -> None:
    rng = np.random.default_rng(seed)
    n = int(rng.integers(1, 220))
    # Irregular spacing: the 1 s grid with dropped fully-stale stretches.
    steps = rng.choice([1.0, 1.0, 1.0, 1.0, 7.0, 45.0], n)
    frame = _frame(np.cumsum(steps), rng=rng)
    cfg = FeatureConfig(window_seconds=int(rng.choice([5, 10, 30])))
    assert_last_row_parity(frame, config=cfg)


@pytest.mark.parametrize("offset", [-1e-9, 0.0, 1e-9, 1e-3])
def test_window_endpoint_is_right_closed(offset: float) -> None:
    """A row exactly ``window`` before the newest is out; just inside is in."""
    seconds = np.array([0.0, 30.0 + offset, 31.0, 60.0 + offset])
    frame = _frame(seconds)
    cfg = FeatureConfig(window_seconds=30)
    assert_last_row_parity(frame, config=cfg)


def test_session_restart_rolls_only_the_last_session() -> None:
    """Several sessions in one frame: the batch emits the sorted-last session's
    newest row, and its window never reaches into another session's rows."""
    rng = np.random.default_rng(3)
    parts = [
        _frame(np.arange(50.0) + 200.0, session="b", rng=rng),
        _frame(np.arange(40.0), session="c", rng=rng),
        _frame(np.arange(60.0) + 100.0, session="a", rng=rng),
    ]
    frame = pd.concat(parts, ignore_index=True)
    tail = compute_features_last_row(frame)
    assert tail["session"].iloc[0] == "c"
    assert_row_parity(frame, compute_features(frame), tail)


def test_a_missing_session_label_sorts_last_like_the_batch() -> None:
    rng = np.random.default_rng(4)
    frame = pd.concat(
        [_frame(np.arange(30.0), session=None, rng=rng), _frame(np.arange(30.0), rng=rng)],
        ignore_index=True,
    )
    tail = compute_features_last_row(frame)
    assert pd.isna(tail["session"].iloc[0])
    assert_row_parity(frame, compute_features(frame), tail)


def test_unsorted_rows_roll_in_time_order() -> None:
    frame = _frame(np.arange(80.0)).sample(frac=1.0, random_state=5).reset_index(drop=True)
    assert_last_row_parity(frame)


def test_constant_and_single_sample_windows_follow_pandas() -> None:
    """A constant window's mean is its value and its std exactly 0 (no float
    residue); a single sample's std is NaN; an all-NaN channel stays NaN."""
    frame = pd.DataFrame(
        {
            "timestamp": T0 + pd.to_timedelta(np.arange(5.0), unit="s"),
            "session": "s",
            "a": [0.1] * 5,
            "b": [np.nan, np.nan, np.nan, np.nan, 3.0],
            "c": np.nan,
            "d": [-0.0, 0.0, -0.0, 0.0, -0.0],
        }
    )
    tail = compute_features_last_row(frame)
    assert tail[mean_column("a")].iloc[0] == np.float32(0.1)
    assert tail[std_column("a")].iloc[0] == 0.0
    assert np.isnan(tail[std_column("b")].iloc[0])
    assert np.isnan(tail[mean_column("c")].iloc[0])
    assert np.signbit(tail[mean_column("d")].iloc[0])
    expected = compute_features(frame).iloc[[-1]].reset_index(drop=True)
    pd.testing.assert_frame_equal(tail, expected, check_exact=True)


@pytest.mark.parametrize(
    "cfg",
    [
        FeatureConfig(include_mean=False),
        FeatureConfig(include_std=False),
        FeatureConfig(include_roc=False),
        FeatureConfig(include_mean=False, include_std=False, include_roc=False),
        FeatureConfig(min_periods=5),
    ],
)
def test_every_feature_family_switch(cfg: FeatureConfig) -> None:
    frame = _frame(np.arange(45.0))
    assert_last_row_parity(frame, config=cfg)


def test_value_column_override_and_refeaturized_input() -> None:
    frame = _frame(np.arange(40.0))
    picked = ["obd:RPM", "did:COUNT"]
    assert_last_row_parity(frame, value_columns=picked)
    # Input that already carries feature columns has them replaced, not doubled.
    once = compute_features(frame, value_columns=picked)
    assert_last_row_parity(once, value_columns=picked)


def test_empty_and_one_row_frames() -> None:
    frame = _frame(np.arange(1.0))
    assert_last_row_parity(frame)
    empty = frame.iloc[0:0]
    pd.testing.assert_frame_equal(compute_features_last_row(empty), compute_features(empty))


def test_the_batch_std_residue_depends_on_where_the_frame_starts() -> None:
    """Why mean/std carry a tolerance: a constant 30 s window after a varying
    history has a batch std that is not 0 and changes when the frame starts
    later (measured on a real GPS heading, 2026-10-04T215234). The last-row
    value is the exact 0, and the contract accepts the residue."""
    # A heading held in 23 s runs, then 30 s parked; seed 13 reproduces it.
    runs = np.repeat(np.round(np.random.default_rng(13).uniform(0, 360, 7), 4), 23)[:150]
    values = np.concatenate([runs, np.full(30, 129.7138)])
    frame = pd.DataFrame(
        {
            "timestamp": T0 + pd.to_timedelta(np.arange(len(values), dtype=float), unit="s"),
            "session": "s",
            "gps:heading": values,
        }
    )
    batch = compute_features(frame)[std_column("gps:heading")].iloc[-1]
    later = compute_features(frame.iloc[100:])[std_column("gps:heading")].iloc[-1]
    tail = compute_features_last_row(frame)[std_column("gps:heading")].iloc[0]
    assert tail == 0.0
    assert later == 0.0
    assert batch != 0.0
    assert_last_row_parity(frame)


def test_the_contract_refuses_what_it_should() -> None:
    """The comparison can fail: one ulp on a ``__roc`` or a passthrough value,
    a mean off by more than the tolerance, a NaN against a number, a dtype."""
    frame = _frame(np.arange(60.0))
    expected = compute_features(frame).iloc[[-1]].reset_index(drop=True)
    assert last_row_mismatches(frame, expected, compute_features_last_row(frame)) == []

    def bumped(column: str, value: object) -> pd.DataFrame:
        out = expected.copy()
        out[column] = pd.Series([value], dtype=expected[column].dtype)
        return out

    roc = roc_column("obd:RPM")
    one_ulp = np.nextafter(expected[roc].iloc[0], np.float32(np.inf))
    assert last_row_mismatches(frame, expected, bumped(roc, one_ulp)) == [roc]
    raw = "obd:RPM"
    assert last_row_mismatches(
        frame, expected, bumped(raw, np.nextafter(expected[raw].iloc[0], np.float32(np.inf)))
    ) == [raw]
    mean = mean_column(raw)
    assert last_row_mismatches(frame, expected, bumped(mean, expected[mean].iloc[0] + 1.0)) == [
        mean
    ]
    assert last_row_mismatches(frame, expected, bumped(mean, np.nan)) == [mean]
    assert last_row_mismatches(frame, expected, expected.astype({mean: np.float64})) == ["<schema>"]


def _history_then_window(level: float) -> tuple[np.ndarray, pd.DataFrame]:
    """A channel held at ``level`` for 60 s, then 40 s near 1 (std ~0.07)."""
    values = np.concatenate([np.full(60, level), 1.0 + 0.1 * np.sin(np.arange(40.0))])
    frame = pd.DataFrame(
        {
            "timestamp": T0 + pd.to_timedelta(np.arange(len(values), dtype=float), unit="s"),
            "session": "s",
            "x": values,
        }
    )
    return values, frame


def test_the_tolerance_follows_the_history_the_batch_slid_over() -> None:
    """Why the bounds scale with the frame's peak, not the window's: after a
    1e5 history, a window near 1 has a batch std off the exact value by 1.2e-4
    of the window's own scale (Welford residue of the history). The last-row
    value is the exact one, and the contract accepts both; the mean bound
    still refuses a 1 % mean error the old shared ``2**-23 x peak`` (1.2e-2)
    let through."""
    values, frame = _history_then_window(1e5)
    std, mean = std_column("x"), mean_column("x")
    exact = np.std(values[-30:], ddof=1)
    expected = compute_features(frame).iloc[[-1]].reset_index(drop=True)
    actual = compute_features_last_row(frame)
    assert actual[std].iloc[0] == np.float32(exact)
    batch_error = abs(float(expected[std].iloc[0]) - exact)
    assert batch_error > 2.0**-23 * (abs(exact) + 1.1)  # a window-relative bound fails
    # The true batch residue (8.6e-6) is accepted.
    assert last_row_mismatches(frame, expected, actual) == []
    off = actual.copy()
    off[std] = np.float32(exact + 0.1)
    assert last_row_mismatches(frame, expected, off) == [std]
    # ddof is unresolvable at this offset: a ddof=0 std differs by 1.2e-3, a
    # variance difference of 78 eps64 peak**2, while the batch reference's own
    # variance residue reaches 61 eps64 peak**2 on real data. The bound (128,
    # here 2.0e-3 in std) must admit the latter, so it admits ddof=0 too; the
    # refusal is asserted on a 1e3 history below.
    # A 1 % mean error (1.0e-2) is refused.
    wrong_mean = actual.copy()
    wrong_mean[mean] = np.float32(float(expected[mean].iloc[0]) * 1.01)
    assert abs(float(wrong_mean[mean].iloc[0]) - float(expected[mean].iloc[0])) < 2.0**-23 * 1e5
    assert last_row_mismatches(frame, expected, wrong_mean) == [mean]


def test_a_ddof0_std_is_refused_where_the_history_resolves_it() -> None:
    """With a 1e3 history the same ddof=0 error is 7.8e5 eps64 peak**2 of
    variance, far past the 128 the batch's residue may reach."""
    values, frame = _history_then_window(1e3)
    std = std_column("x")
    expected = compute_features(frame).iloc[[-1]].reset_index(drop=True)
    actual = compute_features_last_row(frame)
    assert last_row_mismatches(frame, expected, actual) == []
    biased = actual.copy()
    biased[std] = np.float32(np.std(values[-30:], ddof=0))
    assert last_row_mismatches(frame, expected, biased) == [std]


def test_the_real_stft_residue_passes() -> None:
    """Regression, 2026-10-04T194342: ``derived:STFT_BANK_BALANCE__std`` over a
    window of two equal values (-0.78125) among 28 NaN, channel peak 1.71875.
    The batch kernel gave 1.9912017e-07 (a variance residue of 61 eps64
    peak**2); the exact, and last-row, value is 0. The split-bound draft
    refused it; the variance bound must accept it."""
    values = np.full(60, np.nan)
    values[:20] = np.linspace(-1.71875, 1.0, 20)
    values[-30] = -0.78125
    values[-1] = -0.78125
    frame = pd.DataFrame(
        {
            "timestamp": T0 + pd.to_timedelta(np.arange(60.0), unit="s"),
            "session": "s",
            "derived:STFT_BANK_BALANCE": values,
        }
    )
    std = std_column("derived:STFT_BANK_BALANCE")
    actual = compute_features_last_row(frame)
    assert actual[std].iloc[0] == 0.0
    expected = actual.copy()
    expected[std] = np.float32(1.9912017e-07)  # the batch value measured on the drive
    assert last_row_mismatches(frame, expected, actual) == []
    # ... and the bound is not slack there: four times that residue is refused.
    expected[std] = np.float32(4 * 1.9912017e-07)
    assert last_row_mismatches(frame, expected, actual) == [std]


def test_a_repeated_label_is_not_a_channel() -> None:
    """Both forms skip a column label that appears twice, as the batch always
    did (it would otherwise shape-mismatch rolling a two-column 'channel')."""
    frame = _frame(np.arange(40.0))
    frame.insert(3, "obd:DUP", 1.0, allow_duplicates=True)
    frame.insert(4, "obd:DUP", 2.0, allow_duplicates=True)
    full = compute_features(frame)
    assert not any(str(c).startswith("obd:DUP__") for c in full.columns)
    assert_last_row_parity(frame)


def test_roc_is_bit_identical_across_dtypes_and_gaps() -> None:
    """``__roc`` is the batch's own arithmetic: a row-count shift, the column's
    dtype for the subtraction, the elapsed-seconds divide."""
    rng = np.random.default_rng(9)
    for _ in range(10):
        steps = rng.choice([1.0, 1.0, 2.0, 600.0], int(rng.integers(2, 150)))
        frame = _frame(np.cumsum(steps), rng=rng)
        full = compute_features(frame).iloc[[-1]].reset_index(drop=True)
        tail = compute_features_last_row(frame)
        rocs = [c for c in full.columns if str(c).endswith("__roc")]
        pd.testing.assert_frame_equal(tail[rocs], full[rocs], check_exact=True)


# ---------------------------------------------------------------------------
# Real data on every push: a committed drive slice, tick by tick
# ---------------------------------------------------------------------------

DRIVE_SLICE = Path(__file__).parent / "fixtures" / "ground_truth" / "drive-slice"


@pytest.fixture(scope="module")
def drive_slice_frame() -> pd.DataFrame:
    """The committed 7-minute drive slice through the real chain (decode,
    resample, derived channels): the frame the live tick rolls."""
    from src.decode.bindings import load_bindings
    from src.features.feature_pipeline import (
        _decode_session_to_resampled,
        _without_committee_columns,
    )
    from src.preprocessing.resampler import resample_config_for

    config = example_config(Path("canary.example.yaml"))
    resampled = _decode_session_to_resampled(
        DRIVE_SLICE,
        load_bindings(Path("data/bindings.yaml")),
        None,
        None,
        resample_config_for(config),
    )
    framed = featurize_wide_frame(resampled, config, behavior_models=None, did_map=None)
    return _without_committee_columns(framed.operating_frame)


@pytest.mark.parametrize("ticks", [range(0, 12), range(200, 230)], ids=["warmup", "driving"])
def test_every_tick_of_a_real_drive_slice(drive_slice_frame: pd.DataFrame, ticks: range) -> None:
    """Each tick's last row, from the frame as it stood at that tick, against
    the batch row for that tick: catches either kernel drifting on real data
    (fast tier, so on every push)."""
    frame = drive_slice_frame
    assert len(frame) > max(ticks) and frame[ROLLING_INPUT_PROBE].notna().any()
    full = compute_features(frame)
    for tick in ticks:
        so_far = frame.iloc[: tick + 1]
        row = compute_features_last_row(so_far)
        expected = full.iloc[[tick]].reset_index(drop=True)
        assert last_row_mismatches(so_far, expected, row) == [], f"tick {tick}"


# ---------------------------------------------------------------------------
# The live tick: one implementation, and parity through reset/replay
# ---------------------------------------------------------------------------


@pytest.fixture
def config() -> CanaryConfig:
    """The example config with a short segmenter lookback, so the ring
    buffer (``max(2 x window, lookback) x hz``) fills and evicts in 60 ticks."""
    config = example_config(Path("canary.example.yaml"))
    preprocessing = config.preprocessing.model_copy(update={"idle_transient_lookback_seconds": 40})
    return config.model_copy(update={"preprocessing": preprocessing})


def _drive(
    src: LiveFeatureSource, start: datetime, ticks: int, *, seed: int
) -> list[tuple[PreparedLiveTick, pd.DataFrame | None]]:
    """Observe a jittery drive and tick 1 Hz; per tick ``(prepared, row)``."""
    rng = np.random.default_rng(seed)
    out: list[tuple[PreparedLiveTick, pd.DataFrame | None]] = []
    for i in range(ticks):
        now = start + timedelta(seconds=i)
        if i % 17 != 5:  # a tick with no fresh RPM now and then
            src.observe(TelemetryRecord.obd(now.timestamp(), "RPM", 900 + 40 * i, "rpm"))
        src.observe(TelemetryRecord.obd(now.timestamp(), "SPEED", float(i % 60), "km/h"))
        src.observe(
            TelemetryRecord.obd(now.timestamp(), "THROTTLE_POS", float(rng.uniform(5, 40)), "%")
        )
        src.observe(TelemetryRecord.obd(now.timestamp(), "COOLANT_TEMP", 85.0, "C"))
        prepared = src.prepare_tick(now)
        if prepared is None:
            continue
        out.append((prepared, src.apply_built_tick(prepared, build_prepared_live_tick(prepared))))
    return out


# ~8 s: every tick runs the whole live transform (twice, for the batch side).
@pytest.mark.slow
def test_live_tick_row_matches_featurizing_its_whole_buffer(config: CanaryConfig) -> None:
    """Every live tick's row equals the last row of the batch transform over
    the same buffer — through warmup, a full buffer and its eviction."""
    src = LiveFeatureSource(config=config)
    ticks = _drive(src, datetime(2026, 10, 1, 12, 0, tzinfo=UTC), 80, seed=1)
    assert len(ticks) > 70
    assert src.buffer_size < 80  # evicting
    # Warmup (first rows), a full buffer, and every tick after eviction began.
    for prepared, row in ticks[:3] + ticks[55:]:
        batch = featurize_wide_frame(
            prepared.wide,
            prepared.config,
            behavior_models=prepared.behavior_models,
            did_map=prepared.did_map,
            feature_config=prepared.feature_config,
            flash_history=prepared.flash_history,
        )
        full, prepared_frame = batch.features, batch.operating_frame
        assert row is not None
        assert_row_parity(prepared_frame, full, row)
        assert any(str(c).endswith(ROLLING_SUFFIXES) for c in row.columns)


# ~8 s: every tick runs the whole live transform (twice, for the batch side).
@pytest.mark.slow
def test_reset_and_replay_start_from_a_clean_window(config: CanaryConfig) -> None:
    """Nothing rolled carries across :meth:`reset` (replay entry/exit, session
    restart): a reset source's rows equal a fresh source's, bit for bit."""
    start = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    used = LiveFeatureSource(config=config)
    _drive(used, start, 40, seed=2)
    used.reset()
    later = start + timedelta(hours=1)
    fresh = LiveFeatureSource(config=config)
    after_reset = _drive(used, later, 35, seed=3)
    from_fresh = _drive(fresh, later, 35, seed=3)
    assert len(after_reset) == len(from_fresh) > 0
    for (_, a), (_, b) in zip(after_reset, from_fresh, strict=True):
        assert a is not None and b is not None
        pd.testing.assert_frame_equal(a, b, check_exact=True)
    # The window refilled: the last rows roll a full 30 s again.
    assert after_reset[-1][1] is not None
    assert after_reset[-1][1][roc_column("obd:RPM")].notna().all()
