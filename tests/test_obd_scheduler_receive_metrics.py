"""Receive accounting reaches ``/status`` (C06.12 hardware procedure).

``frames_seen`` / ``discovery_frames_seen`` were counted in
``CanObdLoopStats`` but never serialized, so the diagnostic-discovery
hardware steps that read them from ``/status`` could not be executed on the
active canable path (where the ``can_receive`` sink is never registered).
"""

from src.collection.obd_scheduler import CanObdLoopStats, _receive_metrics


def test_receive_metrics_surface_the_loop_counters() -> None:
    stats = CanObdLoopStats()
    stats.frames_seen = 131_346
    stats.discovery_frames_seen = 412
    stats.parse_errors = 3

    assert _receive_metrics(stats) == {
        "frames_seen": 131_346,
        "discovery_frames_seen": 412,
        "parse_errors": 3,
    }


def test_receive_block_is_serialized_into_the_status_payload() -> None:
    from src.collection.obd_scheduler import ObdSchedulerMetrics

    metrics = ObdSchedulerMetrics(
        configured_poll_rate_hz=1.0,
        bus_budget_requests_per_second=8.0,
        active_requests=0,
        backed_off_requests=0,
        requests_sent=0,
        readings_emitted=0,
        request_timeouts=0,
        negative_responses=0,
        bus_errors=0,
        response_rate=None,
        by_tier={},
        receive={"frames_seen": 7, "discovery_frames_seen": 2, "parse_errors": 0},
    )

    assert metrics.to_dict()["receive"] == {
        "frames_seen": 7,
        "discovery_frames_seen": 2,
        "parse_errors": 0,
    }
