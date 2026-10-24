"""A06.2 — the sequence detector as a non-alerting novelty screen.

The operator decided (2026-09-23) that the sequence model is evidence, not an
alarm: its useful answer is "this window is outside what the model trained
on, and where". On one vehicle "unusual" mostly meant exactly that — the
2026-09-18 trip to 63-73 kPa fired on baro every time — so each sequence event
now says whether its window is outside the artifact's recorded training
envelope (A03.8) and on which quantity, and ``/status`` keeps a running
coverage meter. An artifact that recorded no envelope says "not recorded",
never "inside".
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from src.alerts.events import AnomalyEvent, sequence_envelope_summary
from src.columns import SESSION_COLUMN, TIMESTAMP_COLUMN
from src.daemon.inference import LiveInferencePipeline, TickContext, _TickResult
from src.daemon.inference_metrics import InferenceTimingRecorder
from src.daemon.inference_scheduling import GenerationToken
from src.models.inference import (
    ENVELOPE_INSIDE,
    ENVELOPE_MODE_NOT_TRAINED,
    ENVELOPE_NOT_COMPARED,
    ENVELOPE_OUTSIDE,
    SequenceInferenceResult,
    SequenceWindowBuffer,
    TrainingEnvelope,
)

BARO = "obd:BAROMETRIC_PRESSURE"
IAT = "obd:INTAKE_TEMP"
SPEED = "obd:SPEED"


def _quantity(channel: str, low: float, high: float, rows: int = 100) -> dict[str, Any]:
    return {"channel": channel, "rows": rows, "min": low, "max": high, "bin_width": 1.0}


def _envelope_record() -> dict[str, Any]:
    """The shape ``sequence_training.merge_envelopes`` writes, by hand: a
    city/idle model trained at home altitude (80-82 kPa)."""
    return {
        "sessions": ["2026-09-21T194247"],
        "quantities": {"baro_kpa": BARO, "iat_c": IAT, "speed_kph": SPEED},
        "modes": {
            "city": {
                "rows": 900,
                "hours": 0.26,
                "quantities": {
                    "baro_kpa": _quantity(BARO, 80.0, 82.0),
                    "iat_c": _quantity(IAT, 19.0, 29.0),
                    "speed_kph": _quantity(SPEED, 0.0, 84.0),
                },
                "unmeasured": [],
            },
            "idle": {
                "rows": 500,
                "hours": 0.14,
                # Speed never measured while idling in this corpus.
                "quantities": {
                    "baro_kpa": _quantity(BARO, 81.0, 81.0),
                    "iat_c": _quantity(IAT, 36.0, 41.0),
                },
                "unmeasured": ["speed_kph"],
            },
        },
    }


def _observed(baro: float | None, iat: float | None, speed: float | None) -> dict[str, Any]:
    return {
        "baro_kpa": None if baro is None else (baro, baro),
        "iat_c": None if iat is None else (iat, iat),
        "speed_kph": None if speed is None else (speed, speed),
    }


# --- the check itself (pure, no torch) -------------------------------------


def test_a_window_at_altitude_is_outside_on_baro_and_names_both_ranges() -> None:
    envelope = TrainingEnvelope.from_record(_envelope_record())
    assert envelope is not None

    verdict = envelope.check("city", _observed(63.0, 22.0, 60.0))

    assert verdict["status"] == ENVELOPE_OUTSIDE
    assert verdict["outside"] is True
    assert verdict["outside_on"] == ["baro_kpa"]
    assert verdict["trained_hours"] == 0.26
    baro = verdict["quantities"]["baro_kpa"]
    assert (baro["window_min"], baro["trained_min"], baro["trained_max"]) == (63.0, 80.0, 82.0)
    assert verdict["quantities"]["iat_c"]["status"] == ENVELOPE_INSIDE

    sentence = sequence_envelope_summary(verdict, ("obd:BAROMETRIC_PRESSURE", "obd:MAF", "obd:RPM"))
    assert sentence == (
        "Outside the learned envelope in city on BAROMETRIC_PRESSURE "
        "(63 kPa vs trained 80–82 kPa); largest deviations: BAROMETRIC_PRESSURE, MAF, RPM."
    )


def test_a_window_inside_every_range_is_inside() -> None:
    envelope = TrainingEnvelope.from_record(_envelope_record())
    assert envelope is not None

    verdict = envelope.check("city", _observed(81.0, 22.0, 40.0))

    assert verdict["status"] == ENVELOPE_INSIDE
    assert verdict["outside"] is False
    assert verdict["outside_on"] == []
    assert sequence_envelope_summary(verdict).startswith("Inside the learned envelope in city")


def test_a_mode_training_never_saw_is_outside() -> None:
    envelope = TrainingEnvelope.from_record(_envelope_record())
    assert envelope is not None

    verdict = envelope.check("highway", _observed(81.0, 22.0, 110.0))

    assert verdict["status"] == ENVELOPE_MODE_NOT_TRAINED
    assert verdict["outside"] is True
    assert "mode 'highway' was never in training" in sequence_envelope_summary(verdict)


def test_nothing_comparable_is_not_compared_never_inside() -> None:
    """Empty says which kind of empty: no fresh baro/IAT in the window, and
    speed never measured at idle, compares nothing — that is not "inside"."""
    envelope = TrainingEnvelope.from_record(_envelope_record())
    assert envelope is not None

    verdict = envelope.check("idle", _observed(None, None, 0.0))

    assert verdict["status"] == ENVELOPE_NOT_COMPARED
    assert verdict["outside"] is False
    assert verdict["quantities"]["baro_kpa"]["status"] == "no_data"
    assert verdict["quantities"]["speed_kph"]["status"] == "not_trained"
    assert sequence_envelope_summary(verdict).startswith("Envelope not compared: ")

    no_mode = envelope.check(None, _observed(81.0, 38.0, 0.0))
    assert no_mode["status"] == ENVELOPE_NOT_COMPARED
    assert no_mode["reason"] == "the window carries no operating mode"


def test_an_artifact_without_an_envelope_says_not_recorded() -> None:
    assert TrainingEnvelope.from_record(None) is None
    assert sequence_envelope_summary(None, ("obd:RPM",)) == (
        "Training envelope not recorded for this artifact; largest deviations: RPM."
    )


# --- the window buffer keeps the condition columns -------------------------


def _row(second: int, *, baro: float | None, session: str = "live") -> dict[str, Any]:
    row: dict[str, Any] = {
        SESSION_COLUMN: session,
        TIMESTAMP_COLUMN: pd.Timestamp("2026-09-18T12:00:00Z") + pd.Timedelta(seconds=second),
        "operating_mode": "city",
        "obd:RPM": 1500.0,
    }
    if baro is not None:
        row[BARO] = baro
    return row


def test_the_buffer_keeps_condition_columns_that_are_not_model_inputs() -> None:
    buffer = SequenceWindowBuffer(
        feature_columns=("obd:RPM",), sequence_length=3, condition_columns=(BARO,)
    )
    for second, baro in enumerate((70.0, None, 63.0)):
        buffer.append(_row(second, baro=baro))

    assert buffer.condition_ranges() == {BARO: (63.0, 70.0)}

    buffer.append(_row(0, baro=81.0, session="other"))  # a session change clears it
    assert buffer.condition_ranges() == {BARO: (81.0, 81.0)}
    buffer.clear()
    assert buffer.condition_ranges() == {BARO: None}


# --- the served engine (a stub model, no training) -------------------------


def _stub_engine(training_envelope: dict[str, Any] | None, *, features: tuple[str, ...]) -> Any:
    torch = pytest.importorskip("torch")
    from src.models.inference import SequenceInferenceEngine

    class _ZeroModel:
        def to(self, *_a: object, **_k: object) -> _ZeroModel:
            return self

        def eval(self) -> _ZeroModel:
            return self

        def forward(self, value: Any, mask: Any, delta: Any) -> Any:
            return torch.zeros_like(value)

    band = {
        "count": 100,
        "mean": 1.0,
        "std": 1.0,
        "minimum": 0.0,
        "maximum": 5.0,
        "p50": 1.0,
        "p90": 2.0,
        "p95": 3.0,
        "p99": 4.0,
        "threshold": 1e9,
    }
    thresholds = {
        "threshold_k": 3.0,
        "calibrated_at": "2026-09-23T00:00:00Z",
        "training_data_hash": "stub",
        "window_count": 100,
        "global_error": band,
        "channel_errors": {column: band for column in features},
    }
    manifest = SimpleNamespace(
        thresholds=thresholds,
        schema={"sequence_length": 3, "feature_columns": list(features)},
        model_version="novelty-stub",
        scaler=None,
        training_envelope=training_envelope,
    )
    trained = SimpleNamespace(model=_ZeroModel(), feature_columns=features, manifest=manifest)
    return SequenceInferenceEngine(trained)


def test_a_scored_window_carries_the_envelope_verdict_into_its_event() -> None:
    engine = _stub_engine(_envelope_record(), features=("obd:RPM", BARO))
    assert engine.condition_columns == (BARO, IAT, SPEED)

    result = None
    for second in range(3):
        row = _row(second, baro=63.0)
        row[IAT] = 22.0
        row[SPEED] = 60.0
        result = engine.observe(row)

    assert result is not None
    assert result.envelope is not None
    assert result.envelope["status"] == ENVELOPE_OUTSIDE
    assert result.envelope["outside_on"] == ["baro_kpa"]
    assert result.to_dict()["envelope"]["mode"] == "city"

    event = AnomalyEvent.from_lstm_result(result, operating_mode="city")
    assert event.detail["envelope"]["status"] == ENVELOPE_OUTSIDE
    assert event.detail["novelty_summary"].startswith(
        "Outside the learned envelope in city on BAROMETRIC_PRESSURE (63 kPa vs trained 80–82 kPa)"
    )
    assert "largest deviations:" in event.detail["novelty_summary"]
    # The stored record round-trips.
    assert AnomalyEvent.from_dict(event.to_dict()).detail["envelope"] == event.detail["envelope"]


def test_an_artifact_without_an_envelope_scores_with_envelope_none() -> None:
    engine = _stub_engine(None, features=("obd:RPM",))
    assert engine.condition_columns == ()

    result = None
    for second in range(3):
        result = engine.observe(_row(second, baro=63.0))

    assert result is not None
    assert result.envelope is None
    event = AnomalyEvent.from_lstm_result(result)
    assert event.detail["envelope"] is None
    assert event.detail["novelty_summary"].startswith(
        "Training envelope not recorded for this artifact"
    )


def test_the_daemon_hands_the_engine_its_condition_columns() -> None:
    """``_sequence_observe`` reconciles the live row to the model's inputs;
    the envelope's columns must survive that even when they are not inputs."""
    from src.daemon.inference import _sequence_observe

    engine = _stub_engine(_envelope_record(), features=("obd:RPM",))
    result = None
    for second in range(3):
        row = _row(second, baro=63.0)
        row[IAT] = 22.0
        row[SPEED] = 60.0
        result = _sequence_observe(engine, pd.DataFrame([row]))

    assert result is not None
    assert result.envelope is not None
    assert result.envelope["quantities"]["baro_kpa"]["window_min"] == 63.0
    assert result.envelope["outside_on"] == ["baro_kpa"]


