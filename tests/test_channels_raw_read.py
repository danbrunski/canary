"""P02.2 — ``read_channels`` reads only the rows and columns its specs use,
and a report asking one session for many channels reads it once.

The filtered, narrowed and shared reads must return exactly what the
whole-session read returned, for every source a spec can name. Two committed
slices cover them all: the ground-truth drive (raw CAN frames, decoded
broadcast, GPS, Mode 01) and a tuning capture (Mode 01, DIDs, memory
channels).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

import src.analysis.channels as channels
import src.preprocessing.loader as loader
from src.analysis.channels import (
    raw_read_filters,
    read_channels,
    read_spec_rows,
    shared_session_read,
)
from src.paths import DIDS_YAML
from src.preprocessing.loader import STORED_COLUMNS, read_session
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord

FIXTURES = Path(__file__).parent / "fixtures"
DRIVE = FIXTURES / "ground_truth" / "drive-slice"
CAPTURE = FIXTURES / "lambda_delivery" / "_capture-tuning-2026-09-21T223734"


def _specs(session: Path) -> list[str]:
    """One spec per stored channel, in every form a reader takes."""
    import yaml

    frame = read_session(session)
    pairs = {(str(s), str(c)) for s, c in frame[["source", "channel"]].itertuples(index=False)}
    specs: set[str] = set()
    for source, channel in pairs:
        if source == "obd" and channel.startswith("mem:"):
            specs.add(channel)
        elif source == "obd" and not channel.startswith("DID_"):
            specs.add(f"obd:{channel}")
        elif source in ("gps", "decoded"):
            specs.add(f"{source}:{channel}")
    for entry in yaml.safe_load(DIDS_YAML.read_text()).get("dids") or []:
        if ("obd", f"DID_{int(entry['did']):04X}") in pairs:
            specs.add(f"did:{entry['name']}")
    decoded = sorted(s.split(":", 1)[1] for s in specs if s.startswith("decoded:"))
    specs.update(f"can:{name}" for name in decoded[:3])
    return sorted(specs)


def _unfiltered(
    monkeypatch: pytest.MonkeyPatch, session: Path, specs: list[str], **kwargs: bool
) -> pd.DataFrame:
    """Today's reference: the whole session, every row and every column, with
    the clock put on all of it (no P02.19 narrowing)."""
    with monkeypatch.context() as patch:
        patch.setattr(channels, "_raw_read_filters", lambda parsed, catalogs: None)
        patch.setattr(channels, "_read_columns", lambda parsed, acquired_time: None)
        patch.setattr(channels, "_narrowing", _no_narrowing)
        return read_channels(session, specs, **kwargs)


def _no_narrowing(parsed: object, catalogs: object) -> channels._Narrowing:
    return lambda frame: frame.copy()


@pytest.mark.parametrize("session", [DRIVE, CAPTURE], ids=["drive", "capture"])
@pytest.mark.parametrize("acquired_time", [False, True])
def test_a_filtered_read_returns_exactly_the_whole_session_read(
    monkeypatch: pytest.MonkeyPatch, session: Path, acquired_time: bool
) -> None:
    specs = _specs(session)
    sources = {spec.split(":", 1)[0] for spec in specs}
    # Every filter branch is exercised by one slice or the other.
    assert sources >= ({"obd", "decoded", "can", "gps"} if session == DRIVE else {"obd", "did"})
    for spec in specs:
        expected = _unfiltered(monkeypatch, session, [spec], acquired_time=acquired_time)
        got = read_channels(session, [spec], acquired_time=acquired_time)
        assert not expected.empty, spec
        pd.testing.assert_frame_equal(
            got.reset_index(drop=True), expected.reset_index(drop=True), check_exact=True
        )
    together_expected = _unfiltered(monkeypatch, session, specs, acquired_time=acquired_time)
    together = read_channels(session, specs, acquired_time=acquired_time)
    pd.testing.assert_frame_equal(
        together.reset_index(drop=True),
        together_expected.reset_index(drop=True),
        check_exact=True,
    )


def test_the_acquisition_clock_has_one_unit_whatever_rows_were_read() -> None:
    """``fillna`` once took the stored unit (µs) when no row carried an
    acquisition stamp and ns when one did, so a narrower read changed the
    dtype. A GPS-only read has no acquisition stamps at all."""
    gps = read_channels(DRIVE, ["gps:speed"], acquired_time=True)
    obd = read_channels(DRIVE, ["obd:RPM"], acquired_time=True)
    assert str(gps["timestamp"].dtype) == str(obd["timestamp"].dtype) == "datetime64[ns, UTC]"


def test_an_unresolvable_spec_adds_no_rows_and_still_fails_on_its_own() -> None:
    """``raw_read_filters`` is the shared read for callers that read once and
    ask spec by spec: one bad name contributes nothing to it."""
    assert raw_read_filters(["did:NOT_A_DID", "obd:RPM"]) == [[("source", "==", "obd")]]
    with pytest.raises(channels.ChannelSpecError):
        read_channels(CAPTURE, ["did:NOT_A_DID"])


# --- Columns (P02.2 remainder, 2026-10-07) ---------------------------------


def _recording_reads(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, tuple[str, ...] | None]]:
    """Every chunk read's (session, columns), as parquet was asked for them."""
    reads: list[tuple[str, tuple[str, ...] | None]] = []
    real = pd.read_parquet

    def recording(path: object, *args: object, **kwargs: object) -> pd.DataFrame:
        columns = kwargs.get("columns")
        reads.append((Path(str(path)).parent.name, None if columns is None else tuple(columns)))  # type: ignore[arg-type]
        return real(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(loader.pd, "read_parquet", recording)
    return reads


@pytest.mark.parametrize(
    ("filters", "decoded"),
    [
        (None, "on_read"),
        ([("source", "==", "obd")], "on_read"),
        ([[("source", "==", "decoded"), ("channel", "==", "ECM_RPM")]], "on_read"),
        ([("source", "in", ["can", "gps"])], "on_read"),
        (None, "as_recorded"),
    ],
    ids=["whole", "polled", "decoded", "frames", "as-recorded"],
)
def test_a_narrowed_read_is_the_whole_read_restricted_to_its_columns(
    filters: object, decoded: loader.DecodedRows
) -> None:
    """Same rows, same order, same dtypes; only the columns differ. The decode
    on read still sees ``payload`` when a ``decoded:`` row is asked for."""
    columns = ["timestamp", "source", "channel", "value_number", "unit"]
    whole = read_session(DRIVE, filters=filters, decoded=decoded)
    narrowed = read_session(DRIVE, filters=filters, decoded=decoded, columns=columns)
    assert not narrowed.empty
    assert "payload" not in narrowed.columns and "value" not in narrowed.columns
    pd.testing.assert_frame_equal(narrowed, whole.loc[:, list(narrowed.columns)], check_exact=True)
    assert narrowed.attrs == whole.attrs


def test_read_session_refuses_a_column_it_does_not_store() -> None:
    with pytest.raises(ValueError, match="stored columns"):
        read_session(DRIVE, columns=["timestamp", "acquired_at"])
    assert "payload" in STORED_COLUMNS


def test_read_channels_reads_payload_only_for_a_spec_that_decodes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A polled read never asks parquet for ``payload`` (a fifth of a session's
    bytes); ``can:`` and ``decoded:`` do, and the acquisition clock adds only
    ``metadata_json``."""
    reads = _recording_reads(monkeypatch)
    read_channels(DRIVE, ["obd:RPM", "gps:speed"])
    assert reads and all(
        columns is not None and "payload" not in columns and "metadata_json" not in columns
        for _, columns in reads
    )
    reads.clear()
    read_channels(DRIVE, ["obd:RPM"], acquired_time=True)
    assert all(columns is not None and "metadata_json" in columns for _, columns in reads)
    assert all("payload" not in columns for _, columns in reads if columns is not None)
    reads.clear()
    decoded = read_channels(DRIVE, ["decoded:ECM_RPM"])
    assert not decoded.empty
    assert all(columns is not None and "payload" in columns for _, columns in reads)


# --- One read for many calls ------------------------------------------------


@pytest.mark.parametrize("session", [DRIVE, CAPTURE], ids=["drive", "capture"])
def test_a_shared_read_serves_every_spec_exactly_from_one_read(
    monkeypatch: pytest.MonkeyPatch, session: Path
) -> None:
    """The tuning digest's shape: many calls, one spec each, both clocks.
    Each answer equals today's whole-session answer, and the disk is read
    once."""
    # One spec per source (every filter branch); every spec of three real
    # captures was checked the same way when this landed (P02.2 evidence).
    by_source: dict[str, str] = {}
    for spec in _specs(session):
        by_source.setdefault(spec.split(":", 1)[0], spec)
    specs = sorted(by_source.values())
    expected = {
        (spec, clock): _unfiltered(monkeypatch, session, [spec], acquired_time=clock)
        for spec in specs
        for clock in (False, True)
    }
    calls: list[Path] = []
    real = channels.read_session

    def counting(path: Path, **kwargs: object) -> pd.DataFrame:
        calls.append(path)
        return real(path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(channels, "read_session", counting)
    with shared_session_read(session, specs) as shared:
        for (spec, clock), want in expected.items():
            got = read_channels(session, [spec], acquired_time=clock)
            pd.testing.assert_frame_equal(got, want, check_exact=True)
            assert got.attrs == want.attrs, spec
        rows = read_spec_rows(session, specs[:2], acquired_time=True)
    assert calls == [session]
    assert shared.disk_reads == 1 and shared.served == len(expected) + 1
    assert set(read_channels(rows, specs[:2], acquired_time=True)["spec"]) == set(specs[:2])


def test_a_spec_the_shared_read_does_not_hold_is_read_from_disk_not_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing a spec from the shared set costs a read, never a row; a
    nested block over rows already held reuses the outer read."""
    calls: list[Path] = []
    real = channels.read_session

    def counting(path: Path, **kwargs: object) -> pd.DataFrame:
        calls.append(path)
        return real(path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(channels, "read_session", counting)
    with shared_session_read(DRIVE, ["obd:RPM", "decoded:ECM_RPM"]) as outer:
        with shared_session_read(DRIVE, ["obd:SPEED"]) as inner:
            assert inner is outer
            assert not read_channels(DRIVE, ["obd:SPEED"]).empty
        assert outer.disk_reads == 1
        gps = read_channels(DRIVE, ["gps:speed"])
        assert not gps.empty and len(calls) == 2  # the shared read, then gps from disk
        frames = read_channels(DRIVE, ["can:ECM_RPM"])  # payload is not held either
        assert not frames.empty and len(calls) == 3
        other = read_channels(CAPTURE, ["obd:RPM"])  # another session
        assert not other.empty and len(calls) == 4
    monkeypatch.setattr(channels, "read_session", real)
    pd.testing.assert_frame_equal(gps, read_channels(DRIVE, ["gps:speed"]), check_exact=True)


def test_a_shared_read_still_says_which_kind_of_empty(tmp_path: Path) -> None:
    """A DID whose only rows are a pre-K01.1 K-line module's is refused by name,
    not returned empty, whether the rows come from the disk or the shared read;
    a DID never read is an empty answer, the same both ways."""
    import yaml

    entries = yaml.safe_load(DIDS_YAML.read_text())["dids"]
    foreign, absent = entries[0], entries[1]
    t0 = pd.Timestamp("2026-08-05T12:00:00Z").to_pydatetime()
    session = tmp_path / "_capture-2026-08-05T120000"
    write_records_to_parquet(
        [
            TelemetryRecord(
                timestamp=t0,
                source="kline",
                channel=f"DID_{int(foreign['did']):04X}",
                value="00f5",
            ),
            TelemetryRecord(timestamp=t0, source="obd", channel="RPM", value=800.0),
        ],
        session,
    )
    specs = [f"did:{foreign['name']}", f"did:{absent['name']}", "obd:RPM"]
    with pytest.raises(channels.ChannelSpecError, match="pre-K01.1 K-line") as on_disk:
        read_channels(session, [specs[0]])
    with shared_session_read(session, specs) as shared:
        with pytest.raises(channels.ChannelSpecError, match="pre-K01.1 K-line") as shared_error:
            read_channels(session, [specs[0]], acquired_time=True)
        assert read_channels(session, [specs[1]]).empty
        assert shared.disk_reads == 1
    assert str(shared_error.value) == str(on_disk.value)


def _rpm_frames() -> list[tuple[str, str, object]]:
    from src.decode.on_read import can_channel_spellings

    return [("source", "==", "can"), ("channel", "in", can_channel_spellings([0x1F9]))]


@pytest.mark.parametrize(
    ("filters", "admits_decoded"),
    [
        pytest.param([_rpm_frames()], False, id="can-only-0x1F9"),
        pytest.param([("source", "!=", "decoded")], False, id="not-decoded"),
        pytest.param(
            [_rpm_frames(), [("source", "==", "decoded"), ("channel", "==", "ECM_RPM")]],
            True,
            id="control-frames-and-their-decode",
        ),
        pytest.param([("source", "!=", "obd")], True, id="control-not-obd"),
    ],
)
def test_a_filtered_read_returns_only_rows_its_filter_passes(
    filters: list[object], admits_decoded: bool
) -> None:
    """P02.18: a decode on read used to add a ``decoded`` row for every catalog
    frame a filter passed, even when the filter excluded ``decoded`` rows
    (``{"can": 3825, "decoded": 3825}`` for the 0x1F9 frames; 26,909 decoded
    rows under ``source != "decoded"``). A filtered read is the whole read's
    rows that pass the filter, exactly, and a filter that admits decoded rows
    still gets them."""
    filtered = read_session(DRIVE, filters=filters)
    whole = loader.filter_rows(read_session(DRIVE), filters)
    pd.testing.assert_frame_equal(filtered, whole, check_exact=True)
    sources = filtered["source"].value_counts().to_dict()
    assert sources.get("can", 0) > 0
    if admits_decoded:
        assert sources.get("decoded", 0) > 0
    else:
        assert "decoded" not in sources
    parts = pd.concat(list(loader.iter_session_parts(DRIVE, filters=filters)), ignore_index=True)
    pd.testing.assert_frame_equal(parts, filtered, check_exact=True)


def test_a_can_only_read_counts_its_frames_and_nothing_else() -> None:
    """The verifier's repro, by count: the 0x1F9 frames alone, and the decode
    of them only when ``decoded`` rows are asked for."""
    frames = read_session(DRIVE, filters=[_rpm_frames()])
    assert frames["source"].value_counts().to_dict() == {"can": 3825}
    both = read_session(
        DRIVE,
        filters=[_rpm_frames(), [("source", "==", "decoded"), ("channel", "==", "ECM_RPM")]],
    )
    assert both["source"].value_counts().to_dict() == {"can": 3825, "decoded": 3825}


@pytest.mark.parametrize("session", [DRIVE, CAPTURE], ids=["drive", "capture"])
@pytest.mark.parametrize("acquired_time", [False, True])
def test_a_frame_read_spec_by_spec_is_exactly_the_whole_frame_read(
    monkeypatch: pytest.MonkeyPatch, session: Path, acquired_time: bool
) -> None:
    """P02.19: a frame handed to ``read_channels`` is narrowed to the rows its
    specs' readers select before the clock goes on. Every spec, every source,
    reads what the whole frame read, ``attrs`` included."""
    frame = read_session(session)
    for spec in _specs(session):
        with monkeypatch.context() as patch:
            patch.setattr(channels, "_narrowing", _no_narrowing)
            expected = read_channels(frame, [spec], acquired_time=acquired_time)
        got = read_channels(frame, [spec], acquired_time=acquired_time)
        assert not expected.empty, spec
        pd.testing.assert_frame_equal(got, expected, check_exact=True)
        assert got.attrs == expected.attrs, spec
    # The caller's frame is never the one the clock rewrote.
    pd.testing.assert_frame_equal(frame, read_session(session), check_exact=True)


def test_reading_one_frame_spec_by_spec_parses_each_row_once_at_most(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P02.19: ``read_delivery_inputs`` and ``lambda_delivery_evidence`` read a
    session once and ask its frame for one channel at a time. Each call put
    the whole frame on the acquisition clock -- 39 ``metadata_json`` parses
    of every polled row per session in the evidence script, 87 % of its time.
    Now N single-spec calls parse no more rows, in total, than the frame
    holds."""
    rows = read_session(CAPTURE, filters=[("source", "==", "obd")])
    specs = [spec for spec in _specs(CAPTURE) if not spec.startswith(("decoded:", "can:"))]
    assert len(specs) >= 10
    parsed: list[int] = []
    clock = channels._on_acquisition_clock

    def counting(frame: pd.DataFrame, session: object) -> object:
        parsed.append(len(frame))
        return clock(frame, session)  # type: ignore[arg-type]

    monkeypatch.setattr(channels, "_on_acquisition_clock", counting)
    for spec in specs:
        assert not read_channels(rows, [spec], acquired_time=True).empty, spec
    assert len(parsed) == len(specs)
    assert max(parsed) < len(rows)
    assert sum(parsed) <= len(rows)
