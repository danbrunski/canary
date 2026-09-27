"""Tests for the derived ``vehicle_state`` block in ``GET /status``.

The classifier rule table is documented in ``classify_vehicle_state``;
these tests pin the truth table so a future tweak to ``engine_off`` vs.
``ignition_only`` thresholds can't silently regress the live-vs-replay
disambiguation the agent docs rely on.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from src.dashboard.status import classify_vehicle_state
from src.telemetry import TelemetryRecord


def _broadcast_rpm(timestamp: datetime, counts: int) -> TelemetryRecord:
    """The ECM's 0x1F9 frame carrying ``counts`` (8 per rpm) at bytes 2-3, the
    ``ECM_RPM`` binding. Sessions store the frame, never a ``decoded`` row
    (P02.8); the Parquet reader decodes it on read."""
    payload = bytes([0x20, 0x00, (counts >> 8) & 0xFF, counts & 0xFF, 0, 0, 0, 0])
    return TelemetryRecord(timestamp=timestamp, source="can", channel="0x1F9", payload=payload)


def _common_telemetry(**overrides: object) -> dict[str, object]:
    """Classifier kwargs; ``rpm`` is shorthand for the Mode 01 ``obd_rpm``."""
    base: dict[str, object] = {
        "obd_rpm": None,
        "broadcast_rpm": None,
        "coolant_temp": None,
        "run_time": None,
        "obd_freshness_seconds": None,
        "can_freshness_seconds": None,
        "replay_id": None,
    }
    if "rpm" in overrides:
        overrides["obd_rpm"] = overrides.pop("rpm")
    base.update(overrides)
    return base


def test_replay_overrides_everything() -> None:
    """Replay precedence holds even when OBD/CAN are fresh and RPM > running."""
    state = classify_vehicle_state(
        **_common_telemetry(
            rpm=800.0,
            coolant_temp=85.0,
            run_time=120.0,
            obd_freshness_seconds=0.5,
            can_freshness_seconds=0.1,
            replay_id="2026-05-20T180000",
        )
    )
    assert state.state == "replay"
    assert state.replay_id == "2026-05-20T180000"
    # raw telemetry fields still surfaced for diagnostics
    assert state.rpm == 800.0


def test_no_data_when_both_buses_stale() -> None:
    state = classify_vehicle_state(
        **_common_telemetry(
            obd_freshness_seconds=120.0,
            can_freshness_seconds=120.0,
        )
    )
    assert state.state == "no_data"


def test_no_data_when_neither_observed() -> None:
    state = classify_vehicle_state(**_common_telemetry())
    assert state.state == "no_data"


def test_engine_running_at_idle_rpm() -> None:
    state = classify_vehicle_state(
        **_common_telemetry(
            rpm=760.0,
            coolant_temp=82.0,
            run_time=240.0,
            obd_freshness_seconds=0.3,
            can_freshness_seconds=0.1,
        )
    )
    assert state.state == "engine_running"


def test_engine_running_at_cruise_rpm() -> None:
    state = classify_vehicle_state(
        **_common_telemetry(
            rpm=2200.0,
            coolant_temp=88.0,
            run_time=1200.0,
            obd_freshness_seconds=0.4,
            can_freshness_seconds=0.1,
        )
    )
    assert state.state == "engine_running"


def test_ignition_only_when_obd_fresh_but_rpm_zero() -> None:
    """KOEO (key on, engine off): ECM responds to OBD with RPM=0."""
    state = classify_vehicle_state(
        **_common_telemetry(
            rpm=0.0,
            coolant_temp=25.0,
            run_time=0.0,
            obd_freshness_seconds=0.5,
            can_freshness_seconds=0.2,
        )
    )
    assert state.state == "ignition_only"


def test_engine_off_when_can_fresh_but_obd_stale() -> None:
    """Plugged into the bus, but ECM isn't responding — typical with key out."""
    state = classify_vehicle_state(
        **_common_telemetry(
            obd_freshness_seconds=45.0,
            can_freshness_seconds=0.1,
        )
    )
    assert state.state == "engine_off"


def test_unknown_when_obd_fresh_and_rpm_in_no_mans_land() -> None:
    """OBD fresh, RPM > 0 but below the running threshold — flag it as unknown
    so the caller doesn't silently classify a noisy reading as ``engine_off``.
    """
    state = classify_vehicle_state(
        **_common_telemetry(
            rpm=120.0,  # below running threshold but not zero
            obd_freshness_seconds=0.5,
            can_freshness_seconds=0.1,
        )
    )
    assert state.state == "unknown"
    assert state.rpm == 120.0


