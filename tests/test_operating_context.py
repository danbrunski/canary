"""B08.2 — firmware-derived operating context, on synthetic images.

No ROM bytes are committed: the image is :func:`tests.rom_fixtures.load_byte_image`
noise with the application model's patch markers and calibration conditions
written in, known Fuel Comp and timing-trim cells and axes at the offsets
``rom_map.yaml`` declares, and the model's program identity re-pinned to the
synthetic program spans. ``tests/test_operating_context_private_rom.py`` runs
the resolution on the operator's two real calibration images.
"""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from src.analysis.channels import read_channels
from src.analysis.ecu_application import ApplicationModel, load_application_model, program_sha256
from src.analysis.operating_context import (
    ContextDefinition,
    ContextProvider,
    ContextRefusalError,
    ResolvedContext,
    image_tables,
    load_context_definition,
    resolve_context,
)
from src.analysis.rom_map import RomMap
from src.analysis.target_phi import load_twin_definition
from src.control.rom_library import RomLibrary
from src.decode.air_charge import rpm_load_lookup
from src.decode.dids import load_did_map
from src.decode.fuel_comp_load import LoadInputCurves, load_byte, rpm_reciprocal
from src.paths import DIDS_YAML
from src.vehicle.tune_revisions import load_tune_revisions
from src.yaml_load import load_yaml
from tests.rom_fixtures import PROFILE, load_byte_image, with_checksums, xterra_map

REPO = Path(__file__).resolve().parents[1]

#: Known cells, asymmetric in row and column so a transposed read differs.
FUEL_RPM_AXIS = tuple(range(8, 129, 8))  # 0xCACD: 400..6,400 rpm (x 50)
FUEL_FILL_AXIS = (*range(16, 241, 16), 255)  # 0xCB29 restored: 16..255
FUEL_FILL_AXIS_COMPRESSED = (*range(0x10, 0x88, 8), 0x90)  # as rom_fixtures: 16..144
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


@pytest.fixture(scope="module")
def image() -> bytes:
    return context_image()


@pytest.fixture(scope="module")
def resolved(image: bytes) -> ResolvedContext:
    return resolve(image)


@pytest.fixture(scope="module")
def provider(image: bytes) -> ContextProvider:
    model = synthetic_program(image)
    tables = image_tables(
        image,
        xterra_map(),
        definition(),
        application_model=model,
        expected_sha256=hashlib.sha256(image).hexdigest(),
    )
    return ContextProvider.from_profile(
        PROFILE, rom_map=xterra_map(), application_model=model, tables=tables
    )


# -- known vectors: the corrected axes and the row-major layout -----------------------


#: (rpm word, fill byte, expected Fuel Comp byte, row edge, column edge), worked by hand.
FUEL_VECTORS = [
    # On a breakpoint of each axis: row 3 (byte 32 x 4 = word 128, 1,600 rpm),
    # column 5 (fill 96): the cell itself.
    (128, 96, fuel_cell(3, 5), None, None),
    # Halfway in both: rows 117->118 and 121->122 at Q16 0x8000, then 117->121.
    (144, 104, 119, None, None),
    # Past both ends: the last rpm row, the first fill column, no fraction.
    (600, 8, fuel_cell(15, 0), "above", "below"),
    # The top fill breakpoint 255 is the last column exactly.
    (32, 255, fuel_cell(0, 15), None, None),
]


@pytest.mark.parametrize(("rpm", "fill", "expected", "row_edge", "column_edge"), FUEL_VECTORS)
def test_fuel_comp_reads_rpm_rows_on_0xcacd_and_fill_columns_on_0xcb29(
    resolved: ResolvedContext,
    rpm: int,
    fill: int,
    expected: int,
    row_edge: str | None,
    column_edge: str | None,
) -> None:
    table = resolved.fuel_comp
    assert table.table_id == "fuel_compensation"
    assert (table.row_axis_id, table.column_axis_id) == (
        "fuel_comp_engine_speed_axis",
        "fuel_comp_flow_axis",
    )
    assert table.row_axis_raw == FUEL_RPM_AXIS and table.column_axis_raw == FUEL_FILL_AXIS
    result = table.lookup(rpm, fill)
    assert result.value == expected
    assert (result.row.edge, result.column.edge) == (row_edge, column_edge)
    # E05's implementation of the node (fuel_comp_lookup) agrees vector for vector.
    assert rpm_load_lookup(table.cells, table.row_axis_raw, table.column_axis_raw, rpm, fill) == (
        expected
    )


