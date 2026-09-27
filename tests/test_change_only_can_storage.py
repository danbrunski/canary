"""P02.10 — CAN frames stored on change, an unchanged id re-written once per
broadcast stale timeout (operator decision 2026-09-26).

The rule lives in :mod:`src.storage.broadcast_storage`; these tests hold every
layer that must agree on it to the same answer: the filter, the writer and its
footer, the config refusal, the capture profile that stores every frame, the
resampler's broadcast stale class, the per-sample readers' held values, the
live feature source, and the readers that count frames and must refuse.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest

from src.analysis.broadcast_discovery import sweep_session
from src.analysis.channels import (
    AS_OF_ATTR,
    align_long,
    concat_channel_frames,
    read_aligned,
    read_channels,
)
from src.analysis.compare import PairingPolicy, align_pairs, compare_channels
from src.collection.capture_profiles import (
    STARTER_CATALOG_PATH,
    CaptureProfile,
    CaptureProfileCatalog,
)
from src.columns import arrived_column, gap_column
from src.config import CanaryConfig
from src.decode.bindings import load_bindings
from src.preprocessing.derived import reduce_numeric_session
from src.preprocessing.live import LiveFeatureSource
from src.preprocessing.loader import read_session, read_sessions, resample_and_segment
from src.preprocessing.resampler import ResampleConfig, resample_config_for
from src.storage.broadcast_storage import (
    FOOTER_KEY,
    BroadcastTimeoutMismatchError,
    CanChangeFilter,
    CanStorage,
    ChangeOnlyStorageError,
    change_only_copy,
    change_only_mask,
    refuse_change_only,
    session_can_storage,
)
from src.storage.parquet_writer import ParquetTelemetryWriter, write_records_to_parquet
from src.telemetry import TelemetryRecord
from tests.configs import example_config

EXAMPLE_YAML = Path("canary.example.yaml")
BINDINGS = Path("data/bindings.yaml")
T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
#: ECM_RPM: 0x1F9 bytes 2-3 big-endian, raw = rpm x 8 (a confirmed binding,
#: so the loaders decode it on read).
RPM_ID = 0x1F9


@pytest.fixture
def config() -> CanaryConfig:
    return example_config(EXAMPLE_YAML)


def _rpm_payload(rpm: float) -> bytes:
    raw = int(round(rpm * 8))
    return bytes([0x20, 0x00, (raw >> 8) & 0xFF, raw & 0xFF, 0, 0, 0x82, 0])


def _frame(seconds: float, payload: bytes, can_id: int = RPM_ID) -> TelemetryRecord:
    return TelemetryRecord.can((T0 + timedelta(seconds=seconds)).timestamp(), can_id, payload)


def _obd(seconds: float, channel: str, value: float) -> TelemetryRecord:
    return TelemetryRecord.obd((T0 + timedelta(seconds=seconds)).timestamp(), channel, value, "")


def _drive(seconds: float = 90.0, *, stop_at: float | None = None) -> list[TelemetryRecord]:
    """A 100 Hz rpm frame that holds for seconds at a time and then changes,
    with an OBD rpm poll at 1 Hz. ``stop_at`` stops the frame (a module that
    goes silent) while the poll carries on."""
    records: list[TelemetryRecord] = []
    for k in range(int(seconds * 100)):
        t = k / 100.0
        if stop_at is None or t < stop_at:
            # Holds for 7 s, ramps for 3 s: long holds cross the re-write.
            phase = t % 10.0
            rpm = 800.0 if phase < 7.0 else 800.0 + 400.0 * (phase - 7.0)
            records.append(_frame(t, _rpm_payload(round(rpm))))
        if k % 100 == 37:
            records.append(_obd(t, "RPM", 800.0 + (t % 10.0) * 40.0))
    return records


# ---------------------------------------------------------------------------
# The filter
# ---------------------------------------------------------------------------


def test_the_filter_keeps_first_frames_changes_and_one_rewrite_per_timeout() -> None:
    flt = CanChangeFilter(10.0)
    assert flt.rewrite_after_seconds == 9.0
    a, b = b"\x01\x02", b"\x01\x03"
    assert flt.keep("0x1F9", a, 100.0)  # first frame of the id
    assert not flt.keep("0x1F9", a, 100.01)  # repeat
    assert flt.keep("0x2A5", a, 100.02)  # another id's first frame
    assert flt.keep("0x1F9", b, 100.03)  # changed
    # The same payload every 10 ms: dropped until 9 s after the last write.
    repeats = [flt.keep("0x1F9", b, 100.03 + k / 100.0) for k in range(1, 901)]
    assert not any(repeats[:-1]) and repeats[-1]  # the re-write, at 109.03
    assert not flt.keep("0x1F9", b, 109.04)
    assert flt.keep("0x1F9", b, 50.0)  # a clock step backwards never silences an id
    assert flt.keep("0x1F9", None, 50.1)  # nothing to compare: kept
    assert (flt.kept, flt.suppressed) == (6, 901)
    flt.reset()
    assert flt.keep("0x1F9", b, 50.2)  # a new session: first frame again


def test_non_can_records_are_always_kept_and_the_filter_is_idempotent() -> None:
    records = _drive(60.0)
    flt = CanChangeFilter(10.0)
    kept = [r for r in records if flt.keep_record(r)]
    assert sum(r.source == "obd" for r in kept) == sum(r.source == "obd" for r in records)
    # A change-only stream filtered again (a replay of a new session) is unchanged.
    again = CanChangeFilter(10.0)
    assert [r for r in kept if again.keep_record(r)] == kept
    # Holds are stored as one frame, then one re-write every 9 s at most.
    can = [r for r in kept if r.source == "can"]
    gaps = np.diff([r.timestamp.timestamp() for r in can])
    assert gaps.max() <= 9.0 + 0.011
    assert len(can) < 0.5 * sum(r.source == "can" for r in records)


def test_change_only_mask_is_the_filter_row_by_row() -> None:
    records = _drive(30.0)
    mask = change_only_mask(
        [r.source for r in records],
        [r.channel for r in records],
        [r.payload for r in records],
        [r.timestamp.timestamp() for r in records],
    )
    flt = CanChangeFilter()
    assert mask.tolist() == [flt.keep_record(r) for r in records]


# ---------------------------------------------------------------------------
# Config and profile refusals
# ---------------------------------------------------------------------------


def test_the_config_refuses_a_timeout_under_two_seconds_and_names_why(
    config: CanaryConfig,
) -> None:
    assert config.collection.broadcast_stale_timeout_seconds == 10.0
    data = config.model_dump()
    data["collection"]["broadcast_stale_timeout_seconds"] = 1.9
    with pytest.raises(ValueError, match="under the 2 s floor.*P02.10"):
        CanaryConfig.model_validate(data)
    data["collection"]["broadcast_stale_timeout_seconds"] = 300.0
    with pytest.raises(ValueError, match="must be under preprocessing.session_gap_seconds"):
        CanaryConfig.model_validate(data)
    data["collection"]["broadcast_stale_timeout_seconds"] = 2.0
    assert CanaryConfig.model_validate(data).collection.broadcast_stale_timeout_seconds == 2.0


def test_the_resample_recipe_takes_the_broadcast_class_from_the_one_setting(
    config: CanaryConfig,
) -> None:
    data = config.model_dump()
    data["collection"]["broadcast_stale_timeout_seconds"] = 6.0
    recipe = resample_config_for(CanaryConfig.model_validate(data))
    assert recipe.broadcast_max_gap_seconds == 6.0
    assert recipe.max_gap_seconds_for("decoded:ECM_RPM") == 6.0
    assert recipe.max_gap_seconds_for("dbc:ECM_RPM") == 6.0
    assert recipe.max_gap_seconds_for("obd:RPM") == 2.0  # polls keep 2 s
    # A slow broadcast channel keeps its own, longer class.
    assert recipe.max_gap_seconds_for("decoded:ECM_COOLANT_TEMP") == 120.0
    with pytest.raises(ValueError, match="2 s floor"):
        ResampleConfig(broadcast_max_gap_seconds=1.0)


def test_the_default_profile_cannot_store_every_frame_and_a_starter_does() -> None:
    with pytest.raises(ValueError, match="change-only.*capture profile"):
        CaptureProfile(name="default", corpus_eligible=True, store_every_frame=True)
    starter = CaptureProfileCatalog.model_validate(
        __import__("yaml").safe_load(STARTER_CATALOG_PATH.read_text())
    )
    every = [p for p in starter.profiles if p.store_every_frame]
    assert [p.name for p in every] == ["broadcast-every-frame"]
    assert every[0].storage_prefix == "_capture-broadcast-every-frame"


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------


def _writer(root: Path, **kwargs: object) -> ParquetTelemetryWriter:
    return ParquetTelemetryWriter(root, session_started_at=T0, flush_interval_seconds=1e9, **kwargs)  # type: ignore[arg-type]


def _footer(path: Path) -> dict[str, object]:
    return json.loads(pq.read_schema(path).metadata[FOOTER_KEY])


def test_the_writer_stores_change_only_and_records_it_in_every_chunk(tmp_path: Path) -> None:
    records = _drive(40.0)
    writer = _writer(tmp_path)
    writer.write_many(records)
    writer.close()
    stored = read_session(writer.paths.root, decoded="as_recorded")
    expected = CanChangeFilter(10.0)
    assert int((stored["source"] == "can").sum()) == sum(
        expected.keep_record(r) for r in records if r.source == "can"
    )
    assert int((stored["source"] == "obd").sum()) == sum(r.source == "obd" for r in records)
    assert writer.can_frames_suppressed == expected.suppressed
    chunk = next(writer.paths.root.glob("telemetry-*.parquet"))
    assert _footer(chunk) == {
        "mode": "change_only",
        "rewrite_after_seconds": 9.0,
        "stale_timeout_seconds": 10.0,
        "version": 1,
    }
    assert stored.attrs["can_storage"]["change_only"] is True


def test_every_session_stands_alone_and_a_capture_can_store_every_frame(tmp_path: Path) -> None:
    writer = _writer(tmp_path, broadcast_stale_timeout_seconds=5.0)
    payload = _rpm_payload(800)
    writer.write_many([_frame(0.0, payload), _frame(0.01, payload)])
    first = writer.paths.root
    writer.rotate_session(started_at=T0 + timedelta(seconds=1))
    # The same payload again, 20 ms later: the new session still stores it.
    writer.write_many([_frame(0.02, payload), _frame(0.03, payload)])
    second = writer.paths.root
    profile = CaptureProfile(
        name="timing",
        storage_prefix="_capture-timing",
        store_every_frame=True,
        mode01={"BAROMETRIC_PRESSURE": 1.0, "INTAKE_TEMP": 1.0, "COOLANT_TEMP": 1.0},
    )
    writer.enter_capture_mode(profile, "cap-1")
    writer.write_many([_frame(0.04, payload), _frame(0.05, payload)])
    capture = writer.paths.root
    writer.exit_capture_mode(started_at=T0 + timedelta(seconds=5))
    writer.write_many([_frame(0.06, payload), _frame(0.07, payload)])
    after = writer.paths.root
    writer.close()

    def can_rows(path: Path) -> int:
        frame = read_session(path, decoded="as_recorded")
        return int((frame["source"] == "can").sum())

    assert [can_rows(p) for p in (first, second, capture, after)] == [1, 1, 2, 1]
    assert _footer(next(capture.glob("*.parquet")))["mode"] == "every_frame"
    assert _footer(next(after.glob("*.parquet")))["stale_timeout_seconds"] == 5.0
    assert writer.can_frames_suppressed_total == 3


def test_the_batch_writer_filters_only_when_told_and_never_fakes_a_footer(tmp_path: Path) -> None:
    records = _drive(20.0)
    legacy = write_records_to_parquet(records, tmp_path / "legacy")
    assert FOOTER_KEY not in (pq.read_schema(legacy[0]).metadata or {})
    assert not session_can_storage(tmp_path / "legacy").any_change_only
    written = write_records_to_parquet(
        records, tmp_path / "new", can_storage=CanStorage.change_only(10.0)
    )
    assert _footer(written[0])["mode"] == "change_only"
    assert pq.read_metadata(written[0]).num_rows < len(records)


def test_a_change_only_copy_never_rewrites_its_source(tmp_path: Path) -> None:
    source = tmp_path / "s"
    write_records_to_parquet(_drive(20.0), source)
    before = sorted((p.name, p.stat().st_mtime_ns) for p in source.iterdir())
    read, written = change_only_copy(source, tmp_path / "copy" / "s")
    assert written < read
    assert sorted((p.name, p.stat().st_mtime_ns) for p in source.iterdir()) == before
    with pytest.raises(ValueError, match="in place"):
        change_only_copy(source, source)
    with pytest.raises(ValueError, match="already stores CAN frames change-only"):
        change_only_copy(tmp_path / "copy" / "s", tmp_path / "copy2" / "s")


# ---------------------------------------------------------------------------
# Readers: the 1 Hz grid, per-sample pairing, a mixed corpus
# ---------------------------------------------------------------------------


def _pair(tmp_path: Path, records: list[TelemetryRecord]) -> tuple[Path, Path]:
    """The same drive stored every frame (as before P02.10) and change-only,
    under the same session name."""
    full = tmp_path / "full" / "2026-09-26T120000"
    write_records_to_parquet(records, full)
    changed = tmp_path / "changed" / "2026-09-26T120000"
    change_only_copy(full, changed)
    return full, changed


def test_the_one_hz_grid_is_identical_for_full_rate_and_change_only(
    tmp_path: Path, config: CanaryConfig
) -> None:
    full, changed = _pair(tmp_path, _drive(120.0))
    a = resample_and_segment([full], config)
    b = resample_and_segment([changed], config)
    assert "decoded:ECM_RPM" in a.columns
    pd.testing.assert_frame_equal(a, b, check_exact=True)
    # Arrival reads "a stored row within the timeout", so holds still arrive.
    assert b[arrived_column("decoded:ECM_RPM")].all()
    assert not b[gap_column("decoded:ECM_RPM")].any()


def test_a_module_that_stops_differs_only_at_the_silence_edge(
    tmp_path: Path, config: CanaryConfig
) -> None:
    """The operator's stated cost: a module that really stops keeps its value
    for up to the timeout after its last *stored* frame, which may be earlier
    than its last frame. Every other cell is identical."""
    full, changed = _pair(tmp_path, _drive(120.0, stop_at=65.5))
    a = resample_and_segment([full], config)
    b = resample_and_segment([changed], config)
    gap = gap_column("decoded:ECM_RPM")
    edge = b[gap] & ~a[gap]
    # The hold began at 60 s and was stored once; the frame stopped at 65.5 s.
    stamps = (a.loc[edge, "timestamp"] - pd.Timestamp(T0)).dt.total_seconds().tolist()
    assert stamps == [71.0, 72.0, 73.0, 74.0, 75.0]
    assert not (a[gap] & ~b[gap]).any()
    same = ~edge.to_numpy()
    rows = [c for c in a.columns if c != "operating_mode"]
    pd.testing.assert_frame_equal(
        a.loc[same, rows].reset_index(drop=True),
        b.loc[same, rows].reset_index(drop=True),
        check_exact=True,
    )


def test_pairing_holds_a_change_only_broadcast_value_inside_the_reader(tmp_path: Path) -> None:
    full, changed = _pair(tmp_path, _drive(120.0))
    policy = PairingPolicy.within("50ms")
    long = read_channels(changed, ["can:ECM_RPM", "obd:RPM"])
    assert long.attrs[AS_OF_ATTR] == {"can:ECM_RPM": 10.0}
    assert AS_OF_ATTR not in read_channels(full, ["can:ECM_RPM", "obd:RPM"]).attrs
    a = compare_channels(full, "can:ECM_RPM", "obd:RPM", policy=policy)
    b = compare_channels(changed, "can:ECM_RPM", "obd:RPM", policy=policy)
    assert a.n_held_pairs == 0 and b.n_held_pairs > 0
    assert b.n_pairs == a.n_pairs
    assert b.scale_verdict == a.scale_verdict
    assert b.r_squared == pytest.approx(a.r_squared, abs=1e-9)
    aligned = align_pairs(changed, "can:ECM_RPM", "obd:RPM", policy=policy)
    held = aligned.pairs[aligned.pairs["held"]]
    assert (held["separation_s"] == 0.0).all()
    assert held["index_a"].duplicated().any()  # one stored row holds for many polls


def test_the_aligned_frame_holds_change_only_cells_and_refuses_a_mean(tmp_path: Path) -> None:
    full, changed = _pair(tmp_path, _drive(120.0))
    specs = ["decoded:ECM_RPM", "obd:RPM"]
    pd.testing.assert_frame_equal(
        read_aligned(full, specs, require_all=False),
        read_aligned(changed, specs, require_all=False),
        check_exact=True,
    )
    with pytest.raises(ValueError, match="averages payload changes"):
        read_aligned(changed, specs, cell_agg="mean")


def test_per_spec_reads_concatenated_keep_the_held_rule(tmp_path: Path) -> None:
    """The workbench reads spec by spec and concatenates; ``pd.concat`` drops
    differing attrs, so the held rule would be lost without
    ``concat_channel_frames``."""
    full, changed = _pair(tmp_path, _drive(120.0))
    specs = ["decoded:ECM_RPM", "obd:RPM"]

    def per_spec(session: Path) -> pd.DataFrame:
        return concat_channel_frames([read_channels(session, [spec]) for spec in specs])

    assert per_spec(changed).attrs[AS_OF_ATTR] == {"decoded:ECM_RPM": 10.0}
    pd.testing.assert_frame_equal(
        align_long(per_spec(full), specs, require_all=False),
        align_long(per_spec(changed), specs, require_all=False),
        check_exact=True,
    )


def test_a_mixed_corpus_reads_and_says_which_sessions_are_change_only(tmp_path: Path) -> None:
    full, changed = _pair(tmp_path, _drive(30.0))
    renamed = tmp_path / "changed" / "2026-09-26T130000"
    changed.rename(renamed)
    mixed = read_sessions([full, renamed])
    assert set(mixed["session"]) == {full.name, renamed.name}
    assert mixed.attrs["can_storage"]["change_only_sessions"] == [renamed.name]
    with pytest.raises(ChangeOnlyStorageError, match=renamed.name) as caught:
        refuse_change_only([full, renamed], reader="frame counter")
    assert caught.value.sessions == (renamed.name,)
    refuse_change_only(full, reader="frame counter")  # stored every frame: fine


def test_frame_count_readers_refuse_a_change_only_session_by_name(tmp_path: Path) -> None:
    full, changed = _pair(tmp_path, _drive(60.0))
    policy = PairingPolicy.within("250ms")
    with pytest.raises(ChangeOnlyStorageError, match="store_every_frame") as caught:
        sweep_session(changed, ["obd:RPM"], policy=policy, can_ids=[RPM_ID], min_pairs=10)
    assert caught.value.reader.startswith("broadcast discovery")
    with pytest.raises(ChangeOnlyStorageError):
        sweep_session(read_session(changed), ["obd:RPM"], policy=policy, can_ids=[RPM_ID])
    assert isinstance(
        sweep_session(full, ["obd:RPM"], policy=policy, can_ids=[RPM_ID], min_pairs=10), list
    )


def test_a_shorter_read_timeout_than_the_session_was_stored_with_is_refused(
    tmp_path: Path, config: CanaryConfig
) -> None:
    session = tmp_path / "s"
    write_records_to_parquet(_drive(20.0), session, can_storage=CanStorage.change_only(20.0))
    with pytest.raises(BroadcastTimeoutMismatchError, match="at least 20 s"):
        reduce_numeric_session(session, resample_config_for(config))


# ---------------------------------------------------------------------------
# Live: the same frames as storage
# ---------------------------------------------------------------------------


def _live_rows(config: CanaryConfig, records: list[TelemetryRecord], ticks: int) -> list[dict]:
    source = LiveFeatureSource(config=config, bindings=load_bindings(BINDINGS))
    rows: list[dict] = []
    pending = sorted(records, key=lambda r: r.timestamp)
    for k in range(ticks):
        now = T0 + timedelta(seconds=k)
        while pending and pending[0].timestamp <= now:
            source.observe(pending.pop(0))
        prepared = source.prepare_tick(now)
        assert prepared is not None
        rows.append(prepared.wide.iloc[-1].to_dict())
    return rows


def test_live_features_on_the_raw_stream_equal_a_replay_of_the_stored_session(
    tmp_path: Path, config: CanaryConfig
) -> None:
    """The live source filters CAN frames as the writer does, so a live run
    over the bus stream and a replay of what storage kept give the same rows,
    across a module that stops too."""
    records = _drive(90.0, stop_at=55.5)
    writer = _writer(tmp_path)
    writer.write_many(records)
    writer.close()
    stored = read_session(writer.paths.root, decoded="as_recorded")
    replayed = [
        TelemetryRecord.can(row.timestamp.timestamp(), RPM_ID, bytes(row.payload))
        if row.source == "can"
        else TelemetryRecord.obd(row.timestamp.timestamp(), row.channel, row.value_number, "")
        for row in stored.itertuples()
    ]
    live = _live_rows(config, records, 85)
    replay = _live_rows(config, replayed, 85)
    pd.testing.assert_frame_equal(pd.DataFrame(live), pd.DataFrame(replay), check_exact=True)
    assert pd.DataFrame(live)[gap_column("decoded:ECM_RPM")].any()  # the stop is covered

    # And the live flags are the offline resampler's on the stored data.
    offline = resample_and_segment([writer.paths.root], config).set_index("timestamp")
    compared = 0
    for k, row in enumerate(live):
        stamp = pd.Timestamp(T0 + timedelta(seconds=k))
        if stamp not in offline.index:
            continue
        compared += 1
        for flag in (gap_column("decoded:ECM_RPM"), arrived_column("decoded:ECM_RPM")):
            assert bool(row[flag]) == bool(offline.loc[stamp, flag]), (k, flag)
    assert compared >= 80


def test_live_can_freshness_reads_the_raw_frames_not_the_kept_ones(
    config: CanaryConfig,
) -> None:
    source = LiveFeatureSource(config=config, bindings=load_bindings(BINDINGS))
    payload = _rpm_payload(800)
    for k in range(800):  # 8 s of an unchanged frame: one kept, 799 dropped
        source.observe(_frame(k / 100.0, payload))
    now = T0 + timedelta(seconds=8.0)
    inputs = source.vehicle_state_inputs(now)
    assert inputs["can_freshness_seconds"] == pytest.approx(0.01, abs=1e-6)
    assert source.channel_age_seconds("decoded:ECM_RPM", now) == pytest.approx(8.0)


# ---------------------------------------------------------------------------
# Dyno motion inputs (H07.3's reader): a steady leg must still build
# ---------------------------------------------------------------------------


def _cruise_session(root: Path) -> Path:
    """A steady 80 km/h leg: the 0x2D1 speed payload never changes, so a
    change-only copy keeps its first frame and a re-write every 9 s."""
    ratio = 0.932
    true_kmh = 80.0
    records: list[TelemetryRecord] = []
    raw = int(round(true_kmh * ratio / 0.1))
    speed_payload = bytes([0, raw & 0xFF, raw >> 8, 0, 0, 0, 0, 0])
    for k in range(-1500, 4500):  # -15 s .. 45 s at 100 Hz
        records.append(_frame(k / 100.0, speed_payload, can_id=0x2D1))
    for t in range(-15, 45):
        stamp = (T0 + timedelta(seconds=t + 0.3)).timestamp()
        records.append(TelemetryRecord.gps(stamp, "speed", true_kmh, "km/h"))
        records.append(TelemetryRecord.gps(stamp, "num_satellites", 10, ""))
        records.append(TelemetryRecord.gps(stamp, "hdop", 0.8, ""))
        records.append(TelemetryRecord.gps(stamp, "altitude", 300.0, "m"))
    session = root / "2026-09-26T120000"
    write_records_to_parquet(sorted(records, key=lambda r: r.timestamp), session)
    return session


def test_dyno_motion_inputs_build_a_steady_cruise_leg_stored_change_only(tmp_path: Path) -> None:
    """``dyno_inputs.read_motion_inputs`` interpolated between samples; on the
    stored changes of a steady leg that read holes (``inputs_gap``) or a
    coarse speed. It now holds each stored value at the channel's period for
    the stale timeout, so the change-only copy builds the same window."""
    from src.analysis.dyno import MotionSources, SpeedSource
    from src.analysis.dyno_inputs import DynoInputs, read_motion_inputs

    full = _cruise_session(tmp_path / "full")
    changed = tmp_path / "changed" / full.name
    read, written = change_only_copy(full, changed)
    assert written < read / 2
    sources = MotionSources(
        speed=[SpeedSource(channel="decoded:VEHICLE_SPEED_2D1", ratio_to_gps=0.932, evidence="t")]
    )
    window = {"start": pd.Timestamp(T0) + pd.Timedelta(seconds=12), "sources": sources}
    window["end"] = window["start"] + pd.Timedelta(seconds=20)
    a = read_motion_inputs(full, **window)  # type: ignore[arg-type]
    b = read_motion_inputs(changed, **window)  # type: ignore[arg-type]
    assert isinstance(a, DynoInputs), a
    assert isinstance(b, DynoInputs), b
    assert b.speed_source == "decoded:VEHICLE_SPEED_2D1"
    np.testing.assert_allclose(b.speed_mps, a.speed_mps, atol=1e-9)
    np.testing.assert_allclose(b.accel_mps2, a.accel_mps2, atol=1e-9)


def test_a_stuck_rule_on_a_broadcast_channel_is_refused_by_name() -> None:
    """A stuck rule counts identical arrived samples; change-only storage (and
    the live features, which see the same frames) never repeats one inside the
    re-write interval, so the rule could not measure a hold (P02.10)."""
    from src.plausibility.limits import PlausibilityProfile, resolve_limits

    def profile(channel: str) -> PlausibilityProfile:
        return PlausibilityProfile.model_validate(
            {
                "schema_version": 1,
                "freshness_k": 3,
                "tiers": {"fast": {"interval_seconds": 1.0, "reason": "test"}},
                "confirm": {
                    check: {"window_seconds": 60, "min_separation_seconds": 1.0}
                    for check in ("range", "stuck", "freshness", "slew")
                },
                "channels": {
                    channel: {
                        "stuck": {
                            "samples": 30,
                            "min_seconds": 40,
                            "modes": ["city"],
                            "reason": "test",
                        }
                    }
                },
            }
        )

    bindings = load_bindings(BINDINGS)
    with pytest.raises(ValueError, match="stuck rule counts identical arrived samples.*P02.10"):
        resolve_limits(profile("decoded:ECM_RPM"), bindings=bindings)
    assert resolve_limits(profile("obd:THROTTLE_POS"), bindings=bindings).channels


# ---------------------------------------------------------------------------
# Verifier round 2026-09-26: events, slow ids, slow replays, every read path
# ---------------------------------------------------------------------------


def test_error_frames_are_tagged_and_every_repeat_is_stored(tmp_path: Path) -> None:
    """SocketCAN's controller error frame arrives as id 0x4; an RX overflow
    repeats the same payload, and each one is an event D02.2 counts."""
    import can

    from src.collection.can_bus import frame_to_record

    overflow = bytes([0, 0x01, 0, 0, 0, 0, 0, 0])
    messages = [
        can.Message(
            arbitration_id=0x4,
            data=overflow,
            is_error_frame=True,
            timestamp=(T0 + timedelta(seconds=k * 0.05)).timestamp(),
        )
        for k in range(20)
    ]
    records = [frame_to_record(m) for m in messages]
    assert all(r.channel == "0x4" and r.metadata["frame_kind"] == "error" for r in records)
    # ISO 15765-4 diagnostic traffic is messages, not values: all kept.
    flt = CanChangeFilter(10.0)
    flow_control = bytes([0x30, 0, 0, 0, 0, 0, 0, 0])
    assert all(flt.keep("0x7E0", flow_control, 1.0 + k / 100) for k in range(5))
    assert all(flt.keep("0x18DAF110", flow_control, 2.0 + k / 100) for k in range(5))
    remote = frame_to_record(can.Message(arbitration_id=0x123, is_remote_frame=True, dlc=0))
    assert remote.metadata["frame_kind"] == "remote"
    plain = frame_to_record(can.Message(arbitration_id=0x1F9, data=_rpm_payload(800)))
    assert "frame_kind" not in plain.metadata  # ordinary frames unchanged

    writer = _writer(tmp_path)
    writer.write_many(records)
    writer.close()
    stored = read_session(writer.paths.root, decoded="as_recorded")
    assert int((stored["channel"] == "0x4").sum()) == 20
    assert writer.can_frames_suppressed == 0
    # A change-only copy of a session recorded with the tag keeps them too.
    full = tmp_path / "full" / "s"
    write_records_to_parquet(records, full)
    copy = tmp_path / "copy" / "s"
    assert change_only_copy(full, copy) == (20, 20)


@pytest.mark.parametrize("period", [1.5, 2.5, 4.0, 7.0])
def test_a_slow_id_never_lapses_while_it_transmits(
    tmp_path: Path, config: CanaryConfig, period: float
) -> None:
    """The re-write fired on the first frame 9 s after the last stored one,
    so a 7 s id reached 14 s old; the filter now keeps a frame when the next
    one would find the stored value past the timeout."""
    flt = CanChangeFilter(10.0)
    stamps = np.arange(0.0, 120.0, period)
    kept = [t for t in stamps if flt.keep("0x1F9", b"\x01", float(t))]
    assert max(np.diff(kept)) < 10.0
    assert len(kept) < len(stamps) or period > 5.0

    records = [_frame(float(t), _rpm_payload(800)) for t in stamps]
    records += [_obd(float(t) + 0.37, "RPM", 800.0) for t in range(120)]
    full, changed = _pair(tmp_path, sorted(records, key=lambda r: r.timestamp))
    a = resample_and_segment([full], config)
    b = resample_and_segment([changed], config)
    pd.testing.assert_frame_equal(a, b, check_exact=True)
    assert not b[gap_column("decoded:ECM_RPM")].any()


async def test_a_change_only_session_is_not_replayed_slower_than_real_time(
    tmp_path: Path,
) -> None:
    import asyncio

    from src.alerts.event_logger import AnomalyEventLogger
    from src.collection.replay import ReplayEmissionStats, replay_session
    from src.control.replay_control import ReplayController, ReplayRefusedError
    from src.storage.broadcast_storage import SlowReplayError, refuse_slow_replay

    root = tmp_path / "sessions"
    session = root / "2026-09-26T120000"
    write_records_to_parquet(_drive(20.0), session, can_storage=CanStorage.change_only(10.0))
    legacy = tmp_path / "legacy"
    write_records_to_parquet(_drive(20.0), legacy)
    refuse_slow_replay(legacy, 0.5)  # stored every frame: any speed
    refuse_slow_replay(session, 1.0)
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    with pytest.raises(SlowReplayError, match="P02.10"):
        await replay_session(
            queue,
            session,
            asyncio.Event(),
            speed=0.5,
            stats=ReplayEmissionStats(),
            broadcast_stale_timeout_seconds=10.0,
        )
    assert queue.empty()
    controller = ReplayController(
        queue=queue,
        writer=ParquetTelemetryWriter(root),
        anomaly_logger=AnomalyEventLogger(tmp_path / "anomalies"),
        storage_root=root,
        broadcast_stale_timeout_seconds=10.0,
    )
    with pytest.raises(ReplayRefusedError, match="speed >= 1"):
        await controller.start(session.name, speed=0.5)
    assert not controller.status().running


async def test_a_session_stored_with_a_longer_timeout_than_the_daemons_is_not_replayed(
    tmp_path: Path,
) -> None:
    """A recording that re-wrote unchanged ids 19 s apart, replayed into a
    daemon whose broadcast class is 10 s, would read every quiet id stale
    between re-writes and store a ``_replay-*`` session whose footer says
    10 s over 19 s gaps. Refused on every replay path, like a slow replay."""
    import asyncio

    from fastapi.testclient import TestClient

    from src.alerts.event_logger import AnomalyEventLogger
    from src.collection.replay import ReplayEmissionStats, make_replay_collector, replay_session
    from src.control.replay_control import ReplayController, ReplayRefusedError
    from src.dashboard.api import create_app
    from src.storage.broadcast_storage import (
        ReplayTimeoutMismatchError,
        UnfaithfulReplayError,
        refuse_unfaithful_replay,
    )
    from tests.configs import isolated_config

    root = tmp_path / "sessions"
    config = isolated_config(tmp_path, "daemon", storage=root)
    assert config.collection.broadcast_stale_timeout_seconds == 10.0
    session = root / "2026-09-26T120000"
    write_records_to_parquet(_drive(20.0), session, can_storage=CanStorage.change_only(20.0))
    legacy = root / "2026-09-26T130000"
    write_records_to_parquet(_drive(20.0), legacy)
    match = "at least 20 s"
    refuse_unfaithful_replay(session, 1.0, stale_timeout_seconds=20.0)  # same timeout
    refuse_unfaithful_replay(session, 1.0, stale_timeout_seconds=30.0)  # longer: holds more
    refuse_unfaithful_replay(legacy, 1.0, stale_timeout_seconds=2.0)  # every frame stored
    with pytest.raises(ReplayTimeoutMismatchError, match=match):
        refuse_unfaithful_replay(session, 1.0, stale_timeout_seconds=10.0)
    # The same family the read paths raise, and the one every replay path catches.
    assert issubclass(ReplayTimeoutMismatchError, BroadcastTimeoutMismatchError)
    assert issubclass(ReplayTimeoutMismatchError, UnfaithfulReplayError)

    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    with pytest.raises(ReplayTimeoutMismatchError, match=match):
        await replay_session(
            queue,
            session,
            asyncio.Event(),
            speed=4.0,
            stats=ReplayEmissionStats(),
            broadcast_stale_timeout_seconds=10.0,
        )
    assert queue.empty()

    controller = ReplayController(
        queue=queue,
        writer=ParquetTelemetryWriter(root),
        anomaly_logger=AnomalyEventLogger(tmp_path / "anomalies"),
        storage_root=root,
        broadcast_stale_timeout_seconds=10.0,
    )
    with pytest.raises(ReplayRefusedError, match=match):
        await controller.start(session.name, speed=4.0)
    assert not controller.status().running

    # HTTP: 422 with the named cause.
    app = create_app(
        config,
        anomalies_dir=tmp_path / "anomalies",
        replay_controller=controller,
        control_token="secret",
    )
    response = TestClient(app).post(
        "/control/replay",
        json={"session_id": session.name, "speed": 4.0},
        headers={"Authorization": "Bearer secret"},
    )
    assert response.status_code == 422, response.text
    assert "P02.10" in response.json()["detail"]

    # The YAML collector logs the refusal and idles until stopped.
    data = config.model_dump()
    data["collection"]["replay_session"] = str(session)
    data["collection"]["replay_speed"] = 4.0
    data["collection"]["replay_loop"] = False
    starter = make_replay_collector(CanaryConfig.model_validate(data))
    stop = asyncio.Event()
    task = asyncio.create_task(starter(queue, stop))
    await asyncio.sleep(0.2)
    assert not task.done() and queue.empty()
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)


def test_every_training_read_path_refuses_a_shorter_read_timeout(
    tmp_path: Path, config: CanaryConfig
) -> None:
    from src.preprocessing.derived import resample_reduced
    from src.preprocessing.feature_pipeline import (
        default_decode_inputs,
        reduce_session,
        resample_long_session,
        resample_session_bounded,
    )

    session = tmp_path / "2026-09-26T120000"
    write_records_to_parquet(_drive(60.0), session, can_storage=CanStorage.change_only(20.0))
    recipe = resample_config_for(config)  # 10 s
    decoders = default_decode_inputs()
    inputs = (decoders.bindings, decoders.dbc, decoders.did_map)
    match = "at least 20 s"
    with pytest.raises(BroadcastTimeoutMismatchError, match=match):
        resample_session_bounded(session, *inputs, recipe, store=None)
    reduced = reduce_session(session, *inputs, recipe)
    assert reduced.broadcast_stale_timeout_seconds == 20.0
    with pytest.raises(BroadcastTimeoutMismatchError, match=match):
        resample_reduced(reduced, recipe)
    with pytest.raises(BroadcastTimeoutMismatchError, match=match):
        resample_long_session(
            read_session(session, decoded="as_recorded"), *inputs, recipe, session_name="s"
        )
    with pytest.raises(BroadcastTimeoutMismatchError, match=match):
        resample_and_segment([session], config, value="value")
    with pytest.raises(BroadcastTimeoutMismatchError, match=match):
        resample_and_segment([session], config)
    # Read with the recorded timeout, it resamples.
    data = config.model_dump()
    data["collection"]["broadcast_stale_timeout_seconds"] = 20.0
    assert not resample_reduced(
        reduced, resample_config_for(CanaryConfig.model_validate(data))
    ).empty


def test_an_injected_broadcast_dropout_arrives_until_the_ceiling() -> None:
    from src.preprocessing.anomaly_injection import AnomalyInjector

    stamps = pd.date_range(T0, periods=40, freq="1s")
    channel = "decoded:ECM_RPM"
    frame = pd.DataFrame(
        {
            "session": "s",
            "timestamp": stamps,
            channel: 6400.0,
            gap_column(channel): False,
            arrived_column(channel): True,
            "obd:RPM": 800.0,
            gap_column("obd:RPM"): False,
            arrived_column("obd:RPM"): True,
        }
    )
    injector = AnomalyInjector(frame)
    injector.dropout(channel=channel, start=stamps[10], end=stamps[30], max_gap_seconds=10.0)
    injector.dropout(channel="obd:RPM", start=stamps[10], end=stamps[30], max_gap_seconds=2.0)
    out = injector.df
    # Held (and arrived, as the resampler reads a broadcast row) until 10 s in.
    assert out[arrived_column(channel)].iloc[10:20].all()
    assert not out[arrived_column(channel)].iloc[20:30].any()
    assert (out[gap_column(channel)].iloc[20:30]).all()
    assert not out[arrived_column("obd:RPM")].iloc[10:30].any()


def test_rows_of_a_change_only_spec_are_never_a_rate_or_a_bin_count(tmp_path: Path) -> None:
    from src.analysis.compare import binned_summary
    from src.analysis.tuning_digest import _achieved_hz, _rate_summary

    full, changed = _pair(tmp_path, _drive(60.0))
    specs = ["decoded:ECM_RPM", "obd:RPM"]
    assert set(_achieved_hz(full, specs)) == set(specs)
    assert set(_achieved_hz(changed, specs)) == {"obd:RPM"}
    summary = _rate_summary(changed, specs)
    assert "P02.10" in str(summary["decoded:ECM_RPM"]["refused"])
    assert summary["obd:RPM"]["observed_hz"] is not None
    assert not binned_summary(full, "decoded:ECM_RPM", "obd:RPM").empty
    with pytest.raises(ChangeOnlyStorageError, match="binned_summary"):
        binned_summary(changed, "decoded:ECM_RPM", "obd:RPM")


def test_a_session_without_can_frames_gets_no_storage_footer(tmp_path: Path) -> None:
    """An UpRev import (no CAN) must not read as change-only and trip the
    frame-count refusals."""
    writer = _writer(tmp_path)
    writer.write_many([_obd(float(t), "RPM", 800.0) for t in range(30)])
    writer.close()
    chunk = next(writer.paths.root.glob("telemetry-*.parquet"))
    assert FOOTER_KEY not in (pq.read_schema(chunk).metadata or {})
    refuse_change_only(writer.paths.root, reader="frame counter")


# ---------------------------------------------------------------------------
# Verifier round 2 (2026-09-26): row-pairing and row-counting callers
# ---------------------------------------------------------------------------


def test_row_pairing_and_counting_callers_refuse_a_change_only_decoded_provider(
    tmp_path: Path,
) -> None:
    """``read_channels`` returns a change-only broadcast spec's payload
    changes, not its frames. Every caller that pairs those rows one-to-one or
    counts them goes through ``channels.as_of_refusal`` / ``refuse_as_of``;
    the same drive stored every frame reads as before."""
    from types import SimpleNamespace

    from src.analysis.channels import as_of_refusal, refuse_as_of
    from src.analysis.lambda_delivery import target_source_agreement
    from src.analysis.pairing import to_seconds
    from src.analysis.rom_trace import AxisTrace, TableInfo, TableTrace, cell_residency
    from src.analysis.tuning import DeliveryInputs, _attach, _check_clock
    from src.analysis.tuning_digest import (
        _did_pull_inventory,
        _measured_afr_summary,
        _wot_inventory,
    )
    from src.analysis.tuning_frame import LoadWotCriterion, build_tuning_frame
    from src.analysis.warmup_checks import runtime_clock_report

    full, changed = _pair(tmp_path, _drive(60.0))
    rpm, poll = "decoded:ECM_RPM", "obd:RPM"
    long_full = read_channels(full, [rpm, poll], acquired_time=True)
    long_changed = read_channels(changed, [rpm, poll], acquired_time=True)

    # The helper: names the change-only spec, never the polled one.
    assert as_of_refusal(long_full, reader="r") is None
    message = as_of_refusal(long_changed, reader="r")
    assert message is not None and rpm in message and "P02.10" in message
    assert poll not in message
    assert as_of_refusal(long_changed, reader="r", specs=[poll]) is None
    with pytest.raises(ChangeOnlyStorageError, match="store_every_frame") as caught:
        refuse_as_of(long_changed, reader="r")
    assert caught.value.reader == "r"

    # tuning: context pairing and the clock check.
    policy = PairingPolicy.within("250ms", reference="a")
    rows = {s: long_changed[long_changed["spec"] == s] for s in (rpm, poll)}
    times = to_seconds(rows[poll]["timestamp"])
    full_rpm = long_full[long_full["spec"] == rpm]
    assert np.isfinite(_attach(times, full_rpm, policy)).any()
    with pytest.raises(ChangeOnlyStorageError, match="context pairing"):
        _attach(times, rows[rpm], policy)
    inputs = DeliveryInputs(
        target=rows[poll], sentinel=None, measured=rows[poll], rpm=rows[rpm],
        fuel_status=None, load=None,
    )  # fmt: skip
    with pytest.raises(ChangeOnlyStorageError, match="rpm pairing"):
        _check_clock(inputs, policy)

    # lambda_delivery: every read_channels frame becomes arrays in one place.
    agreement = {
        "policy": PairingPolicy.within("250ms"),
        "closed_loop_band": 0.02,
        "steady_window_s": 1.0,
    }
    assert target_source_agreement(full, rpm, poll, **agreement)["n_pairs"] > 0  # type: ignore[arg-type]
    with pytest.raises(ChangeOnlyStorageError, match="lambda delivery"):
        target_source_agreement(changed, rpm, poll, **agreement)  # type: ignore[arg-type]

    # warmup_checks: reads, rates and resets of a clock channel.
    assert runtime_clock_report(long_full, runtime_spec=rpm).reads > 0
    assert runtime_clock_report(long_changed, runtime_spec=poll).reads > 0
    with pytest.raises(ChangeOnlyStorageError, match="warmup"):
        runtime_clock_report(long_changed, runtime_spec=rpm)

    # rom_trace: cell residency counts and times pairs of stored rows.
    axis = {"axis_mapping": "verified", "scale": 1.0, "offset": 0.0, "evidence": "test"}
    table = TableInfo(
        kind="fuel_targets", title="t", x_label="x", x_unit="rpm", y_label="y",
        y_unit="rpm", unit="afr",
        trace=TableTrace(
            x=AxisTrace(channel=rpm, **axis), y=AxisTrace(channel=poll, **axis)  # type: ignore[arg-type]
        ),
    )  # fmt: skip
    target = SimpleNamespace(load_breakpoints=[800.0, 2000.0], rpm_breakpoints=[800.0, 2000.0])
    residency = {
        "table": table,
        "revision": "r1",
        "revision_source": "test",
        "calibration": SimpleNamespace(target_map=lambda _rev: target),
        "bindings_path": BINDINGS,
        "dids_path": Path("data/dids.yaml"),
    }
    assert cell_residency(full, **residency)["available"] is True  # type: ignore[arg-type]
    refused = cell_residency(changed, **residency)  # type: ignore[arg-type]
    assert refused["available"] is False and refused["reason"] == "change_only_storage"
    assert rpm in refused["message"]

    # tuning_frame: a decoded provider is an unavailable role, by name.
    providers = tmp_path / "tuning_providers.yaml"
    providers.write_text(
        "schema_version: 1\n"
        "providers:\n"
        f"  - {{role: rpm, spec: '{rpm}'}}\n"
        f"  - {{role: operator_demand, spec: '{poll}'}}\n"
    )
    frame_full = build_tuning_frame(full, providers_path=providers, afr_stoich=14.7)
    frame_changed = build_tuning_frame(changed, providers_path=providers, afr_stoich=14.7)
    assert frame_full.capability["rpm"]["available"] is True
    assert frame_changed.capability["rpm"]["available"] is False
    assert "P02.10" in str(frame_changed.capability["rpm"]["reason"])
    assert frame_changed.capability["operator_demand"]["available"] is True

    # tuning_digest: tagged WOT seconds and the pull evidence row counts.
    for session in (full, changed):
        start = (T0 + timedelta(seconds=5)).timestamp()
        end = (T0 + timedelta(seconds=55)).timestamp()
        (session / "tags.jsonl").write_text(
            json.dumps({"t": start, "label": "tuning_pull_start"})
            + "\n"
            + json.dumps({"t": end, "label": "tuning_pull_end"})
            + "\n"
        )
    wot = {
        "rpm_spec": rpm,
        "did_continuous": False,
        "load_wot": LoadWotCriterion(
            load_floor_pct=85.0, rpm_floor=900.0, rpm_bands=(900.0, 1500.0, 2500.0)
        ),
    }
    assert _wot_inventory(full, **wot).source == "tagged"  # type: ignore[arg-type]
    inventory = _wot_inventory(changed, **wot)  # type: ignore[arg-type]
    assert inventory.source == "unavailable" and "tagged WOT seconds" in str(inventory.reason)
    capabilities = {
        "rpm": {"available": True, "provider": rpm},
        "operator_demand": {"available": True, "provider": rpm},
    }
    pulls = {"target_lookup_ready": False, "rpm_floor": 900.0}
    assert _did_pull_inventory(full, role_capabilities=capabilities, **pulls)["available"]  # type: ignore[arg-type]
    evidence = _did_pull_inventory(changed, role_capabilities=capabilities, **pulls)  # type: ignore[arg-type]
    assert evidence["available"] is False and "pull evidence rpm" in evidence["reason"]
    polled_rpm = {**capabilities, "rpm": {"available": True, "provider": poll}}
    counts = _did_pull_inventory(changed, role_capabilities=polled_rpm, **pulls)  # type: ignore[arg-type]
    assert counts["available"] is True and counts["operator_demand_samples"] is None
    assert "P02.10" in counts["refused_counts"]["operator_demand_samples"]
    transfer = SimpleNamespace(censored_floor_afr=10.0, censored_ceiling_afr=20.0)
    afr_calibration = SimpleNamespace(voltage_transfer=transfer, provenance={})
    bank = {"measured_afr_b1": {"available": True, "provider": rpm}}
    summary = _measured_afr_summary(
        changed,
        calibration=afr_calibration,  # type: ignore[arg-type]
        role_capabilities=bank,
    )
    assert summary["available"] is False  # a refused bank is no measured AFR
    assert summary["banks"] and "P02.10" in summary["banks"][0]["refused"]


def test_the_ground_truth_fixture_tool_refuses_a_change_only_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its per-id frame stride and its footer-less output both assume every
    frame was stored; a change-only slice would read back as the bus's."""
    import sys

    from tools.make_ground_truth_fixture import extract, main

    full, changed = _pair(tmp_path, _drive(30.0))
    start = pd.Timestamp(T0)
    assert not extract(full, start, 20.0, tmp_path / "out-full", can_stride=5).empty
    out = tmp_path / "out-changed"
    with pytest.raises(ChangeOnlyStorageError, match="ground-truth fixture"):
        extract(changed, start, 20.0, out, can_stride=5)
    assert not out.exists()
    argv = ["make_ground_truth_fixture", "--session", str(changed), "--start", T0.isoformat()]
    monkeypatch.setattr(sys, "argv", [*argv, "--out", str(out)])
    assert main() == 2
    assert "P02.10" in capsys.readouterr().err
    assert not out.exists()


