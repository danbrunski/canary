"""B06.4: the live lambda delivery readout gives the offline engine's rows.

The offline engine (:func:`src.analysis.lambda_delivery.delivery_samples`) and
the live readout (:mod:`src.daemon.lambda_delivery_live`) share one row judge
(``judge_target_rows``). These tests hold them to the same rows — pairing,
state and residual — on real tuning captures: the committed slice of
``_capture-tuning-2026-09-21T223734`` (every run), and a held-out capture
under ``~/canary-data`` when it is on this machine. The live side is driven
the way the daemon drives it: raw records through
``LiveFeatureSource.observe`` and a tick per second of data.

The readout is a tuning readout, not a detector: it runs under a tuning
capture (R03.12 gates learned-normal detectors only) and never alerts.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.alerts.event_logger import read_events_dir
from src.analysis.lambda_delivery import (
    STATES,
    JudgedRows,
    LambdaDeliveryDeclaration,
    _long_values,
    delivery_samples,
    load_lambda_delivery,
)
from src.analysis.lambda_delivery_stream import DeliveryStream
from src.analysis.noise_floor import load_noise_floor
from src.analysis.tuning import DeliveryRoute, InputsNotCapturedError, read_delivery_inputs
from src.analysis.tuning_context import TuningContext
from src.collection import tuning_profile
from src.collection.capture_profiles import CaptureProfile
from src.collection.replay import _metadata, _value
from src.daemon import build_components
from src.daemon.inference_metrics import InferenceTimingRecorder
from src.daemon.lambda_delivery_live import (
    LambdaDeliveryReadout,
    LiveLambdaDelivery,
    build_live_lambda_delivery,
)
from src.decode.dids import load_did_map
from src.features.live import LiveFeatureSource
from src.paths import DIDS_YAML
from src.preprocessing.loader import read_session
from src.signals.afr import AfrCalibration, load_afr_calibration
from src.telemetry import TelemetryRecord
from tests.configs import isolated_config

REPO = Path(__file__).resolve().parents[1]
PROFILE = REPO / "data" / "nissan-xterra-2008"
SLICE = REPO / "tests" / "fixtures" / "lambda_delivery" / "_capture-tuning-2026-09-21T223734"
#: A clean held-out capture of R05.16's enrichment rule (not in the slice).
HELD_OUT = (
    Path.home()
    / "canary-data"
    / "sessions"
    / "nissan-xterra-2008"
    / "_capture-tuning-2026-09-22T235224"
)
#: B06.7: the same capture's PID 0x33 rows, a companion slice, so the voltage
#: routes convert at the ECM's barometric gain (the slice above has no baro).
BARO_SLICE = REPO / "tests" / "fixtures" / "af_baro_gain" / "_capture-tuning-2026-09-21T223734"
STOICH = 14.7


def _slice() -> pd.DataFrame:
    """The committed capture with its baro rows, in stored order."""
    rows = pd.concat(
        [
            read_session(SLICE, filters=[("source", "==", "obd")]),
            read_session(BARO_SLICE, filters=[("source", "==", "obd")]),
        ],
        ignore_index=True,
    )
    return rows.sort_values("timestamp", kind="stable").reset_index(drop=True)


@pytest.fixture(scope="module")
def declaration() -> LambdaDeliveryDeclaration:
    return load_lambda_delivery(PROFILE / "lambda_delivery.yaml")


@pytest.fixture(scope="module")
def calibration() -> AfrCalibration:
    return load_afr_calibration(PROFILE / "afr_calibration.yaml")


def _routes(declaration: LambdaDeliveryDeclaration) -> list[tuple[str, str]]:
    return [
        (c.name, m.name)
        for c in declaration.commanded_sources
        for m in declaration.measured_sources
    ]


def _offline(
    session: pd.DataFrame,
    declaration: LambdaDeliveryDeclaration,
    calibration: AfrCalibration,
    target: str,
    measured: str,
) -> tuple[pd.DataFrame, Any]:
    """The offline engine's rows, read the way ``delivery_error`` reads them."""
    route = DeliveryRoute(target, measured, "obd:RPM", declaration.measured(measured).spec)
    inputs = read_delivery_inputs(
        session, route, declaration, calibration=calibration, afr_stoich=STOICH
    )
    samples = delivery_samples(
        target=inputs.target,
        measured=inputs.measured,
        sentinel=inputs.sentinel,
        declaration=declaration,
        target_source=target,
        measured_source=measured,
        calibration=calibration,
        afr_stoich=STOICH,
        baro=inputs.baro,
    )
    return samples.frame.reset_index(drop=True), inputs


