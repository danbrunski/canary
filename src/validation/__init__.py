"""Sustained end-to-end validation harness (roadmap task C10).

A synthetic bus with numbered broadcast frames drives the *actual* Canable
collector (``run_can_obd_loop`` under ``collect_can_obd_from_config``), the
daemon fanout (``drain_queue_with_broadcast``), the real Parquet writer and
the live inference loop, on the real clock. Every report reconciles what the
adapter delivered against what reached disk, so a scheduler counter can
never stand in for persisted evidence.

Nothing here runs against hardware. ``docs/sustained-validation.md`` is the
handoff record and the Pi procedure.
"""
