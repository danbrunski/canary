"""Tests for the derived ``vehicle_state`` block in ``GET /status``.

The classifier rule table is documented in ``classify_vehicle_state``;
these tests pin the truth table so a future tweak to ``engine_off`` vs.
``ignition_only`` thresholds can't silently regress the live-vs-replay
disambiguation the agent docs rely on.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from src.dashboard.status import classify_vehicle_state
from src.paths import REPO_ROOT
from src.telemetry import TelemetryRecord


def _broadcast_rpm(timestamp: datetime, counts: int) -> TelemetryRecord:
    """The ECM's 0x1F9 frame carrying ``counts`` (8 per rpm) at bytes 2-3, the
    ``ECM_RPM`` binding. Sessions store the frame, never a ``decoded`` row
    (P02.8); the Parquet reader decodes it on read."""
    payload = bytes([0x20, 0x00, (counts >> 8) & 0xFF, counts & 0xFF, 0, 0, 0, 0])
    return TelemetryRecord(timestamp=timestamp, source="can", channel="0x1F9", payload=payload)


def _common_telemetry(**overrides: object) -> dict[str, Any]:
    """Classifier kwargs; ``rpm`` is shorthand for the Mode 01 ``obd_rpm``."""
    base: dict[str, Any] = {
        "obd_rpm": None,
        "broadcast_rpm": None,
        "coolant_temp": None,
        "run_time": None,
        "obd_freshness_seconds": None,
        "can_freshness_seconds": None,
        "supply_voltage": None,
        "supply_freshness_seconds": None,
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


def test_parked_when_both_buses_sleep_on_a_live_battery() -> None:
    """2026-10-04: the Xterra's bus sleeps at key-off. Hours parked read
    ``no_data`` ("Pi probably disconnected") while the daemon answered; the
    HAT's 12 V sense is the evidence that tells the two apart."""
    state = classify_vehicle_state(
        **_common_telemetry(
            obd_freshness_seconds=9000.0,
            can_freshness_seconds=9000.0,
            supply_voltage=12.41,
            supply_freshness_seconds=0.1,
        )
    )
    assert state.state == "parked"
    assert state.supply_voltage == 12.41
    assert state.to_dict()["supply_voltage"] == 12.41


def test_bench_when_no_vehicle_is_on_the_port() -> None:
    """USB-C or UPS power with no vehicle (2026-10-05): the 12 V sense reads
    ~0. That is the desk setup, not a fault."""
    state = classify_vehicle_state(
        **_common_telemetry(supply_voltage=0.01, supply_freshness_seconds=0.1)
    )
    assert state.state == "bench"


def test_no_data_when_the_supply_is_stale_ambiguous_or_absent() -> None:
    for supply, age in ((12.4, 60.0), (0.3, 60.0), (5.0, 0.1), (None, None)):
        state = classify_vehicle_state(
            **_common_telemetry(supply_voltage=supply, supply_freshness_seconds=age)
        )
        assert state.state == "no_data", (supply, age)
        assert state.supply_voltage == (supply if age is not None and age <= 10 else None)


def test_a_live_bus_outranks_the_supply() -> None:
    state = classify_vehicle_state(
        **_common_telemetry(
            rpm=0.0,
            obd_freshness_seconds=0.5,
            supply_voltage=12.4,
            supply_freshness_seconds=0.1,
        )
    )
    assert state.state == "ignition_only"


def test_parked_from_stored_rows() -> None:
    from datetime import UTC

    from src.vehicle_state import derive_from_latest_rows

    now = datetime(2026, 10, 5, 3, 0, tzinfo=UTC)
    rows: list[dict[str, Any]] = [
        {
            "source": "can",
            "channel": "0x1F9",
            "value": None,
            "timestamp": "2026-10-05T00:02:17+00:00",
        },
        {
            "source": "sensor",
            "channel": "SUPPLY_VOLTAGE",
            "value": 12.38,
            "timestamp": "2026-10-05T02:59:59.9+00:00",
        },
    ]
    state = derive_from_latest_rows(rows, replay_id=None, now=now, passive=False)
    assert state.state == "parked"
    assert state.can_freshness_seconds == pytest.approx(10663.0)