# --- the /status coverage meter --------------------------------------------


def test_the_coverage_meter_counts_outside_windows_by_mode_and_quantity() -> None:
    envelope = TrainingEnvelope.from_record(_envelope_record())
    assert envelope is not None
    recorder = InferenceTimingRecorder()
    for verdict in (
        envelope.check("city", _observed(63.0, 22.0, 60.0)),  # outside on baro
        envelope.check("city", _observed(81.0, 22.0, 60.0)),  # inside
        envelope.check("city", _observed(81.0, 35.0, 90.0)),  # outside on IAT and speed
        envelope.check("highway", _observed(81.0, 20.0, 100.0)),  # mode never trained
        envelope.check("idle", _observed(None, None, None)),  # nothing compared
    ):
        recorder.note_sequence_envelope(verdict, model_version="v1")

    meter = recorder.snapshot().to_dict()["sequence_envelope"]

    assert meter["model_version"] == "v1"
    assert meter["envelope_recorded"] is True
    assert (meter["windows"], meter["inside"], meter["outside"]) == (5, 1, 3)
    assert (meter["mode_not_trained"], meter["not_compared"], meter["not_recorded"]) == (1, 1, 0)
    assert meter["share_outside"] == 0.75  # of the four windows compared
    city = meter["by_mode"]["city"]
    assert (city["windows"], city["outside"], city["share_outside"]) == (3, 2, round(2 / 3, 4))
    assert city["by_quantity"]["baro_kpa"] == {
        "compared": 3,
        "outside": 1,
        "no_data": 0,
        "not_trained": 0,
        "share_outside": round(1 / 3, 4),
    }
    assert meter["by_mode"]["highway"]["share_outside"] == 1.0
    assert meter["by_quantity"]["speed_kph"]["not_trained"] == 1
    assert meter["by_quantity"]["iat_c"]["outside"] == 1


