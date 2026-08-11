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


def _did_scheduler() -> dict[str, object]:
    """An all-DID capture as the scheduler actually reports one.

    DIDs come back by identifier while the profile names them semantically,
    and DTC reads are always `capture_selected` regardless of profile.
    """
    return {
        "capture_observed_seconds": 120.0,
        "achieved_hz_capture": {"DID_1209": 19.4, "DID_1208": 19.1, "DID_1219": 18.9},
        "capture_samples": {"DID_1209": 2328, "DID_1208": 2292, "DID_1219": 2268},
        "capture_max_gap_seconds": {"DID_1209": 0.4, "DID_1208": 0.35, "DID_1219": 0.5},
        "requests": [
            {
                "channel": "DID_1209",
                "profile_channel": "ECM_MAF_GPS",
                "kind": "did",
                "poll_tier": "fast",
                "capture_selected": True,
            },
            {
                "channel": "DID_1208",
                "profile_channel": "ECM_INJECTOR_PW_B1",
                "kind": "did",
                "poll_tier": "fast",
                "capture_selected": True,
            },
            {
                "channel": "DID_1219",
                "profile_channel": "ECM_RPM",
                "kind": "did",
                "poll_tier": "fast",
                "capture_selected": True,
            },
            # A catalogue alias resolving to a DID already listed.
            {
                "channel": "DID_1209",
                "profile_channel": "ECM_MAF_GPS",
                "kind": "did",
                "poll_tier": "fast",
                "capture_selected": True,
            },
            {
                "channel": "DTC_STORED",
                "profile_channel": None,
                "kind": "dtc",
                "poll_tier": "dtc",
                "capture_selected": True,
            },
            {
                "channel": "DTC_PENDING",
                "profile_channel": None,
                "kind": "dtc",
                "poll_tier": "dtc",
                "capture_selected": True,
            },
        ],
        "batching": {"max_pids": 3},
    }


def _did_profile() -> dict[str, object]:
    return {
        "name": "fuel-did-research",
        "mode01": {},
        "mode22": ["ECM_MAF_GPS", "ECM_INJECTOR_PW_B1", "ECM_RPM"],
        "target_rate_hz": 20.0,
        "required_tuple": ["ECM_RPM", "ECM_MAF_GPS", "ECM_INJECTOR_PW_B1"],
        "min_achieved_rate_hz": 15.0,
        "round_timestamps": True,
    }


def test_a_did_capture_is_scored_under_the_profiles_own_channel_names() -> None:
    """The first live run called four answering DIDs "never answered".

    The scheduler reports `DID_1209`; the profile says `ECM_MAF_GPS`. Matching
    one against the other found nothing, so `required_tuple_incomplete` fired
    on a capture whose channels were all healthy.
    """
    quality = evaluate_capture_quality(profile=_did_profile(), scheduler=_did_scheduler())

    assert quality.verdict == "pass", quality.failures
    assert quality.tuple_rate_bound_hz == 18.9
    # Reported under the name the operator selected, not the transport's.
    assert {row.channel for row in quality.channels} == {
        "ECM_MAF_GPS",
        "ECM_INJECTOR_PW_B1",
        "ECM_RPM",
    }


def test_dtc_reads_are_not_capture_channels() -> None:
    """`request_eligible` returns True for DTC reads whatever the profile.

    They poll on their own slow schedule, so scoring them against a 20 Hz
    capture reported DTC_STORED/PENDING as unanswered on the first live run.
    """
    quality = evaluate_capture_quality(profile=_did_profile(), scheduler=_did_scheduler())
    assert not any(row.channel.startswith("DTC_") for row in quality.channels)
    assert "channel_unanswered" not in {f["clause"] for f in quality.failures}


def test_one_did_reached_by_two_requests_is_listed_once() -> None:
    quality = evaluate_capture_quality(profile=_did_profile(), scheduler=_did_scheduler())
    names = [row.channel for row in quality.channels]
    assert len(names) == len(set(names))


def test_sub_second_jitter_at_a_high_rate_is_not_a_stall() -> None:
    """A 20 Hz channel was called stalled after 0.25 s of quiet.

    That is ordinary scheduling jitter and it flagged every DID in the first
    live capture. Two seconds of silence is a coverage gap; a quarter-second
    is not, at any rate.
    """
    scheduler = _did_scheduler()
    scheduler["capture_max_gap_seconds"] = {"DID_1209": 0.9, "DID_1208": 1.5, "DID_1219": 0.4}
    quality = evaluate_capture_quality(profile=_did_profile(), scheduler=scheduler)
    assert "channel_stalled" not in {f["clause"] for f in quality.failures}

    # A genuinely long silence still fails.
    scheduler["capture_max_gap_seconds"] = {"DID_1209": 30.0, "DID_1208": 0.3, "DID_1219": 0.4}
    stalled = evaluate_capture_quality(profile=_did_profile(), scheduler=scheduler)
    failure = next(f for f in stalled.failures if f["clause"] == "channel_stalled")
    assert failure["channels"] == ["ECM_MAF_GPS"]