def test_transposed_and_axis_swapped_reads_fail_the_known_vectors(
    resolved: ResolvedContext,
) -> None:
    """Controls: the grid read column-major (UpRev's grid transposed), or the
    two inputs fed to each other's axis, cannot reproduce the vectors."""
    table = resolved.fuel_comp
    transposed = tuple(zip(*table.cells, strict=True))
    misses_t = misses_swap = 0
    for rpm, fill, expected, _r, _c in FUEL_VECTORS[:2]:
        misses_t += (
            rpm_load_lookup(transposed, FUEL_RPM_AXIS, FUEL_FILL_AXIS, rpm, fill) != expected
        )
        swapped = rpm_load_lookup(
            table.cells, table.column_axis_raw, table.row_axis_raw, min(fill * 4, 0xFFFF), rpm // 4
        )
        misses_swap += swapped != expected
    assert misses_t == 2 and misses_swap == 2


#: (rpm word, X, coolant raw, pedal, high det, expected table, trim, flag).
TRIM_VECTORS = [
    # Warm, HI DET set: trim B at row 3 (byte 24 x 4 = 96), column 4 (X 40).
    (96, 40, 200, False, True, "timing_trim_b", trim_b_cell(3, 4), False),
    # Warm, HI DET clear: trim A, same cell.
    (96, 40, 200, False, False, "timing_trim_a", trim_a_cell(3, 4), False),
    # Row 9 (byte 72 x 4 = 288) of trim A carries bit 7: masked value, flag kept.
    (288, 40, 200, False, False, "timing_trim_a", trim_a_cell(9, 4) & 0x7F, True),
    # Pedal flag: the load input is 0xFF, the last column, past the axis end.
    (96, 40, 200, True, False, "timing_trim_a", trim_a_cell(3, 15), False),
    # Cold (coolant <= 106): trim A whatever the latch says.
    (96, 40, 100, False, True, "timing_trim_a", trim_a_cell(3, 4), False),
]


def _flags(bit: int, on: bool) -> float:
    return float(1 << bit) if on else 0.0


