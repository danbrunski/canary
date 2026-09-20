"""Round-trip test for the DBC emitter — guards the bit math.

Build a DBC from a known binding, reload it with cantools, decode a
frame, and assert the value matches what bindings.decode_raw +
apply_scaling produce. If the Motorola start-bit formula is wrong this
fails loudly.
"""

from __future__ import annotations

from pathlib import Path

import cantools
import pytest

from src.decode.bindings import load_bindings
from tools.emit_dbc import build_database

_BINDINGS = """\
schema_version: 1
vehicle: {make: Nissan}
signals:
  - name: ECM_RPM
    module: ECM
    can_id: 0x1F9
    byte_start: 2
    byte_length: 2
    byte_order: big
    scale: 0.125
    offset: 0.0
    unit: rpm
    confidence: confirmed
  - name: FLAG_BYTE0
    module: ECM
    can_id: 0x1F9
    byte_start: 0
    byte_length: 1
    byte_order: big
    scale: null
    offset: null
    unit: unknown
    confidence: high
"""


def _db(tmp_path):  # type: ignore[no-untyped-def]
    bpath = tmp_path / "bindings.yaml"
    bpath.write_text(_BINDINGS)
    dbc_str = build_database(bpath).as_dbc_string()
    return cantools.database.load_string(dbc_str, database_format="dbc"), bpath


def test_dbc_roundtrip_big_endian_scaled(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db, _ = _db(tmp_path)
    msg = db.get_message_by_frame_id(0x1F9)
    # bytes 2-3 = 0x1840 = 6208 → ×0.125 = 776.0 rpm; byte 0 raw = 0x20.
    frame = bytes([0x20, 0x00, 0x18, 0x40, 0, 0, 0, 0])
    decoded = msg.decode(frame)
    assert decoded["ECM_RPM"] == 776.0
    assert decoded["FLAG_BYTE0"] == 0x20  # unscaled binding → raw passthrough


def test_dbc_matches_bindings_decoder(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Cross-check: cantools decode == our own decode_raw/apply_scaling."""
    from src.decode.bindings import apply_scaling, decode_raw

    db, bpath = _db(tmp_path)
    rpm_sig = next(s for s in load_bindings(bpath).signals if s.name == "ECM_RPM")
    frame = bytes([0xAA, 0xBB, 0x0A, 0xF0, 0, 0, 0, 0])  # bytes 2-3 = 0x0AF0 = 2800
    ours = apply_scaling(rpm_sig, decode_raw(rpm_sig, frame) or 0)
    theirs = db.get_message_by_frame_id(0x1F9).decode(frame)["ECM_RPM"]
    assert ours == theirs == 350.0  # 2800 × 0.125


def test_real_bindings_emit_loads(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The real data/bindings.yaml emits a DBC cantools can parse back."""
    from pathlib import Path

    db = build_database(Path("data/bindings.yaml"))
    reloaded = cantools.database.load_string(db.as_dbc_string(), database_format="dbc")
    assert len(reloaded.messages) == len(db.messages) > 0
    # ECM_RPM (confirmed) must survive the round trip on 0x1F9.
    rpm = reloaded.get_message_by_frame_id(0x1F9)
    assert any(s.name == "ECM_RPM" for s in rpm.signals)


def test_model_feature_flag_does_not_change_the_emitted_dbc(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """`model_feature: false` keeps a signal out of the trained feature set and
    nothing else: the DBC (and so every decoder built from it) is unchanged."""
    plain = tmp_path / "plain.yaml"
    flagged = tmp_path / "flagged.yaml"
    plain.write_text(_BINDINGS)
    flagged.write_text(
        _BINDINGS.replace(
            "    confidence: high\n", "    confidence: high\n    model_feature: false\n"
        )
    )
    assert load_bindings(flagged).signals[1].model_feature is False
    assert build_database(flagged).as_dbc_string() == build_database(plain).as_dbc_string()


_OVERLAPPING = """\
schema_version: 1
vehicle: {make: Nissan}
signals:
  - name: VEHICLE_SPEED_2D1
    module: ECM
    can_id: 0x2D1
    byte_start: 1
    byte_length: 2
    byte_order: little
    scale: 0.1
    offset: 0.0
    unit: km/h
    confidence: high
  - name: MOTION_SIGNAL_2D1_1
    module: unknown
    can_id: 0x2D1
    byte_start: 1
    byte_length: 1
    byte_order: big
    scale: null
    offset: null
    unit: unknown
    confidence: hypothesized
__SUPERSEDED__
"""


def test_superseded_binding_is_not_emitted(tmp_path: Path) -> None:
    """The real 0x2D1 case: the legacy byte is the new signal's high byte.

    It has to stay in bindings.yaml because stored telemetry names it, so
    the emit — not the file — is where the two stop coexisting.
    """
    path = tmp_path / "bindings.yaml"
    path.write_text(_OVERLAPPING.replace("__SUPERSEDED__", "    superseded_by: VEHICLE_SPEED_2D1"))

    db = build_database(path)

    names = [signal.name for message in db.messages for signal in message.signals]
    assert names == ["VEHICLE_SPEED_2D1"]


def test_overlapping_live_bindings_name_both_signals(tmp_path: Path) -> None:
    """Without the supersede record this is a genuine conflict, not noise."""
    path = tmp_path / "bindings.yaml"
    path.write_text(_OVERLAPPING.replace("__SUPERSEDED__", ""))

    with pytest.raises(ValueError, match="claimed by both"):
        build_database(path)


def test_real_bindings_have_no_overlapping_live_signals() -> None:
    """The shipped file must stay emittable; 911d0f1 made it briefly not."""
    build_database(Path("data/bindings.yaml"))
