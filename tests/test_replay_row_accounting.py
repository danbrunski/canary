"""Replay row accounting through the sustained-validation harness (D03.7).

``tests/fixtures/ground_truth/drive-slice`` holds no ``decoded`` rows, so a
replay of it cannot show D02.9's doubling. These runs replay
``tests/fixtures/replay/decoded-slice`` instead — 20 s of the D02.9 evidence
session (``2026-09-10T142711``, cut by ``tools/make_replay_fixture.py``) whose
stored ``decoded`` rows are exactly the ones its kept ``can`` frames produced,
plus its stored ``derived`` and ``gps`` rows. Each run goes through the real
emit path (``replay_session``), the daemon fanout, the storage decoder and the
Parquet writer, and the report's ``replay_accounting`` checks the persisted
rows against the source as identities.

Covered: a payload-bearing source (stored ``decoded``/``derived`` skipped,
re-derived once), the same source with its payloads stripped (a pre-2026-07-29
recording: stored rows are the only copy and must be kept), a negative control
proving the identity fails when the emit filter regresses, and live-GPS
contamination (xfail: live-producer isolation is still owed under D02.9).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from src.collection.replay import Rederivability
from src.validation.harness import RunArtifacts, harness_config, run_scenario_clean
from src.validation.report import build_report
from src.validation.scenarios import ScenarioSpec, scenario

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

DECODED_SLICE = Path("tests/fixtures/replay/decoded-slice")

#: Committed fixture composition (``tools/make_replay_fixture.py`` output).
SOURCE_ROWS = {"can": 784, "decoded": 1377, "derived": 200, "gps": 304, "obd": 64}


def _spec(source: Path, **overrides: Any) -> ScenarioSpec:
    # 20 s of recording at 20x is ~1 s of emission; 4 s leaves the pass room
    # to complete on a loaded host (the identities need a complete pass).
    fields: dict[str, Any] = {
        "name": "replay-decoded-slice",
        "replay_source": str(source),
        "replay_speed": 20.0,
        "duration_s": 4.0,
        "warmup_s": 0.0,
        "known_answer": False,
    }
    fields.update(overrides)
    return dataclasses.replace(scenario("replay-drive-slice"), **fields)


async def _run(spec: ScenarioSpec, tmp_path: Path) -> tuple[RunArtifacts, dict[str, Any]]:
    config = harness_config(tmp_path / "scratch", models_enabled=False)
    artifacts = await run_scenario_clean(
        spec,
        label="test",
        sessions_root=tmp_path / "sessions",
        scratch=tmp_path / "scratch",
        config=config,
        enable_inference=False,
    )
    return artifacts, build_report(artifacts)["evidence"]["replay_accounting"]


def test_fixture_is_a_decoded_bearing_source() -> None:
    """The fixture is only useful while it carries stored decoded/derived rows
    and payload-bearing frames with an exact frame -> rows correspondence."""
    frame = pd.read_parquet(DECODED_SLICE / "telemetry-00000.parquet")
    assert frame["source"].value_counts().to_dict() == SOURCE_ROWS
    can = frame[frame["source"] == "can"]
    assert can["payload"].map(lambda p: isinstance(p, bytes) and len(p) > 0).all()


async def test_payload_bearing_replay_persists_decoded_once(tmp_path: Path) -> None:
    """D02.9 item 2 — stored decoded/derived rows are skipped at the emit path
    and decoded is re-derived exactly once from the replayed frames."""
    artifacts, acct = await _run(_spec(DECODED_SLICE), tmp_path)
    stats = artifacts.replay_emission
    assert stats is not None and stats.rederivable is True, stats
    assert stats.passes == 1, stats
    assert stats.rows_skipped == SOURCE_ROWS["decoded"] + SOURCE_ROWS["derived"]
    assert acct["pass_complete"] is True, acct
    assert acct["decoded"]["identity_holds"] is True, acct
    assert acct["decoded"]["stored_emitted"] == 0
    assert acct["decoded"]["ratio_to_expected"] == 1.0
    assert acct["rederived_decoded_rows"] > 0
    assert acct["captured"]["identity_holds"] is True, acct
    assert "derived" not in acct["persisted_rows_by_source"]


async def test_regressed_emit_filter_is_caught_as_doubling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control: with the emit filter disabled while the probe still
    says re-derivable (the pre-``c15f66a`` behaviour), the identity fails at
    exactly 2x — the instrument can see what D02.9 measured as 1.985x."""
    monkeypatch.setattr(Rederivability, "skip_sources", property(lambda self: frozenset()))
    _artifacts, acct = await _run(_spec(DECODED_SLICE), tmp_path)
    assert acct["pass_complete"] is True, acct
    assert acct["decoded"]["identity_holds"] is False, acct
    assert acct["decoded"]["ratio_to_expected"] == 2.0, acct


async def test_legacy_payloadless_replay_keeps_stored_decoded(tmp_path: Path) -> None:
    """D02.9 constraint — a recording from before raw-payload persistence
    cannot re-derive anything, so its stored decoded/derived rows are the only
    copy: all emitted, none re-derived, none doubled."""
    legacy = tmp_path / "legacy-source"
    legacy.mkdir()
    frame = pd.read_parquet(DECODED_SLICE / "telemetry-00000.parquet")
    frame["payload"] = [None] * len(frame)
    frame.to_parquet(legacy / "telemetry-00000.parquet", index=False)

    artifacts, acct = await _run(_spec(legacy), tmp_path)
    stats = artifacts.replay_emission
    assert stats is not None and stats.rederivable is False, stats
    assert "predates raw-payload persistence" in (stats.rederivable_reason or "")
    assert stats.rows_skipped == 0
    assert acct["rederived_decoded_rows"] == 0
    assert acct["decoded"]["identity_holds"] is True, acct
    assert acct["decoded"]["persisted"] == SOURCE_ROWS["decoded"]
    assert acct["persisted_rows_by_source"]["derived"] == SOURCE_ROWS["derived"]
    assert acct["captured"]["identity_holds"] is True, acct


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "D02.9 item 3 still owed: live producers (the Pi's GPS receiver) share the "
        "replay's queue and nothing at the queue/writer boundary tells a live row "
        "from a replayed one, so they persist into _replay-*"
    ),
)
async def test_live_gps_does_not_contaminate_replay_output(tmp_path: Path) -> None:
    """D02.9 item 3 — a live receiver running during the replay must not add
    rows the recording never contained."""
    artifacts, acct = await _run(_spec(DECODED_SLICE, live_gps_hz=5.0), tmp_path)
    assert artifacts.live_gps_rows_pushed > 0
    assert acct["pass_complete"] is True, acct
    assert acct["captured"]["foreign_rows"] == {}, acct


def test_accounting_is_absent_for_synthetic_scenarios(tmp_path: Path) -> None:
    """The identity is replay-only; a synthetic capture reports it as such."""
    from src.validation.evidence import replay_accounting

    class _A:
        scenario = scenario("fuel-map")
        replay_emission = None
        replay_source = None

    empty = pd.DataFrame(columns=["source", "channel", "payload"])
    assert replay_accounting(empty, _A())["status"] == "not_a_replay"  # type: ignore[arg-type]