def _feed(
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


@pytest.mark.parametrize(
    ("rpm", "x", "coolant", "pedal", "high_det", "table", "trim", "flag"), TRIM_VECTORS
)
def test_timing_trim_reads_rpm_0xcc93_bfs_x_0xcc83_masked_and_selected_by_hi_det(
    provider: ContextProvider,
    rpm: int,
    x: int,
    coolant: int,
    pedal: bool,
    high_det: bool,
    table: str,
    trim: int,
    flag: bool,
) -> None:
    stream = provider.stream(role="serve", captured=None)
    _feed(stream, 100.0, rpm_word=rpm, x=x, coolant_raw=coolant, pedal=pedal, high_det=high_det)
    timing = stream.context_at(100.1)["timing_trim"]
    assert "refused" not in timing, timing
    assert (timing["table"], timing["trim"], timing["trim_flag"]) == (table, trim, flag)
    assert timing["is_final_timing"] is False


def test_timing_trim_transpose_and_unmasked_controls_fail(resolved: ResolvedContext) -> None:
    from src.analysis.ecu_state_models import trim_lookup

    a = resolved.trim_a
    assert (a.row_axis_id, a.column_axis_id) == ("timing_trim_rpm_axis", "timing_trim_load_axis")
    good = trim_lookup(a.cells, a.row_axis_raw, a.column_axis_raw, 96, 40)
    transposed = tuple(zip(*a.cells, strict=True))
    assert trim_lookup(transposed, a.row_axis_raw, a.column_axis_raw, 96, 40).trim != good.trim
    # Unmasked, the flagged row 9 reads 128 counts high.
    from src.decode.ecm_lookup import lookup_2d

    raw = lookup_2d(a.cells, [b * 4 for b in a.row_axis_raw], list(a.column_axis_raw), 288, 40)
    assert raw.value - (trim_a_cell(9, 4) & 0x7F) == 128


# -- resolution refusals (wrong axis), through W08 ---------------------------------------


def _swap_declared_axes(defn: ContextDefinition, block: str) -> ContextDefinition:
    decl = getattr(defn, block)
    rows, columns = decl.rows, decl.columns
    swapped = decl.model_copy(
        update={
            "rows": rows.model_copy(update={"rom_map_axis": columns.rom_map_axis}),
            "columns": columns.model_copy(update={"rom_map_axis": rows.rom_map_axis}),
        }
    )
    return defn.model_copy(update={block: swapped})


@pytest.mark.parametrize("block", ["fuel_comp", "timing_trim"])
def test_a_declared_axis_swap_is_refused_by_name(image: bytes, block: str) -> None:
    with pytest.raises(ContextRefusalError) as caught:
        resolve(image, defn=_swap_declared_axes(definition(), block))
    assert caught.value.reason == "axis_mismatch"


def test_a_w08_map_with_the_axes_the_editor_shows_is_refused(image: bytes) -> None:
    """UpRev's labels (0xCB29 on the rows): a map carrying them disagrees with
    the context's corrected axes."""
    rom_map = xterra_map()
    tables = [
        t.model_copy(
            update={"axes": t.axes.model_copy(update={"rows": t.axes.cols, "cols": t.axes.rows})}
        )
        if t.kind == "fuel_compensation"
        else t
        for t in rom_map.tables
    ]
    # Square 16 x 16, so the swapped map still validates; only the roles move.
    swapped = RomMap.model_validate(
        {**rom_map.model_dump(), "tables": [t.model_dump() for t in tables]}
    )
    with pytest.raises(ContextRefusalError) as caught:
        resolve(image, rom_map=swapped)
    assert caught.value.reason == "axis_mismatch"


def test_an_application_model_reading_other_axes_is_refused(image: bytes) -> None:
    model, sha = synthetic_program(image)
    nodes = []
    for node in model.nodes:
        if node.id == "fuel_comp_lookup":
            rom = [
                r.model_copy(
                    update={"role": {"rows": "columns", "columns": "rows"}.get(r.role, r.role)}
                )
                for r in node.rom
            ]
            node = node.model_copy(update={"rom": rom})
        nodes.append(node)
    with pytest.raises(ContextRefusalError) as caught:
        resolve(image, model=(model.model_copy(update={"nodes": nodes}), sha))
    assert caught.value.reason == "application_model_mismatch"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("hash", "image_hash_mismatch"),
        ("program", "wrong_program"),
        ("literal", "wrong_program"),
        ("slot", "table_unverified"),
        ("curve", "curves_not_monotone"),
    ],
)
def test_resolution_refuses_by_name(image: bytes, change: str, reason: str) -> None:
    rom_map, defn = xterra_map(), definition()
    model = synthetic_program(image)
    expected = hashlib.sha256(image).hexdigest()
    slot = 1
    if change == "hash":
        expected = "0" * 64
    elif change == "program":
        model = load_application_model(PROFILE)
    elif change == "literal":
        image = bytearray(image)  # type: ignore[assignment]
        struct.pack_into(">I", image, 0x3C5E8, 0x00DC1D01)
        image = bytes(image)
        model = synthetic_program(image)
        expected = hashlib.sha256(image).hexdigest()
    elif change == "slot":
        slot = 2  # placed by the slot copy, never seen live: inferred
    elif change == "curve":
        image = bytearray(image)  # type: ignore[assignment]
        struct.pack_into(">H", image, 0xEACC + 2 * 15, 0)
        image = bytes(image)
        model = synthetic_program(image)
        expected = hashlib.sha256(image).hexdigest()
    with pytest.raises(ContextRefusalError) as caught:
        resolve_context(
            image, rom_map, defn, application_model=model, expected_sha256=expected, slot=slot
        )
    assert caught.value.reason == reason


