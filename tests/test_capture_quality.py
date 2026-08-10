"""The capture contract: does a capture deliver what its profile promised?

Every case here is drawn from the 2026-08-10 fuel-map capture, which ran for
24 minutes, reported nothing alarming, and yielded two usable delivery-error
samples.
"""

from __future__ import annotations

from src.collection.capture_quality import (
    evaluate_capture_quality,
    tuple_rate_bound_hz,
)


def _profile(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "name": "fuel-map",
        "mode01": {
            "RPM": 1.0,
            "ENGINE_LOAD": 1.0,
            "FUEL_STATUS": 1.0,
            "COMMANDED_EQUIV_RATIO": 1.0,
            "O2_WR_LAMBDA_S1": 1.0,
            "COOLANT_TEMP": 0.25,
        },
        "target_rate_hz": 4.0,
        "required_tuple": [
            "RPM",
            "ENGINE_LOAD",
            "FUEL_STATUS",
            "COMMANDED_EQUIV_RATIO",
            "O2_WR_LAMBDA_S1",
        ],
        "min_achieved_rate_hz": 3.0,
        "round_timestamps": True,
        "broadcast_decode": True,
    }
    base.update(overrides)
    return base


def _scheduler(rates: dict[str, float], *, observed: float = 20.0) -> dict[str, object]:
    return {
        "capture_observed_seconds": observed,
        "achieved_hz_capture": dict(rates),
        "capture_samples": {name: int(hz * observed) for name, hz in rates.items()},
        "capture_max_gap_seconds": {name: 1.0 / hz if hz else None for name, hz in rates.items()},
        "requests": [{"channel": name, "kind": "pid", "capture_selected": True} for name in rates],
        "batching": {"max_pids": 6, "mean_pids_per_batch": 5.2},
    }


def test_a_capture_meeting_its_contract_passes() -> None:
    rates = {
        "RPM": 4.0,
        "ENGINE_LOAD": 4.0,
        "FUEL_STATUS": 3.9,
        "COMMANDED_EQUIV_RATIO": 4.0,
        "O2_WR_LAMBDA_S1": 3.8,
        "COOLANT_TEMP": 1.0,
    }
    quality = evaluate_capture_quality(profile=_profile(), scheduler=_scheduler(rates))
    assert quality.verdict == "pass"
    assert quality.failures == []
    assert quality.tuple_rate_bound_hz == 3.8
    assert quality.tuple_bound_is_exact is True


def test_the_context_weight_is_part_of_the_promise_not_a_shortfall() -> None:
    """A 0.25-weight channel is *asked* for a quarter rate.

    Holding it to the full target would report a shortfall the profile never
    claimed — and would have made every correctly-configured capture fail.
    """
    quality = evaluate_capture_quality(
        profile=_profile(),
        scheduler=_scheduler(
            {
                "RPM": 4.0,
                "ENGINE_LOAD": 4.0,
                "FUEL_STATUS": 4.0,
                "COMMANDED_EQUIV_RATIO": 4.0,
                "O2_WR_LAMBDA_S1": 4.0,
                "COOLANT_TEMP": 1.0,
            }
        ),
    )
    coolant = next(row for row in quality.channels if row.channel == "COOLANT_TEMP")
    assert coolant.requested_hz == 1.0
    assert coolant.status == "ok"
    assert quality.verdict == "pass"


def test_the_2026_08_10_capture_would_have_failed_before_the_drive() -> None:
    """The rates that actually ran, scored against what the profile claimed.

    This is the whole point: every one of these channels answered, none looked
    dead, and the capture could not answer its own question.
    """
    quality = evaluate_capture_quality(
        profile=_profile(),
        scheduler=_scheduler(
            {
                "RPM": 0.78,
                "ENGINE_LOAD": 0.78,
                "FUEL_STATUS": 0.29,
                "COMMANDED_EQUIV_RATIO": 0.28,
                "O2_WR_LAMBDA_S1": 0.67,
                "COOLANT_TEMP": 0.29,
            }
        ),
    )
    assert quality.verdict == "fail"
    clauses = {failure["clause"] for failure in quality.failures}
    assert "rate_shortfall" in clauses
    assert "required_tuple_rate" in clauses
    # The tuple bound is the number per-channel rates could not express.
    assert quality.tuple_rate_bound_hz == 0.28