def _same_rows(live: pd.DataFrame, offline: pd.DataFrame) -> None:
    # Values exactly; the string column's dtype differs only by how pandas
    # infers it when a batch is empty.
    pd.testing.assert_frame_equal(
        live.reset_index(drop=True), offline, check_exact=True, check_dtype=False
    )


def _concat(batches: list[JudgedRows]) -> JudgedRows:
    """Every batch a route published, as one block of rows."""
    return JudgedRows(
        **{
            name: np.concatenate([getattr(batch, name) for batch in batches])
            for name in JudgedRows.__dataclass_fields__
        }
    )


def _records(session: pd.DataFrame) -> Iterator[TelemetryRecord]:
    """The stored polled rows as the collector's records, in stored order."""
    stamps = pd.to_datetime(session["timestamp"], utc=True)
    for (_, row), stamp in zip(session.iterrows(), stamps, strict=True):
        unit = row.get("unit")
        yield TelemetryRecord(
            timestamp=stamp.to_pydatetime(),
            source=str(row["source"]),  # type: ignore[arg-type]
            channel=str(row["channel"]),
            value=_value(row),
            unit="" if unit is None or pd.isna(unit) else str(unit),
            metadata=_metadata(row),
        )


def _readout(
    declaration: LambdaDeliveryDeclaration,
    calibration: AfrCalibration,
    rows: dict[tuple[str, str], list[JudgedRows]] | None = None,
) -> LambdaDeliveryReadout:
    def sink(target: str, measured: str, judged: JudgedRows) -> None:
        if rows is not None:
            rows.setdefault((target, measured), []).append(judged)

    return LambdaDeliveryReadout(
        declaration,
        calibration=calibration,
        afr_stoich=STOICH,
        noise_floor=load_noise_floor(PROFILE / "noise_floor.yaml"),
        did_map=load_did_map(DIDS_YAML),
        declaration_path=PROFILE / "lambda_delivery.yaml",
        session="capture",
        on_rows=sink,
    )


def _drive_live(
    tmp_path: Path, session: pd.DataFrame, readout: LambdaDeliveryReadout
) -> dict[str, Any]:
    """Records through a real LiveFeatureSource, one tick per data second."""
    recorder = InferenceTimingRecorder()
    live = LiveLambdaDelivery(readout, recorder=recorder)
    source = LiveFeatureSource(config=isolated_config(tmp_path, profiles={"enabled": False}))
    live.attach(source)
    next_tick: datetime | None = None
    for record in _records(session):
        if next_tick is None:
            next_tick = record.timestamp + timedelta(seconds=1)
        while record.timestamp >= next_tick:
            live.tick(session="capture")
            next_tick += timedelta(seconds=1)
        source.observe(record)
    live.tick(session="capture")
    readout.flush()
    block = recorder.snapshot().to_dict()["lambda_delivery"]
    assert block is not None
    return {
        "block": live.snapshot(),
        "recorded": block,
        "failures": live.observe_failures,
    }