def test_both_calibration_layouts_resolve_through_the_w08_library(tmp_path: Path) -> None:
    """The compressed (k106 / 20260920) and restored (20261004) load axes, as
    two library images fetched back by sha256: the path a deployment uses."""
    library = RomLibrary(
        tmp_path,
        profile_dir=PROFILE,
        registry=lambda: load_tune_revisions(PROFILE / "tune_revisions.yaml"),
    )
    shas = {}
    for name, axis in (("compressed", FUEL_FILL_AXIS_COMPRESSED), ("restored", FUEL_FILL_AXIS)):
        image = context_image(fill_axis=axis)
        library.add_image(image, source="upload_plain", filename=f"{name}.bin")
        shas[name] = hashlib.sha256(image).hexdigest()
    axes = {}
    for name, sha in shas.items():
        image = library.image(sha)
        got = resolve_context(
            image,
            library.rom_map(),
            definition(),
            application_model=synthetic_program(image),
            expected_sha256=sha,
            slot=1,
        )
        assert got.image_sha256 == sha
        assert got.fuel_comp.row_axis_raw == FUEL_RPM_AXIS
        axes[name] = got.fuel_comp.column_axis_raw
    assert axes == {"compressed": FUEL_FILL_AXIS_COMPRESSED, "restored": FUEL_FILL_AXIS}


# -- the two saturations ------------------------------------------------------------------


def test_the_fill_ceiling_and_the_reciprocal_rpm_saturation_come_from_the_image(
    image: bytes, resolved: ResolvedContext
) -> None:
    """Both computed from W08 ids, cross-checked against the N02 chain."""
    curves = LoadInputCurves(
        area_breakpoints=struct.unpack_from(">32H", image, 0xEE6A),
        area_values=struct.unpack_from(">32H", image, 0xDD94),
        fill_low=struct.unpack_from(">16H", image, 0xEAEC),
        fill_high=struct.unpack_from(">16H", image, 0xEACC),
        rpm_multiplier=image[0x8D25],
        reciprocal_numerator=0x00DC1D00,
        idle_air_cap=1000,
        limp_home_area=700,
    )
    assert resolved.fill_ceiling == load_byte(0xFFFF, curves)
    assert max(load_byte(x, curves) for x in range(0, 0x10000, 97)) <= resolved.fill_ceiling
    word = resolved.reciprocal_saturation_word
    assert curves.rpm_multiplier * (word - 1) < 0xFFFF <= curves.rpm_multiplier * word
    # From the saturation word up, w116A no longer falls with rpm.
    assert rpm_reciprocal(word, curves) == rpm_reciprocal(word + 40, curves)
    assert rpm_reciprocal(word - 20, curves) > rpm_reciprocal(word, curves)


def test_fill_ceiling_and_rpm_saturation_are_reported_separately(
    provider: ContextProvider, resolved: ResolvedContext
) -> None:
    sat, ceiling = resolved.reciprocal_saturation_word, resolved.fill_ceiling
    stream = provider.stream(role="serve", captured=None)
    _feed(stream, 10.0, rpm_word=sat - 20, fill=ceiling)
    low_rpm = stream.context_at(10.1)["fuel_comp"]
    _feed(stream, 20.0, rpm_word=sat + 20, fill=ceiling - 40)
    high_rpm = stream.context_at(20.1)["fuel_comp"]
    assert (low_rpm["fill_at_ceiling"], low_rpm["rpm_reciprocal_saturated"]) == (True, False)
    assert (high_rpm["fill_at_ceiling"], high_rpm["rpm_reciprocal_saturated"]) == (False, True)
    assert high_rpm["reciprocal_saturation_rpm"] == sat * 12.5


# -- freshness, skew and named refusals ----------------------------------------------------


