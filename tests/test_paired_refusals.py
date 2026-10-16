"""E09.2c: what leaves the paired path when it cannot run is a refusal, as data.

The paired comparison runs on the twin runner's ECU path, so a runner refusal
(``stage_order``'s ``kernel_mismatch`` and the rest) must cross into the
simulator's callers as the simulator's own refusal type, and every refusal
type must survive a process boundary (the workbench's spawn child pickles
what it returns).
"""

from __future__ import annotations

import dataclasses
import pickle
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from src.analysis import rom_editor
from src.analysis.paired_scenarios import SimKit, run_paired
from src.analysis.rom_effects import CellEdit, make_candidate
from src.analysis.rom_image import revision_match, sha256_hex
from src.analysis.rom_map import DTYPE_BYTES
from src.analysis.sim_contract import SimulationRefusalError
from src.analysis.target_phi import TwinRefusalError
from src.analysis.twin_runner import TwinRunnerRefusalError
from src.vehicle.tune_revisions import TuneRevision, TuneRevisionRegistry
from tests import test_rom_editor as w09
from tests.test_paired_scenarios import WOT, _scenario
from tests.test_sim_montecarlo import build_kit_and_image


@pytest.fixture(scope="module")
def kit_and_image() -> tuple[SimKit, bytes]:
    return build_kit_and_image()


def _matched(image: bytes) -> tuple[Any, dict[str, Any]]:
    """W09's revision registry and match for the synthetic image, as
    ``tests.test_rom_editor``'s fixtures build them."""
    registry = TuneRevisionRegistry(
        revisions=[
            TuneRevision(
                revision=w09.REVISION,
                flashed_at=datetime(2026, 10, 1, tzinfo=UTC),
                image_sha256=sha256_hex(image),
            )
        ]
    )
    found = revision_match(sha256_hex(image), registry)
    assert found["status"] == "matched"
    return registry, found


@pytest.mark.parametrize("cls", [TwinRunnerRefusalError, TwinRefusalError, SimulationRefusalError])
def test_every_refusal_type_on_the_paired_path_pickles(cls: type[ValueError]) -> None:
    exc = cls("some_reason", "what happened")
    back = pickle.loads(pickle.dumps(exc))
    assert type(back) is cls
    assert (back.reason, back.message, str(back)) == (  # type: ignore[attr-defined]
        "some_reason",
        "what happened",
        "some_reason: what happened",
    )


def _rekerneled(kit: SimKit) -> SimKit:
    nodes = [
        n.model_copy(update={"implemented_by": "src.somewhere.else"})
        if n.id == "base_fuel_schedule"
        else n
        for n in kit.model.nodes
    ]
    return dataclasses.replace(kit, model=kit.model.model_copy(update={"nodes": nodes}))


def test_a_runner_refusal_leaves_run_paired_as_a_simulation_refusal(
    kit_and_image: tuple[SimKit, bytes],
) -> None:
    kit, image = kit_and_image
    moved = _rekerneled(kit)
    with pytest.raises(SimulationRefusalError) as refused:
        run_paired(moved, _scenario(moved, image, image, WOT), image, image)
    assert refused.value.reason == "kernel_mismatch"
    assert isinstance(refused.value.__cause__, TwinRunnerRefusalError)


def test_a_workbench_job_on_a_model_the_runner_refuses_returns_the_refusal(
    kit_and_image: tuple[SimKit, bytes], tmp_path: Path
) -> None:
    """The verifier's case: a kit whose BFS node names another kernel. The job
    returns the refusal as data (W09's ``REFUSALS``), never raises it."""
    kit, image = kit_and_image
    registry, matched = _matched(image)
    params = w09._params(image, tmp_path, matched, request=w09.request(draws=2))
    out = rom_editor.run_job("rom_sim", params, kit=_rekerneled(kit), registry=registry)
    assert out["refused"]["kind"] == "simulation"
    assert out["refused"]["reason"] == "kernel_mismatch"
    assert "base_fuel_schedule" in out["refused"]["message"]


def test_a_zero_load_point_scaler_is_refused_by_name_not_divided_by(
    kit_and_image: tuple[SimKit, bytes],
) -> None:
    """X = BFS << shift / scaler: a scaler of 0 used to raise ZeroDivisionError
    from run_paired. The side that carries it names ``load_point_scaler_invalid``
    (as a missing one names ``load_point_scaler_unresolved``) and computes
    nothing; the base side is untouched."""
    kit, image = kit_and_image
    table = kit.rom_map.table("load_point_scaler")
    at = kit.rom_map.table_address(table, None)
    assert int.from_bytes(image[at : at + DTYPE_BYTES[table.dtype]], "big") > 0
    zero = make_candidate(
        image, kit.rom_map, [CellEdit("load_point_scaler", 0, 0, 0, slot=None)], base_label="s"
    ).image
    result = run_paired(kit, _scenario(kit, image, zero, WOT), image, zero)
    (point,) = result.points
    bfs = point["outputs"]["base_fuel_schedule"]
    assert bfs["base"] is not None and bfs["candidate"] is None
    assert "load_point_scaler_invalid" in bfs["reasons"]
    assert "load_point_scaler_invalid" in result.unresolved
