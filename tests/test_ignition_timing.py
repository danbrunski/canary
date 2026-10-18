"""E09.7: ECU-side ignition timing in the paired simulator.

Two kinds of evidence, labelled per test:

- **synthetic (code)**: hand-worked from the decoded chain
  (``src.analysis.ecu_state_models``; ``ecu_application_model.yaml`` nodes
  ``timing_trim_lookup`` .. ``timing_final_byte``). They pin our reading of
  the code, not the engine.
- **private ROM**: the operator's images (``tests.rom_fixtures.OPERATOR_ROMS``),
  skipping by name without them. k106's ten trim-A edits are the known answer
  for where a trim change lands; a -3 count trim-B edit on ZS44A-20261004 is
  the code's prediction for N01.2's "degrees per count", not a measurement.
"""

from __future__ import annotations

import contextlib
import io
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml

from src.analysis.paired_scenarios import (
    ENRICHMENT_RETARD_UNMODELLED,
    LOAD_TERM_UNMODELLED,
    TIMING_STATE_UNSTATED,
    TimingState,
    TimingTables,
    coolant_byte,
    ignition_timing_at,
    ignition_timing_cells,
    timing_cell_summary,
    timing_tables,
)
from src.analysis.rom_map import load_rom_map
from src.analysis.sim_cells import ignition_timing_block
from src.analysis.sim_contract import SimulationRefusalError
from src.analysis.sim_montecarlo import load_sim_kit
from tests.rom_fixtures import OPERATOR_ROMS, PROFILE, operator_image
from tools import rom_sim

RPM_AXIS = tuple(8 * (i + 1) for i in range(16))
LOAD_AXIS = tuple(8 * (i + 1) for i in range(16))
WARM_C = 90.0  # coolant byte 140, above cal 0x8CCC (110)


def _tables(
    a: int = 50, b: int = 45, *, edit: tuple[str, int, int, int] | None = None
) -> TimingTables:
    cells = {"a": [[0x80 | a] * 16 for _ in range(16)], "b": [[b] * 16 for _ in range(16)]}
    if edit is not None:
        name, r, c, by = edit
        row = cells[name][r]
        row[c] = (row[c] & 0x80) | ((row[c] & 0x7F) + by)
    return TimingTables(
        a=tuple(tuple(r) for r in cells["a"]),
        b=tuple(tuple(r) for r in cells["b"]),
        rpm_axis=RPM_AXIS,
        load_axis=LOAD_AXIS,
    )


def _state(high_det: bool, knock: str = "active") -> TimingState:
    return TimingState(high_det=high_det, knock_control=knock, source="test")  # type: ignore[arg-type]


def _at(base: TimingTables, cand: TimingTables, state: TimingState | None, **kw: Any) -> Any:
    args: dict[str, Any] = dict(
        rpm_word=RPM_AXIS[3] * 4,
        x_base=LOAD_AXIS[5],
        x_candidate=LOAD_AXIS[5],
        coolant_c=WARM_C,
        pedal_flag=False,
        phi_moved=False,
    )
    return ignition_timing_at(base, cand, state, **{**args, **kw})


# -- synthetic (code) ------------------------------------------------------------------


def test_the_timing_state_is_stated_and_consistent() -> None:
    # The fallback sets HI DET only inside a gate (0x04A914-0x04A926), so
    # fallback with HI DET clear is a state, not a contradiction.
    assert _state(False, "fallback").high_det is False
    with pytest.raises(SimulationRefusalError):
        _state(True, "off")
    with pytest.raises(SimulationRefusalError):
        TimingState(high_det=True, knock_control="active", source=" ")
    assert coolant_byte(90.0) == 140 and coolant_byte(-60.0) == 0


def test_a_trim_b_edit_moves_timing_only_where_trim_b_is_read() -> None:
    base, cand = _tables(), _tables(edit=("b", 3, 5, -3))
    hot = _at(base, cand, _state(True, "fallback"))
    assert (hot["delta"], hot["status"], hot["table_read"]) == (-3, "delta_if_no_clamp", "b")
    assert hot["reasons"] == ["clamp_headroom_unknown"] and hot["base"] is None
    # HI DET clear: A is read, so the edit gives 0 and says why.
    clear = _at(base, cand, _state(False))
    assert (clear["delta"], clear["reasons"]) == (0, ["trim_b_not_read:high_det_latch_clear"])
    cold = _at(base, cand, _state(True), coolant_c=50.0)
    assert (cold["delta"], cold["reasons"]) == (0, ["trim_b_not_read:coolant_at_or_below_56c"])
    blend = _at(base, cand, _state(True), coolant_c=58.0)
    assert (blend["status"], blend["reasons"]) == ("unevaluable", ["coolant_blend_band"])
    assert _at(base, cand, None)["reasons"] == [TIMING_STATE_UNSTATED]
    # The logged base timing gives both absolutes (30 -> 27, no clamp binds).
    logged = _at(base, cand, _state(True), base_timing_deg=30)
    assert (logged["base"], logged["candidate"], logged["status"]) == (30, 27, "computed")