def test_a_fresh_tuple_gives_the_cell_its_units_and_its_clamp_state(
    provider: ContextProvider,
) -> None:
    stream = provider.stream(role="serve", captured=None)
    _feed(stream, 50.0, rpm_word=144, fill=104)
    record = stream.context_at(50.2)
    fuel = record["fuel_comp"]
    assert fuel["value_raw"] == 119 and fuel["value"] == pytest.approx(119 * 0.78125)
    assert fuel["rpm"] == 1800.0 and fuel["fill"] == pytest.approx(104 * 100 / 224)
    assert fuel["clamped"] == {"rows": None, "columns": None}
    assert fuel["row"]["index"] == 3 and fuel["column"]["index"] == 5
    assert fuel["acquisition"]["reads"]["engine_speed_word"]["age_s"] == pytest.approx(0.2)
    assert record["identity"]["slot"] == 1
    assert record["pedal_enrichment"] == {"set": False, "raw": 0, "age_s": pytest.approx(0.2)}
    assert record["fuel_cut"]["active"] is False
    assert record["retained"]["purge"]["raw"] == 20
    assert record["retained"]["idle_air_correction"]["refused"] == (
        "not_catalogued:idle_air_correction"
    )
    assert record["retained"]["throttle_fallback"]["refused"] == "not_catalogued:throttle_fallback"


def test_a_five_second_old_window_read_is_stale_not_fresh(provider: ContextProvider) -> None:
    """The memory windows of a default-rate capture come every ~5 s; held over
    to the next sample they are refused, never carried forward as fresh."""
    stream = provider.stream(role="serve", captured=None)
    _feed(stream, 0.0)
    record = stream.context_at(5.0)
    assert record["fuel_comp"]["refused"] == "stale:engine_speed_word"
    assert record["timing_trim"]["refused"] == "stale:engine_speed_word"
    assert record["pedal_enrichment"]["refused"] == "stale:pedal_flags"
    assert record["fuel_cut"]["refused"] == "stale:command_word"
    # Map identity (10 s) and coolant (10 s) are still within their own limits.
    assert record["identity"]["slot"] == 1


def test_a_tuple_wider_than_the_skew_is_refused(provider: ContextProvider) -> None:
    stream = provider.stream(role="serve", captured=None)
    _feed(stream, 0.0)
    stream.observe("mem:ECM_ENGINE_SPEED_RAM", 128 * 12.5, 0.4, basis="acquired")
    record = stream.context_at(0.45)
    assert record["fuel_comp"]["refused"] == "skew:fuel_comp"
    assert record["fuel_comp"]["acquisition"]["tuple_skew_s"] == pytest.approx(0.4)


@pytest.mark.parametrize(
    ("setup", "item", "reason"),
    [
        ({"skip": ("mem:ECM_FUEL_COMP_X_INPUT",)}, "fuel_comp", "missing:fuel_comp_fill"),
        ({"basis": "logical"}, "fuel_comp", "clock_unacquired:map_magic"),
        ({"magic": 0}, "fuel_comp", "slot_unresolved:map_uninitialised"),
        ({"active": 9, "selector": 3}, "fuel_comp", "slot_unresolved:slot_ambiguous"),
        ({"high_det": None}, "timing_trim", "missing:high_det_flags"),
        ({"coolant_raw": 108}, "timing_trim", "coolant_blend_band"),
        ({"command": False}, "fuel_cut", None),
    ],
)
def test_missing_or_unknown_state_is_refused_by_name(
    provider: ContextProvider, setup: dict[str, Any], item: str, reason: str | None
) -> None:
    stream = provider.stream(role="serve", captured=None)
    _feed(stream, 0.0, **setup)
    got = stream.context_at(0.1)[item]
    if reason is None:
        assert got["active"] is True  # the sentinel: fuel cut is a state
    else:
        assert got["refused"] == reason


def test_a_stored_value_off_the_catalog_scale_is_wrong_units(provider: ContextProvider) -> None:
    stream = provider.stream(role="serve", captured=None)
    _feed(stream, 0.0)
    stream.observe("mem:ECM_ENGINE_SPEED_RAM", 1000.3, 0.05, basis="acquired")
    assert stream.context_at(0.1)["fuel_comp"]["refused"] == "wrong_units:engine_speed_word"


