"""Tests for the engineering-units channel reader (roadmap Phase 1.1/1.2).

The reader exists because one quantity is stored three ways and each shape
bit during the 2026-08 campaign. These tests pin the specific traps, not just
the happy path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from src.analysis.channels import (
    TIMESTAMP_BASIS_COLUMN,
    ChannelSpecError,
    parse_spec,
    read_acquisition_frame,
    read_aligned,
    read_channels,
)
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord

BINDINGS = Path("data/bindings.yaml")
DIDS = Path("data/dids.yaml")
T0 = datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)


def _session(tmp_path: Path, records: list[TelemetryRecord]) -> Path:
    out = tmp_path / "2026-08-05T120000"
    write_records_to_parquet(records, out)
    return out


def _rpm_frame(rpm: float) -> bytes:
    """0x1F9 bytes 2-3, big-endian, 8 counts per rpm (the ECM_RPM binding)."""
    raw = int(round(rpm * 8))
    return bytes([0x00, 0x00, (raw >> 8) & 0xFF, raw & 0xFF, 0, 0, 0, 0])


def test_parse_spec_defaults_to_obd_and_rejects_nonsense() -> None:
    bare, qualified = parse_spec("RPM"), parse_spec("obd:RPM")
    assert (bare.source, bare.name) == (qualified.source, qualified.name)
    # `text` keeps the caller's spelling, so aligned frames get the column
    # name that was actually asked for.
    assert bare.text == "RPM"
    assert parse_spec("did:0x1225").source == "did"
    with pytest.raises(ChannelSpecError):
        parse_spec("bogus:RPM")
    with pytest.raises(ChannelSpecError):
        parse_spec("obd:")


def test_decoded_rows_are_scaled_not_taken_at_face_value(tmp_path: Path) -> None:
    """``decoded:`` carries the RAW integer on purpose — the reader must scale it.

    ``ECM_RPM`` is 8 counts/rpm, so a raw 9600 is 1200 rpm. Reading
    ``value_number`` directly is what first looked like a factor-of-8 decoder
    bug (it isn't one: src/decode/frame_decoder.py keeps rows raw so training's
    scaler stays comparable across sessions). The session stores only the
    frame (P02.8); the row is decoded on read.
    """
    session = _session(
        tmp_path,
        [
            TelemetryRecord.can(T0.timestamp(), 0x1F9, _rpm_frame(1200.0)),
            TelemetryRecord(timestamp=T0, source="obd", channel="RPM", value=1200.0, unit="rpm"),
        ],
    )
    frame = read_channels(
        session, ["decoded:ECM_RPM", "obd:RPM"], bindings_path=BINDINGS, dids_path=DIDS
    )
    decoded = frame.loc[frame["spec"] == "decoded:ECM_RPM", "value"].iloc[0]
    assert decoded == pytest.approx(1200.0)
    assert frame.loc[frame["spec"] == "decoded:ECM_RPM", "unit"].iloc[0] == "rpm"


def test_kline_module_rows_read_as_raw_counts_from_their_hex(tmp_path: Path) -> None:
    """K-line rows store hex like DID rows; ``value_number`` is empty, so the
    old numeric reader returned nothing for every ``kline:`` spec."""
    session = _session(
        tmp_path,
        [
            TelemetryRecord(timestamp=T0, source="kline", channel="ABS:1202", value="00f5"),
            TelemetryRecord(timestamp=T0, source="kline", channel="ECM:1202", value="0001"),
        ],
    )
    frame = read_channels(session, ["kline:ABS:1202"], bindings_path=BINDINGS, dids_path=DIDS)
    assert frame["value"].tolist() == [245.0]
    assert frame["unit"].tolist() == ["counts"]


def test_a_legacy_kline_did_spec_is_refused_by_name(tmp_path: Path) -> None:
    """``kline:DID_1202`` names no module (pre-K01.1 rows); reading it would mix
    the ABS's 0x1202 with the ECM's. The refusal names the way forward."""
    session = _session(
        tmp_path,
        [TelemetryRecord(timestamp=T0, source="kline", channel="DID_1202", value="00f5")],
    )
    with pytest.raises(ChannelSpecError, match="attribute_modules"):
        read_channels(session, ["kline:DID_1202"], bindings_path=BINDINGS, dids_path=DIDS)


def test_did_values_come_from_value_text_hex(tmp_path: Path) -> None:
    """DID readings live in ``value_text`` as hex; ``value_number`` is empty.

    An analysis that reached for ``value_number`` got an empty column and no
    error, which is how the first A/F-voltage comparison silently found nothing.
    """
    session = _session(
        tmp_path,
        [TelemetryRecord(timestamp=T0, source="obd", channel="DID_1225", value="01b8")],
    )
    frame = read_channels(
        session, ["did:ECM_AF_SENSOR_V_B1"], bindings_path=BINDINGS, dids_path=DIDS
    )
    # 0x01b8 = 440 counts x 0.005 V
    assert frame["value"].iloc[0] == pytest.approx(2.20)
    assert frame["unit"].iloc[0] == "V"
    # Same DID by id rather than name.
    by_id = read_channels(session, ["did:0x1225"], bindings_path=BINDINGS, dids_path=DIDS)
    assert by_id["value"].iloc[0] == pytest.approx(2.20)


def test_gps_channels_are_readable_despite_their_lower_case_names(tmp_path: Path) -> None:
    """GPS is the only road-speed reference a Mode 22-only capture has.

    Its channels are stored lower-case (``speed``), while OBD and UpRev are
    upper-case. The reader used to upper-case the requested name, so
    ``gps:speed`` matched nothing and returned an empty frame with no error —
    the same silent-empty failure the DID hex trap above pins.
    """
    session = _session(
        tmp_path,
        [
            TelemetryRecord(timestamp=T0, source="gps", channel="speed", value=42.5),
            TelemetryRecord(timestamp=T0, source="obd", channel="RPM", value=1500.0),
        ],
    )
    frame = read_channels(session, ["gps:speed"], bindings_path=BINDINGS, dids_path=DIDS)
    assert frame["value"].iloc[0] == pytest.approx(42.5)
    # Upper-case OBD channels still resolve — the match is case-insensitive,
    # not lower-cased in place of upper-cased.
    assert read_channels(session, ["obd:RPM"], bindings_path=BINDINGS, dids_path=DIDS)[
        "value"
    ].iloc[0] == pytest.approx(1500.0)


def test_acquired_time_opt_in_uses_persisted_read_arrival(tmp_path: Path) -> None:
    session = _session(
        tmp_path,
        [
            TelemetryRecord(
                timestamp=T0,
                source="obd",
                channel="DID_1225",
                value="01b8",
                metadata={"acquired_at": T0.timestamp() + 0.125},
            )
        ],
    )
    logical = read_channels(session, ["did:ECM_AF_SENSOR_V_B1"], dids_path=DIDS)
    acquired = read_channels(
        session, ["did:ECM_AF_SENSOR_V_B1"], dids_path=DIDS, acquired_time=True
    )
    offset = (acquired["timestamp"].iloc[0] - logical["timestamp"].iloc[0]).total_seconds()
    assert offset == pytest.approx(0.125)


def test_timestamp_basis_is_read_back_from_production_parquet(tmp_path: Path) -> None:
    """Provenance survives the real writer: ``acquired_at`` in, ``acquired`` out.

    Rows without the field are ``stored`` — their stamp is the decode-time
    clock read — and broadcast frames are always ``stored``. The column only
    exists when ``acquired_time=True`` is asked for.
    """
    session = _session(
        tmp_path,
        [
            TelemetryRecord(
                timestamp=T0,
                source="obd",
                channel="RPM",
                value=1200.0,
                metadata={"acquired_at": T0.timestamp() + 0.3},
            ),
            TelemetryRecord(timestamp=T0, source="obd", channel="MAF", value=5.0),
            TelemetryRecord(
                timestamp=T0, source="can", channel="0x1F9", payload=_rpm_frame(1200.0)
            ),
        ],
    )
    plain = read_channels(session, ["obd:RPM"], bindings_path=BINDINGS, dids_path=DIDS)
    assert TIMESTAMP_BASIS_COLUMN not in plain

    frame = read_channels(
        session,
        ["obd:RPM", "obd:MAF", "can:ECM_RPM"],
        acquired_time=True,
        bindings_path=BINDINGS,
        dids_path=DIDS,
    )
    basis = dict(zip(frame["spec"], frame[TIMESTAMP_BASIS_COLUMN], strict=True))
    assert basis == {"obd:RPM": "acquired", "obd:MAF": "stored", "can:ECM_RPM": "stored"}
    acquired_stamp = frame.loc[frame["spec"] == "obd:RPM", "timestamp"].iloc[0]
    # ``acquired_at`` is float Unix seconds in JSON; it round-trips to the
    # microsecond, which is far inside any pairing tolerance in use.
    assert (acquired_stamp - pd.Timestamp(T0)).total_seconds() == pytest.approx(0.3, abs=1e-5)


def test_round_stamped_captures_are_flagged_from_the_manifest_only(tmp_path: Path) -> None:
    """Only the sidecar proves a stamp is a round stamp; shared stamps do not.

    A batched Mode 01 response legitimately stamps every PID it carried with
    the one decode instant, so several channels sharing a stamp is normal
    and must not be mistaken for a round stamp.
    """
    import json

    records = [
        TelemetryRecord(timestamp=T0, source="obd", channel="RPM", value=1200.0),
        TelemetryRecord(timestamp=T0, source="obd", channel="MAF", value=5.0),
        TelemetryRecord(timestamp=T0, source="can", channel="0x1F9", payload=_rpm_frame(1200.0)),
    ]
    session = _session(tmp_path, records)
    specs = ["obd:RPM", "obd:MAF", "can:ECM_RPM"]
    before = read_channels(
        session, specs, acquired_time=True, bindings_path=BINDINGS, dids_path=DIDS
    )
    assert set(before[TIMESTAMP_BASIS_COLUMN]) == {"stored"}

    (session / "capture_profile.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile": "tuning",
                "started_at": T0.isoformat(),
                "profile_snapshot": {"round_timestamps": True},
            }
        )
    )
    after = read_channels(
        session, specs, acquired_time=True, bindings_path=BINDINGS, dids_path=DIDS
    )
    basis = dict(zip(after["spec"], after[TIMESTAMP_BASIS_COLUMN], strict=True))
    # Polled rows become ``round``; the broadcast frame keeps its arrival stamp.
    assert basis == {"obd:RPM": "round", "obd:MAF": "round", "can:ECM_RPM": "stored"}
    # The raw acquisition frame carries the same verdict per row.
    raw = read_acquisition_frame(session)
    assert set(raw.loc[raw["source"] == "obd", TIMESTAMP_BASIS_COLUMN]) == {"round"}


def test_a_frame_without_metadata_has_unknown_provenance() -> None:
    frame = pd.DataFrame(
        {
            "timestamp": [pd.Timestamp(T0)],
            "source": ["obd"],
            "channel": ["RPM"],
            "value_number": [1200.0],
            "value_text": [None],
            "unit": ["rpm"],
            "payload": [None],
        }
    )
    out = read_channels(
        frame, ["obd:RPM"], acquired_time=True, bindings_path=BINDINGS, dids_path=DIDS
    )
    assert out[TIMESTAMP_BASIS_COLUMN].iloc[0] == "unknown"


def _metadata_frame(metadata: list[str | None]) -> pd.DataFrame:
    """One OBD row per raw ``metadata_json`` text, in that order."""
    return pd.DataFrame(
        {
            "timestamp": [pd.Timestamp(T0) + timedelta(seconds=i) for i in range(len(metadata))],
            "source": ["obd"] * len(metadata),
            "channel": ["RPM"] * len(metadata),
            "value_number": [1200.0] * len(metadata),
            "value_text": [None] * len(metadata),
            "unit": ["rpm"] * len(metadata),
            "payload": [None] * len(metadata),
            "metadata_json": metadata,
        }
    )


def test_unusable_metadata_json_carries_no_round_identity() -> None:
    """Absent, malformed and wrong-typed metadata all mean "no identity".

    Each of these used to fall out of a ``json.loads``/``dict.get`` chain by a
    different route (``TypeError``, ``ValueError``, ``AttributeError``, a type
    check); they are pinned here because the parse now happens once, in one
    place, and a refactor must not quietly promote any of them to a reading.
    ``true`` is the sharp one: ``bool`` is an ``int`` subclass, so an
    unguarded cast reads it as ``acquired_at=1.0`` — 1970, and numeric enough
    to survive downstream.
    """
    unusable = [
        None,
        "",
        "not json at all",
        "{unterminated",
        "[1, 2]",
        "null",
        "3",
        "{}",
        '{"acquired_at": null}',
        '{"acquired_at": true}',
        '{"acquired_at": "1.0"}',
        '{"acquired_at": [1, 2]}',
        '{"nested": {"acquired_at": 5.0}}',
        '{"can_id": 1783, "dlc": 8}',
    ]
    frame = read_acquisition_frame(_metadata_frame(unusable))
    assert frame["acquired_at"].isna().all()
    assert frame["generation"].isna().all()
    assert frame["round_id"].isna().all()
    assert set(frame[TIMESTAMP_BASIS_COLUMN]) == {"stored"}
    # The stored stamp is kept, not nulled, so the rows stay comparable.
    assert frame["timestamp"].notna().all()


def test_escaped_metadata_keys_are_parsed_not_skipped() -> None:
    """``{"acquired_a\\u0074": …}`` names ``acquired_at`` and must be read.

    The reader skips the JSON parse for rows whose text provably cannot hold
    these keys, and the proof is "no key substring *and* no backslash" —
    because an escape is the only way to spell a key without its literal
    characters. Narrowing that to a plain substring test would silently drop
    this row's acquisition time, so the escape case is pinned.
    """
    frame = read_acquisition_frame(
        _metadata_frame(
            [
                '{"acquired_a\\u0074": 1786028252.75}',
                '{"round_i\\u0064": 3, "generatio\\u006e": 1}',
            ]
        )
    )
    assert frame["acquired_at"].iloc[0] == pytest.approx(1786028252.75)
    assert frame[TIMESTAMP_BASIS_COLUMN].iloc[0] == "acquired"
    assert (int(frame["round_id"].iloc[1]), int(frame["generation"].iloc[1])) == (3, 1)
    assert frame[TIMESTAMP_BASIS_COLUMN].iloc[1] == "round"


def test_metadata_json_is_parsed_at_most_once_per_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """The parse is per row, not per consumer.

    Six ``Series.map(json.loads)`` passes over the same column cost 115 s of a
    122 s ``recompute_capture_quality`` on a 3.5 M-row capture (2026-09-17).
    A caller that needs no acquisition clock must pay nothing at all.
    """
    import src.analysis.channels as channels

    calls = 0
    real_loads = channels.json.loads

    def counting_loads(text: str, /, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return real_loads(text, **kwargs)

    monkeypatch.setattr(channels.json, "loads", counting_loads)
    frame = _metadata_frame(
        [f'{{"acquired_at": {T0.timestamp() + i}}}' for i in range(4)]
        + ['{"can_id": 1783, "dlc": 8}'] * 6
    )

    read_channels(frame, ["obd:RPM"], bindings_path=BINDINGS, dids_path=DIDS)
    assert calls == 0, "acquired_time=False must not parse metadata at all"

    read_acquisition_frame(frame)
    assert calls <= len(frame)
    # The six broadcast rows cannot carry a round key, and are not parsed.
    assert calls == 4


def test_can_specs_decode_frame_bytes_by_signal_name(tmp_path: Path) -> None:
    """``can:`` is named by signal, not CAN id — one frame carries several."""
    session = _session(
        tmp_path,
        [TelemetryRecord(timestamp=T0, source="can", channel="0x1F9", payload=_rpm_frame(2500.0))],
    )
    frame = read_channels(session, ["can:ECM_RPM"], bindings_path=BINDINGS, dids_path=DIDS)
    assert frame["value"].iloc[0] == pytest.approx(2500.0)


def test_unpinned_scale_yields_counts_not_an_invented_value(tmp_path: Path) -> None:
    """``scale: null`` must surface as raw counts, flagged by unit.

    ECM_AIR_CHARGE has no pinned scale. Emitting a plausible-looking
    engineering number for it would be exactly the mistake that got the old
    ECM_AIRFLOW_PROXY_23D binding mislabelled twice.
    """
    session = _session(
        tmp_path,
        [TelemetryRecord(timestamp=T0, source="obd", channel="DID_1208", value="1362")],
    )
    frame = read_channels(session, ["did:ECM_AIR_CHARGE"], bindings_path=BINDINGS, dids_path=DIDS)
    assert frame["value"].iloc[0] == pytest.approx(float(0x1362))
    assert frame["unit"].iloc[0] == "counts"


def test_aligned_requires_every_channel_fresh_in_the_cell(tmp_path: Path) -> None:
    """The stale-join guard (G.4): no row unless all channels sampled that cell.

    The 2026-07 corpus produced four "WOT AFR" samples that were one reading
    aged 24-35 s, paired in by an ASOF join and indistinguishable from
    measurements. Freshness is the default so that cannot recur silently.
    """
    records = [
        TelemetryRecord(timestamp=T0, source="obd", channel="RPM", value=1000.0),
        TelemetryRecord(timestamp=T0, source="obd", channel="MAF", value=5.0),
        # Second 1: RPM only. No MAF -> the pair must not appear.
        TelemetryRecord(
            timestamp=T0 + timedelta(seconds=1), source="obd", channel="RPM", value=2000.0
        ),
    ]
    session = _session(tmp_path, records)
    aligned = read_aligned(session, ["obd:RPM", "obd:MAF"], bindings_path=BINDINGS, dids_path=DIDS)
    assert len(aligned) == 1
    assert aligned["obd:RPM"].iloc[0] == pytest.approx(1000.0)

    loose = read_aligned(
        session, ["obd:RPM", "obd:MAF"], require_all=False, bindings_path=BINDINGS, dids_path=DIDS
    )
    assert len(loose) == 2
    assert pd.isna(loose["obd:MAF"].iloc[1])


def test_max_staleness_is_opt_in_and_reports_age(tmp_path: Path) -> None:
    """Carrying a value forward is allowed, but never silent."""
    records = [
        TelemetryRecord(timestamp=T0, source="obd", channel="RPM", value=1000.0),
        TelemetryRecord(timestamp=T0, source="obd", channel="MAF", value=5.0),
        TelemetryRecord(
            timestamp=T0 + timedelta(seconds=1), source="obd", channel="RPM", value=2000.0
        ),
        TelemetryRecord(
            timestamp=T0 + timedelta(seconds=9), source="obd", channel="RPM", value=3000.0
        ),
    ]
    session = _session(tmp_path, records)
    aligned = read_aligned(
        session,
        ["obd:RPM", "obd:MAF"],
        max_staleness="3s",
        bindings_path=BINDINGS,
        dids_path=DIDS,
    )
    # t+1 s: MAF is 1 s old, inside tolerance and reported as such.
    assert len(aligned) == 2
    assert aligned["obd:MAF__age_s"].iloc[1] == pytest.approx(1.0)
    # t+9 s: MAF is 9 s old, beyond tolerance -> dropped, not carried.
    assert (aligned["timestamp"] == T0 + timedelta(seconds=9)).sum() == 0


def test_missing_binding_names_the_catalog(tmp_path: Path) -> None:
    session = _session(
        tmp_path, [TelemetryRecord(timestamp=T0, source="obd", channel="RPM", value=1.0)]
    )
    with pytest.raises(ChannelSpecError, match="NOT_A_SIGNAL"):
        read_channels(session, ["decoded:NOT_A_SIGNAL"], bindings_path=BINDINGS, dids_path=DIDS)


def _calibration(convention: str):
    from src.analysis.afr import AfrCalibration

    return AfrCalibration(commanded_equiv_ratio_convention=convention)  # type: ignore[arg-type]


def test_commanded_afr_uses_the_recorded_convention(tmp_path: Path) -> None:
    """phi vs lambda is a reciprocal, and it hides at stoich.

    PID 0x44 read 1.242 at WOT on this ECM. As phi that is 11.83 AFR (power
    enrichment, matches the ROM target curve); as lambda it is 18.26, which no
    ECU commands under load and which produced a -27 % phantom delivery error
    before the convention was pinned (commit 78592f4).
    """
    session = _session(
        tmp_path,
        [TelemetryRecord(timestamp=T0, source="obd", channel="COMMANDED_EQUIV_RATIO", value=1.242)],
    )
    as_phi = read_channels(
        session,
        ["obd:COMMANDED_AFR"],
        calibration=_calibration("phi"),
        afr_stoich=14.7,
        bindings_path=BINDINGS,
        dids_path=DIDS,
    )
    assert as_phi["value"].iloc[0] == pytest.approx(14.7 / 1.242, rel=1e-6)
    assert as_phi["unit"].iloc[0] == "AFR"

    as_lambda = read_channels(
        session,
        ["obd:COMMANDED_AFR"],
        calibration=_calibration("lambda"),
        afr_stoich=14.7,
        bindings_path=BINDINGS,
        dids_path=DIDS,
    )
    assert as_lambda["value"].iloc[0] == pytest.approx(14.7 * 1.242, rel=1e-6)


def test_a_session_opening_on_fuel_cut_still_converts(tmp_path: Path) -> None:
    """Commanded phi 0.0 is decel fuel cut, not a malformed value.

    The convention probe used to take the session's FIRST value, so a session
    that opened on a closed-throttle decel raised "equivalence ratio must be
    positive" and was lost whole — four sessions of the 2026-09-21 corpus run.
    The cut sample is dropped; the rest convert.
    """
    session = _session(
        tmp_path,
        [
            TelemetryRecord(timestamp=T0, source="obd", channel="COMMANDED_EQUIV_RATIO", value=0.0),
            TelemetryRecord(
                timestamp=T0 + timedelta(seconds=1),
                source="obd",
                channel="COMMANDED_EQUIV_RATIO",
                value=1.242,
            ),
        ],
    )
    out = read_channels(
        session,
        ["obd:COMMANDED_AFR"],
        calibration=_calibration("phi"),
        afr_stoich=14.7,
        bindings_path=BINDINGS,
        dids_path=DIDS,
    )
    assert len(out) == 1
    assert out["value"].iloc[0] == pytest.approx(14.7 / 1.242, rel=1e-6)


def test_unverified_convention_refuses_rather_than_guessing(tmp_path: Path) -> None:
    """A vehicle nobody checked at WOT must not get a silently-wrong AFR."""
    session = _session(
        tmp_path,
        [TelemetryRecord(timestamp=T0, source="obd", channel="COMMANDED_EQUIV_RATIO", value=1.242)],
    )
    with pytest.raises(ValueError, match="unverified"):
        read_channels(
            session,
            ["obd:COMMANDED_AFR"],
            calibration=_calibration("unverified"),
            afr_stoich=14.7,
            bindings_path=BINDINGS,
            dids_path=DIDS,
        )


def test_commanded_afr_without_a_calibration_says_so(tmp_path: Path) -> None:
    session = _session(
        tmp_path,
        [TelemetryRecord(timestamp=T0, source="obd", channel="COMMANDED_EQUIV_RATIO", value=1.0)],
    )
    with pytest.raises(ChannelSpecError, match="calibration"):
        read_channels(session, ["obd:COMMANDED_AFR"], bindings_path=BINDINGS, dids_path=DIDS)


def test_commanded_afr_without_a_stoich_says_so(tmp_path: Path) -> None:
    """R05.1: no gasoline default — a diesel profile must not get 14.7 silently."""
    session = _session(
        tmp_path,
        [TelemetryRecord(timestamp=T0, source="obd", channel="COMMANDED_EQUIV_RATIO", value=1.0)],
    )
    with pytest.raises(ChannelSpecError, match="stoichiometric"):
        read_channels(
            session,
            ["obd:COMMANDED_AFR"],
            calibration=_calibration("phi"),
            bindings_path=BINDINGS,
            dids_path=DIDS,
        )


def test_the_xterra_profile_records_phi() -> None:
    """Regression guard: the shipped vehicle profile must keep the WOT verdict."""
    from src.analysis.afr import load_afr_calibration

    cal = load_afr_calibration(Path("data/nissan-xterra-2008/afr_calibration.yaml"))
    assert cal.commanded_equiv_ratio_convention == "phi"


def test_rescale_channel_rows_fixes_decoded_but_leaves_obd_alone() -> None:
    """The query path reads ``value_number`` raw; ``decoded:`` needs scaling.

    Without this, a binned "X by rpm" over ``decoded:ECM_RPM`` silently bins
    on counts — 8x the rpm — and the bin edges look plausible enough to go
    unnoticed.
    """
    from src.analysis.channels import rescale_channel_rows

    rows: list[dict[str, object]] = [
        {"channel_key": "decoded:ECM_RPM", "value_number": 9600.0},
        {"channel_key": "obd:RPM", "value_number": 1200.0},
        {"channel_key": "decoded:ECM_COOLANT_TEMP", "value_number": 90.0},
    ]
    out = rescale_channel_rows(rows, bindings_path=BINDINGS, dids_path=DIDS)
    assert out[0]["value_number"] == pytest.approx(1200.0)  # x0.125
    assert out[1]["value_number"] == pytest.approx(1200.0)  # untouched
    assert out[2]["value_number"] == pytest.approx(50.0)  # offset -40


def test_rescale_leaves_unpinned_and_unknown_bindings_untouched() -> None:
    """No scale pinned means no engineering value — never invent one."""
    from src.analysis.channels import rescale_channel_rows

    rows: list[dict[str, object]] = [
        {"channel_key": "decoded:NOT_A_BINDING", "value_number": 7.0},
        {"channel_key": "did:ECM_AF_SENSOR_V_B1", "value_number": 440.0},
    ]
    out = rescale_channel_rows(rows, bindings_path=BINDINGS, dids_path=DIDS)
    assert out[0]["value_number"] == pytest.approx(7.0)
    assert out[1]["value_number"] == pytest.approx(440.0)


def test_every_captured_source_can_be_named_in_a_spec(tmp_path: Path) -> None:
    """H01.2: a captured source nobody can read is a silent-empty trap.

    ``imu`` was a ``TelemetrySource`` for months before a spec could name it,
    so ``compare_channels`` could not pair IMU acceleration with anything.
    Every captured source must be a spec namespace, and every namespace must
    have a reader.
    """
    from src.analysis.channels import SPEC_SOURCES
    from src.telemetry import CAPTURED_SOURCES

    assert set(CAPTURED_SOURCES) <= set(SPEC_SOURCES)
    session = _session(
        tmp_path,
        [TelemetryRecord.imu(T0.timestamp(), "ACCEL_X", 0.25, "g")],
    )
    frame = read_channels(session, ["imu:ACCEL_X"], bindings_path=BINDINGS, dids_path=DIDS)
    assert frame["value"].tolist() == [pytest.approx(0.25)]
    with pytest.raises(ChannelSpecError, match="obd/did/decoded/can/uprev/gps/imu/mem/sensor"):
        parse_spec("bogus:X")