def test_the_meter_counts_an_unrecorded_envelope_apart_and_restarts_on_a_new_artifact() -> None:
    recorder = InferenceTimingRecorder()
    assert recorder.snapshot().to_dict()["sequence_envelope"]["envelope_recorded"] is None

    recorder.note_sequence_envelope(None, model_version="old")
    recorder.note_sequence_envelope(None, model_version="old")
    meter = recorder.snapshot().to_dict()["sequence_envelope"]
    assert meter["envelope_recorded"] is False
    assert (meter["windows"], meter["not_recorded"], meter["inside"]) == (2, 2, 0)
    assert meter["share_outside"] is None  # never "0 % outside"

    envelope = TrainingEnvelope.from_record(_envelope_record())
    assert envelope is not None
    recorder.note_sequence_envelope(
        envelope.check("city", _observed(81.0, 22.0, 40.0)), model_version="new"
    )
    meter = recorder.snapshot().to_dict()["sequence_envelope"]
    assert (meter["model_version"], meter["windows"], meter["not_recorded"]) == ("new", 1, 0)


def test_applying_a_sequence_result_feeds_the_meter() -> None:
    envelope = TrainingEnvelope.from_record(_envelope_record())
    assert envelope is not None
    recorder = InferenceTimingRecorder()
    pipeline = LiveInferencePipeline.__new__(LiveInferencePipeline)
    pipeline._recorder = recorder
    pipeline._components = SimpleNamespace(  # type: ignore[assignment]
        replay_controller=None,
        capture_controller=None,
        model_managers={"lstm_autoencoder": SimpleNamespace(current=None)},
    )
    pipeline._if = SimpleNamespace(apply=lambda *_a: [])  # type: ignore[assignment]
    pipeline._sequence = SimpleNamespace(apply=lambda *_a: [])  # type: ignore[assignment]
    pipeline._behavior = SimpleNamespace(apply=lambda *_a: [])  # type: ignore[assignment]
    pipeline._fuse = lambda *_a: None  # type: ignore[method-assign]
    request = SimpleNamespace(
        token=GenerationToken(
            replay_id=None, capture_id=None, model_versions=(("lstm_autoencoder", None),)
        ),
        ctx=TickContext(
            now=pd.Timestamp("2026-09-18T12:00:00Z").to_pydatetime(),
            operating_mode="city",
            timestamp_str="2026-09-18T12:00:00Z",
            scoring_allowed=True,
        ),
        behavior_residuals=(),
        snapshot_loop_seconds=0.0,
        snapshot_started=0.0,
    )
    result = SequenceInferenceResult(
        model_version="v1",
        timestamp="2026-09-18T12:00:00Z",
        session="live",
        window_start_timestamp="2026-09-18T11:59:30Z",
        window_end_timestamp="2026-09-18T12:00:00Z",
        global_score=0.1,
        global_threshold=1.0,
        channel_scores={},
        channel_thresholds={},
        anomalous_channels=(),
        is_anomaly=False,
        latency_ms=1.0,
        envelope=envelope.check("city", _observed(63.0, 22.0, 60.0)),
    )

    pipeline._apply(
        request,  # type: ignore[arg-type]
        _TickResult(if_computed=None, sequence_result=result, durations={}),
    )

    meter = recorder.snapshot().to_dict()["sequence_envelope"]
    assert (meter["windows"], meter["outside"]) == (1, 1)
    assert meter["by_mode"]["city"]["by_quantity"]["baro_kpa"]["outside"] == 1