def _assert_live_matches_offline(
    tmp_path: Path,
    session: pd.DataFrame,
    declaration: LambdaDeliveryDeclaration,
    calibration: AfrCalibration,
) -> dict[tuple[str, str], int]:
    rows: dict[tuple[str, str], list[JudgedRows]] = {}
    readout = _readout(declaration, calibration, rows)
    result = _drive_live(tmp_path, session, readout)
    assert result["failures"] == 0
    routes = {(r["target_source"], r["measured_source"]): r for r in result["block"]["routes"]}
    eligible: dict[tuple[str, str], int] = {}
    for key in _routes(declaration):
        published = routes[key]
        try:
            offline, _inputs = _offline(session, declaration, calibration, *key)
        except InputsNotCapturedError as exc:
            # A declared route this capture never polled (e.g. bank 2's PID
            # 0x28 before 2026-10-04): the offline engine refuses it, and the
            # live route must say the same thing rather than publish rows.
            # The live route still decides its target rows (as unpaired), but
            # none may carry a pairing or a residual.
            assert published["result"] == "inputs_not_observed"
            assert published["missing_inputs"] == list(exc.missing)
            assert published["paired"] == 0
            assert published["states"]["eligible"] == 0
            if key in rows:
                decided = _concat(rows[key])
                assert (decided.paired < 0).all()
                assert np.isnan(decided.residual_pct).all()
            continue
        live = _concat(rows[key]).to_frame()
        _same_rows(live, offline)
        assert set(published["late_reads"].values()) == {0}
        assert published["undecided_rows"] == 0
        counts = offline["state"].value_counts().to_dict()
        assert published["states"] == {
            **{name: int(counts.get(name, 0)) for name in STATES},
            "total": len(offline),
        }
        for regime in ("closed_loop", "enrichment"):
            values = offline.loc[
                (offline["state"] == "eligible") & (offline["regime"] == regime), "residual_pct"
            ].to_numpy(dtype=float)
            stats = published["regimes"][regime]
            assert stats["n"] == len(values)
            if len(values):
                assert stats["median_pct"] == float(np.median(values))
                assert stats["p95_pct"] == float(np.quantile(values, 0.95))
        eligible[key] = int(counts.get("eligible", 0))
    return eligible


# ---------------------------------------------------------------------------
# Parity on real captures


@pytest.mark.slow  # ~25 s: a whole capture through the live path
def test_live_readout_gives_the_offline_rows_on_a_committed_tuning_capture(
    tmp_path: Path, declaration: LambdaDeliveryDeclaration, calibration: AfrCalibration
) -> None:
    """Every declared route, every row: same pairing, state and residual.

    The slice is the whole 2026-09-21T223734 capture's polled delivery
    channels (1,078 RAM-target and 4,035 PID 0x44 rows).
    """
    session = _slice()
    eligible = _assert_live_matches_offline(tmp_path, session, declaration, calibration)
    # A parity test over empty results would prove nothing.
    assert eligible[("ram_final_target", "ecm_lambda_pid_0x24")] == 560
    assert eligible[("mode01_pid_0x44", "ecm_lambda_pid_0x24")] == 229


@pytest.mark.slow  # ~40 s: a whole held-out capture through the live path
@pytest.mark.skipif(not HELD_OUT.exists(), reason="held-out capture not on this machine")
def test_live_readout_gives_the_offline_rows_on_a_held_out_capture(
    tmp_path: Path, declaration: LambdaDeliveryDeclaration, calibration: AfrCalibration
) -> None:
    """The same on a whole capture no delivery constant was chosen on."""
    session = read_session(HELD_OUT, filters=[("source", "==", "obd")])
    eligible = _assert_live_matches_offline(tmp_path, session, declaration, calibration)
    assert eligible[("mode01_pid_0x44", "ecm_lambda_pid_0x24")] > 0