def test_supply_voltage_is_a_required_argument() -> None:
    kwargs = _common_telemetry()
    del kwargs["supply_voltage"]
    with pytest.raises(TypeError):
        classify_vehicle_state(**kwargs)


def test_broadcast_rpm_is_a_required_argument() -> None:
    """No caller can skip the fallback: leaving ``broadcast_rpm`` out is a
    TypeError, not a silent OBD-only classification (the inference copy's
    bug)."""
    import pytest

    kwargs = _common_telemetry(rpm=800.0, obd_freshness_seconds=0.2)
    del kwargs["broadcast_rpm"]
    with pytest.raises(TypeError):
        classify_vehicle_state(**kwargs)


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


# --- R29.14: one "engine running" definition and named state groups ----------


def _pre_r29_14_state(**kw: Any) -> str:
    """The rule table as it stood before R29.14, copied verbatim, so the grid
    below proves the new ``broadcast_rpm`` field changed no verdict."""
    from src.collection.vehicle_power import classify_supply
    from src.vehicle_state import FRESHNESS_THRESHOLD_SECONDS, RPM_RUNNING_THRESHOLD

    def fresh(age: float | None) -> bool:
        return age is not None and age <= FRESHNESS_THRESHOLD_SECONDS

    obd_fresh = fresh(kw["obd_freshness_seconds"])
    can_fresh = fresh(kw["can_freshness_seconds"])
    supply = kw["supply_voltage"] if fresh(kw["supply_freshness_seconds"]) else None
    rpm = kw["obd_rpm"]
    from_broadcast = False
    if not (obd_fresh and rpm is not None) and kw["broadcast_rpm"] is not None and can_fresh:
        rpm, from_broadcast = kw["broadcast_rpm"], True
    if kw["replay_id"] is not None:
        return "replay"
    if not obd_fresh and not can_fresh:
        port = classify_supply(supply)
        state = "parked" if port == "vehicle" else "bench" if port == "bench" else "no_data"
    elif (obd_fresh or from_broadcast) and rpm is not None and rpm >= RPM_RUNNING_THRESHOLD:
        state = "engine_running"
    elif obd_fresh and (rpm is None or rpm == 0):
        state = "ignition_only"
    elif can_fresh and not obd_fresh:
        state = "engine_off"
    else:
        state = "unknown"
    if kw["passive"] and state in ("engine_off", "unknown"):
        state = "passive"
    return state


def test_no_verdict_changed_when_broadcast_rpm_joined_the_state() -> None:
    import itertools

    ages = (None, 1.0, 20.0)
    grid = itertools.product(
        (None, 0.0, 200.0, 800.0),  # obd_rpm
        (None, 0.0, 200.0, 800.0),  # broadcast_rpm
        ages,  # obd age
        ages,  # can age
        ((None, None), (12.6, 1.0), (0.5, 1.0), (12.6, 20.0)),  # supply, age
        (None, "r1"),
        (False, True),
    )
    checked = 0
    for obd, broadcast, obd_age, can_age, (volts, volts_age), replay, passive in grid:
        kw: dict[str, Any] = {
            "obd_rpm": obd,
            "broadcast_rpm": broadcast,
            "coolant_temp": None,
            "run_time": None,
            "obd_freshness_seconds": obd_age,
            "can_freshness_seconds": can_age,
            "supply_voltage": volts,
            "supply_freshness_seconds": volts_age,
            "replay_id": replay,
            "passive": passive,
        }
        state = classify_vehicle_state(**kw)
        assert state.state == _pre_r29_14_state(**kw), kw
        can_fresh = can_age is not None and can_age <= 10.0
        assert state.broadcast_rpm == (broadcast if can_fresh else None), kw
        checked += 1
    assert checked == 4 * 4 * 3 * 3 * 4 * 2 * 2