def test_a_trim_a_edit_with_hi_det_set_is_zero_with_a_reason() -> None:
    base, cand = _tables(), _tables(edit=("a", 3, 5, 2))
    hot = _at(base, cand, _state(True, "fallback"))
    assert (hot["delta"], hot["reasons"]) == (0, ["trim_a_not_read:high_det_latch_set"])
    assert _at(base, cand, _state(False))["delta"] == 2
    # The cold side reads A too.
    assert _at(base, cand, _state(True), coolant_c=50.0)["delta"] == 2


def test_an_x_change_gives_the_trim_term_but_no_timing() -> None:
    """K or Fuel Comp moves X: the trims' column moves (second order), and so
    does 0xFFFF27C6, which is not computed."""
    base = cand = TimingTables(
        a=tuple(tuple(0x80 | (60 - c) for c in range(16)) for _ in range(16)),
        b=_tables().b,
        rpm_axis=RPM_AXIS,
        load_axis=LOAD_AXIS,
    )
    moved = _at(base, cand, _state(False), x_base=LOAD_AXIS[5], x_candidate=LOAD_AXIS[7])
    assert (moved["delta"], moved["trim_term_delta"]) == (None, -2)
    assert moved["reasons"] == [LOAD_TERM_UNMODELLED]
    phi = _at(base, cand, _state(False), phi_moved=True)
    assert (phi["delta"], phi["reasons"]) == (None, [ENRICHMENT_RETARD_UNMODELLED])


def test_the_pedal_flag_reads_the_last_column() -> None:
    base, cand = _tables(), _tables(edit=("a", 3, 15, -1))
    assert _at(base, cand, _state(False))["delta"] == 0  # column 5
    assert _at(base, cand, _state(False), pedal_flag=True)["delta"] == -1


def test_the_cells_show_an_edit_in_exactly_its_cells() -> None:
    base, cand = _tables(), _tables(edit=("b", 9, 14, -3))
    cells = ignition_timing_cells(base, cand, _state(True, "fallback"), coolant_c=WARM_C)
    changed = [(c["row"], c["col"], c["delta"]) for c in cells if c["delta"]]
    assert changed == [(9, 14, -3)]
    summary = timing_cell_summary(cells)
    assert (summary["cells"], summary["changed"], summary["unevaluable"]) == (256, 1, 0)


# -- private ROM ----------------------------------------------------------------------


def _images(*names: str) -> list[bytes]:
    found = [operator_image(n) for n in names]
    if any(i is None for i in found):
        pytest.skip(f"operator ROMs {names} not on this machine")
    return [i for i in found if i is not None]


@pytest.mark.slow
def test_k106s_ten_trim_a_edits_move_timing_in_those_cells_and_only_with_hi_det_clear() -> None:
    """The ROM Store base against k106 ("added spark at 3000-4500 high load"):
    trim A rows 7-9 x columns 12-15, +1/+2. With HI DET clear the timing change
    is the trim change in exactly those ten cells; set (as on the truck since
    the knock sensor fallback, 0xFFFF13FE = 0xFB00) the ECM reads trim B and
    the change is 0 in every cell, each of the ten naming why."""
    base_image, k106_image = _images("base", "k106")
    rom_map = load_rom_map(PROFILE)
    base, k106 = (
        timing_tables(base_image, rom_map, slot=1),
        timing_tables(k106_image, rom_map, slot=1),
    )
    assert base.b == k106.b and base.a != k106.a
    clear = ignition_timing_cells(base, k106, _state(False), coolant_c=WARM_C)
    changed = {(c["row"], c["col"]): c["delta"] for c in clear if c["delta"]}
    assert len(changed) == 10
    assert all(7 <= r <= 9 and 12 <= c <= 15 for r, c in changed)
    assert set(changed.values()) <= {1, 2}
    for (r, c), delta in changed.items():  # the masked cell difference itself
        assert delta == (k106.a[r][c] & 0x7F) - (base.a[r][c] & 0x7F)
    held = ignition_timing_cells(base, k106, _state(True, "fallback"), coolant_c=WARM_C)
    assert not [c for c in held if c["delta"]]
    named = {(c["row"], c["col"]) for c in held if c["reasons"]}
    assert named == set(changed)
    assert {r for c in held for r in c["reasons"]} == {"trim_a_not_read:high_det_latch_set"}
    # k106 also raised K 6 %, which moves BFS and so X and 0xFFFF27C6: the
    # trim grid's block (every other term held) gives no timing for the pair.
    kit = load_sim_kit(PROFILE).kit
    block = ignition_timing_block(
        kit, base_image, k106_image, _state(False), slot=1, coolant_c=WARM_C, all_cells=False
    )
    assert block["status"] == "unevaluable" and block["trims_changed"]
    assert any(r.startswith("timing_input_changed:") for r in block["reasons"]), block["reasons"]