def test_classifier_returns_raw_signals_alongside_state() -> None:
    """Diagnostic fields are always populated so the agent can second-guess."""
    state = classify_vehicle_state(
        **_common_telemetry(
            rpm=760.0,
            coolant_temp=82.0,
            run_time=180.0,
            obd_freshness_seconds=0.4,
            can_freshness_seconds=0.1,
        )
    )
    assert state.rpm == 760.0
    assert state.coolant_temp == 82.0
    assert state.run_time == 180.0
    assert state.obd_freshness_seconds == 0.4
    assert state.can_freshness_seconds == 0.1


def test_to_dict_rounds_floats_for_stable_json() -> None:
    state = classify_vehicle_state(
        **_common_telemetry(
            rpm=760.4444,
            coolant_temp=82.999,
            obd_freshness_seconds=0.345678,
            can_freshness_seconds=0.123456,
        )
    )
    payload = state.to_dict()
    assert payload["rpm"] == 760.44
    assert payload["coolant_temp"] == 83.0
    assert payload["obd_freshness_seconds"] == 0.35
    assert payload["can_freshness_seconds"] == 0.12


def test_capture_sessions_do_not_blank_vehicle_state(tmp_path: Path) -> None:
    """A capture must not make the truck look absent (bug found 2026-08-04).

    Capture sessions are `_`-prefixed and therefore invisible to the
    newest-session search *on purpose* — they must stay out of the training
    corpus. The side effect was that `_derive_vehicle_state` kept reading the
    previous, no-longer-written session, so OBD and CAN freshness both grew in
    lockstep from the arming instant and the state read `no_data` for the whole
    of every tuning capture, engine running or not.
    """
    from datetime import UTC, datetime

    from src.dashboard.status import _derive_vehicle_state
    from src.storage.parquet_writer import write_records_to_parquet
    from src.telemetry import TelemetryRecord

    now = datetime(2026, 8, 5, 12, 0, 30, tzinfo=UTC)
    stale = now.replace(second=0)

    storage = tmp_path / "vehicle"
    # The normal session stopped being written when the capture started.
    write_records_to_parquet(
        [
            TelemetryRecord(timestamp=stale, source="obd", channel="RPM", value=800.0),
            TelemetryRecord(timestamp=stale, source="can", channel="0x1F9", payload=b"\x00"),
        ],
        storage / "2026-08-05T115900",
    )
    capture = storage / "_capture-tuning-did-2026-08-05T120000"
    write_records_to_parquet(
        [
            TelemetryRecord(
                timestamp=now.replace(second=29), source="obd", channel="RPM", value=2500.0
            ),
            TelemetryRecord(
                timestamp=now.replace(second=29), source="can", channel="0x1F9", payload=b"\x00"
            ),
        ],
        capture,
    )

    # Correct WITHOUT the caller passing anything: the fix lives in the
    # resolver, not in six callers remembering a parameter. This assertion
    # deliberately used to expect `no_data` — the bug — when the fix was still
    # per-call-site.
    without = _derive_vehicle_state(storage, replay_controller=None, now=now)
    assert without.state == "engine_running"
    assert without.rpm == 2500.0

    # An explicit session_dir stays supported: the writer knows its own path
    # exactly, where the resolver infers it from disk.
    with_capture = _derive_vehicle_state(
        storage, replay_controller=None, now=now, session_dir=capture
    )
    assert with_capture.state == "engine_running"
    assert with_capture.rpm == 2500.0


def test_all_did_capture_classifies_from_decoded_broadcast(tmp_path: Path) -> None:
    """Mode 01 is dark by ECU design during an all-DID capture.

    Session 0xC0 blocks Mode 01 outright, so there is no `obd:RPM` to classify
    from — but broadcast decode never stops. `decoded:ECM_RPM` is stored raw
    (8 counts/rpm), so the fallback has to scale it or a running engine reads
    as 20 000 rpm.
    """
    from datetime import UTC, datetime

    from src.dashboard.status import _derive_vehicle_state
    from src.storage.parquet_writer import write_records_to_parquet

    now = datetime(2026, 8, 5, 12, 0, 30, tzinfo=UTC)
    storage = tmp_path / "vehicle"
    capture = storage / "_capture-tuning-did-2026-08-05T120000"
    write_records_to_parquet(
        [
            # 2500 rpm x 8 counts in the frame; decoded on read.
            _broadcast_rpm(now.replace(second=29), 20000),
        ],
        capture,
    )
    state = _derive_vehicle_state(storage, replay_controller=None, now=now, session_dir=capture)
    assert state.rpm == 2500.0
    # Not `engine_off`: that rule infers a powered-down ECM from OBD silence,
    # which is wrong when Mode 01 is blocked by design.
    assert state.state == "engine_running"


