"""R29.7: the DID helpers ``/dids`` and ``/telemetry/latest`` used from closures.

``polled_did_names`` resolves the ``obd_dids`` config tokens (a catalog name or a
bare hex id) against the catalog; ``decoded_did_rows`` decodes the raw
``DID_<hex>`` telemetry rows into ``did:<NAME>`` rows. They were nested in
``register_public_routes`` and reachable only through an app.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.dashboard.readers import decoded_did_rows, polled_did_names
from src.decode.dids import DidMap, load_did_map

_DIDS_YAML = """
schema_version: 1
vehicle: {make: Nissan, model: Xterra, year: 2008}
access:
  ecu_request_id: 0x7E0
  ecu_response_id: 0x7E8
  session: 0xC0
dids:
  - did: 0x1225
    name: ECM_AF_SENSOR_V_B1
    byte_start: 0
    byte_length: 2
    scale: 0.005
    offset: 0.0
    unit: V
    confidence: high
  - did: 0x1208
    name: ECM_AIR_CHARGE
    byte_start: 0
    byte_length: 2
    confidence: high
"""


@pytest.fixture
def dids_path(tmp_path: Path) -> Path:
    path = tmp_path / "dids.yaml"
    path.write_text(_DIDS_YAML)
    return path


@pytest.fixture
def did_map(dids_path: Path) -> DidMap:
    return load_did_map(dids_path)


def _row(channel: str, value: Any, *, source: str = "obd", timestamp: str = "t0") -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "source": source,
        "channel": channel,
        "value": value,
        "unit": "",
    }


# -- polled_did_names ----------------------------------------------------------


def test_a_name_and_a_hex_id_both_resolve_to_the_catalog_name(did_map: DidMap) -> None:
    assert polled_did_names(did_map, ["ECM_AF_SENSOR_V_B1", "1208"]) == [
        "ECM_AF_SENSOR_V_B1",
        "ECM_AIR_CHARGE",
    ]


def test_a_hex_id_is_case_insensitive_and_tolerates_whitespace(did_map: DidMap) -> None:
    assert polled_did_names(did_map, [" 1225 ", "0x1208"]) == [
        "ECM_AF_SENSOR_V_B1",
        "ECM_AIR_CHARGE",
    ]


def test_a_token_the_catalog_does_not_hold_is_dropped_not_an_error(did_map: DidMap) -> None:
    """Neither a catalog name, nor hex, nor a hex id the catalog has."""
    assert polled_did_names(did_map, ["NOT_A_DID", "ZZZZ", "9999", ""]) == []


def test_order_and_repeats_follow_the_configuration(did_map: DidMap) -> None:
    tokens = ["1208", "ECM_AIR_CHARGE", "1225"]
    assert polled_did_names(did_map, tokens) == [
        "ECM_AIR_CHARGE",
        "ECM_AIR_CHARGE",
        "ECM_AF_SENSOR_V_B1",
    ]


def test_nothing_configured_is_nothing_polled(did_map: DidMap) -> None:
    assert polled_did_names(did_map, []) == []


# -- decoded_did_rows ----------------------------------------------------------


def test_a_raw_did_row_decodes_to_a_did_source_row(dids_path: Path) -> None:
    rows = [_row("DID_1225", "01ae", timestamp="2026-11-12T12:00:00+00:00")]
    (hit,) = decoded_did_rows(rows, dids_path)
    assert hit["timestamp"] == "2026-11-12T12:00:00+00:00"
    assert hit["source"] == "did"
    assert hit["channel"] == "ECM_AF_SENSOR_V_B1"
    assert hit["unit"] == "V"
    assert hit["value"] == pytest.approx(2.15)


def test_rows_that_are_not_raw_dids_decode_to_nothing(dids_path: Path) -> None:
    assert (
        decoded_did_rows([_row("RPM", 720.0), _row("ECM_RPM", 1.0, source="decoded")], dids_path)
        == []
    )


def test_a_did_the_catalog_lacks_or_a_value_that_is_not_hex_is_skipped(dids_path: Path) -> None:
    rows = [_row("DID_FFFF", "01ae"), _row("DID_1225", "not hex"), _row("DID_1225", None)]
    assert decoded_did_rows(rows, dids_path) == []


def test_without_a_catalog_file_there_is_nothing_to_decode(tmp_path: Path) -> None:
    assert decoded_did_rows([_row("DID_1225", "01ae")], tmp_path / "absent.yaml") == []


def test_the_catalog_is_not_read_when_no_row_needs_it(tmp_path: Path) -> None:
    """A malformed catalog cannot break a seed that carries no raw DID rows."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("not: [a, catalog")
    assert decoded_did_rows([_row("RPM", 720.0)], bad) == []


def test_each_raw_row_decodes_on_its_own_timestamp(dids_path: Path) -> None:
    rows = [
        _row("DID_1225", "01ae", timestamp="t1"),
        _row("RPM", 700.0),
        _row("DID_1208", "0010", timestamp="t2"),
    ]
    decoded = decoded_did_rows(rows, dids_path)
    assert [(r["timestamp"], r["channel"]) for r in decoded] == [
        ("t1", "ECM_AF_SENSOR_V_B1"),
        ("t2", "ECM_AIR_CHARGE"),
    ]
