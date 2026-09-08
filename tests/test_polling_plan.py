from __future__ import annotations

from src.collection.polling_plan import PlanMember, ResolvedPollingPlan


def _member(
    identifier: int,
    rate_hz: float,
    *,
    channel: str | None = None,
    width: int | None = 2,
    priority: bool = False,
    required: bool = False,
) -> PlanMember:
    return PlanMember(
        kind="did",
        service=0x22,
        request_id=0x7E0,
        identifier=identifier,
        channel=channel or f"DID_{identifier:04X}",
        profile_channel=channel,
        rate_hz=rate_hz,
        priority=priority,
        required=required,
        response_payload_bytes=width,
    )


def test_alias_duplicates_resolve_before_demand_is_counted() -> None:
    plan = ResolvedPollingPlan.build(
        [
            _member(0x1209, 4.0, channel="ECM_MAF_GPS"),
            _member(0x1209, 2.0, channel="OLD_MAF_ALIAS", priority=True),
        ],
        bus_budget_requests_per_second=8.0,
        pid_batch_max=1,
        did_batch_max=3,
    ).to_dict()

    assert plan["member_count"] == 1
    assert plan["aliases_deduplicated"] == 1
    assert plan["channel_reads_per_second"] == 4.0
    assert plan["priority_requests_per_second_fallback"] == 4.0


def test_heterogeneous_rates_use_nested_batch_layers_without_accelerating_context() -> None:
    plan = ResolvedPollingPlan.build(
        [_member(0x1101, 10.0), _member(0x1219, 10.0), _member(0x1208, 2.0)],
        bus_budget_requests_per_second=20.0,
        pid_batch_max=1,
        did_batch_max=2,
    ).to_dict()

    assert plan["channel_reads_per_second"] == 22.0
    assert plan["diagnostic_requests_per_second"]["best_case_batched"] == 12.0
    assert plan["diagnostic_requests_per_second"]["single_request_fallback"] == 22.0
    assert plan["fallback_feasible"] is False


def test_plan_keeps_transport_units_overhead_and_tuple_yield_distinct() -> None:
    plan = ResolvedPollingPlan.build(
        [
            _member(0x1101, 8.0, required=True),
            _member(0x1219, 5.0, required=True),
        ],
        bus_budget_requests_per_second=20.0,
        pid_batch_max=1,
        did_batch_max=2,
        session_control_requests_per_second=0.5,
        discovery_requests_per_second=1.0,
    ).to_dict()

    assert plan["channel_reads_per_second"] == 13.0
    assert plan["decoded_rows_per_second"] == 13.0
    assert plan["complete_tuples_per_second"] == 5.0
    assert plan["diagnostic_requests_per_second"]["session_control"] == 0.5
    assert plan["diagnostic_requests_per_second"]["discovery"] == 1.0
    assert plan["diagnostic_requests_per_second"]["best_case_batched"] == 9.5
    assert plan["raw_frames_per_second"]["best_case_batched"] != 9.5


def test_priority_share_is_refused_independently_of_optional_demand() -> None:
    plan = ResolvedPollingPlan.build(
        [_member(0x1101, 6.0, priority=True), _member(0x1219, 3.0)],
        bus_budget_requests_per_second=5.0,
        pid_batch_max=1,
        did_batch_max=2,
    ).to_dict()

    assert plan["priority_feasible"] is False
    assert plan["priority_requests_per_second_fallback"] == 6.0


def test_unknown_response_width_makes_raw_frame_estimate_explicitly_incomplete() -> None:
    plan = ResolvedPollingPlan.build(
        [_member(0x1999, 1.0, width=None)],
        bus_budget_requests_per_second=5.0,
        pid_batch_max=1,
        did_batch_max=3,
    ).to_dict()

    assert plan["raw_frames_per_second"]["estimate_complete"] is False
    assert plan["raw_frames_per_second"]["unknown_response_width_channels"] == ["DID_1999"]
    assert plan["diagnostic_requests_per_second"]["best_case_batched"] == 1.0
