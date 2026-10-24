"""E03.3/E03.5 over stored drives: extraction, the policy freeze, the
independent-drive report, evidence, and the candidate replay.

Two kinds of drive, kept apart:

* **Real** — ``tests/fixtures/change_detection/``: the gated bank-balance
  samples of every local session on calibration ``ZS44A-20260920``
  (2026-09-21 .. 2026-10-04), dumped by ``tools/change_detection_report.py
  --dump-fixture`` with each excluded session's reason in ``meta.json``.
  Under the declared policy these drives *refuse* a reference; that refusal
  is pinned here as the real result.
* **Synthetic** — nine drives generated below (labelled synthetic in every
  name), on which the whole pipeline (tuning, freeze, evaluation, evidence,
  replay) is exercised and its outputs pinned.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.analysis.change_evaluation import (
    CHANGE_RESIDUALS_FILE,
    ChangeReport,
    PolicyNotFrozenError,
    Preparation,
    ResidualDefinition,
    SessionSamples,
    _fit,
    assemble_drives,
    evaluate,
    freeze_policy,
    invalidating_boundaries,
    load_change_residuals,
    mint_change_report,
    prepare,
    read_session_samples,
    replay_candidate,
    samples_from_frame,
    unresolved_alarms,
)
from src.evidence.ledger import MemoryLedger
from src.evidence.records import (
    Conditions,
    Evidence,
    Intervention,
    Investigation,
    Provenance,
    Reference,
    VehicleIdentity,
)
from src.models.adaptive_candidate import InterventionBoundaryError, batch_fit
from src.models.change_detection import ReferenceRefusedError, blocks_of, calibration_fold_bias
from src.paths import DATA_DIR
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord

PROFILE = "nissan-xterra-2008"
FIXTURE = Path(__file__).parent / "fixtures" / "change_detection"
CALIBRATION = "ZS44A-20260920"
T0 = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)
DAY = 86_400.0
VEHICLE = VehicleIdentity(profile=PROFILE, vin=None, program_id=None, calibration=CALIBRATION)


@pytest.fixture(scope="module")
def definition() -> ResidualDefinition:
    residuals, _sha = load_change_residuals(DATA_DIR / PROFILE / CHANGE_RESIDUALS_FILE)
    return residuals.residual("stft_bank_balance")


@pytest.fixture(scope="module")
def residuals_sha() -> str:
    return load_change_residuals(DATA_DIR / PROFILE / CHANGE_RESIDUALS_FILE)[1]


# -- extraction ----------------------------------------------------------------


def _obd(channel: str, values: list[float], *, offset_s: float = 0.0) -> list[TelemetryRecord]:
    out = []
    for i, value in enumerate(values):
        stamp = T0 + timedelta(seconds=3.0 * i + offset_s)
        out.append(
            TelemetryRecord(
                timestamp=stamp,
                source="obd",
                channel=channel,
                value=value,
                metadata={"acquired_at": stamp.timestamp()},
            )
        )
    return out


def _session(tmp_path: Path, name: str, *, n: int = 40, drop: str | None = None) -> Path:
    status = [2.0] * n
    status[:5] = [1.0] * 5  # open loop at start
    coolant = [80.0] * n
    coolant[5:8] = [60.0] * 3  # cold blip
    ltft2 = [0.0] * n
    ltft2[20] = 1.6  # long-term trim learning: the balance is not STFT alone
    stft1 = [1.0 + 0.1 * (i % 3) for i in range(n)]
    stft1[30] = 25.0  # at the trim's authority: clamped
    channels = {
        "SHORT_FUEL_TRIM_1": stft1,
        "SHORT_FUEL_TRIM_2": [-0.5] * n,
        "LONG_FUEL_TRIM_1": [0.0] * n,
        "LONG_FUEL_TRIM_2": ltft2,
        "FUEL_STATUS": status,
        "COOLANT_TEMP": coolant,
        "RPM": [750.0] * n,
        "MAF": [4.0] * n,
    }
    records: list[TelemetryRecord] = []
    for channel, values in channels.items():
        if channel != drop:
            records += _obd(
                channel, values, offset_s=0.2 if channel == "SHORT_FUEL_TRIM_2" else 0.0
            )
    out = tmp_path / name
    write_records_to_parquet(records, out)
    return out


def test_samples_are_paired_gated_and_every_drop_counted(tmp_path, definition):
    samples = read_session_samples(
        _session(tmp_path, "2026-09-25T120000"), definition, registry=None
    )
    c = samples.counts
    assert c["paired"] == 40 and c["unpaired"] == 0
    assert c["open_loop"] == 5 and c["cold"] == 3 and c["clamped"] == 1
    assert c["ltft_adapting"] == 1
    assert c["eligible"] == 30 == len(samples.y)
    kept = [i for i in range(40) if i >= 8 and i not in (20, 30)]
    expected = np.array([1.0 + 0.1 * (i % 3) + 0.5 for i in range(40)])[kept]
    assert np.allclose(samples.y, expected)


def test_a_missing_input_is_missing_input_not_an_empty_drive(tmp_path, definition):
    samples = read_session_samples(
        _session(tmp_path, "2026-09-25T120000", drop="LONG_FUEL_TRIM_2"), definition, registry=None
    )
    assert samples.unevaluable is not None and samples.unevaluable.startswith("missing_input")
    assert "obd:LONG_FUEL_TRIM_2" in samples.unevaluable
    assert len(samples.y) == 0


def test_a_tuning_session_never_scores_the_detector(tmp_path, definition):
    samples = read_session_samples(
        _session(tmp_path, "_tuning-2026-09-25T120000"), definition, registry=None
    )
    assert samples.exclusion is not None and "default profile" in samples.exclusion
    assert len(samples.y) == 0


def _synthetic_session(
    name: str,
    start: datetime,
    calibration: str = CALIBRATION,
    *,
    n: int = 30,
    exclusion: str | None = None,
) -> SessionSamples:
    t = start.timestamp() + 3.0 * np.arange(n)
    return SessionSamples(
        session=name, first=start, last=start + timedelta(seconds=3 * n),
        calibration=calibration, exclusion=exclusion, unevaluable=None,
        t=t if exclusion is None else np.empty(0), y=np.zeros(n if exclusion is None else 0),
        condition=np.full(n if exclusion is None else 0, 4.0), counts={"eligible": n},
    )  # fmt: skip


def test_a_session_on_another_calibration_is_excluded_by_name():
    drives, excluded = assemble_drives(
        [
            _synthetic_session("2026-09-25T120000", T0),
            _synthetic_session("2026-10-05T120000", T0 + timedelta(days=10), "ZS44A-20261004"),
        ],
        calibration=CALIBRATION,
    )
    assert [d.sessions for d in drives] == [("2026-09-25T120000",)]
    assert "ZS44A-20261004" in excluded["2026-10-05T120000"]


def test_a_drive_is_named_by_its_first_eligible_piece():
    drives, excluded = assemble_drives(
        [
            _synthetic_session("_capture-tuning-2026-09-25T115900", T0 - timedelta(seconds=60),
                               exclusion="capture profile 'tuning' is not corpus-eligible"),
            _synthetic_session("2026-09-25T120000", T0),
        ],
        calibration=CALIBRATION,
    )  # fmt: skip
    assert [d.drive_id for d in drives] == ["2026-09-25T120000"]
    assert "_capture-tuning-2026-09-25T115900" in excluded


# -- real drives: the declared policy refuses them -------------------------------


@pytest.fixture(scope="module")
def real(definition, residuals_sha):
    frame = pd.read_csv(FIXTURE / "samples.csv")
    meta = json.loads((FIXTURE / "meta.json").read_text())
    drives, excluded = assemble_drives(samples_from_frame(frame, meta), calibration=CALIBRATION)
    prep = prepare(
        drives, definition, calibration=CALIBRATION, excluded_sessions=excluded,
        residuals_sha256=residuals_sha,
    )  # fmt: skip
    return drives, excluded, meta, prep


def test_real_drives_keep_every_exclusion_and_name_the_folds(real):
    drives, excluded, meta, prep = real
    used = {s for d in drives for s in d.sessions}
    assert used | set(excluded) == set(meta)
    assert len(excluded) == 36
    assert prep.folds == {
        "train": ["2026-09-21T142126", "2026-09-21T194247"],
        "calibration": ["2026-09-21T223515", "2026-09-22T163415", "2026-09-22T233855"],
        "test": ["2026-09-23T150658", "2026-10-04T190056", "2026-10-04T202737"],
    }
    assert prep.tuning_folds["test"] == ["2026-09-22T233855"]
    assert prep.unevaluable_drives == {
        "2026-10-04T215234": "uncovered: no block of the minimum length inside a declared cell"
    }


def test_real_drives_refuse_a_reference_under_the_declared_policy(real, definition):
    """The pinned real result. Inner tuning split: every candidate has 5-6
    calibration blocks per drive, too few to estimate within-drive dependence
    (the demeaned lag-1 correction needs 8). Outer folds: merged cells has
    7.4 effective idle blocks and 5.8 at part load (8 needed); four cells
    exceeds the lag-1 limit (0.74 corrected from 0.39). The bias gate is not
    reached, but measured on its own the merged-cell calibration fold sits
    +1.565 points off at part load, past half the shift of interest (1.5):
    the balance drifted inside train/calibration, and that alone would
    refuse the reference."""
    drives, _excluded, _meta, prep = real
    assert prep.reference is None
    assert prep.refusal is not None and prep.refusal.startswith("every tuning candidate")
    inner = {row.name: row.refusal or "" for row in prep.tuning}
    assert all("too few to estimate within-drive dependence" in r for r in inner.values())
    by_id = {d.drive_id: d for d in drives}
    outer = {}
    for candidate in definition.tuning.candidates:
        try:
            _fit(definition.policy_for(candidate.name), definition, by_id,
                 prep.folds["train"], prep.folds["calibration"], CALIBRATION)  # fmt: skip
        except ReferenceRefusedError as exc:
            outer[candidate.name] = str(exc)
    assert set(outer) == {"merged_cells", "four_cells", "long_blocks"}
    assert "7.4 effective" in outer["merged_cells"]
    assert "0.74" in outer["four_cells"]
    policy = definition.policy_for("merged_cells")

    def fold(name: str) -> list[Any]:
        return [
            b
            for i in prep.folds[name]
            for b in blocks_of(policy, i, by_id[i].samples(definition))[0]
        ]

    bias = calibration_fold_bias(fold("train"), fold("calibration"))
    assert bias["part_load"] == pytest.approx(1.565, abs=0.001)
    assert abs(bias["part_load"]) > policy.calibration.max_bias_fraction * 3.0


def test_real_idle_only_drives_fix_one_combination_not_each_parameter(real, definition):
    drives, _e, _m, _prep = real
    idle: list[Any] = []
    mixed: list[Any] = []
    for drive in drives:
        blocks = blocks_of(definition.policy, drive.drive_id, drive.samples(definition))[0]
        if blocks:
            (idle if {b.cell for b in blocks} == {"idle"} else mixed).append(blocks)
    assert len(idle) == 3 and mixed
    _theta_i, cov_i = batch_fit([b for bl in idle for b in bl])
    _theta_m, cov_m = batch_fit([b for bl in mixed for b in bl])
    assert np.sqrt(cov_i[1, 1]) > 5 * np.sqrt(cov_m[1, 1])


def test_the_report_tool_reports_the_real_refusal(capsys):
    from tools.change_detection_report import main

    assert main(["--from-fixture", str(FIXTURE)]) == 1
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["refusal"].startswith("every tuning candidate")


# -- synthetic drives: the pipeline end to end -----------------------------------


def synthetic_sessions(n_drives: int = 9, seed: int = 0) -> list[SessionSamples]:
    """Nine synthetic 45-minute drives a day apart: idle (4 g/s) and cruise
    (30 g/s) alternating every five minutes, balance = -1 + 2/MAF, a drive
    offset of sd 0.3 points and sample noise of sd 1 point."""
    rng = np.random.default_rng(seed)
    out = []
    for k in range(n_drives):
        start = T0 + timedelta(days=k)
        t = start.timestamp() + 3.0 * np.arange(900)
        maf = np.where((np.arange(900) // 100) % 2 == 0, 4.0, 30.0)
        y = -1.0 + 2.0 / maf + rng.normal(0.0, 0.3) + rng.normal(0.0, 1.0, 900)
        out.append(
            SessionSamples(
                session=f"2026-09-{25 + k:02d}T120000"
                if 25 + k <= 30
                else f"2026-10-{k - 5:02d}T120000",
                first=start,
                last=start + timedelta(seconds=2700),
                calibration=CALIBRATION,
                exclusion=None,
                unevaluable=None,
                t=t,
                y=y,
                condition=maf,
                counts={"eligible": 900},
            )  # fmt: skip
        )
    return out


@pytest.fixture(scope="module")
def synthetic(definition, residuals_sha):
    drives, excluded = assemble_drives(synthetic_sessions(), calibration=CALIBRATION)
    prep = prepare(
        drives, definition, calibration=CALIBRATION, excluded_sessions=excluded,
        residuals_sha256=residuals_sha,
    )  # fmt: skip
    return drives, prep


def test_test_drives_are_not_monitored_without_a_policy_freeze(tmp_path, synthetic):
    _drives, prep = synthetic
    assert prep.reference is not None, prep.refusal
    ledger = MemoryLedger(tmp_path / "memory.jsonl", vehicle=PROFILE)
    with pytest.raises(PolicyNotFrozenError):
        evaluate(prep, ledger=ledger, frozen_record="ev-nothing")
    frozen = freeze_policy(ledger, prep, vehicle=VEHICLE)
    moved = type(prep)(**{**prep.__dict__, "folds": {**prep.folds, "test": prep.folds["test"][:1]}})
    with pytest.raises(PolicyNotFrozenError, match="test_drives"):
        evaluate(moved, ledger=ledger, frozen_record=frozen)
    record = ledger.get(frozen)
    assert isinstance(record, Evidence) and record.labels["kind"] == "policy_frozen"
    assert record.policy["test_drives"] == prep.folds["test"]


def test_a_test_drive_cannot_be_refrozen_under_another_policy(tmp_path, synthetic, definition):
    """Freeze A, evaluate, change the policy, freeze B on the same drives:
    refused. Re-freezing the identical policy returns the first record."""
    from src.analysis.change_evaluation import PolicyRefreezeError

    _drives, prep = synthetic
    ledger = MemoryLedger(tmp_path / "memory.jsonl", vehicle=PROFILE)
    first = freeze_policy(ledger, prep, vehicle=VEHICLE)
    evaluate(prep, ledger=ledger, frozen_record=first)
    assert freeze_policy(ledger, prep, vehicle=VEHICLE) == first
    other = definition.policy_for("long_blocks")
    assert other.sha256() != prep.policy.sha256()
    changed = type(prep)(**{**prep.__dict__, "policy": other, "selected": "long_blocks"})
    with pytest.raises(PolicyRefreezeError, match="frozen by"):
        freeze_policy(ledger, changed, vehicle=VEHICLE)


def test_the_tool_refuses_to_monitor_test_drives_without_a_persistent_ledger(tmp_path, capsys):
    from tools.change_detection_report import main

    rows = []
    meta = {}
    for s in synthetic_sessions():
        rows.append(
            pd.DataFrame({"session": s.session, "t": s.t, "y": s.y, "condition": s.condition})
        )
        assert s.first is not None and s.last is not None
        meta[s.session] = {
            "first": s.first.isoformat(), "last": s.last.isoformat(),
            "calibration": s.calibration, "exclusion": None, "unevaluable": None,
            "counts": s.counts,
        }  # fmt: skip
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    pd.concat(rows).to_csv(fixture / "samples.csv", index=False)
    (fixture / "meta.json").write_text(json.dumps(meta))
    assert main(["--from-fixture", str(fixture)]) == 2
    assert "persistent memory ledger" in capsys.readouterr().out
    ledger = tmp_path / "memory.jsonl"
    assert main(["--from-fixture", str(fixture), "--ledger", str(ledger)]) == 0
    assert '"policy_frozen"' in ledger.read_text()


def _report(tmp_path: Path, prep: Preparation) -> tuple[MemoryLedger, ChangeReport]:
    ledger = MemoryLedger(tmp_path / "memory.jsonl", vehicle=PROFILE)
    frozen = freeze_policy(ledger, prep, vehicle=VEHICLE)
    return ledger, evaluate(prep, ledger=ledger, frozen_record=frozen)


def test_synthetic_pipeline_selects_on_train_and_calibration_only(synthetic):
    _drives, prep = synthetic
    assert prep.selected == SYNTHETIC_PINS["selected"]
    assert set(prep.tuning_folds["test"]) <= set(prep.folds["train"]) | set(
        prep.folds["calibration"]
    )
    assert not set(prep.tuning_folds["test"]) & set(prep.folds["test"])


def test_synthetic_pipeline_pins_detection_and_latency_per_drive(tmp_path, synthetic):
    _drives, prep = synthetic
    _ledger, report = _report(tmp_path, prep)
    got = {
        (run.shift, run.drive): (
            None if run.latency_s["cusum"] is None else round(run.latency_s["cusum"]),
            run.blocks_after_onset,
        )
        for run in report.synthetic
    }
    assert got == SYNTHETIC_PINS["latency"]
    assert [sum(len(r.events) for r in report.observed)] == [SYNTHETIC_PINS["observed_events"]]
    ramps = {r.shift: (r.first_drive_alarm, r.first_block_alarm) for r in report.drive_ramps}
    assert ramps == SYNTHETIC_PINS["drive_ramp"]
    interval = report.coverage_interval()
    assert (
        len(interval["per_drive"]) == 3 and interval["low"] <= interval["mean"] <= interval["high"]
    )


def test_synthetic_report_mints_labelled_evidence(tmp_path, synthetic, definition):
    _drives, prep = synthetic
    ledger, report = _report(tmp_path, prep)
    ids = mint_change_report(ledger, report, vehicle=VEHICLE)
    (ref_id,) = ids["reference"]
    reference = ledger.get(ref_id)
    assert isinstance(reference, Reference)
    assert set(reference.channels) == set(definition.channels)
    assert report.frozen_record in reference.provenance.derived_from
    for rid in ids["synthetic"]:
        record = ledger.get(rid)
        assert isinstance(record, Evidence) and record.labels["data_origin"] == "synthetic"
        assert "synthetic" in record.quality.flags
    for rid in ids["observed"]:
        record = ledger.get(rid)
        assert isinstance(record, Evidence) and record.labels["data_origin"] == "observed"
    later = datetime(2030, 1, 1, tzinfo=UTC)
    assert set(unresolved_alarms(ledger, definition.name, before=later)) == set(ids["alarms"])


def test_a_resolved_investigation_releases_its_alarm(tmp_path, synthetic, definition):
    _drives, prep = synthetic
    ledger, report = _report(tmp_path, prep)
    ids = mint_change_report(ledger, report, vehicle=VEHICLE)
    later = datetime(2030, 1, 1, tzinfo=UTC)
    for alarm in ids["alarms"]:
        ledger.append(
            Investigation(
                vehicle=VEHICLE,
                provenance=Provenance(producer="operator", producer_kind="operator", version="1"),
                conditions=Conditions(regime="all conditions", vehicle_state=None),
                valid_from=T0,
                title="Bank balance moved",
                question="Did the bank balance change?",
                status="resolved",
                observations=(alarm,),
            )
        )
    assert unresolved_alarms(ledger, definition.name, before=later) == ()


def test_the_candidate_cannot_adapt_across_a_recorded_intervention(tmp_path, synthetic, definition):
    drives, prep = synthetic
    ledger = MemoryLedger(tmp_path / "memory.jsonl", vehicle=PROFILE)
    change_at = (T0 + timedelta(days=4)).replace(hour=0)
    ledger.append(
        Intervention(
            vehicle=VEHICLE,
            provenance=Provenance(producer="operator", producer_kind="operator", version="1"),
            conditions=Conditions(regime="all conditions", vehicle_state=None),
            valid_from=change_at,
            intervention_kind="hardware",
            title="Bank one injector replaced",
            source="none",
            source_ref=None,
            affects=("obd:SHORT_FUEL_TRIM_1",),
            effective_precision="day",
        )
    )
    assert invalidating_boundaries(ledger, definition.channels) == (change_at,)
    ordered = [
        (d.drive_id, blocks_of(prep.policy, d.drive_id, d.samples(definition))[0]) for d in drives
    ]
    with pytest.raises(InterventionBoundaryError):
        replay_candidate(
            ordered, prep.reference, definition.candidate, ledger=ledger,
            calibration=CALIBRATION, channels=definition.channels,
        )  # fmt: skip


#: Pinned outputs of the synthetic pipeline (seed 0); regenerate by running
#: the module's helpers if the generator or the policy changes on purpose.
SYNTHETIC_PINS: dict[str, object] = {
    "selected": "merged_cells",
    "observed_events": 0,
    # (shift, drive) -> (CUSUM latency s, blocks after onset)
    "latency": {
        ("bank1_lean_step_3pct", "2026-10-01T120000"): (177, 40),
        ("bank1_lean_step_3pct", "2026-10-02T120000"): (177, 40),
        ("bank1_lean_step_3pct", "2026-10-03T120000"): (177, 40),
        ("bank2_lean_step_3pct", "2026-10-01T120000"): (177, 40),
        ("bank2_lean_step_3pct", "2026-10-02T120000"): (177, 40),
        ("bank2_lean_step_3pct", "2026-10-03T120000"): (177, 40),
        ("bank1_developing_4pct_15min", "2026-10-01T120000"): (657, 43),
        ("bank1_developing_4pct_15min", "2026-10-02T120000"): (777, 43),
        ("bank1_developing_4pct_15min", "2026-10-03T120000"): (657, 43),
    },
    # Over only three test drives the per-block detector sees the third
    # drive's +1.2 points before the drive level can (within-drive noise is
    # small here); the drive-level case is the kernel test's ten-drive ramp.
    "drive_ramp": {"bank1_drifting_0p4pct_per_drive": (None, 3)},
}