def test_broadcast_rpm_rides_on_the_state_and_its_dict_only_while_can_is_fresh() -> None:
    fresh = classify_vehicle_state(
        **_common_telemetry(
            rpm=0.0,
            broadcast_rpm=812.3456,
            obd_freshness_seconds=1.0,
            can_freshness_seconds=0.2,
        )
    )
    assert fresh.state == "ignition_only"  # the verdict still prefers fresh OBD
    assert fresh.rpm == 0.0
    assert fresh.broadcast_rpm == 812.3456
    assert fresh.to_dict()["broadcast_rpm"] == 812.35
    stale = classify_vehicle_state(
        **_common_telemetry(
            rpm=0.0,
            broadcast_rpm=812.0,
            obd_freshness_seconds=1.0,
            can_freshness_seconds=30.0,
        )
    )
    assert stale.broadcast_rpm is None
    assert stale.to_dict()["broadcast_rpm"] is None


def test_every_derivation_carries_the_fresh_broadcast_rpm(tmp_path: Path) -> None:
    """Parquet and live derivations of one instant just after a start: the last
    OBD ``0`` is a second old and the broadcast reads 800."""
    from datetime import UTC, datetime

    from src.dashboard.status import derive_vehicle_state_from_storage
    from src.storage.parquet_writer import write_records_to_parquet
    from src.vehicle_state import derive_from_inputs, derive_from_live_source

    now = datetime(2026, 11, 12, 12, 0, 30, tzinfo=UTC)
    storage = tmp_path / "vehicle"
    session = storage / "2026-11-12T120000"
    write_records_to_parquet(
        [
            TelemetryRecord(
                timestamp=now.replace(second=29), source="obd", channel="RPM", value=0.0
            ),
            _broadcast_rpm(now.replace(second=29), 6400),  # 800 rpm x 8 counts
        ],
        session,
    )
    parquet = derive_vehicle_state_from_storage(
        storage, replay_controller=None, now=now, session_dir=session
    )
    inputs = {
        "rpm": 0.0,
        "broadcast_rpm": 800.0,
        "obd_freshness_seconds": 1.0,
        "can_freshness_seconds": 1.0,
    }
    live = derive_from_inputs(inputs, None, now, passive=False)

    class _Source:
        def vehicle_state_inputs(self, _now: datetime) -> dict[str, float | None]:
            return dict(inputs)

    from_source = derive_from_live_source(_Source(), replay_controller=None, now=now)
    for state in (parquet, live, from_source):
        assert state.state == "ignition_only"
        assert state.broadcast_rpm == 800.0

    stale_now = now.replace(minute=1)  # 31 s later: CAN no longer fresh
    stale = derive_vehicle_state_from_storage(
        storage, replay_controller=None, now=stale_now, session_dir=session
    )
    assert stale.broadcast_rpm is None


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        (
            {"rpm": 0.0, "broadcast_rpm": 800.0, "obd_freshness_seconds": 1.0},
            "broadcast engine speed 800 rpm >= 400 (vehicle_state ignition_only)",
        ),
        (
            {"rpm": 750.0, "broadcast_rpm": None, "obd_freshness_seconds": 1.0},
            "vehicle_state engine_running, rpm 750",
        ),
        ({"rpm": 0.0, "broadcast_rpm": 399.0, "obd_freshness_seconds": 1.0}, None),
        ({"rpm": 0.0, "broadcast_rpm": 0.0, "obd_freshness_seconds": 1.0}, None),
        # A stale broadcast is no evidence: the classifier drops it.
        (
            {
                "rpm": 0.0,
                "broadcast_rpm": 800.0,
                "obd_freshness_seconds": 1.0,
                "can_freshness_seconds": 30.0,
            },
            None,
        ),
    ],
)
def test_engine_running_evidence_reads_the_verdict_or_a_fresh_broadcast(
    kw: dict[str, Any], expected: str | None
) -> None:
    from src.vehicle_state import engine_running_evidence

    telemetry = {"can_freshness_seconds": 0.2, **kw}
    state = classify_vehicle_state(**_common_telemetry(**telemetry))
    assert engine_running_evidence(state) == expected