def test_a_channel_the_capture_does_not_carry_is_not_captured(provider: ContextProvider) -> None:
    captured = set(provider.channels()) - {"mem:ECM_HIGH_DET_FLAGS"}
    stream = provider.stream(role="fit", captured=captured)
    _feed(stream, 0.0, high_det=None)
    assert stream.context_at(0.1)["timing_trim"]["refused"] == "not_captured:high_det_flags"
    # Cold, the latch is not read, so its absence does not refuse.
    cold = provider.stream(role="fit", captured=captured)
    _feed(cold, 0.0, high_det=None, coolant_raw=90)
    assert cold.context_at(0.1)["timing_trim"]["table"] == "timing_trim_a"


# -- fit and serve: one contract, one evaluator ---------------------------------------------


def test_fit_and_serve_contracts_carry_skew_freshness_units_and_applicability(
    provider: ContextProvider,
) -> None:
    fit, serve = provider.contract("fit"), provider.contract("serve")
    assert {k: v for k, v in fit.items() if k != "role"} == {
        k: v for k, v in serve.items() if k != "role"
    }
    for c in (fit, serve):
        assert c["interpretation"] == "relative_model_coordinates"
        for name, entry in c["inputs"].items():
            assert entry["raw_units"] == "raw_counts", name
            assert entry["freshness"]["max_age_s"] > 0 and entry["freshness"]["clock"] == "acquired"
            assert "scale" in entry["stored_decode"] and "engineering_unit" in entry
        for lookup in c["lookups"].values():
            assert lookup["max_acquisition_skew_s"] > 0
            assert lookup["layout"] == "row_major"
            assert lookup["applicability"]["status"] in ("conditional", "resolved")
    fuel = fit["lookups"]["fuel_comp"]
    assert (fuel["rows"]["rom_map_axis"], fuel["columns"]["rom_map_axis"]) == (
        "fuel_comp_engine_speed_axis",
        "fuel_comp_flow_axis",
    )
    # The observed fill byte cuts its writer; the RAM copy stays assumed.
    assert "fuel_comp_load_byte" in fuel["applicability"]["observed_by_context"]
    assert fuel["applicability"]["unresolved"] == ["air_charge_ram_copy"]
    timing = fit["lookups"]["timing_trim"]["applicability"]
    assert timing["output_role"] == "intermediate"
    assert timing["unresolved"] == ["timing_trim_ram_copy"]


def test_the_contract_fingerprint_moves_with_freshness(provider: ContextProvider) -> None:
    defn = provider.definition
    inputs = dict(defn.inputs)
    inputs["fuel_comp_fill"] = inputs["fuel_comp_fill"].model_copy(update={"max_age_s": 5.0})
    looser = ContextProvider(
        definition=defn.model_copy(update={"inputs": inputs}),
        definition_sha256=provider.definition_sha256,
        twin=provider.twin,
        application_model=provider.application_model,
        rom_map=provider.rom_map,
        catalog=provider.catalog,
        tables=provider.tables,
    )
    assert looser.contract("serve")["fingerprint"] != provider.contract("serve")["fingerprint"]


def _long(reads: list[tuple[float, str, float]], basis: str = "acquired") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime([t for t, _s, _v in reads], unit="s", utc=True),
            "spec": [s for _t, s, _v in reads],
            "value": [v for _t, _s, v in reads],
            "unit": [""] * len(reads),
            "timestamp_basis": [basis] * len(reads),
        }
    )


