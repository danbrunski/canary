"""Unit tests for the small training helpers in ``src.models.sequence_training``
that don't fit the trainer's larger end-to-end fixtures."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.columns import SESSION_COLUMN, TIMESTAMP_COLUMN
from src.models.sequence_dataset import SequenceWindowDataset
from src.models.sequence_training import (
    dataloader_kwargs_for_device,
    default_training_device,
    error_distribution,
    split_dataset,
    validate_dataset_observations,
)


def _session_block(session: str, rows: int, start: str) -> pd.DataFrame:
    rng = np.random.default_rng(hash(session) % 2**32)
    return pd.DataFrame(
        {
            SESSION_COLUMN: [session] * rows,
            TIMESTAMP_COLUMN: pd.date_range(start, periods=rows, freq="1s", tz="UTC"),
            "rpm": rng.normal(800.0, 30.0, rows),
            "speed": rng.normal(0.0, 1.0, rows),
        }
    )


def _two_session_frame() -> pd.DataFrame:
    return pd.concat(
        [
            _session_block("A", 30, "2026-05-11T00:00:00Z"),
            _session_block("B", 30, "2026-05-11T01:00:00Z"),
            _session_block("C", 30, "2026-05-11T02:00:00Z"),
        ],
        ignore_index=True,
    )


def _single_session_frame(rows: int) -> pd.DataFrame:
    return _session_block("only", rows, "2026-05-11T00:00:00Z")


def test_dataloader_kwargs_for_device_cuda_opts_in_pin_memory_and_workers() -> None:
    """CUDA training overlaps host→device copies with kernel time; that needs
    pinned host memory and at least one prefetch worker, so the helper opts in."""
    kwargs = dataloader_kwargs_for_device("cuda")
    assert kwargs["pin_memory"] is True
    assert kwargs["num_workers"] in {0, 1}


def test_dataloader_workers_can_be_forced_off_for_memory_constrained_wsl() -> None:
    assert dataloader_kwargs_for_device("cuda", num_workers=0) == {
        "num_workers": 0,
        "pin_memory": True,
    }
    # ``cuda:0`` (explicit index) is still CUDA and honors an explicit override.
    assert dataloader_kwargs_for_device("cuda:0", num_workers=0) == {
        "num_workers": 0,
        "pin_memory": True,
    }


def test_dataloader_kwargs_for_device_cpu_stays_conservative() -> None:
    """CPU has no copy to overlap; the extra worker processes only add IPC
    noise. Stay on the conservative Pi-friendly defaults."""
    assert dataloader_kwargs_for_device("cpu") == {"num_workers": 0, "pin_memory": False}
    # Unknown device strings fall back to CPU defaults (safer than guessing).
    assert dataloader_kwargs_for_device("mps") == {"num_workers": 0, "pin_memory": False}


def test_default_training_device_returns_cpu_or_cuda() -> None:
    """The helper just asks torch; result depends on the test host's GPU. The
    invariant is that it returns one of the two strings, never raises, and
    never silently picks something unknown."""
    assert default_training_device() in {"cpu", "cuda"}


def test_sequence_calibration_rejects_effectively_empty_windows() -> None:
    frame = _single_session_frame(rows=12)
    frame["rpm__gap"] = True
    frame["speed__gap"] = True
    dataset = SequenceWindowDataset(frame, sequence_length=4, feature_columns=("rpm", "speed"))
    with pytest.raises(ValueError, match="coverage is unusable"):
        validate_dataset_observations(
            dataset,
            minimum_window_fraction=0.01,
            minimum_usable_fraction=0.9,
            context="calibration",
        )


def test_split_dataset_holds_out_whole_sessions() -> None:
    """Multi-session corpora split at session granularity — no validation
    window shares rows with any train window."""
    torch = pytest.importorskip("torch")
    del torch
    frame = _two_session_frame()
    dataset = SequenceWindowDataset(frame, sequence_length=4, feature_columns=("rpm", "speed"))
    train, validation = split_dataset(dataset, validation_fraction=0.3, random_state=7)
    assert validation is not None
    train_sessions = {dataset.windows[i].session for i in train.indices}
    val_sessions = {dataset.windows[i].session for i in validation.indices}
    assert train_sessions and val_sessions
    assert train_sessions.isdisjoint(val_sessions)


def test_split_dataset_single_session_uses_gapped_tail_block() -> None:
    """A single-session corpus falls back to a contiguous tail block with a
    sequence_length gap, so train and validation share no rows."""
    torch = pytest.importorskip("torch")
    del torch
    frame = _single_session_frame(rows=60)
    dataset = SequenceWindowDataset(frame, sequence_length=4, feature_columns=("rpm", "speed"))
    train, validation = split_dataset(dataset, validation_fraction=0.2, random_state=7)
    assert validation is not None
    last_train_end = max(dataset.windows[i].end_row for i in train.indices)
    first_val_start = min(dataset.windows[i].start_row for i in validation.indices)
    assert first_val_start >= last_train_end  # no shared rows


def test_error_distribution_quantile_threshold() -> None:
    values = np.linspace(0.0, 1.0, 1001)
    dist = error_distribution(values, threshold_k=3.0, threshold_quantile=0.99)
    assert dist.threshold == pytest.approx(0.99, abs=1e-6)
    sigma = error_distribution(values, threshold_k=3.0)
    assert sigma.threshold == pytest.approx(
        float(values.mean()) + 3.0 * float(values.std()), abs=1e-9
    )
    with pytest.raises(ValueError, match="threshold_quantile"):
        error_distribution(values, threshold_k=3.0, threshold_quantile=1.5)


# ---------------------------------------------------------------------------
# 2026-09-20: a one-session calibration must say it is one session
# ---------------------------------------------------------------------------


def test_calibration_holdout_report_flags_fewer_than_three_sessions(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    from src.models.sequence_autoencoder_training import _calibration_holdout_report
    from src.models.sequence_registry import LSTM_TRAINING_BACKEND, MIN_CALIBRATION_SESSIONS

    with caplog.at_level(logging.WARNING, logger="src.models.sequence_autoencoder_training"):
        report = _calibration_holdout_report(
            LSTM_TRAINING_BACKEND,
            used=("2026-09-18T134345",),
            requested=["2026-09-18T134345", "2026-09-18T213818", "2026-08-06T162657"],
        )
    assert MIN_CALIBRATION_SESSIONS == 3
    assert report == {
        "sessions_requested": ["2026-09-18T134345", "2026-09-18T213818", "2026-08-06T162657"],
        "sessions_used": ["2026-09-18T134345"],
        # Asked for and silently absent is its own kind of empty.
        "sessions_without_windows": ["2026-08-06T162657", "2026-09-18T213818"],
        "recommended_minimum_sessions": 3,
        "below_recommended_minimum": True,
    }
    assert any("1 held-out session" in record.getMessage() for record in caplog.records)


def test_calibration_holdout_report_is_quiet_for_a_full_pooled_set(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    from src.models.sequence_autoencoder_training import _calibration_holdout_report
    from src.models.sequence_registry import LSTM_TRAINING_BACKEND

    with caplog.at_level(logging.WARNING, logger="src.models.sequence_autoencoder_training"):
        report = _calibration_holdout_report(
            LSTM_TRAINING_BACKEND, used=("a", "b", "c"), requested=None
        )
    assert report["below_recommended_minimum"] is False
    assert report["sessions_without_windows"] == []
    assert not caplog.records


def test_sequence_manifest_round_trips_calibration_holdout_and_tolerates_its_absence() -> None:
    from src.models.sequence_registry import SequenceAutoencoderManifest

    payload = {
        "artifact_format_version": 1,
        "model_type": "lstm_autoencoder",
        "model_version": "v",
        "created_at": "2026-09-20T00:00:00Z",
        "training_data_hash": "sha256:x",
        "training_sessions": ["s"],
        "input_windows": 1,
        "train_windows": 1,
        "validation_windows": 0,
        "best_epoch": 1,
        "best_train_loss": 0.1,
        "stopped_epoch": 1,
        "early_stopped": False,
        "architecture": {},
        "hyperparams": {},
        "schema": {},
        "history": [],
    }
    assert SequenceAutoencoderManifest.from_dict(payload).calibration_holdout is None
    holdout = {"sessions_used": ["a"], "below_recommended_minimum": True}
    manifest = SequenceAutoencoderManifest.from_dict({**payload, "calibration_holdout": holdout})
    assert manifest.to_dict()["calibration_holdout"] == holdout


# ---------------------------------------------------------------------------
# A03.5 — validation sessions by a stated rule
# ---------------------------------------------------------------------------


def _mode_session(session: str, start: str, modes: list[tuple[str, int]]) -> pd.DataFrame:
    rows = sum(n for _mode, n in modes)
    frame = _session_block(session, rows, start)
    frame["operating_mode"] = [mode for mode, n in modes for _ in range(n)]
    return frame


def _mode_corpus() -> pd.DataFrame:
    """Two tiny idle-only drives (what the old smallest-first split always
    picked), and bigger drives carrying city and highway. ``C``/``C2`` are one
    physical drive (restart one minute apart)."""
    return pd.concat(
        [
            _mode_session("A", "2026-05-11T00:00:00Z", [("idle", 44)]),
            _mode_session("B", "2026-05-11T01:00:00Z", [("idle", 48)]),
            _mode_session("C", "2026-05-11T02:00:00Z", [("city", 120), ("idle", 40)]),
            _mode_session("C2", "2026-05-11T02:03:40Z", [("city", 60)]),
            _mode_session("D", "2026-05-11T03:00:00Z", [("highway", 160), ("city", 80)]),
            _mode_session("E", "2026-05-11T04:00:00Z", [("highway", 200), ("city", 120)]),
            _mode_session("F", "2026-05-11T05:00:00Z", [("idle", 120), ("highway", 60)]),
        ],
        ignore_index=True,
    )


def test_validation_spans_the_scored_modes_by_whole_drive() -> None:
    from src.models.sequence_training import (
        MIN_CALIBRATION_SAMPLES,
        VALIDATION_RULE,
        choose_validation_sessions,
    )

    frame = _mode_corpus()
    choice = choose_validation_sessions(
        frame, sequence_length=4, validation_fraction=0.25, random_state=7
    )
    record = choice.record
    assert record["rule"] == VALIDATION_RULE
    assert set(record["modes_scored"]) == {"idle", "city", "highway"}
    assert set(record["modes_covered"]) == {"idle", "city", "highway"}
    # Not the two smallest sessions (the old stable-sort outcome).
    assert set(choice.sessions) != {"A", "B"}
    # A drive is held out whole: C and C2 go together or not at all.
    assert ("C" in choice.sessions) == ("C2" in choice.sessions)
    # Every scored mode is still trainable.
    train = frame[~frame["session"].isin(choice.sessions)]
    for mode in record["modes_scored"]:
        assert (train["operating_mode"] == mode).sum() // 4 >= MIN_CALIBRATION_SAMPLES
    # Deterministic for a seed, and the record names what it held.
    again = choose_validation_sessions(
        frame, sequence_length=4, validation_fraction=0.25, random_state=7
    )
    assert again.sessions == choice.sessions
    assert record["sessions"] == list(choice.sessions)
    assert 0 < record["windows"] <= 2 * record["target_windows"] + 1


def test_one_drive_falls_back_to_a_tail_block_and_says_so() -> None:
    torch = pytest.importorskip("torch")
    del torch
    from src.models.sequence_training import validation_split

    dataset = SequenceWindowDataset(
        _single_session_frame(rows=60), sequence_length=4, feature_columns=("rpm", "speed")
    )
    split = validation_split(dataset, validation_fraction=0.2, random_state=7)
    assert split.record is not None
    assert split.record["fallback"] == "tail_block"
    assert min(split.validation_indexes) > max(split.train_indexes)


# ---------------------------------------------------------------------------
# A03.8 — training envelope per mode
# ---------------------------------------------------------------------------


def _envelope_frame(session: str, baro: list[float], mode: str = "city") -> pd.DataFrame:
    frame = _session_block(session, len(baro), "2026-05-11T00:00:00Z")
    frame["operating_mode"] = mode
    frame["obd:BAROMETRIC_PRESSURE"] = baro
    frame["obd:SPEED"] = 40.0
    return frame


def test_envelope_records_hours_and_ranges_per_mode_and_merges_exactly() -> None:
    from src.models.sequence_training import merge_envelopes, session_envelopes

    frame = pd.concat(
        [
            _envelope_frame("home", [80.0] * 30 + [float("nan")] * 6),
            _envelope_frame("trip", [63.5] * 12 + [72.0] * 24),
            _envelope_frame("idle", [80.0] * 36, mode="idle"),
        ],
        ignore_index=True,
    )
    per_session = session_envelopes(frame, interval_seconds=1.0)
    both = merge_envelopes(per_session, ["home", "trip"])
    city = both["modes"]["city"]
    assert city["rows"] == 72
    assert city["hours"] == pytest.approx(72 / 3600, abs=1e-4)
    baro = city["quantities"]["baro_kpa"]
    assert (baro["min"], baro["max"], baro["rows"]) == (63.5, 80.0, 66)
    assert baro["histogram"] == {"63": 12, "72": 24, "80": 30}
    assert city["unmeasured"] == ["iat_c"]
    assert set(both["modes"]) == {"city"}  # "idle" session not requested
    assert merge_envelopes(per_session)["modes"]["idle"]["rows"] == 36


def test_a_calibration_that_misses_the_training_envelope_is_a_violation() -> None:
    """The 2026-09-20 shape: calibrated at 71–73 kPa, trained mostly at 80."""
    from src.models.sequence_training import (
        envelope_coverage,
        envelope_coverage_violations,
        merge_envelopes,
        session_envelopes,
    )

    training = merge_envelopes(
        session_envelopes(_envelope_frame("t", [80.0] * 90 + [72.0] * 10), interval_seconds=1.0)
    )
    thin = merge_envelopes(
        session_envelopes(_envelope_frame("c", [71.0, 72.0, 73.0] * 10), interval_seconds=1.0)
    )
    wide = merge_envelopes(
        session_envelopes(_envelope_frame("c", [72.0] * 5 + [80.0] * 25), interval_seconds=1.0)
    )
    assert envelope_coverage(training, thin, mode="city", quantity="baro_kpa") == pytest.approx(0.1)
    violations = envelope_coverage_violations(training, thin, modes=["city"])
    assert len(violations) == 1
    assert "baro_kpa spans 71–73, covering 10%" in violations[0]
    assert envelope_coverage_violations(training, wide, modes=["city"]) == []
    # A mode the calibration never visited is the mode check's to report.
    assert envelope_coverage_violations(training, wide, modes=["highway"]) == []
