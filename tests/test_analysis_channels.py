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
    ChannelSpecError,
    parse_spec,
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
    """``decoded:`` stores the RAW integer on purpose — the reader must scale it.

    ``ECM_RPM`` is 8 counts/rpm, so a stored 9600 is 1200 rpm. Reading
    ``value_number`` directly is what first looked like a factor-of-8 decoder
    bug (it isn't one: src/decode/frame_decoder.py keeps rows raw so training's
    scaler stays comparable across sessions).
    """
    session = _session(
        tmp_path,
        [
            TelemetryRecord(timestamp=T0, source="decoded", channel="ECM_RPM", value=9600.0),
            TelemetryRecord(timestamp=T0, source="obd", channel="RPM", value=1200.0, unit="rpm"),
        ],
    )
    frame = read_channels(
        session, ["decoded:ECM_RPM", "obd:RPM"], bindings_path=BINDINGS, dids_path=DIDS
    )
    decoded = frame.loc[frame["spec"] == "decoded:ECM_RPM", "value"].iloc[0]
    assert decoded == pytest.approx(1200.0)
    assert frame.loc[frame["spec"] == "decoded:ECM_RPM", "unit"].iloc[0] == "rpm"


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

    ECM_INJECTOR_PW_B1 has no pinned scale. Emitting a plausible-looking
    engineering number for it would be exactly the mistake that got the old
    ECM_AIRFLOW_PROXY_23D binding mislabelled twice.
    """
    session = _session(
        tmp_path,
        [TelemetryRecord(timestamp=T0, source="obd", channel="DID_1208", value="1362")],
    )
    frame = read_channels(
        session, ["did:ECM_INJECTOR_PW_B1"], bindings_path=BINDINGS, dids_path=DIDS
    )
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
        bindings_path=BINDINGS,
        dids_path=DIDS,
    )
    assert as_phi["value"].iloc[0] == pytest.approx(14.7 / 1.242, rel=1e-6)
    assert as_phi["unit"].iloc[0] == "AFR"

    as_lambda = read_channels(
        session,
        ["obd:COMMANDED_AFR"],
        calibration=_calibration("lambda"),
        bindings_path=BINDINGS,
        dids_path=DIDS,
    )
    assert as_lambda["value"].iloc[0] == pytest.approx(14.7 * 1.242, rel=1e-6)


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