def test_the_stored_path_replays_through_the_live_stream(provider: ContextProvider) -> None:
    class Collect:
        def __init__(self) -> None:
            self.reads: list[tuple[float, str, float]] = []

        def observe(self, spec: str, value: float, t: float, *, basis: str) -> None:
            self.reads.append((t, spec, value))

    collect = Collect()
    _feed(collect, 1.0, rpm_word=144, fill=104)
    _feed(collect, 7.0, rpm_word=200, fill=150, high_det=True)
    times = [1.1, 4.0, 7.2]
    fit = provider.evaluate_frame(_long(collect.reads), times)
    serve = provider.stream(role="serve", captured=None)
    live = []
    for t in times:
        for read in [r for r in collect.reads if r[0] <= t]:
            serve.observe(read[1], read[2], read[0], basis="acquired")
        live.append(serve.context_at(t))
    assert [{**r, "role": None} for r in fit] == [{**r, "role": None} for r in live]
    assert fit[0]["fuel_comp"]["value_raw"] == 119
    assert fit[1]["fuel_comp"]["refused"] == "stale:engine_speed_word"
    assert fit[2]["timing_trim"]["table"] == "timing_trim_b"


def test_a_resampled_grid_is_refused(provider: ContextProvider) -> None:
    wide = pd.DataFrame(
        {
            "timestamp": pd.to_datetime([0, 1, 2], unit="s", utc=True),
            "mem:ECM_FUEL_COMP_X_INPUT": [96.0, 96.0, 96.0],
            "mem:ECM_FUEL_COMP_X_INPUT__arrived": [True, False, False],
        }
    )
    with pytest.raises(ContextRefusalError) as caught:
        provider.evaluate_frame(wide, [2.0])
    assert caught.value.reason == "not_acquisition_timed"
    rounds = _long([(0.0, "mem:ECM_FUEL_COMP_X_INPUT", 96.0)]).drop(columns="timestamp_basis")
    with pytest.raises(ContextRefusalError) as caught:
        provider.evaluate_frame(rounds, [0.1])
    assert caught.value.reason == "clock_unacquired"


# -- real captures: history does not carry these channels ------------------------------------


