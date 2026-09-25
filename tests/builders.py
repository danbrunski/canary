"""Shared test-data builders (R07.5).

Before this module, tests imported builders out of *other test modules*
(``from tests.test_digest import _config`` pulled a whole test file, and the
``test_trip_speed_sources -> test_trip_end_digest -> test_digest ->
test_fingerprint`` chain made four files one), and copied them where they
did not: ``_write_obd_session`` three times, ``_IFConfig`` four. One home
each, here; fakes that stand in for real objects live in ``tests/fakes.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.analysis.refit import Surface2DRecipe
from src.config import CanaryConfig
from src.decode.behavior_models import (
    DeviationConfig,
    NoiseFloor,
    ResolutionSpec,
    SyntheticFault,
    TransientControl,
    ValidationCriteria,
)
from src.preprocessing.resampler import SESSION_COLUMN, TIMESTAMP_COLUMN
from src.preprocessing.segmenter import MODE_COLUMN
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord
from tests.configs import READ_ONLY_INPUTS, isolated_config

# ---------------------------------------------------------------------------
# OBD sessions on disk
# ---------------------------------------------------------------------------


def write_obd_session(root: Path, name: str, *, rows: int, start_epoch: float) -> Path:
    session = root / name
    records: list[TelemetryRecord] = []
    for i in range(rows):
        t = start_epoch + i
        records.extend(
            [
                TelemetryRecord.obd(t, "RPM", 780.0 + (i % 4), "rpm"),
                TelemetryRecord.obd(t, "SPEED", float(i % 5), "km/h"),
                TelemetryRecord.obd(t, "THROTTLE_POS", 6.0 + (i % 3), "%"),
            ]
        )
    write_records_to_parquet(records, session, max_rows=10_000)
    return session


def write_fixture_session(root: Path, name: str, *, seed: int = 0, n: int = 40) -> Path:
    rng = np.random.default_rng(seed)
    session = root / name
    records: list[TelemetryRecord] = []
    for i in range(n):
        t = 1_778_000_000.0 + i
        records.append(TelemetryRecord.obd(t, "RPM", float(rng.normal(1500.0, 50.0)), "rpm"))
        records.append(TelemetryRecord.obd(t, "SPEED", float(rng.normal(30.0, 3.0)), "mph"))
    write_records_to_parquet(records, session, max_rows=1000)
    return session


def write_rich_session(root: Path, *, name: str = "2026-05-20T120000") -> Path:
    """A session with a captured cold start, a warm-idle stretch, and a
    highway stretch — enough to exercise every stat category at once.

    Phase layout (1 sample/second, matching resample_hz: 1):
      * i in [0, 5): cranking — RPM below running threshold, cold coolant,
        low module voltage.
      * i in [5, 47): warmup ramp — coolant climbs 20C -> ~88C (crosses the
        70C idle-mode gate and the 80C warmup-stat target inside this
        window), RPM/speed already at idle levels.
      * i in [47, 87): steady warm idle — coolant pinned at 90C, well clear
        of the 70C gate, RPM/speed/MAF/load/trims constant.
      * i in [87, 147): highway cruise — speed/rpm up, different MAF/load/
        trim constants, GPS speed present (for the committee stat).

    The operating-mode segmenter's dwell hysteresis
    (``mode_dwell_ticks: 3``) means a couple of rows at the idle->highway
    boundary land on the "wrong" side of the label; the steady-idle window
    is long enough that per-stat tests tolerate that bleed with a loose
    relative tolerance rather than asserting exact contamination-free means.
    """
    session = root / name
    records: list[TelemetryRecord] = []
    base = 1_779_300_000.0
    for i in range(147):
        t = base + i
        if i < 5:
            rpm, speed, coolant, voltage = 150.0, 0.0, 20.0, 10.5
        elif i < 47:
            rpm, speed, coolant, voltage = 800.0, 0.0, 20.0 + 1.667 * (i - 5), 14.2
        elif i < 87:
            rpm, speed, coolant, voltage = 800.0, 0.0, 90.0, 14.2
        else:
            rpm, speed, coolant, voltage = 2200.0, 90.0, 90.0, 14.2

        records.extend(
            [
                TelemetryRecord.obd(t, "RPM", rpm, "rpm"),
                TelemetryRecord.obd(t, "SPEED", speed, "km/h"),
                TelemetryRecord.obd(t, "THROTTLE_POS", 10.0, "%"),
                TelemetryRecord.obd(t, "COOLANT_TEMP", coolant, "C"),
                TelemetryRecord.obd(t, "RUN_TIME", float(max(i - 4, 0)), "s"),
                TelemetryRecord.obd(t, "CONTROL_MODULE_VOLTAGE", voltage, "V"),
            ]
        )
        if i >= 5:
            # Idle-phase constants (i in [5, 87)) vs. cruise-phase constants
            # (i in [87, 147)) — deliberately different so idle/cruise means
            # are distinguishable in assertions.
            if i < 87:
                maf, load, stft1, stft2, ltft1, ltft2 = 3.0, 25.0, 2.0, -1.0, 3.0, -2.0
            else:
                maf, load, stft1, stft2, ltft1, ltft2 = 15.0, 45.0, 1.0, 0.5, 4.0, -1.0
            records.extend(
                [
                    TelemetryRecord.obd(t, "MAF", maf, "g/s"),
                    TelemetryRecord.obd(t, "ENGINE_LOAD", load, "%"),
                    TelemetryRecord.obd(t, "SHORT_FUEL_TRIM_1", stft1, "%"),
                    TelemetryRecord.obd(t, "SHORT_FUEL_TRIM_2", stft2, "%"),
                    TelemetryRecord.obd(t, "LONG_FUEL_TRIM_1", ltft1, "%"),
                    TelemetryRecord.obd(t, "LONG_FUEL_TRIM_2", ltft2, "%"),
                ]
            )
        if i >= 87:
            # GPS speed diverges from OBD speed by a constant 5 km/h, with a
            # trustworthy fix, so the speed committee actually scores.
            records.extend(
                [
                    TelemetryRecord.gps(t, "speed", speed + 5.0, "km/h"),
                    TelemetryRecord.gps(t, "num_satellites", 8.0),
                    TelemetryRecord.gps(t, "hdop", 1.0),
                ]
            )
        # Task 3.2 — a growing cylinder-1 misfire count over the drive (Mode
        # 06 counters are cumulative within a session); no cylinder-2
        # reading at all this session, proving the stat is per-cylinder
        # (only cylinders actually polled get a stat key).
        if i == 10:
            records.append(TelemetryRecord.obd(t, "MISFIRE_COUNT_CYL1", 1.0, "count"))
        elif i == 100:
            records.append(TelemetryRecord.obd(t, "MISFIRE_COUNT_CYL1", 5.0, "count"))
    write_records_to_parquet(records, session, max_rows=10_000)
    return session


#: Past the end of :func:`write_rich_session`'s ~146 s drive.
RICH_SESSION_END = datetime(2026, 5, 20, 18, 5, 0, tzinfo=UTC)


def digest_config(tmp_path: Path) -> CanaryConfig:
    """The example config for digest tests: real per-vehicle inputs, every
    writable path under ``tmp_path`` (the anomaly log, vehicle ledger and
    reference epoch at the top level, where the digest tests seed them)."""
    return isolated_config(
        tmp_path,
        "example",
        keep=READ_ONLY_INPUTS,
        daemon={
            "anomalies_dir": tmp_path / "anomalies",
            "vehicle_ledger": tmp_path / "vehicle_ledger.jsonl",
            "reference_epoch": tmp_path / "reference_epoch.json",
        },
    )


# ---------------------------------------------------------------------------
# isolation-forest trainer config
# ---------------------------------------------------------------------------


#: The trainer reads these attributes off ``config.models.isolation_forest``;
#: small and seeded so a test fit is fast and deterministic.
@dataclass(frozen=True)
class IFConfig:
    enabled: bool = True
    n_estimators: int = 60
    contamination: float = 0.02
    per_mode_contamination: dict[str, float] = field(default_factory=dict)
    random_state: int = 7
    calibration_holdout_fraction: float = 0.2
    target_flag_rate: float = 0.05


# ---------------------------------------------------------------------------
# behavior-surface validation (refit recipes)
# ---------------------------------------------------------------------------


BEHAVIOR_T0 = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


BEHAVIOR_PROFILE_CRITERIA = Path("data/nissan-xterra-2008/behavior_validation.yaml")


def behavior_resolution_spec(**overrides: Any) -> ResolutionSpec:
    fields: dict[str, Any] = {
        "sustained_change": 5.0,
        "min_duration_seconds": 60.0,
        "max_detection_delay_seconds": 10.0,
        "measured_noise": NoiseFloor(value=0.4, basis="synthetic corpus: generated sigma"),
        "diagnostic_question": "did the output shift by a sustained 5 units?",
        "claim": "unexpected sustained change versus the learned surface",
        "not_identified": ["the cause of the change"],
        "transient_controls": [
            TransientControl(magnitude=6.0, direction="negative", duration_seconds=2.0)
        ],
    }
    fields.update(overrides)
    return ResolutionSpec(**fields)


def behavior_criteria(**overrides: Any) -> ValidationCriteria:
    fields: dict[str, Any] = {
        "kind": "predictive_surface",
        "unit": "deg",
        "noise_floor": NoiseFloor(value=0.5, basis="test LSB"),
        "meaningful_deviation": 5.0,
        "meaningful_deviation_basis": "test",
        "min_validation_drives": 2,
        "min_eligible_observations": 100,
        "min_envelope_coverage": 0.0,
        "min_skill_vs_baseline": 0.3,
        "synthetic_faults": [
            SyntheticFault(magnitude=6.0, direction="negative", duration_seconds=60)
        ],
        "resolution": behavior_resolution_spec(),
    }
    fields.update(overrides)
    return ValidationCriteria(**fields)


def behavior_drive(name: str, start: datetime, *, n: int = 400, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rpm = rng.uniform(800, 4000, n)
    load = rng.uniform(10, 90, n)
    output = 10.0 + 0.004 * rpm - 0.1 * load + rng.normal(0.0, 0.4, n)
    return pd.DataFrame(
        {
            TIMESTAMP_COLUMN: pd.date_range(start, periods=n, freq="1s", tz="UTC"),
            SESSION_COLUMN: name,
            MODE_COLUMN: "city",
            "obd:RPM": rpm,
            "obd:ENGINE_LOAD": load,
            "obd:TIMING_ADVANCE": output,
        }
    )


def behavior_corpus() -> pd.DataFrame:
    names = [f"2026-08-0{d + 1}T120000" for d in range(5)]
    return pd.concat(
        [
            behavior_drive(name, BEHAVIOR_T0 + timedelta(days=day), seed=day)
            for day, name in enumerate(names)
        ],
        ignore_index=True,
    )


def behavior_recipe(criteria: ValidationCriteria) -> Surface2DRecipe:
    return Surface2DRecipe(
        name="timing_test",
        output="obd:TIMING_ADVANCE",
        x_column="obd:RPM",
        y_column="obd:ENGINE_LOAD",
        x_edges=(800.0, 1600.0, 2400.0, 3200.0, 4000.0),
        y_edges=(10.0, 30.0, 50.0, 70.0, 90.0),
        min_samples=200,
        min_cell_count=5,
        min_rpm=400.0,
        max_validation_warning_rate=1.0,
        max_validation_critical_rate=1.0,
        deviation=DeviationConfig(
            kind="sigma", warning_band=3.0, critical_band=6.0, sigma_floor=0.5
        ),
        validation=criteria,
    )


# ---------------------------------------------------------------------------
# persisted capture rounds
# ---------------------------------------------------------------------------


CAPTURE_T0 = 1_700_000_000.0


CAPTURE_STARTED = datetime.fromtimestamp(CAPTURE_T0, tz=UTC)


def capture_obd_record(
    t: float, channel: str, *, round_id: int | None, generation: int = 1
) -> TelemetryRecord:
    metadata = None
    if round_id is not None:
        metadata = {
            "ts_schema": 2,
            "acquired_at": t,
            "acquired_clock": "kernel",
            "generation": generation,
            "round_id": round_id,
        }
    return TelemetryRecord.obd(t, channel, 1.0, "", metadata=metadata)


def write_capture_session(
    root: Path, records: list[TelemetryRecord], *, profile: dict | None = None
) -> Path:
    session = root / "_capture-fuel-map-2026-08-05T142720"
    write_records_to_parquet(records, session)
    snapshot = {
        "name": "fuel-map",
        "mode01": {"A": 1.0, "B": 1.0},
        "mode22": [],
        "target_rate_hz": 4.0,
        "required_tuple": ["A", "B"],
        "min_achieved_rate_hz": 3.0,
        "round_timestamps": True,
        "max_tuple_skew_s": 0.125,
        **(profile or {}),
    }
    (session / "capture_profile.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile": "fuel-map",
                "started_at": CAPTURE_STARTED.isoformat(),
                "tune_revision": None,
                "profile_snapshot": snapshot,
            }
        )
    )
    return session


def capture_rounds(n: int, *, both: bool = True) -> list[TelemetryRecord]:
    records: list[TelemetryRecord] = []
    for r in range(n):
        t = CAPTURE_T0 + r * 0.25
        records.append(capture_obd_record(t, "A", round_id=r))
        if both or r % 2:
            records.append(capture_obd_record(t + 0.003, "B", round_id=r))
    return records


# ---------------------------------------------------------------------------
# sequence-autoencoder training frames
# ---------------------------------------------------------------------------


def sequence_training_frame(rows: int = 14) -> pd.DataFrame:
    """One clean session of two slowly cycling channels, 1 Hz."""
    return pd.DataFrame(
        {
            SESSION_COLUMN: ["clean"] * rows,
            TIMESTAMP_COLUMN: pd.date_range("2026-05-12T00:00:00Z", periods=rows, freq="s"),
            "rpm": [700.0 + (idx % 4) for idx in range(rows)],
            "speed": [float(idx % 5) for idx in range(rows)],
        }
    )


# ---------------------------------------------------------------------------
# K-line wire traffic (K01)
# ---------------------------------------------------------------------------

#: A wake-up on the wire: the sniffer reads the 25 ms low as one 0x00 byte.
KLINE_WAKE = None
#: The scan tool's ``22`` trailer, the same ``04 01`` UpRev sends the ECM.
KLINE_TRAILER = b"\x04\x01"
#: The ABS live-data screen's cycle as the scan tool read it on 2026-09-24
#: (data/analyses/2026-09-24-abs-kline-scan-tool.md), with the parked values.
ABS_SCREEN_CYCLE: tuple[tuple[int, bytes], ...] = (
    (0x1301, b"\x00\x0f"),
    (0x111A, b"\x7f"),
    (0x1105, b"\x00"),
    (0x1104, b"\x00"),
    (0x1102, b"\x00"),
    (0x1103, b"\x00"),
    (0x1302, b"\x00\x8f"),
    (0x1303, b"\xa0\xfe"),
    (0x1210, b"\xff\xff"),
    (0x1118, b"\xa1"),
    (0x1202, b"\x00\x00"),
)


def kline_wire(
    frames: list[bytes | None],
    *,
    start: float = 1000.0,
    byte_s: float = 0.001,
    frame_gap_s: float = 0.03,
) -> list[tuple[float, int]]:
    """``(at, byte)`` as the sniffer's reader sees them: bytes ``byte_s``
    apart, frames ``frame_gap_s`` apart (longer than the sniffer's
    inter-byte gap), :data:`KLINE_WAKE` as a lone 0x00."""
    out: list[tuple[float, int]] = []
    t = start
    for frame in frames:
        t += frame_gap_s
        if frame is None:
            out.append((t, 0x00))
            continue
        for byte in frame:
            out.append((t, byte))
            t += byte_s
    return out


def _kline_read(did: int, value: bytes) -> list[bytes | None]:
    from src.collection.kline import protocol as kp

    request = kp.read_did(did, KLINE_TRAILER)
    return [kp.encode_length_frame(request), kp.encode_length_frame(b"\x62" + request[1:3] + value)]


def abs_scan_tool_frames(
    *, cycles: int = 6, brake_cycles: tuple[int, ...] = ()
) -> list[bytes | None]:
    """A scan tool on the Xterra's ABS screen, built from the 2026-09-24 bytes.

    Modelled on the documented capture, not the capture itself (session
    2026-09-24T162507 is still on the Pi): a read the capture attached in the
    middle of (no StartCommunication yet), the wake-up and StartCommunication
    to 0x28 (``81 28 FC 81 26`` -> ``83 FC 28 C1 6D 8F 64``), identification
    (one refused ``7F 1A 12``), one-off reads at screen load (one refused
    ``7F 22 12``), ``cycles`` repetitions of the screen's cycle with a
    TesterPresent each, the stop-lamp bit 0x0200 of 0x1303 set during
    ``brake_cycles``, one reply whose request was missed, StopCommunication;
    then UpRev's ECM session reading its own 0x1210 (TPS2).
    """
    from src.collection.kline import protocol as kp

    length = kp.encode_length_frame
    frames: list[bytes | None] = _kline_read(0x1202, b"\x00\xf5")  # attached mid-session
    frames += [KLINE_WAKE, bytes.fromhex("8128FC8126"), bytes.fromhex("83FC28C16D8F64")]
    frames += [length(b"\x1a\x80"), length(b"\x5a\x80\x41\x42\x53")]
    frames += [length(b"\x1a\x00"), length(b"\x7f\x1a\x12")]
    for did, value in ((0x1100, b"\xf8\x00\x0b\x41"), (0x1200, b"\xc1\xbf\x00\x00")):
        frames += _kline_read(did, value)
    frames += [length(kp.read_did(0x1307, KLINE_TRAILER)), length(b"\x7f\x22\x12")]
    for cycle in range(cycles):
        for did, value in ABS_SCREEN_CYCLE:
            if did == 0x1303 and cycle in brake_cycles:
                value = b"\xa2\xfe"  # stop-lamp switch 0x0200 set
            frames += _kline_read(did, value)
        frames += [length(b"\x3e"), length(b"\x7e")]
    frames += [length(b"\x62\x11\x18\xa1")]  # its request was never heard
    frames += [length(b"\x82"), length(b"\xc2")]
    frames += [KLINE_WAKE, bytes.fromhex("8110FC810E"), bytes.fromhex("83FC10C15D8F3C")]
    frames += _kline_read(0x1210, b"\x00\x1a")
    return frames