@pytest.mark.slow  # 2-6 s per seed: a whole capture, irregular ticks
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_the_stream_decides_the_same_rows_whatever_the_tick_cadence(
    seed: int, declaration: LambdaDeliveryDeclaration, calibration: AfrCalibration
) -> None:
    """Irregular ticks (0.2-6 s apart) decide exactly the whole-drive rows."""
    session = _slice()
    rng = np.random.default_rng(seed)
    for target, measured in [
        ("mode01_pid_0x44", "ecm_lambda_pid_0x24"),
        ("ram_final_target", "af_voltage_b1"),
    ]:
        offline, inputs = _offline(session, declaration, calibration, target, measured)
        tt, tv = _long_values(inputs.target)
        st, _ = _long_values(inputs.sentinel)
        mt, mv = _long_values(inputs.measured)
        bt, bv = _long_values(inputs.baro) if inputs.baro is not None else (mt[:0], mv[:0])
        stream = DeliveryStream(
            declaration,
            target_source=target,
            measured_source=measured,
            calibration=calibration,
            afr_stoich=STOICH,
        )
        frames = []
        lo, end = -math.inf, max(tt.max(), mt.max())
        edge = min(tt.min(), mt.min())
        while lo < end:
            hi = edge
            stream.extend(
                target=(tt[(tt > lo) & (tt <= hi)], tv[(tt > lo) & (tt <= hi)]),
                sentinel=st[(st > lo) & (st <= hi)],
                measured=(mt[(mt > lo) & (mt <= hi)], mv[(mt > lo) & (mt <= hi)]),
                baro=(bt[(bt > lo) & (bt <= hi)], bv[(bt > lo) & (bt <= hi)]),
            )
            frames.append(stream.advance().to_frame())
            lo, edge = hi, hi + float(rng.uniform(0.2, 6.0))
        frames.append(stream.flush().to_frame())
        _same_rows(pd.concat(frames, ignore_index=True), offline)
        assert set(stream.late_reads.values()) == {0}


# ---------------------------------------------------------------------------
# Contract details


def _read(
    at: datetime, channel: str, value: object, *, acquired: float | None = None
) -> TelemetryRecord:
    metadata: dict[str, Any] = {} if acquired is None else {"acquired_at": acquired}
    return TelemetryRecord(at, "obd", channel, value, "", metadata=metadata)  # type: ignore[arg-type]


T0 = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)


def _route(block: dict[str, Any], target: str, measured: str) -> dict[str, Any]:
    return next(
        r
        for r in block["routes"]
        if r["target_source"] == target and r["measured_source"] == measured
    )


def test_every_empty_result_says_which_kind(
    declaration: LambdaDeliveryDeclaration, calibration: AfrCalibration
) -> None:
    readout = _readout(declaration, calibration)
    readout.tick()
    first = _route(readout.snapshot(), "mode01_pid_0x44", "ecm_lambda_pid_0x24")
    assert first["result"] == "inputs_not_observed"
    assert first["missing_inputs"] == ["obd:COMMANDED_EQUIV_RATIO", "obd:O2_WR_LAMBDA_S1"]

    # Target reads only: the measured side is named, not reported as zero rows.
    for i in range(10):
        readout.observe(_read(T0 + timedelta(seconds=i), "COMMANDED_EQUIV_RATIO", 1.16))
    readout.tick()
    only_target = _route(readout.snapshot(), "mode01_pid_0x44", "ecm_lambda_pid_0x24")
    assert only_target["result"] == "inputs_not_observed"
    assert only_target["missing_inputs"] == ["obd:O2_WR_LAMBDA_S1"]

    # Both read, never within 250 ms of each other.
    for i in range(10):
        readout.observe(_read(T0 + timedelta(seconds=i, milliseconds=500), "O2_WR_LAMBDA_S1", 0.87))
    readout.tick()
    apart = _route(readout.snapshot(), "mode01_pid_0x44", "ecm_lambda_pid_0x24")
    assert apart["result"] == "not_co_observed"
    assert apart["states"]["measured_missing"] == apart["states"]["total"] > 0

    # Paired, but a target that never held the enrichment dwell after a cut.
    readout.reset("next")
    for i in range(20):
        at = T0 + timedelta(seconds=i * 0.25)
        readout.observe(_read(at, "COMMANDED_EQUIV_RATIO", 0.0 if i == 0 else 1.16))
        readout.observe(_read(at, "O2_WR_LAMBDA_S1", 0.87))
    readout.tick()
    gated = _route(readout.snapshot(), "mode01_pid_0x44", "ecm_lambda_pid_0x24")
    assert gated["result"] == "no_eligible_rows"
    assert gated["states"]["fuel_cut"] == 1
    assert gated["states"]["post_fuel_cut"] > 0
    assert gated["undecided_rows"] > 0  # the newest reads wait for the settle margin