def test_the_write_guard_and_the_rom_backup_share_one_engine_running_definition() -> None:
    import inspect

    from src.control import nonvolatile_write, rom_backup

    for module in (nonvolatile_write, rom_backup):
        assert "engine_running_evidence(" in inspect.getsource(module), module.__name__
    reading = rom_backup.VehicleReading(state="ignition_only", rpm=0.0, broadcast_rpm=800.0)
    assert rom_backup._engine_running(reading) == (
        "broadcast engine speed 800 rpm >= 400 (vehicle_state ignition_only)"
    )


def test_named_state_groups_keep_their_membership() -> None:
    """Membership moved into ``src.vehicle_state`` unchanged (R29.14)."""
    from src.vehicle_state import (
        ECM_SILENT_STATES,
        ENGINE_ON_STATES,
        IDLE_STATES,
        KEY_ON_STATES,
        QUIET_STATES,
        SCORABLE_STATES,
        UNTRUSTED_STATES,
    )

    assert ENGINE_ON_STATES == {"engine_running"}
    assert KEY_ON_STATES == {"ignition_only", "engine_running"}
    assert UNTRUSTED_STATES == {"replay", "passive", "unknown"}
    assert QUIET_STATES == {"parked", "bench", "no_data"}
    assert ECM_SILENT_STATES == {"engine_off", "parked", "bench", "no_data"}
    assert IDLE_STATES == {"engine_off", "parked", "no_data"}
    assert SCORABLE_STATES == {"engine_running", "replay"}


def test_consumers_use_the_named_groups() -> None:
    from src.config.collection import IdleShutdownConfig
    from src.control import capture_autostart, key_cycle_pairing
    from src.daemon import idle_shutdown, inference
    from src.vehicle_state import (
        ENGINE_ON_STATES,
        IDLE_STATES,
        KEY_ON_STATES,
        SCORABLE_STATES,
        UNTRUSTED_STATES,
    )

    assert capture_autostart.RUNNING_STATES is ENGINE_ON_STATES
    assert key_cycle_pairing.ON_STATES is KEY_ON_STATES
    assert key_cycle_pairing.UNTRUSTED_STATES is UNTRUSTED_STATES
    assert inference._SCORABLE_VEHICLE_STATES is SCORABLE_STATES
    assert set(idle_shutdown.DEFAULT_ACTIVE_STATES) == KEY_ON_STATES
    assert set(idle_shutdown.DEFAULT_IDLE_STATES) == IDLE_STATES
    # ``src.config`` sits below ``src.vehicle_state`` (tests/test_layering.py),
    # so its default cannot import the group; it is pinned to it instead.
    assert set(IdleShutdownConfig().idle_states) == IDLE_STATES


#: String literals naming two or more vehicle states that may stay outside
#: ``src/vehicle_state.py``, by file. May only shrink.
_STATE_SET_LITERALS_ALLOWED: dict[str, int] = {
    # Layer 0: cannot import src.vehicle_state; pinned to IDLE_STATES above.
    "src/config/collection.py": 1,
    # The ROM read's own mid-read rule (KOEO, or passive while it holds the
    # bus), not a shared meaning.
    "src/control/rom_backup.py": 1,
}


def test_no_new_string_set_of_state_names_outside_vehicle_state() -> None:
    """Regrowth guard (R29.14): a set, tuple or list literal of two or more
    distinct vehicle-state names belongs in ``src.vehicle_state`` as a named
    group. ``parked`` (2026-10-04) took several edits because each consumer
    restated its own."""
    import ast
    from typing import get_args

    from src.vehicle_state import VehicleStateName

    names = set(get_args(VehicleStateName))
    root = REPO_ROOT
    found: dict[str, int] = {}
    for path in sorted((root / "src").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel == "src/vehicle_state.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, (ast.Set, ast.Tuple, ast.List)) or len(node.elts) < 2:
                continue
            values = [e.value for e in node.elts if isinstance(e, ast.Constant)]
            if len(values) != len(node.elts) or not all(isinstance(v, str) for v in values):
                continue
            if set(values) <= names and len(set(values)) >= 2:
                found[rel] = found.get(rel, 0) + 1
    assert found == _STATE_SET_LITERALS_ALLOWED