def _cli(*argv: str | Path) -> tuple[int, dict[str, Any]]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = rom_sim.main([str(a) for a in argv])
    return code, json.loads(out.getvalue())


def _trim_b_spec(tmp_path: Path) -> Path:
    spec = tmp_path / "trim_b_minus_3.yaml"
    spec.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "base": {"revision": "ZS44A-20261004"},
                "label": "trim B -3 counts",
                "edits": [{"table": "timing_trim_b", "slot": 1, "all": True, "add_raw": -3}],
            }
        )
    )
    return spec


@pytest.mark.slow
def test_a_minus_three_count_trim_b_edit_is_minus_three_degrees_in_every_cell(
    tmp_path: Path,
) -> None:
    """ZS44A-20261004, trim B -3 counts everywhere, through the edit spec and
    ``rom_sim inspect``: with HI DET set every trim cell's timing changes -3
    degrees (the chain adds the trim term without scaling; the absolute is
    unknown, so ``clamp_headroom_unknown``), torque refused with the spark
    reason. With HI DET clear it is 0 in every cell, each naming why."""
    _images("r20261004")
    rom_dir = OPERATOR_ROMS["r20261004"].parent
    spec = _trim_b_spec(tmp_path)
    common = ["inspect", "--edits", spec, "--rom-dir", rom_dir, "--timing-coolant-c", "90"]
    code, payload = _cli(*common, "--high-det", "set", "--knock-control", "fallback")
    assert code == 0, payload
    timing = payload["ignition_timing"]
    assert Counter(c["delta"] for c in timing["cells"]) == Counter({-3: 256})
    assert timing["summary"]["reasons"] == ["clamp_headroom_unknown"]
    assert timing["torque"]["reason"].startswith("spark_response_unmeasured")
    assert timing["knock"]["reasons"][0].startswith("knock_unmodelled")
    code, payload = _cli(*common, "--high-det", "clear", "--knock-control", "active")
    assert code == 0, payload
    cells = payload["ignition_timing"]["cells"]
    assert len(cells) == 256 and {c["delta"] for c in cells} == {0}
    assert {r for c in cells for r in c["reasons"]} == {"trim_b_not_read:high_det_latch_clear"}
    code, payload = _cli("inspect", "--edits", spec, "--rom-dir", rom_dir)
    assert (code, payload["refused"]) == (2, "timing_state_unstated")


@pytest.mark.slow
def test_the_paired_run_carries_the_timing_change_at_every_design_point(tmp_path: Path) -> None:
    """``rom_sim run`` over a grid design on ZS44A-20261004 with the -3 trim B
    edit: every (point, draw) pair is -3 degrees, and the report shows it."""
    _images("r20261004")
    rom_dir = OPERATOR_ROMS["r20261004"].parent
    designs = tmp_path / "designs"
    grid = [
        "design", "grid", "--base-revision", "ZS44A-20261004",
        "--rpm-edges", "1900", "2100", "2900", "3100", "--load-edges", "25", "32", "60", "70",
        "--load-basis", "fill_fraction_as_manifold_ratio", "--baro-kpa", "80", "--iat-c", "25",
        "--coolant-c", "88", "--battery-v", "14", "--rail-kpa", "working_assumption",
        "--throttle-pct", "20", "--pedal-flag", "0", "--out", designs,
    ]  # fmt: skip
    assert _cli(*grid)[0] == 0
    (design,) = designs.iterdir()
    run = tmp_path / "run"
    code, payload = _cli(
        "run", "--edits", _trim_b_spec(tmp_path), "--rom-dir", rom_dir, "--design", design,
        "--draws", "3", "--seed", "1", "--stft-pct", "0", "--ltft-pct", "0",
        "--candidate-events", "none", "--high-det", "set", "--knock-control", "fallback",
        "--out", run,
    )  # fmt: skip
    assert code == 0, payload
    result = json.loads((run / "result.json").read_text())
    points = result["ignition_timing"]["points"]
    assert len(points) == 9
    outcomes = [o for p in points for o in p["outcomes"]]
    assert {o["delta"] for o in outcomes} == {-3}
    assert sum(o["draws"] for o in outcomes) == 9 * 3
    assert all(o["x_base"] == o["x_candidate"] for o in outcomes)
    assert "Ignition timing (ECU side" in (run / "report.html").read_text()