def _ram_study_frame() -> pd.DataFrame:
    expect = load_yaml((REPO / "tests/fixtures/ground_truth/target_phi.yaml").read_text())
    frames = []
    for part in expect["telemetry"]:
        path = REPO / part["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == part["sha256"]
        frames.append(pd.read_parquet(path))
    frame = pd.concat(frames, ignore_index=True)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    return frame


def test_the_october_4_ram_study_slice_names_what_it_lacks(provider: ContextProvider) -> None:
    """The committed slice of _capture-ram-study-2026-10-04T204557 carries
    rpm, X, the pedal flags, the command and the map identity, but not the
    fill byte, HI DET or coolant: those items refuse by name, while fuel cut
    is read at every command read and is stale five seconds on."""
    long = read_channels(_ram_study_frame(), provider.channels(), acquired_time=True)
    coverage = provider.coverage(long["spec"])
    assert set(coverage["not_captured"]) == {
        "fuel_comp_fill",
        "high_det_flags",
        "coolant_byte",
        "purge_command",
    }
    commands = long[
        long["spec"].isin(["mem:ECM_TARGET_EQUIV_RATIO", "mem:ECM_TARGET_EQUIV_RATIO_SENTINEL"])
    ]
    from src.signals.pairing import to_seconds

    times = sorted(set(to_seconds(commands["timestamp"]).tolist()))
    records = provider.evaluate_frame(long, times)
    assert {r["fuel_comp"].get("refused") for r in records} <= {
        "not_captured:fuel_comp_fill",
        "stale:engine_speed_word",
        "stale:map_magic",
        "missing:map_magic",
    }
    assert any(r["fuel_comp"].get("refused") == "not_captured:fuel_comp_fill" for r in records)
    cut = [r["fuel_cut"]["active"] for r in records if "active" in r["fuel_cut"]]
    sentinels = int((commands["spec"] == "mem:ECM_TARGET_EQUIV_RATIO_SENTINEL").sum())
    assert len(cut) == len(times) and sum(cut) == sentinels
    later = provider.evaluate_frame(long, [t + 4.9 for t in times[:20]])
    # The newest read of either channel decides, so a stale one is named for it.
    assert {r["fuel_cut"].get("refused") for r in later} <= {
        "stale:command_word",
        "stale:command_word_sentinel",
    }


def test_twin_map_identity_is_shared_not_restated() -> None:
    """The context names the twin's file for the slot rules and declares none
    of its own channels for them."""
    defn = definition()
    twin, _ = load_twin_definition(PROFILE)
    assert defn.map_identity_from == "target_phi_twin.yaml"
    declared = {d.channel for d in defn.inputs.values()}
    assert not declared & set(twin.map_identity.channels())


def test_every_mem_input_reads_the_catalog_address() -> None:
    from src.analysis.operating_context import check_catalog

    check_catalog(definition(), load_did_map(DIDS_YAML))


# -- verification fixes: newest-read decides, no future reads, caller order -----------------


@pytest.mark.parametrize(
    ("value", "basis", "reason"),
    [
        (14746 / 16384, "logical", "clock_unacquired:command_word"),
        (0.12345678, "acquired", "wrong_units:command_word"),
    ],
)
def test_a_refused_newer_command_read_is_not_covered_by_an_older_sentinel(
    provider: ContextProvider, value: float, basis: str, reason: str
) -> None:
    """The newest read of either channel decides fuel cut. A newer command
    read that cannot be used refuses; the older sentinel never stands in."""
    stream = provider.stream(role="serve", captured=None)
    _feed(stream, 0.0, command=False)
    stream.observe("mem:ECM_TARGET_EQUIV_RATIO", value, 0.3, basis=basis)
    assert stream.context_at(0.4)["fuel_cut"] == {"refused": reason}


def test_a_query_before_a_held_read_is_refused(provider: ContextProvider) -> None:
    stream = provider.stream(role="serve", captured=None)
    _feed(stream, 10.0)
    with pytest.raises(ContextRefusalError) as caught:
        stream.context_at(5.0)
    assert caught.value.reason == "query_before_reads"


def test_fit_and_serve_agree_at_an_instant_before_later_reads(provider: ContextProvider) -> None:
    class Collect:
        def __init__(self) -> None:
            self.reads: list[tuple[float, str, float]] = []

        def observe(self, spec: str, value: float, t: float, *, basis: str) -> None:
            self.reads.append((t, spec, value))

    collect = Collect()
    _feed(collect, 4.9)
    _feed(collect, 10.0, rpm_word=300)
    (fit,) = provider.evaluate_frame(_long(collect.reads), [5.0])
    serve = provider.stream(role="serve", captured=None)
    for t, spec, value in collect.reads:
        if t <= 5.0:
            serve.observe(spec, value, t, basis="acquired")
    live = serve.context_at(5.0)
    assert {**fit, "role": None} == {**live, "role": None}
    assert fit["fuel_comp"]["rpm_word"] == 128
    assert fit["fuel_comp"]["acquisition"]["reads"]["engine_speed_word"]["age_s"] >= 0


def test_evaluate_frame_returns_records_in_the_callers_order(provider: ContextProvider) -> None:
    class Collect:
        def __init__(self) -> None:
            self.reads: list[tuple[float, str, float]] = []

        def observe(self, spec: str, value: float, t: float, *, basis: str) -> None:
            self.reads.append((t, spec, value))

    collect = Collect()
    _feed(collect, 1.0, rpm_word=144, fill=104)
    _feed(collect, 7.0, rpm_word=200, fill=150)
    times = [7.2, 1.1, 4.0]
    records = provider.evaluate_frame(_long(collect.reads), times)
    assert [r["t"] for r in records] == times
    assert records[0]["fuel_comp"]["rpm_word"] == 200
    assert records[1]["fuel_comp"]["rpm_word"] == 144
    assert records[2]["fuel_comp"]["refused"] == "stale:engine_speed_word"


def test_known_observation_gaps_are_named_applicability_limits(provider: ContextProvider) -> None:
    lookups = provider.contract("serve")["lookups"]
    assert [x["id"] for x in lookups["timing_trim"]["applicability"]["limits"]] == [
        "high_det_read_previous_cycle"
    ]
    assert [x["id"] for x in lookups["fuel_comp"]["applicability"]["limits"]] == [
        "rpm_word_is_cycle_copy"
    ]