def test_an_unanswered_channel_is_not_a_shortfall() -> None:
    """PID 0x25 never answers on the dev ECM: opposite fix from throttling.

    A shortfall wants budget. A channel the ECU refuses wants removing from
    the profile — it burns a schedule slot every round and returns nothing.
    """
    scheduler = _scheduler(
        {
            "RPM": 4.0,
            "ENGINE_LOAD": 4.0,
            "FUEL_STATUS": 4.0,
            "COMMANDED_EQUIV_RATIO": 4.0,
            "O2_WR_LAMBDA_S1": 4.0,
            "COOLANT_TEMP": 1.0,
        }
    )
    scheduler["requests"] = [
        *scheduler["requests"],  # type: ignore[misc]
        {"channel": "O2_WR_LAMBDA_S2", "kind": "pid", "capture_selected": True},
    ]
    profile = _profile()
    profile["mode01"] = {**profile["mode01"], "O2_WR_LAMBDA_S2": 1.0}  # type: ignore[dict-item]

    quality = evaluate_capture_quality(profile=profile, scheduler=scheduler)
    assert quality.verdict == "fail"
    failure = next(f for f in quality.failures if f["clause"] == "channel_unanswered")
    assert failure["channels"] == ["O2_WR_LAMBDA_S2"]
    assert "rate_shortfall" not in {f["clause"] for f in quality.failures}


def test_a_missing_tuple_member_is_reported_as_missing_not_as_a_rate() -> None:
    """Zero-because-absent and zero-because-slow demand opposite responses."""
    rates = {
        "RPM": 4.0,
        "ENGINE_LOAD": 4.0,
        "FUEL_STATUS": 4.0,
        "O2_WR_LAMBDA_S1": 4.0,
        "COOLANT_TEMP": 1.0,
    }
    quality = evaluate_capture_quality(
        profile=_profile(),
        scheduler=_scheduler(rates),
    )
    clauses = {failure["clause"] for failure in quality.failures}
    assert "required_tuple_incomplete" in clauses
    assert "required_tuple_rate" not in clauses
    assert quality.tuple_rate_bound_hz is None


def test_dropped_storage_rows_fail_the_contract() -> None:
    """The bus delivering it and the disk keeping it are different claims."""
    quality = evaluate_capture_quality(
        profile=_profile(),
        scheduler=_scheduler(
            {
                "RPM": 4.0,
                "ENGINE_LOAD": 4.0,
                "FUEL_STATUS": 4.0,
                "COMMANDED_EQUIV_RATIO": 4.0,
                "O2_WR_LAMBDA_S1": 4.0,
                "COOLANT_TEMP": 1.0,
            }
        ),
        storage={"dropped_records": 2491, "queue_depth": 4096},
    )
    assert quality.verdict == "fail"
    assert {f["clause"] for f in quality.failures} == {"storage_dropped_records"}


def test_too_short_a_window_is_unverified_not_a_pass() -> None:
    quality = evaluate_capture_quality(
        profile=_profile(),
        scheduler=_scheduler({"RPM": 4.0}, observed=1.0),
    )
    assert quality.verdict == "unverified"
    assert any("minimum" in note for note in quality.notes)


def test_batching_off_is_called_out() -> None:
    scheduler = _scheduler({"RPM": 4.0, "COOLANT_TEMP": 1.0})
    scheduler["batching"] = None
    quality = evaluate_capture_quality(profile=_profile(required_tuple=[]), scheduler=scheduler)
    assert any("batching" in note for note in quality.notes)


def test_tuple_bound_is_the_slowest_member() -> None:
    assert tuple_rate_bound_hz({"A": 4.0, "B": 0.3}, ("A", "B")) == 0.3
    assert tuple_rate_bound_hz({"A": 4.0}, ("A", "B")) is None
    assert tuple_rate_bound_hz({"A": 4.0}, ()) is None