def test_a_broadcast_freshness_threshold_under_the_stale_timeout_is_disabled_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: CanaryConfig
) -> None:
    """A broadcast id's unchanged value arrives once per re-write, so a
    freshness threshold shorter than the stale timeout would call every hold
    stale; the channel reports freshness unchecked with the cause."""
    import yaml

    from src.plausibility import limits as limits_module
    from src.plausibility.limits import PlausibilityProfile, resolve_limits

    def profile(interval: float) -> PlausibilityProfile:
        return PlausibilityProfile.model_validate(
            {
                "schema_version": 1,
                "freshness_k": 3,
                "tiers": {
                    "broadcast": {"interval_seconds": interval, "reason": "test"},
                    "fast": {"interval_seconds": 1.0, "reason": "test"},
                },
                "confirm": {
                    check: {"window_seconds": 60, "min_separation_seconds": 1.0}
                    for check in ("range", "stuck", "freshness", "slew")
                },
            }
        )

    bindings = load_bindings(BINDINGS)
    short = resolve_limits(profile(1.0), bindings=bindings).channels
    rpm = short["decoded:ECM_RPM"]
    assert rpm.freshness_threshold_seconds is None
    assert rpm.freshness_disabled_reason is not None
    assert "P02.10" in rpm.freshness_disabled_reason and "3 s" in rpm.freshness_disabled_reason
    assert rpm.coverage()["freshness"].startswith("unchecked: freshness threshold 3 s")
    # A polled channel on a 3 s threshold is unaffected.
    assert short["obd:RPM"].freshness_threshold_seconds == 3.0
    # At or over the timeout the check runs; the floor follows the setting.
    assert (
        resolve_limits(profile(4.0), bindings=bindings)
        .channels["decoded:ECM_RPM"]
        .freshness_threshold_seconds
        == 12.0
    )
    longer = resolve_limits(profile(4.0), bindings=bindings, broadcast_stale_timeout_seconds=15.0)
    assert longer.channels["decoded:ECM_RPM"].freshness_threshold_seconds is None

    # resolve_limits_for_config takes the floor from the config.
    path = tmp_path / "plausibility.yaml"
    path.write_text(yaml.safe_dump(profile(4.0).model_dump(mode="json", exclude_none=True)))
    monkeypatch.setattr(limits_module, "plausibility_path", lambda _config: path)
    data = config.model_dump()
    data["collection"]["broadcast_stale_timeout_seconds"] = 15.0
    catalog = limits_module.resolve_limits_for_config(
        CanaryConfig.model_validate(data), bindings=bindings
    )
    reason = catalog.channels["decoded:ECM_RPM"].freshness_disabled_reason
    assert reason is not None and "15 s" in reason