def test_round_stamped_reads_are_refused_not_paired(
    declaration: LambdaDeliveryDeclaration, calibration: AfrCalibration
) -> None:
    readout = _readout(declaration, calibration)
    for i in range(10):
        at = T0 + timedelta(seconds=i)
        readout.observe(
            TelemetryRecord(at, "obd", "COMMANDED_EQUIV_RATIO", 1.16, "", metadata={"round_id": i})
        )
        readout.observe(_read(at, "O2_WR_LAMBDA_S1", 0.87, acquired=at.timestamp()))
    readout.tick()
    route = _route(readout.snapshot(), "mode01_pid_0x44", "ecm_lambda_pid_0x24")
    assert route["result"] == "clock_unsupported"
    assert route["states"]["total"] == 0


def test_the_acquisition_clock_decides_not_the_stored_stamp(
    declaration: LambdaDeliveryDeclaration, calibration: AfrCalibration
) -> None:
    """Round stamps a second apart, acquired 10 ms apart: the reads pair."""
    readout = _readout(declaration, calibration)
    for i in range(12):
        stamp = T0 + timedelta(seconds=i)
        acquired = (T0 + timedelta(seconds=i * 0.25)).timestamp()
        readout.observe(_read(stamp, "COMMANDED_EQUIV_RATIO", 1.16, acquired=acquired))
        readout.observe(_read(stamp, "O2_WR_LAMBDA_S1", 0.87, acquired=acquired + 0.01))
    readout.flush()
    route = _route(readout.snapshot(), "mode01_pid_0x44", "ecm_lambda_pid_0x24")
    assert route["paired"] == 12
    enrichment = route["regimes"]["enrichment"]
    assert enrichment["n"] > 0
    assert enrichment["median_pct"] == pytest.approx(100 * (0.87 / (1 / 1.16) - 1))
    assert route["latest"]["state"] == "eligible"
    # B06.6's fields travel together.
    assert set(route["latest"]) >= {"target_lambda", "measured_lambda", "residual_pct", "regime"}


def test_a_late_read_is_counted_not_silently_folded(
    declaration: LambdaDeliveryDeclaration, calibration: AfrCalibration
) -> None:
    stream = DeliveryStream(
        declaration,
        target_source="mode01_pid_0x44",
        measured_source="ecm_lambda_pid_0x24",
        calibration=calibration,
        afr_stoich=STOICH,
    )
    times = np.arange(0.0, 20.0, 0.25)
    stream.extend(
        target=(times, np.full(len(times), 1.16)), measured=(times, np.full(len(times), 0.87))
    )
    decided = stream.advance()
    assert len(decided.rows) > 0
    stream.extend(measured=(np.array([5.1]), np.array([0.87])))
    assert stream.late_reads == {"target": 0, "sentinel": 0, "measured": 1}


def test_a_session_change_starts_a_new_drive(
    declaration: LambdaDeliveryDeclaration, calibration: AfrCalibration
) -> None:
    readout = _readout(declaration, calibration)
    live = LiveLambdaDelivery(readout, recorder=None)
    for i in range(40):
        at = T0 + timedelta(seconds=i * 0.25)
        readout.observe(_read(at, "COMMANDED_EQUIV_RATIO", 1.16))
        readout.observe(_read(at, "O2_WR_LAMBDA_S1", 0.87))
    live.tick(session="capture")
    assert _route(readout.snapshot(), "mode01_pid_0x44", "ecm_lambda_pid_0x24")["paired"] > 0
    live.tick(session="_replay-x")
    after = _route(readout.snapshot(), "mode01_pid_0x44", "ecm_lambda_pid_0x24")
    assert after["result"] == "inputs_not_observed"
    assert after["states"]["total"] == 0
    assert readout.session == "_replay-x"


def test_an_empty_measured_side_is_measured_missing_not_an_error(
    declaration: LambdaDeliveryDeclaration, calibration: AfrCalibration
) -> None:
    """The row judge names every unpaired row; it used to index an empty array."""
    empty = pd.DataFrame({"timestamp": pd.to_datetime([], utc=True), "value": []})
    target = pd.DataFrame(
        {"timestamp": pd.to_datetime([T0, T0 + timedelta(seconds=1)]), "value": [1.16, 1.16]}
    )
    samples = delivery_samples(
        target=target,
        measured=empty,
        declaration=declaration,
        target_source="mode01_pid_0x44",
        measured_source="ecm_lambda_pid_0x24",
        calibration=calibration,
        afr_stoich=STOICH,
    )
    assert samples.counts["measured_missing"] == samples.counts["total"] == 2