def test_live_session_resolution_includes_captures(tmp_path: Path) -> None:
    """ "What is live" and "what is eligible for training" are different questions.

    `discover_sessions` excludes `_`-prefixed directories so captures stay out
    of the training corpus — correct there, and wrong for every live read.
    Reusing it meant /status, /telemetry/latest, OBD coverage and the replay
    guard all silently showed the *previous* session for the whole duration of
    a capture. Fixing that per-caller needed six call sites to remember; this
    pins the fix at the resolver instead.
    """
    from src.preprocessing.loader import discover_sessions
    from src.storage.sessions import latest_session

    root = tmp_path / "vehicle"
    for name in ("2026-08-05T120000", "_capture-fuel-map-2026-08-05T130000"):
        (root / name).mkdir(parents=True)
        (root / name / "telemetry-00000.parquet").write_bytes(b"x")

    live = latest_session(root)
    assert live is not None and live.name == "_capture-fuel-map-2026-08-05T130000"
    # The training view still excludes it — that exclusion is the point.
    assert [p.name for p in discover_sessions(root)] == ["2026-08-05T120000"]


def test_live_session_resolution_orders_by_time_not_name(tmp_path: Path) -> None:
    """Capture prefixes make name-ordering wrong, which is its own bug class.

    `_capture-` sorts before `_tuning-` regardless of date, so a capture a
    month later compares as earlier.
    """
    from src.storage.sessions import latest_session

    root = tmp_path / "vehicle"
    for name in ("_tuning-2026-08-01T100000", "_capture-fuel-map-2026-09-01T100000"):
        (root / name).mkdir(parents=True)
        # A session only counts once it holds data — an empty directory exists
        # from the moment the writer rotates into it.
        (root / name / "telemetry-00000.parquet").write_bytes(b"x")

    live = latest_session(root)
    assert live is not None and live.name == "_capture-fuel-map-2026-09-01T100000"


def test_one_parser_for_session_timestamps() -> None:
    """Every session shape resolves through the same function.

    Three separate copies of this parse had accumulated; a fourth was about
    to. Ordering sessions any other way is a bug waiting for a new prefix.
    """
    from src.storage.sessions import session_started_at
    from src.vehicle.tune_revisions import session_started_at as via_revisions

    for name in (
        "2026-08-05T142720",
        "_tuning-2026-08-05T142720",
        "_capture-fuel-map-2026-08-05T142720",
    ):
        parsed = session_started_at(name)
        assert parsed is not None
        assert parsed.isoformat() == "2026-08-05T14:27:20+00:00"
        assert via_revisions(name) == parsed
    assert session_started_at("not-a-session") is None


def test_an_empty_freshly_rotated_session_does_not_win(tmp_path: Path) -> None:
    """A session directory exists before its first chunk flushes.

    Without this the newest directory is briefly an empty one and every live
    read returns nothing — the same "shows the wrong session" failure in a new
    costume. Falling back to the last session holding data is correct and
    self-correcting once the first chunk lands.
    """
    from src.storage.sessions import latest_session

    root = tmp_path / "vehicle"
    has_data = root / "2026-08-05T120000"
    has_data.mkdir(parents=True)
    (has_data / "telemetry-00000.parquet").write_bytes(b"x")
    (root / "_capture-fuel-map-2026-08-05T130000").mkdir()  # rotated, nothing flushed

    live = latest_session(root)
    assert live is not None and live.name == "2026-08-05T120000"


