"""A05.8 under the operator decision of 2026-10-13: who owns which plumbing
fault, and what the committees catch of the ones they own.

The decision: the sensor committees own RPM/THROTTLE faults at idle and
stuck-at-cruise; plausibility keeps single-channel range and slew.
``scripts/evaluate_committee_coverage.py`` grades both detectors on A05.8's own
injections. These tests pin the ownership rule against the case A05.2
documented by hand, and the committee result on the committed real slice.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.plausibility.offline import Injection, SessionStream

REPO = Path(__file__).resolve().parents[1]
DRIVE_SLICE = REPO / "tests" / "fixtures" / "ground_truth" / "drive-slice"
CONFIG = REPO / "canary.example.yaml"
KINDS = ("spike", "drift", "dropout", "stuck", "scale")


@pytest.fixture(scope="module")
def context():
    from src.plausibility.benchmark import load_benchmark_context

    return load_benchmark_context(CONFIG, "nissan-xterra-2008")


def _idle_rpm_with_spike(spike: float) -> tuple[SessionStream, Injection]:
    """RPM at 700 every 1.03 s; samples 20-29 replaced by ``spike``."""
    t = 1_000.0 + 1.03 * np.arange(60)
    values = np.full(60, 700.0)
    values[20:30] = spike
    samples = pd.DataFrame({"t": t, "channel": "obd:RPM", "value": values})
    stream = SessionStream("idle", samples, np.full(60, "idle", dtype=object))
    return stream, Injection("spike", "obd:RPM", float(t[20]), float(t[30]), 10, spike)


def test_the_idle_rpm_spike_a05_2_described_is_the_committees(context) -> None:  # type: ignore[no-untyped-def]
    """A05.2's known answer: on 2026-08-07T164617, which never leaves idle,
    5 x max + 1,000 = 5,187.5 rpm sits inside 0-7,000 and its ~4,487 rpm step
    in 1.03 s inside the 4,500 rpm/s slew. No single-channel check can see it,
    so under the decision it is the committees' fault, not a plausibility miss.
    """
    from scripts.evaluate_committee_coverage import envelope_breaches, fault_owner

    limits = context.catalog.get("obd:RPM")
    stream, injection = _idle_rpm_with_spike(5_187.5)
    assert envelope_breaches(stream, injection, limits) == []
    assert fault_owner("spike", limits, [])[0] == "committee"

    # The same spike past the 7,000 rpm bound is plausibility's.
    stream, injection = _idle_rpm_with_spike(7_500.0)
    breaches = envelope_breaches(stream, injection, limits)
    assert "range" in breaches
    assert fault_owner("spike", limits, breaches)[0] == "plausibility"


def test_ownership_follows_the_decision_and_names_what_neither_owns(context) -> None:  # type: ignore[no-untyped-def]
    from scripts.evaluate_committee_coverage import fault_owner

    get = context.catalog.get
    # Stuck-at-cruise: RPM and SPEED have no stuck rule (cruise holds them).
    assert fault_owner("stuck", get("obd:RPM"), [])[0] == "committee"
    assert fault_owner("stuck", get("obd:SPEED"), [])[0] == "committee"
    # A channel with a stuck rule keeps it.
    assert fault_owner("stuck", get("obd:THROTTLE_POS"), [])[0] == "plausibility"
    # Coolant holds at warm idle and is not cruise-held: nobody's.
    assert fault_owner("stuck", get("obd:COOLANT_TEMP"), [])[0] == "unassigned"
    # Dropout belongs to freshness only where freshness runs (priority PIDs).
    assert fault_owner("dropout", get("obd:RPM"), [])[0] == "plausibility"
    owner, why = fault_owner("dropout", get("obd:SPEED"), [])
    assert owner == "unassigned" and "unchecked" in why
    # An in-envelope fault on a channel the decision did not name: nobody's.
    assert fault_owner("drift", get("obd:SPEED"), [])[0] == "unassigned"


def test_on_the_real_slice_no_committee_reads_rpm_and_speed_is_caught_in_part(
    tmp_path: Path,
) -> None:
    """Measured, not assumed. On the committed 08-03 slice (420 s, fix-health
    asserted as in ``test_committee_checks``): no committee declares
    ``obd:RPM``, so every RPM fault is ``uncovered``; the speed committee
    raises nothing on the clean slice and, on A05.8's own windows (all five
    kinds in benchmark order, seed 42), catches SPEED spike 1/5, drift 2/5,
    scale 3/5, stuck 1/5 and dropout 0/5 (Mode 01 SPEED arrives every ~2 s, so its
    departed verdicts often sit more than the 3 s ``max_verdict_gap_seconds``
    apart and the run restarts)."""
    from scripts.evaluate_committee_coverage import main

    out = tmp_path / "coverage.json"
    argv = [
        "--config",
        str(CONFIG),
        "--profile",
        "nissan-xterra-2008",
        "--sessions",
        str(DRIVE_SLICE),
        "--fault-channels",
        "obd:RPM",
        "obd:SPEED",
        "--assume-gps-fix",
        "--out",
        str(out),
    ]
    assert main(argv) == 0
    report = json.loads(out.read_text())
    (session,) = report["sessions"]
    assert session["committee_clean_flags"] == []
    rpm = [f for f in session["faults"] if f["channel"] == "obd:RPM"]
    assert rpm and all(f["committee"]["result"] == "uncovered" for f in rpm)
    assert all("no committee declares obd:RPM" in f["committee"]["reason"] for f in rpm)

    cells = report["summary"]["by_channel_kind"]
    caught = {kind: cells[f"obd:SPEED {kind}"]["committee_caught"] for kind in KINDS}
    assert caught == {"spike": 1, "drift": 2, "dropout": 0, "stuck": 1, "scale": 3}
    assert all(cells[f"obd:SPEED {kind}"]["graded"] == 5 for kind in KINDS)
    assert cells["obd:SPEED stuck"]["owner_committee"] == 5
    assert cells["obd:SPEED stuck"]["committee_caught"] == 1
    assert cells["obd:RPM stuck"]["owner_committee"] == 5
    assert cells["obd:RPM stuck"]["committee_caught"] == 0
