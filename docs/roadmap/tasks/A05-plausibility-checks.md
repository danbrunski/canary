# A05 — deterministic plausibility checks

**Depends on:** A03.1's "sample arrived" flag for the stuck and freshness
checks; A02.4 to evaluate. No retrain. Follow
[A00](A00-anomaly-detectors-README.md).

Gross sensor faults are the cheapest thing to catch, and the current
detectors miss them. In the 2026-09-20 fault-injection benchmark the
Isolation Forest caught these (injected faults per kind: 50):

| Fault | Caught |
|---|---|
| Spike | 2 |
| Scale | 4 |
| Dropout | 5 |
| Stuck | 7 |
| Drift | 8 |

In one run it never flagged an RPM spike of five times the channel maximum
plus 1,000.

An isolation tree cannot tell "slightly beyond the training range" from "far
beyond it". Its split points come from the training data, so any value past
the training maximum takes the same path as the maximum itself. A
per-channel rule catches these faults by construction and says which channel
failed.

This is the OBD-II monitor pattern (per-component checks gated on operating
conditions, confirmed on repeat), and the second brain's deterministic layer.
Build it once, as channel metadata plus one evaluator, not as per-channel
code.

## Checklist

- [ ] **A05.1 — One place for each channel's physical limits.** Limits
  already exist as data: `plausible_min`/`plausible_max` on broadcast
  bindings (`src/decode/bindings.py`) and memory channels
  (`src/analysis/memory_verification.py`), plus the encoding range of each
  Mode 01 PID. Today they are used only by analysis and verification.
  - Expose one resolver: channel → physical limits, slew limit, expected
    sample interval (from its poll tier), and the operating modes where each
    applies.
  - Keep the values in profile/catalog data; the Xterra's numbers go in its
    files, not in shared code.
  - A channel with no stated limit reports "unchecked", not "ok".
- [ ] **A05.2 — Range check.** Flag a value outside its physical limits, per
  sample, naming the channel, value and limit.
  - Check against the raw sample, before resampling or forward-fill.
  - The first run should reproduce the known answer: every injected spike and
    scale fault in the 2026-09-20 benchmark is caught.
- [ ] **A05.3 — Stuck check.** Flag a channel whose value has not changed
  across N consecutive *arrived* samples (A03.1), where the channel is
  expected to move in that operating mode.
  - Coolant at warm idle legitimately holds, and must not be flagged.
  - N and the "expected to move" modes are channel metadata.
- [ ] **A05.4 — Freshness check.** Flag a channel that has not produced a
  sample within its poll tier's expected interval × k while its source is
  otherwise answering.
  - This is distinct from a whole source going stale, which
    `vehicle_state` already reports.
  - Reuse the existing `__gap`/freshness signals, don't duplicate them.
- [ ] **A05.5 — Rate-of-change check.** Flag a step between consecutive
  arrived samples that exceeds the channel's physical slew limit, scaled by
  the real time between them. Example: coolant cannot move 20 °C in a
  second.
- [ ] **A05.6 — Enable conditions and confirmation.** Each check runs only in
  the operating modes where it is valid.
  - A single detection is *pending*; a repeat inside a configured window (or
    on the next drive, for slow checks) *confirms*.
  - Only a confirmed detection alerts, the same way OBD-II requires a repeat
    detection before lighting the malfunction indicator lamp (MIL).
  - Both states are stored.
- [ ] **A05.7 — Emit evidence, not just alerts.** Every detection writes a
  record naming the channel, check, value, limit, operating mode and sample
  times. This is the shape the second brain's evidence store will use
  ([ECU second brain](../ecu-second-brain.md), capability 2). Dashboard text
  is rendered from the record.
- [ ] **A05.8 — Evaluate on A02's harness.** Required results:
  - 100 % of the plumbing faults (spike, scale, stuck, dropout, drift at
    today's magnitudes);
  - zero confirmed alerts on clean held-out drives;
  - the recall-versus-severity curve on A02.6's realistic faults, reported
    honestly, since range checks will miss subtle faults by design.
- [ ] **A05.9 — Run it live and measure the cost.** Wire the evaluator into
  the live tick (or the collector's decode path for raw-sample checks).
  Measure per-tick cost on the Pi against the missed-tick budget before
  enabling it on a drive (Pi item).

## Acceptance

A05.1–A05.8 are desk work over stored sessions and replay. A05.9 needs the
Pi. Sensor committees (`src/analysis/committees.py`) remain the cross-sensor
check, and nothing here replaces them. A committee disagreement and a
plausibility failure on the same channel are two pieces of evidence, not one.