def test_a_stale_obd_rpm_does_not_beat_a_live_broadcast(tmp_path: Path) -> None:
    """A second tester on the bus must not make a running engine read `engine_off`.

    Observed live 2026-08-07: a Z1 scan tool shared the OBD-II connector on a
    Y-cable, took the ECM's tester session, and canary's Mode 01 went
    unanswered. The last OBD rpm (750) sat 27 s stale while `decoded:ECM_RPM`
    reported 781 rpm at 96 Hz throughout. The fallback was gated on
    `rpm is None`, so a held-over reading suppressed it and the state read
    `engine_off` — "plugged into the bus, key out" — with the engine idling.

    A stale OBD rpm is no more informative than a missing one, and worse
    because it looks like data.
    """
    from datetime import UTC, datetime

    from src.dashboard.status import _derive_vehicle_state
    from src.storage.parquet_writer import write_records_to_parquet
    from src.telemetry import TelemetryRecord

    now = datetime(2026, 8, 7, 20, 0, 30, tzinfo=UTC)
    storage = tmp_path / "vehicle"
    session = storage / "2026-08-07T200000"
    write_records_to_parquet(
        [
            # OBD rpm, 27 s old — present, plausible, and useless.
            TelemetryRecord(
                timestamp=now.replace(second=3),
                source="obd",
                channel="RPM",
                value=750.0,
            ),
            # Broadcast rpm, current: 781 rpm x 8 counts.
            _broadcast_rpm(now.replace(second=29), 6248),
        ],
        session,
    )

    state = _derive_vehicle_state(storage, replay_controller=None, now=now, session_dir=session)

    assert state.state == "engine_running"
    assert state.rpm == 781.0


def test_broadcast_fallback_still_reports_a_genuinely_stopped_engine(tmp_path: Path) -> None:
    """The fallback must not manufacture a running engine out of a parked one.

    Same shape as above — OBD stale, CAN live — but the broadcast says 0 rpm,
    which is the real `engine_off` case the rule exists for.
    """
    from datetime import UTC, datetime

    from src.dashboard.status import _derive_vehicle_state
    from src.storage.parquet_writer import write_records_to_parquet
    from src.telemetry import TelemetryRecord

    now = datetime(2026, 8, 7, 20, 0, 30, tzinfo=UTC)
    storage = tmp_path / "vehicle"
    session = storage / "2026-08-07T200000"
    write_records_to_parquet(
        [
            TelemetryRecord(
                timestamp=now.replace(second=3), source="obd", channel="RPM", value=750.0
            ),
            _broadcast_rpm(now.replace(second=29), 0),
        ],
        session,
    )

    state = _derive_vehicle_state(storage, replay_controller=None, now=now, session_dir=session)
    assert state.state == "engine_off"


def test_fresh_obd_still_wins_over_the_broadcast(tmp_path: Path) -> None:
    """The fallback is for stale OBD only; a fresh PID stays the primary."""
    from datetime import UTC, datetime

    from src.dashboard.status import _derive_vehicle_state
    from src.storage.parquet_writer import write_records_to_parquet
    from src.telemetry import TelemetryRecord

    now = datetime(2026, 8, 7, 20, 0, 30, tzinfo=UTC)
    storage = tmp_path / "vehicle"
    session = storage / "2026-08-07T200000"
    write_records_to_parquet(
        [
            TelemetryRecord(
                timestamp=now.replace(second=29), source="obd", channel="RPM", value=900.0
            ),
            _broadcast_rpm(now.replace(second=29), 6248),
        ],
        session,
    )

    state = _derive_vehicle_state(storage, replay_controller=None, now=now, session_dir=session)
    assert state.rpm == 900.0
    assert state.state == "engine_running"


# ---------------------------------------------------------------------------
# R03.1 — the broadcast-RPM fallback lives in the classifier's signature
# ---------------------------------------------------------------------------


def test_broadcast_rpm_is_a_required_argument() -> None:
    """No caller can skip the fallback: leaving ``broadcast_rpm`` out is a
    TypeError, not a silent OBD-only classification (the inference copy's
    bug)."""
    import pytest

    kwargs = _common_telemetry(rpm=800.0, obd_freshness_seconds=0.2)
    del kwargs["broadcast_rpm"]
    with pytest.raises(TypeError):
        classify_vehicle_state(**kwargs)  # type: ignore[arg-type]


def test_classifier_uses_a_fresh_broadcast_when_obd_is_stale() -> None:
    """The all-DID capture: Mode 01 dark by design, the broadcast live."""
    state = classify_vehicle_state(
        **_common_telemetry(
            rpm=750.0,
            broadcast_rpm=2500.0,
            obd_freshness_seconds=40.0,
            can_freshness_seconds=0.05,
        )
    )
    assert state.state == "engine_running"
    assert state.rpm == 2500.0


def test_classifier_uses_the_broadcast_when_obd_rpm_is_absent() -> None:
    state = classify_vehicle_state(
        **_common_telemetry(
            broadcast_rpm=2500.0, obd_freshness_seconds=None, can_freshness_seconds=0.05
        )
    )
    assert state.state == "engine_running"