# ---------------------------------------------------------------------------
# Daemon wiring: a readout, not a detector


def _xterra_context(declaration: LambdaDeliveryDeclaration, calibration: AfrCalibration) -> Any:
    return lambda _config: TuningContext(
        afr_stoich=STOICH,
        calibration=calibration,
        noise_floor=load_noise_floor(PROFILE / "noise_floor.yaml"),
        lambda_delivery=declaration,
        lambda_delivery_path=PROFILE / "lambda_delivery.yaml",
    )


def test_it_runs_under_a_tuning_capture_and_never_alerts(
    tmp_path: Path, declaration: LambdaDeliveryDeclaration, calibration: AfrCalibration
) -> None:
    """R03.12 stops the learned-normal detectors in tuning captures; this
    readout is not one of them, and it has no path to the emitter."""
    config = isolated_config(tmp_path, "example", profiles={"enabled": False})
    components = build_components(config)
    tuning_profile.activate(
        CaptureProfile(
            name="tuning",
            mode01={"COMMANDED_EQUIV_RATIO": 4.0, "O2_WR_LAMBDA_S1": 4.0},
            corpus_eligible=False,
            storage_prefix="_capture-tuning",
        ),
        source="test",
    )
    # The conftest resets the capture profile after the test (R07.4).
    recorder = InferenceTimingRecorder()
    source = LiveFeatureSource(config=components.config)
    live = build_live_lambda_delivery(
        components, source, recorder, context_factory=_xterra_context(declaration, calibration)
    )
    assert live is not None
    for i in range(60):
        at = T0 + timedelta(seconds=i * 0.25)
        # A 20 % lean delivery at a steady enrichment target.
        source.observe(_read(at, "COMMANDED_EQUIV_RATIO", 1.16))
        source.observe(_read(at, "O2_WR_LAMBDA_S1", 1.2 / 1.16))
        if i % 4 == 3:
            live.tick_live(components)
    block = recorder.snapshot().to_dict()["lambda_delivery"]
    route = _route(block, "mode01_pid_0x44", "ecm_lambda_pid_0x24")
    assert route["result"] == "eligible"
    assert route["regimes"]["enrichment"]["median_pct"] == pytest.approx(20.0)
    assert block["alerts"] is False
    assert block["capture_profile_gated"] is False
    assert block["validated_model"] is False
    assert block["pairing"]["claims_instantaneous_target_tracking"] is False
    assert read_events_dir(config.daemon.anomalies_dir) == []
    stages = recorder.snapshot().to_dict()["stages"]
    assert "lambda_delivery" in stages


def test_a_vehicle_without_a_declaration_says_so(tmp_path: Path) -> None:
    components = build_components(isolated_config(tmp_path, "example", profiles={"enabled": False}))
    recorder = InferenceTimingRecorder()
    source = LiveFeatureSource(config=components.config)
    live = build_live_lambda_delivery(
        components,
        source,
        recorder,
        context_factory=lambda _config: TuningContext(afr_stoich=STOICH),
    )
    assert live is None
    assert source.record_observers == []
    block = recorder.snapshot().to_dict()["lambda_delivery"]
    assert block["result"] == "not_declared"


def test_a_malformed_declaration_withholds_the_readout(tmp_path: Path) -> None:
    components = build_components(isolated_config(tmp_path, "example", profiles={"enabled": False}))
    recorder = InferenceTimingRecorder()

    def broken(_config: object) -> TuningContext:
        raise ValueError("eligibility.regimes.enrichment: field required")

    live = build_live_lambda_delivery(
        components, LiveFeatureSource(config=components.config), recorder, context_factory=broken
    )
    assert live is None
    block = recorder.snapshot().to_dict()["lambda_delivery"]
    assert block["result"] == "withheld"
    assert "field required" in block["reason"]
