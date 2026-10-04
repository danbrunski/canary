"""Warmup input verification and the cold-start inventory (B07.1, B07.2).

The committed slice under ``tests/fixtures/warmup_inputs`` is the Mode 01
rows of ``sessions-0918/2026-09-18T134345`` — the genuine cold start the
B02 report used (first coolant read 15 °C, first ``RUN_TIME`` 20 s) — restricted
to the ten channels these checks read.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.analysis.channels import read_channels
from src.analysis.cold_start_inventory import (
    IAT_CHANNEL,
    INVENTORY_CHANNELS,
    LOAD_CHANNEL,
    InventorySpec,
    SessionInventory,
    group_into_drives,
    per_second_view,
    runs_of_session,
    usable_context_runs,
)
from src.analysis.warmup_checks import catalyst_estimation_evidence, runtime_clock_report
from src.preprocessing.loader import read_session

SLICE = Path("tests/fixtures/warmup_inputs/2026-09-18T134345")
SPECS = [
    "obd:RUN_TIME",
    "obd:CATALYST_TEMP_B1S1",
    "obd:CATALYST_TEMP_B2S1",
    "obd:INTAKE_TEMP",
    "obd:COOLANT_TEMP",
    "obd:RPM",
]
T0 = datetime(2026, 9, 18, 13, 43, tzinfo=UTC)


@pytest.fixture(scope="module")
def long() -> pd.DataFrame:
    return read_channels(SLICE, SPECS, acquired_time=True)


# ---------------------------------------------------------------------------
# B07.1 — verify the inputs before modelling them


def test_run_time_is_a_seconds_since_start_clock(long: pd.DataFrame) -> None:
    report = runtime_clock_report(long)
    assert report.reads == 164
    assert report.median_rate == pytest.approx(0.992, abs=0.01)
    assert report.resets == 0
    assert report.gaps == 0
    # The clock's first answer arrives after the engine is already running:
    # 20 s, which is what places this start (B02.5's engine-start fix).
    assert report.first_reading_s == 20.0
    assert report.max_reading_s == 971.0


def test_catalyst_channel_is_ecu_estimated_not_measured(long: pd.DataFrame) -> None:
    """Three independent indicators, each computed per sample.

    This is why a catalyst deviation is a *temperature-trajectory* deviation
    and cannot support a conversion-efficiency or aging claim (B07.1/B07.5).
    """
    evidence = catalyst_estimation_evidence(long)
    assert evidence.verdict == "ecu_estimated"
    indicators = evidence.indicators
    assert indicators["first_reading_c"] == 0.0
    assert indicators["intake_air_at_first_reading_c"] == 16.0
    assert indicators["first_reading_below_intake_air"] is True
    assert indicators["bank_pairs"] == 45
    assert indicators["banks_identical_fraction"] == pytest.approx(0.7778, abs=1e-3)
    assert indicators["value_step_c"] == 4
    assert len(evidence.reasons) == 3


def test_catalyst_verdict_is_unresolved_without_two_indicators() -> None:
    """A single indicator must not settle it."""
    times = [T0 + timedelta(seconds=10 * i) for i in range(30)]
    frame = pd.DataFrame(
        {
            "timestamp": times * 2,
            "spec": ["obd:CATALYST_TEMP_B1S1"] * 30 + ["obd:INTAKE_TEMP"] * 30,
            "value": list(np.linspace(20.0, 320.0, 30)) + [16.0] * 30,
        }
    )
    evidence = catalyst_estimation_evidence(frame)
    assert evidence.verdict == "unresolved"
    assert evidence.indicators["first_reading_below_intake_air"] is False


# ---------------------------------------------------------------------------
# B07.2 — the inventory's evidence rule


@pytest.fixture(scope="module")
def inventory_runs() -> list:
    session = read_session(SLICE)
    session["source_channel"] = session["source"] + ":" + session["channel"]
    view = per_second_view(session, INVENTORY_CHANNELS)
    return runs_of_session(view, InventorySpec())


def test_the_committed_cold_start_is_inventoried_as_one(inventory_runs: list) -> None:
    cold = [run for run in inventory_runs if run.classification == "cold_start"]
    assert len(cold) == 1
    run = cold[0]
    assert run.start_observed is True
    # 17 °C, not the session's first coolant read (15 °C at 13:45:31): the
    # tracker only accepts a coolant reading once the start is *evidenced*,
    # and the evidence here is the first RUN_TIME answer (20 s, 13:45:35).
    # A start's initial temperature is what was measured after the start was
    # established, never the earliest number in the file.
    assert run.initial_coolant_c == 17.0
    assert run.initial_iat_c == 16.0
    # No ambient-air PID on this profile: absent, not assumed.
    assert run.ambient_c is None
    assert run.runtime_continuous is True
    assert run.catalyst_rows > 0
    assert run.warmup_seconds > 0


def test_a_recording_that_starts_mid_drive_has_an_unknown_initial_temperature() -> None:
    """The observed-start rule: no transition and no young clock, no cold start."""
    seconds = 120
    view = pd.DataFrame(
        {
            "timestamp": [T0 + timedelta(seconds=i) for i in range(seconds)],
            "obd:RPM": [1500.0] * seconds,
            "obd:RUN_TIME": [900.0 + i for i in range(seconds)],
            "obd:COOLANT_TEMP": [20.0] * seconds,
        }
    )
    runs = runs_of_session(view, InventorySpec())
    assert [run.classification for run in runs] == ["unknown_initial_temperature"]
    assert runs[0].start_observed is False
    assert runs[0].initial_coolant_c is None


def test_a_warm_restart_is_not_a_cold_start() -> None:
    seconds = 120
    rpm = [0.0] * 10 + [1500.0] * (seconds - 10)
    view = pd.DataFrame(
        {
            "timestamp": [T0 + timedelta(seconds=i) for i in range(seconds)],
            "obd:RPM": rpm,
            "obd:RUN_TIME": [0.0] * 10 + [float(i) for i in range(seconds - 10)],
            "obd:COOLANT_TEMP": [85.0] * seconds,
        }
    )
    runs = runs_of_session(view, InventorySpec())
    assert [run.classification for run in runs] == ["warm_restart"]
    assert runs[0].start_observed is True
    assert runs[0].initial_coolant_c == 85.0


def test_two_runs_in_one_recording_are_counted_separately() -> None:
    rpm = [0.0] * 5 + [1500.0] * 40 + [0.0] * 5 + [1500.0] * 40
    coolant = [15.0] * 50 + [85.0] * 40
    view = pd.DataFrame(
        {
            "timestamp": [T0 + timedelta(seconds=i) for i in range(90)],
            "obd:RPM": rpm,
            "obd:RUN_TIME": [0.0] * 5
            + [float(i) for i in range(40)]
            + [0.0] * 5
            + [float(i) for i in range(40)],
            "obd:COOLANT_TEMP": coolant,
        }
    )
    runs = runs_of_session(view, InventorySpec())
    assert [run.classification for run in runs] == ["cold_start", "warm_restart"]


def test_a_start_without_load_or_iat_spread_is_not_usable_context() -> None:
    """The 4.6 "usable" rule, applied per run."""
    base = dict(
        started_at=None,
        ended_at=None,
        duration_s=300.0,
        classification="cold_start",
        start_observed=True,
        initial_coolant_c=15.0,
        initial_iat_c=14.0,
        ambient_c=None,
        runtime_continuous=True,
        stationary_before_start=True,
        moving_seconds_in_warmup=0.0,
        warmup_seconds=300.0,
        catalyst_rows=10,
    )
    from src.analysis.cold_start_inventory import EngineRun

    flat = EngineRun(**base, load_spread=2.0, iat_spread=1.0)
    varied = EngineRun(**base, load_spread=40.0, iat_spread=1.0)
    inventories = [
        SessionInventory("flat", "flat", False, None, 10, [], runs=[flat]),
        SessionInventory("varied", "varied", False, None, 10, [], runs=[varied]),
    ]
    thresholds = {LOAD_CHANNEL: 15.0, IAT_CHANNEL: 8.0}
    usable = usable_context_runs(inventories, thresholds)
    assert [name for name, _ in usable] == ["varied"]


def test_two_directories_from_one_outing_are_one_drive() -> None:
    first = SessionInventory("2026-09-18T134052", "a", False, None, 10, [])
    second = SessionInventory("2026-09-18T134345", "b", False, None, 10, [])
    apart = SessionInventory("2026-09-18T171938", "c", False, None, 10, [])
    drives = group_into_drives([first, second, apart])
    assert [drive["pieces"] for drive in drives] == [
        ["2026-09-18T134052", "2026-09-18T134345"],
        ["2026-09-18T171938"],
    ]


# ---------------------------------------------------------------------------
# B07.1 — the legacy artifact is regression evidence, not a baseline to bless


def test_the_legacy_catalyst_artifact_is_preserved_and_quarantined() -> None:
    """The 30/80 °C fit and its September failure must stay visible.

    Applying the recipe's newer 45/115 °C bands to the same artifact would
    erase the evidence that it failed; the artifact keeps its own bands and
    the policy keeps it out of alerting until a new fit passes B02.
    """
    import yaml

    artifact = yaml.safe_load(Path("data/nissan-xterra-2008/behavior_models.yaml").read_text())
    model = next(m for m in artifact["models"] if m["name"] == "cat_warmup")
    assert model["deviation"]["warning_band"] == 30.0
    assert model["deviation"]["critical_band"] == 80.0
    assert model["version"] == 4
    assert model["fit"]["validation_sample_count"] == 0

    policy = yaml.safe_load(Path("data/nissan-xterra-2008/behavior_policy.yaml").read_text())
    entry = next(item for item in policy["entries"] if item["model"] == "cat_warmup")
    assert entry["lifecycle"] == "quarantined"
    assert entry["permitted_uses"] == ["display"]


def test_start_context_measures_the_start_window_in_real_seconds() -> None:
    """A microsecond-resolution frame must not compress the start window.

    ``append_start_context`` bounds how long after a start a first coolant
    reading may still stand in for the initial temperature. Converting
    ``datetime64[us]`` stamps with ``astype("int64") / 1e9`` reads every
    interval 1000x short (B04.3 found the same shape in the evaluator), which
    would accept a reading taken two minutes after the start as the start
    temperature. The conversion goes through ``src.signals.pairing.to_seconds``.
    """
    from src.behavior.session_context import StartContextSpec, append_start_context

    spec = StartContextSpec(
        rpm_channel="obd:RPM",
        runtime_channel="obd:RUN_TIME",
        coolant_channel="obd:COOLANT_TEMP",
        min_running_rpm=400.0,
        max_start_runtime_s=60.0,
    )
    seconds = 200
    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [T0 + timedelta(seconds=i) for i in range(seconds)], utc=True
            ).astype("datetime64[us, UTC]"),
            "obd:RPM": [0.0] * 5 + [1500.0] * (seconds - 5),
            # No run-time clock: the window is wall-clock seconds since the
            # observed start.
            "obd:RUN_TIME": [float("nan")] * seconds,
            "obd:COOLANT_TEMP": [float("nan")] * 130 + [85.0] * (seconds - 130),
        }
    )
    out = append_start_context(frame, [spec])
    assert out["timestamp"].dtype == frame["timestamp"].dtype
    # The first coolant reading lands 125 s after the start — outside the
    # 60 s window — so the initial temperature stays unknown.
    assert out[spec.start_coolant_column].isna().all()
    assert (out[spec.observed_column] == 0.0).all()

    early = frame.copy()
    early["obd:COOLANT_TEMP"] = [float("nan")] * 30 + [85.0] * (seconds - 30)
    out_early = append_start_context(early, [spec])
    assert out_early[spec.start_coolant_column].dropna().unique().tolist() == [85.0]


def test_session_discovery_weighs_duplicate_copies_once_per_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deduplication must be per directory, not per chunk.

    Weighing the copies once per *file* re-globbed both directories for every
    chunk: the corpus scan read 17 MB and then spun at 100 % CPU for an hour
    (2026-09-22). This pins the cost at one weighing per copy.
    """
    import scripts.inventory_cold_starts as cli

    small = tmp_path / "pull-a" / "2026-09-18T134345"
    big = tmp_path / "pull-b" / "2026-09-18T134345"
    other = tmp_path / "pull-b" / "2026-09-18T171938"
    for directory, chunks, size in ((small, 20, 10), (big, 20, 100), (other, 5, 10)):
        directory.mkdir(parents=True)
        for index in range(chunks):
            (directory / f"telemetry-{index:05d}.parquet").write_bytes(b"x" * size)
    replay = tmp_path / "pull-b" / "_replay-2026-09-18T134345"
    replay.mkdir()
    (replay / "telemetry-00000.parquet").write_bytes(b"x")

    calls: list[Path] = []
    real_bytes = cli._bytes
    monkeypatch.setattr(cli, "_bytes", lambda path: (calls.append(path), real_bytes(path))[1])
    found = cli.find_sessions(tmp_path)

    assert found == [big, other]
    assert sorted(calls) == sorted([big, small])