def test_fresh_obd_rpm_outranks_the_broadcast() -> None:
    state = classify_vehicle_state(
        **_common_telemetry(
            rpm=900.0,
            broadcast_rpm=2500.0,
            obd_freshness_seconds=0.2,
            can_freshness_seconds=0.05,
        )
    )
    assert state.rpm == 900.0


def test_a_stale_broadcast_is_not_engine_evidence() -> None:
    """The freshness rule R03.1 chose: the fallback needs CAN freshness.

    The Parquet derivation used to take a stale decoded rpm whenever the OBD
    rpm was unusable; with OBD fresh but RPM never polled, a minutes-old
    broadcast rpm then read ``engine_running``.
    """
    state = classify_vehicle_state(
        **_common_telemetry(
            broadcast_rpm=2500.0, obd_freshness_seconds=0.2, can_freshness_seconds=60.0
        )
    )
    assert state.state == "ignition_only"
    assert state.rpm is None


def test_the_three_derivations_agree_on_an_all_did_capture(tmp_path: Path) -> None:
    """Parquet, live-source and inference-gate derivations of one instant."""
    from datetime import UTC, datetime

    from src.dashboard.status import derive_vehicle_state_from_storage
    from src.storage.parquet_writer import write_records_to_parquet
    from src.vehicle_state import derive_from_inputs

    now = datetime(2026, 9, 24, 12, 0, 30, tzinfo=UTC)
    storage = tmp_path / "vehicle"
    capture = storage / "_capture-tuning-did-2026-09-24T120000"
    write_records_to_parquet(
        [
            _broadcast_rpm(now.replace(second=29), 20000),
        ],
        capture,
    )
    parquet = derive_vehicle_state_from_storage(
        storage, replay_controller=None, now=now, session_dir=capture
    )
    live = derive_from_inputs(
        {
            "rpm": None,
            "broadcast_rpm": 2500.0,
            "coolant_temp": None,
            "run_time": None,
            "obd_freshness_seconds": None,
            "can_freshness_seconds": 1.0,
        },
        None,
        now,
    )
    assert parquet.state == live.state == "engine_running"
    assert parquet.rpm == live.rpm == 2500.0


# --- D03.30: while passive, vehicle_state names passive ----------------------


def test_passive_turns_a_confident_engine_off_into_passive() -> None:
    """CAN fresh, OBD stale because CANary is not asking: KOEO or running,
    this used to read ``engine_off``."""
    state = classify_vehicle_state(
        **_common_telemetry(obd_freshness_seconds=120.0, can_freshness_seconds=0.2),
        passive=True,
    )
    assert state.state == "passive"
    assert state.passive is True
    assert state.to_dict()["passive"] is True
    assert state.to_dict()["can_freshness_seconds"] == 0.2


def test_passive_keeps_verdicts_that_rest_on_evidence() -> None:
    running = classify_vehicle_state(
        **_common_telemetry(
            broadcast_rpm=780.0, obd_freshness_seconds=120.0, can_freshness_seconds=0.2
        ),
        passive=True,
    )
    assert running.state == "engine_running"
    assert running.passive is True
    silent = classify_vehicle_state(**_common_telemetry(), passive=True)
    assert silent.state == "no_data"


def test_the_derivations_read_passive_from_the_gate() -> None:
    """No caller passes ``passive``: the in-memory and Parquet derivations
    read :mod:`src.collection.tx_gate` themselves."""
    from datetime import UTC, datetime, timedelta

    from src.collection import tx_gate
    from src.vehicle_state import derive_from_inputs, derive_from_latest_rows

    now = datetime(2026, 9, 24, 16, 53, 40, tzinfo=UTC)
    inputs = {"obd_freshness_seconds": 60.0, "can_freshness_seconds": 0.1}
    rows = [
        {
            "source": "can",
            "channel": "0x180",
            "value": None,
            "timestamp": (now - timedelta(seconds=0.1)).isoformat(),
        }
    ]
    assert derive_from_inputs(inputs, None, now).state == "engine_off"
    assert derive_from_latest_rows(rows, replay_id=None, now=now).state == "engine_off"
    tx_gate.set_passive(True, source="control")
    assert derive_from_inputs(inputs, None, now).state == "passive"
    assert derive_from_latest_rows(rows, replay_id=None, now=now).state == "passive"
