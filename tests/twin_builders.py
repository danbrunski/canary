"""The twin's other fixtures: operating-context images and reads, air-charge and fuel-comp
calibrations, command-change identities and the investigation store (R30.2).

Moved out of the ``tests/test_*.py`` files that provided them, bodies unchanged; private helpers
got public names."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from src.analysis import air_charge_twin as twin
from src.analysis.ecu_application import ApplicationModel, load_application_model, program_sha256
from src.analysis.fuel_comp_rom import FuelCompCalibration
from src.analysis.operating_context import (
    ContextDefinition,
    ResolvedContext,
    load_context_definition,
    resolve_context,
)
from src.analysis.rom_map import RomMap
from src.analysis.target_phi import ResolvedTable
from src.decode.fuel_comp_load import LoadInputCurves
from src.evidence import VehicleIdentity
from src.paths import REPO_ROOT
from src.storage.parquet_writer import TELEMETRY_SCHEMA
from src.yaml_load import load_yaml
from tests.rom_fixtures import load_byte_image, with_checksums, xterra_map
from tests.target_phi_builders import PROFILE

SLUG = "nissan-xterra-2008"


PATCH = PROFILE / "rom-targets-known-edit.json"


def change_vehicle(table: ResolvedTable) -> VehicleIdentity:
    return VehicleIdentity(
        profile=SLUG,
        vin=None,
        program_id=table.application.program_id,
        calibration=table.revision,
    )


def air_charge_calibration(**changes: object) -> twin.AirChargeCalibration:
    cells = tuple(tuple(128 + ((r + c) % 5) - 2 for c in range(16)) for r in range(16))
    base = twin.AirChargeCalibration(
        image_sha256="0" * 64,
        slot=1,
        k=28715,
        flow_multiplier=226,
        speed_multiplier=3,
        maf_scale=149,
        fuel_comp=cells,
        fuel_comp_rpm_axis=tuple(range(8, 136, 8)),
        fuel_comp_load_axis=(16, 24, 32, 40, 48, 56, 64, 72, 80, 88, 96, 104, 112, 120, 128, 144),
        ceiling=(255,) * 16,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def private_rom_read(paths: list[str]) -> pd.DataFrame:
    glob = ", ".join(f"'{p}'" for p in paths)
    return duckdb.sql(
        f"select * from read_parquet([{glob}], union_by_name=true) where channel in "
        "('DID_1208', 'DID_1209', 'RPM', 'MAF', 'mem:ECM_AIR_CHARGE_SLOT_103A', "
        "'mem:ECM_FUEL_COMP_X_INPUT')"
    ).df()


def application_synthetic(model: ApplicationModel) -> tuple[bytes, ApplicationModel]:
    image = bytearray(b"\xa5" * 0x100000)
    for marker in model.program.patch.markers:
        raw = bytes.fromhex(marker.bytes)
        image[marker.offset : marker.offset + len(raw)] = raw
    for node in model.nodes:
        for c in node.calibration_conditions:
            for k in range(c.count):
                image[c.offset + k] = (image[c.offset + k] & ~c.mask & 0xFF) | c.equals
    identity = model.program.identity.model_copy(
        update={"sha256": program_sha256(bytes(image), model)}
    )
    program = model.program.model_copy(update={"identity": identity})
    return bytes(image), model.model_copy(update={"program": program})


def fuel_comp_calibration(name: str = "synthetic") -> FuelCompCalibration:
    """Invented tables: rising curves, a flat Fuel Comp, the 20261004-shape axes."""
    curves = LoadInputCurves(
        area_breakpoints=tuple(100 * i for i in range(32)),
        area_values=tuple(200 + 30 * i * i for i in range(32)),
        # Steep first segment, as a fill curve is: small X spans many bytes.
        fill_low=(0, *(16000 + 2400 * i for i in range(15))),
        fill_high=tuple(49600 + 500 * i for i in range(16)),
        rpm_multiplier=150,
        reciprocal_numerator=10_000_000,
        idle_air_cap=1000,
        limp_home_area=700,
    )
    load_axis = tuple(range(16, 256, 16))
    return FuelCompCalibration(
        name=name,
        image_sha256="0" * 64,
        source_path=Path("synthetic.bin"),
        image_format="plain",
        slot=1,
        fuel_comp=tuple((128,) * 16 for _ in range(16)),
        rpm_axis_raw=tuple(range(8, 136, 8)),
        load_axis_raw=(*load_axis, 255),
        rpm_breakpoints=tuple(4 * b for b in range(8, 136, 8)),
        k=28000,
        purge_gain=128,
        curves=curves,
        table_ids={"fuel_comp": "synthetic"},
    )


INVESTIGATION_SLUG = "nissan-xterra-2008"


INVESTIGATION_REVISION = "ZS44A-20260920"


def investigation_store(root: Path, frame: pd.DataFrame, name: str) -> Path:
    """``frame`` as a stored session (the writer's schema)."""
    session = root / name
    session.mkdir(parents=True)
    rows = [
        {
            **row,
            "timestamp": row["timestamp"].floor("us").to_pydatetime(),
            "value_number": None if pd.isna(row["value_number"]) else row["value_number"],
            "value_bool": None,
            "payload": None,
        }
        for row in frame.to_dict("records")
    ]
    table = pa.Table.from_pylist(rows, schema=TELEMETRY_SCHEMA)
    pq.write_table(table, session / "telemetry-00000.parquet")  # type: ignore[no-untyped-call]
    return session


REPO = REPO_ROOT


#: Known cells, asymmetric in row and column so a transposed read differs.
FUEL_RPM_AXIS = tuple(range(8, 129, 8))  # 0xCACD: 400..6,400 rpm (x 50)


FUEL_FILL_AXIS = (*range(16, 241, 16), 255)  # 0xCB29 restored: 16..255


TRIM_RPM_AXIS = (8, 12, 16, 24, 32, 40, 48, 56, 64, 72, 80, 88, 96, 104, 112, 120)  # 0xCC93


TRIM_LOAD_AXIS = tuple(range(8, 129, 8))  # 0xCC83


def fuel_cell(r: int, c: int) -> int:
    return 100 + 4 * r + c


def trim_a_cell(r: int, c: int) -> int:
    """Rows 8 and up carry the bit-7 flag, as a contiguous block does in the ROM."""
    return (40 + r + 2 * c) | (0x80 if r >= 8 else 0)


def trim_b_cell(r: int, c: int) -> int:
    return 30 + r + c


def context_image(*, fill_axis: tuple[int, ...] = FUEL_FILL_AXIS) -> bytes:
    model, _ = load_application_model(PROFILE)
    image = bytearray(load_byte_image())
    for marker in model.program.patch.markers:
        raw = bytes.fromhex(marker.bytes)
        image[marker.offset : marker.offset + len(raw)] = raw
    for node in model.nodes:
        for ref in node.rom:
            if ref.offset is not None:
                assert ref.width is not None and ref.value is not None
                width = ref.width // 8
                image[ref.offset : ref.offset + width] = ref.value.to_bytes(width, "big")
        for c in node.calibration_conditions:
            for k in range(c.count):
                image[c.offset + k] = (image[c.offset + k] & ~c.mask & 0xFF) | c.equals
    image[0xCACD : 0xCACD + 16] = bytes(FUEL_RPM_AXIS)
    image[0xCB29 : 0xCB29 + 16] = bytes(fill_axis)
    image[0xCC93 : 0xCC93 + 16] = bytes(TRIM_RPM_AXIS)
    image[0xCC83 : 0xCC83 + 16] = bytes(TRIM_LOAD_AXIS)
    rom_map = xterra_map()
    slots = rom_map.map_slots
    assert slots is not None
    base = slots.slot_base(1)
    image[base + 0x660 : base + 0x760] = bytes(
        fuel_cell(r, c) for r in range(16) for c in range(16)
    )
    image[base + 0x020 : base + 0x120] = bytes(
        trim_a_cell(r, c) for r in range(16) for c in range(16)
    )
    image[base + 0x120 : base + 0x220] = bytes(
        trim_b_cell(r, c) for r in range(16) for c in range(16)
    )
    return with_checksums(bytes(image), rom_map)


def synthetic_program(image: bytes) -> tuple[ApplicationModel, str]:
    model, sha = load_application_model(PROFILE)
    identity = model.program.identity.model_copy(update={"sha256": program_sha256(image, model)})
    program = model.program.model_copy(update={"identity": identity})
    return model.model_copy(update={"program": program}), sha


def definition() -> ContextDefinition:
    return load_context_definition(PROFILE)[0]


def resolve(
    image: bytes | None = None,
    *,
    defn: ContextDefinition | None = None,
    rom_map: RomMap | None = None,
    model: tuple[ApplicationModel, str] | None = None,
    slot: int = 1,
) -> ResolvedContext:
    image = image if image is not None else context_image()
    return resolve_context(
        image,
        rom_map or xterra_map(),
        defn or definition(),
        application_model=model or synthetic_program(image),
        expected_sha256=hashlib.sha256(image).hexdigest(),
        slot=slot,
    )


def _flags(bit: int, on: bool) -> float:
    return float(1 << bit) if on else 0.0


def context_feed(
    stream: Any,
    t: float,
    *,
    rpm_word: int = 128,
    fill: int = 96,
    x: int = 40,
    coolant_raw: int = 200,
    pedal: bool = False,
    high_det: bool | None = False,
    magic: int = 0xAA5555AA,
    active: int = 0,
    selector: int = 0,
    command: bool = True,
    skip: tuple[str, ...] = (),
    basis: str = "acquired",
) -> None:
    """One read of every channel the context uses, all at ``t``."""
    reads = {
        "mem:ECM_ENGINE_SPEED_RAM": rpm_word * 12.5,
        "mem:ECM_FUEL_COMP_X_INPUT": float(fill),
        "mem:ECM_FUEL_TARGET_MAP_X_TRACE": x * 0.250859,
        "mem:ECM_PEDAL_ENRICH_FLAGS": _flags(0, pedal),
        "did:ECM_COOLANT_TEMP": coolant_raw - 50.0,
        "mem:ECM_UPREV_MAGIC": float(magic),
        "mem:ECM_ACTIVE_MAP": float(active),
        "mem:ECM_UPREV_MAP_SELECTOR": float(selector),
        "did:ECM_EVAP_PURGE_CMD": 10.0,
    }
    if high_det is not None:
        reads["mem:ECM_HIGH_DET_FLAGS"] = _flags(6, high_det)
    if command:
        reads["mem:ECM_TARGET_EQUIV_RATIO"] = 14746 / 16384
    else:
        reads["mem:ECM_TARGET_EQUIV_RATIO_SENTINEL"] = float("nan")
    for spec, value in reads.items():
        if spec not in skip:
            stream.observe(spec, value, t, basis=basis)


def context_long(reads: list[tuple[float, str, float]], basis: str = "acquired") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime([t for t, _s, _v in reads], unit="s", utc=True),
            "spec": [s for _t, s, _v in reads],
            "value": [v for _t, _s, v in reads],
            "unit": [""] * len(reads),
            "timestamp_basis": [basis] * len(reads),
        }
    )


def context_ram_study_frame() -> pd.DataFrame:
    expect = load_yaml((REPO / "tests/fixtures/ground_truth/target_phi.yaml").read_text())
    frames = []
    for part in expect["telemetry"]:
        path = REPO / part["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == part["sha256"]
        frames.append(pd.read_parquet(path))
    frame = pd.concat(frames, ignore_index=True)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    return frame
