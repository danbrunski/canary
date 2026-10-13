# Implementation task index

**Status 2026-10-13: fourteen finished checklists archived.** D05, P01,
R01–R04, R06–R09, S03, W05 and W08 had every box ticked; they moved to
[`archive/`](archive/) with their links. W00 went with them, since every W file
is now archived. T01 stays here although it is fully ticked: `scripts/ci_gate.sh`
and AGENTS.md send "FLAKY UNDER LOAD" reports to it, so it is a standing
receptacle, not a finished plan. R00 stays until R05.16 closes.

**Status 2026-10-04: E07, coupled engine/ECU simulator.**
[`E07-engine-plant-simulator.md`](E07-engine-plant-simulator.md) plans an
engine physics model connected to the ROM-backed ECU twin, validated with
logged observations and held-out tuning interventions. It separates whole-ROM
loading from effect coverage, starts with conditional fuel/lambda response,
and adds dynamic air/torque only as measurements qualify them. Coupled runs
and parameter fitting use bounded Pi jobs after collection stops.

**Status 2026-10-04: E06, optional Rust twin-core trial.**
[`E06-rust-twin-core-trial.md`](E06-rust-twin-core-trial.md) defines the
computation boundary, preserves the Python reference, and requires exact
numerical/state parity plus a complete Python-to-Rust benchmark on the Pi.
Adoption includes packaging and maintenance costs; the required second-brain
milestones can proceed in Python while the trial is evaluated.

**Status 2026-10-04: B08, firmware-informed behavior models.**
[`B08-firmware-informed-behavior.md`](B08-firmware-informed-behavior.md)
connects the decoded throttle-fill path, corrected Fuel Comp/timing axes and
ECU state to behavior-model candidates and the E-series second brain. It
adds artifact-lineage checks, independent-drive comparisons, the October 4
Fuel Comp intervention evaluation and Pi publication criteria. Findings are
recorded; model improvements and intervention outcomes remain to be measured.

**Status 2026-10-04: E01–E05, second brain and ECU digital twin.**
[`E00-second-brain-README.md`](E00-second-brain-README.md) indexes the
implementation checklists for evidence/memory, the first target-φ twin,
deterministic investigations/learning, Pi runtime and acceptance, and broader
reproduction of decoded ECU application logic. The Pi is the required host
for prediction, verification, reasoning, persistence and post-collection
evaluation; a phone dashboard is sufficient and a laptop is optional for
development. The application-code work carries forward the October 4
firmware findings and expands qualified outputs incrementally. All new
implementation work starts unchecked; the overnight queue is unchanged.

**Status 2026-09-27: R09, package layering.**
[`R09-package-layering.md`](archive/R09-package-layering.md) gives `src/` one import
direction. Nineteen pairs of packages import each other, up from 3 in May and
13 two weeks ago. They trace to four misplacements, not nineteen:

- runtime building blocks kept in `analysis/`;
- session storage kept in `control/`;
- feature assembly mixed into `preprocessing/`;
- column constants kept in `preprocessing/`.

R09.1 turns R01.8's `tests/test_layering.py` into a layer order and records
today's 84 upward edges and 3 pairs in `OWED`, which stops the growth
immediately. R09.2–R09.8 each move one group down and delete their entries.
The operator adopted the strict order on 2026-09-27, replacing R01.8's
named-edge rule, and R09.1 landed the same day. R09.2–R09.5 landed the same
evening, each verified separately, and took `OWED` from 84 to 15. Each was a
pure move with an identical 900 s replay. The remaining work is R09.6 (the
`behavior`/`features` split), R09.7, R09.8 and the R09.9 close-out. The
overnight queue is unchanged.

**Status 2026-09-26: P01 and P02, pipeline memory and time.**
[`P01-derived-session-frames.md`](archive/P01-derived-session-frames.md) took the
training and evaluation chain from 12–15 GB per long session to about 1.3 GB.
It does this by reducing sessions before the pivot and storing each 1 Hz frame
once. [`P02-raw-reads-evaluation-and-pi-costs.md`](P02-raw-reads-evaluation-and-pi-costs.md)
lists what remains:
- readers that still load whole sessions: the digest on the Pi,
  `read_channels`;
- the evaluation harness's own compute;
- the Pi's write path and tick.

P02.1–P02.6 are desk items; P02.7–P02.10 need the Pi.

**Status 2026-09-24: B02 and W02 archived.** Every box in both is ticked;
they moved to [`archive/`](archive/), and the links in B00, W00 and
`../workbench-workflows.md` point there.

**Status 2026-09-24: R01–R08, the second refactor review.**
[`R00-refactor-README.md`](R00-refactor-README.md) indexes eight checklists
from a six-area review of the ~125k lines added since the July refactor
([`../archive/refactoring.md`](../archive/refactoring.md)). The biggest items
are the 1,640-line `register_control_routes`, the 727-line
`run_can_obd_loop`, the 3,137-line `obd_scheduler.py`, and a dashboard whose
new pages bypass its shared helpers.

It also turned up over thirty places where duplicated copies now disagree.
The serve-path ones come first:

- the inference gate's vehicle-state classifier lacks the broadcast-RPM
  fallback that `/status` has;
- an slcan parse error during a discovery probe is re-raised as a config
  error;
- session classification computes in-process, unbounded;
- flashes recorded from the dashboard are invisible to the digest and MCP.

R00 separates the bugs it confirmed from those only reported, lists the
operator decisions, and orders the work. The overnight queue is unchanged.

**Status 2026-09-23 — H01–H07: CANary-HAT integration.**
[`H00-hat-integration-README.md`](H00-hat-integration-README.md) indexes seven
checklists that take the HAT from "passed checkout" to "recorded, checked and
used": the source policy that keeps external sensors out of the detectors
(operator decision), the IMU, 12 V/PPS/GNSS platform work, committees that
flag, a native K-line collector, live sniffing of UpRev and scan tools, and a
driving dyno. H00 records the operator's decisions and the defaults awaiting
confirmation.

**Status 2026-09-23 — T01: test-suite reliability.**
[`T01-test-suite-reliability.md`](T01-test-suite-reliability.md) opened when the
new pre-push gate first ran: `tests/test_daemon.py` gives a real daemon 1–5 s
of wall clock and fails under another session's load, then passes alone.

**Status 2026-09-23 — N01: ROM-response experiments before the supercharger.**
[`N01-na-rom-response-experiments.md`](N01-na-rom-response-experiments.md) is
the operator's goal of learning how the NA engine responds to ROM edits while
it is still NA. It records the same-ROM noise floor for STFT (±1.6 points per
cruise cell), then two flashes: a −4 % Fuel Compensation block over cruise,
then a spark-trim retard over the same block. The operator flashes; CANary
freezes predictions and measures. It is Phase 1 item NA9 in
[`../supercharger-build.md`](../supercharger-build.md).

**Status 2026-09-22 — anomaly detector checklists (A-series).**
[`A00-anomaly-detectors-README.md`](A00-anomaly-detectors-README.md) indexes
A01–A06, written from the
[anomaly detector review](../../../data/analyses/2026-09-22-anomaly-detector-review.md).
The review found four things:

- serve-time defects in the sequence detector (A01);
- no evaluation that measures fault detection on unseen drives (A02);
- an Isolation Forest that cannot see a single-quantity fault of any size
  (A04);
- sequence thresholds resting on one session, which flag 38–75 % of windows
  on unseen drives.

A05 adds deterministic plausibility checks. A06 holds the operator's
decisions on what the two detectors are for. The existing overnight queue is
unchanged.

**Status 2026-09-22 — two new checklists: cold start and the Pi updater.**
[`S01-cold-start.md`](S01-cold-start.md) picks up after `04b6cd0` and
`a870887` took the daemon's first record from 93 s to 69 s after the unit
starts on a cold boot: startup timing in `/status`, torch and the other heavy
imports off the path to recording, a shorter VIN probe against a sleeping ECU,
a cheaper retention tick, a key-on check, and the operator's NVMe decision.
[`S02-pi-updater.md`](S02-pi-updater.md) covers `canary-update`: tests first,
re-exec after self-update, skipping unchanged steps, no gratuitous gpsd/chrony
restarts, rollback on a failed health check, and a summary with exit codes.

**Status 2026-09-22 — six checklists finished and archived.** W01, W03, W04,
W06, W07 and B01 have every box ticked and moved to [`archive/`](archive/).
Still live: W02 and W05 (one truck item each), B02–B07 (B02.4 needs
independent drives; the B03 refit/replay/deploy and the IF + sequence retrain
are deferred by the operator, 2026-09-22), D03 and D04.

**Status 2026-09-21 — behavior-model accuracy and consolidation checklists.**
[`B00-behavior-models-README.md`](B00-behavior-models-README.md) indexes B01–B07:
fit/serve correctness, trustworthy validation, model retirement/refitting,
timing and airflow, trim/cam/TPS relationships, lambda delivery error, and
warmup models. These implement the
[behavior-model review](../../../data/analyses/2026-09-21-behavior-model-review.md).
B00 records dependencies and the overlap with D03's relabel/retrain and
altitude work. The existing overnight queue is unchanged.

**Status 2026-09-21 — D03 gained four items carried out of archived roadmaps.**
D03.23–D03.26 are the live remainders of `refactoring`, `multi-pid-polling`,
`ml-correctness-hardening` and `eval-longitudinal-and-dtc`, which moved to
[`../archive/`](../archive/README.md) once those remainders had a home here.
D03.25 is worth reading before any retrain: it is the same debt as D02.5 and
D03.22, and a fourth reason for it arrived this week.

**Status 2026-09-21 — a second live set: the workbench checklists.**
[`W00-workbench-README.md`](archive/W00-workbench-README.md) indexes seven files
(W01–W07) covering the dashboard regroup and the Engine Tuning pages planned in
[`../workbench-workflows.md`](../workbench-workflows.md). They are independent
of D03, which remains the live checklist for collection and diagnostics. W07
(flash interlocks) has no dependency on the rest and must land before anything
that can program a module exists.

**Status 2026-09-18 — performance fixes and tomorrow's drive are queued.**
[`D03-open-work.md`](D03-open-work.md) remains the only live checklist. Its
[September 18 drive order](D03-open-work.md#suggested-order-for-the-2026-09-18-drive)
prioritizes deployed-fix verification, a bracketed receive-loss measurement,
and 0x1134 paired with commanded AFR. **September 18 is CAN-only:** D03.14–17
add calculated-load/BFS reconstruction, a discriminating DID search, known
Mode 21 block tests and a controlled CAN memory-read experiment. UpRev/K-line
reference work is deferred. Secondary DID candidates follow if time permits.
Preparation, data needed, acceptance evidence and desk-only work are specified
in each task.

The digest now reduces projected long-form telemetry before pivoting, shares
one reduced frame between classification and fingerprinting, and routes HTTP
requests through the bounded single-flight child. Live status uses in-memory
telemetry, idle dashboard decoding is skipped, and capture-quality workers are
single-flighted/serialized. D03.6 remains partial until the real large session
is remeasured and shutdown cancellation is fixed; D03.13 remains partial only
for the UPS heartbeat. The latest Pi deployment is to be verified, not inferred
from the local checkout. No new hardware measurements were made in this review.

**Status 2026-09-11 — the C0x, D01 and D02 sets were closed and archived.**
Everything that was open on 2026-09-09 has either landed, been decided with a
measurement behind it, or moved to [`D03-open-work.md`](D03-open-work.md),
which became the only live checklist here. Eight items remained: three needed
the truck, one needed an operator decision on the retrain corpus, and four
could be done at a desk.

What closed in the two days between:

| set | outcome |
|---|---|
| **PENDING-HARDWARE** (H0–H9) | all ten measured across two drives; only H6 carried forward, and its premise changed |
| **D01** (daemon throughput) | Phase 3 closed on hardware; D01.6 resolved, D01.8 shipped and measured, D01.9 implemented, D01.7 decided |
| **D02** (drive findings) | six of ten closed; the replay that closed D02.6 opened D02.9 and D02.10 |

The two results that outrank the checkboxes:

- **The request-rate ceiling was never the bus.** An 8 → 16 → 32 req/s ladder
  could not beat ~7.9 req/s at idle on 2026-09-09 and that read as a hardware
  limit. It was the starved event loop. After D01.8 the same daemon sustained
  **116.8 req/s** on the truck with 41 timeouts in 167,885 requests.
- **A binding retraction is a feature-spec change.** The Isolation Forest went
  blind because two broadcast bindings were *corrected* out of
  `data/bindings.yaml` three weeks after it was trained on them. Fixing a
  wrong binding silently blinded the detector that depended on it, and nothing
  connected the two. Still owed: the retrain (D02.5).

Archived documents keep their evidence and their history; read them for the
reasoning behind anything in D03, not as live work.

## History

Created 2026-09-06 from the collection, tuning, and diagnostic-mode review.
These documents live in `docs/roadmap/tasks/` as requested. Existing roadmap documents
remain under `docs/roadmap/`; this set does not change the overnight queue.

The individual checkboxes are implementation truth. Mark `[x]` only with
evidence; use `[~]` for partial completion and name the remaining work.

**Status 2026-09-08 — the C01–C10 set is closed.** All ten task documents
are complete and archived; every checkbox in them is `[x]`.

**Status 2026-09-10 — all ten hardware items are measured; the C0x vehicle
set is closed.** Two drives on 2026-09-10 closed H1, H7 and H9 (`default`
profile) and then H8 (`tuning-did`). H6 stays `[~]`: its budget ladder ran at
idle, and its conclusion that the daemon was stuck near 7.9 req/s was a
property of the starved event loop — the same daemon sustained **116.8 req/s**
on the H8 drive once D01.8 shipped. Evidence:
[`2026-09-10-drive-h1-h7-h9.md`](../../../data/analyses/2026-09-10-drive-h1-h7-h9.md)
and
[`2026-09-10-h8-continuous-did.md`](../../../data/analyses/2026-09-10-h8-continuous-did.md).

**Status 2026-09-09 — six of the ten hardware items are measured.** The Pi
was plugged in and the engine run at warm idle: H0, H2, H3, H4 and H5 are
`[x]`, H6 is `[~]` (idle ladder done, drive protocol still owed), and H1, H7,
H8, H9 need a 20–30 minute drive the operator deferred that day. Evidence:
[`data/analyses/2026-09-09-pending-hardware.md`](../../../data/analyses/2026-09-09-pending-hardware.md).

Two results outrank the checkboxes. The 4 Hz hybrid claim is **denied** — the
cycle is infeasible by plan (0.294 s needed against 0.250 s) and measures
0.510 s — so `tuning-hybrid-research` stays experimental. And the deployed
daemon **cannot hold its 1 Hz inference tick on the Pi**: `total` median
863 ms against a 1000 ms period, 48 % of ticks missed, event-loop stalls that
left a concurrent `fuel-map` capture with 0 of 468 complete tuples and ~100 s
gaps in every channel. The bus was never the constraint — request throughput
is flat at ~7.9 req/s across a 4× budget ladder while the ECM answers 97.6 %.

The ten measurements that need the vehicle were moved out into a single
checklist, [`PENDING-HARDWARE.md`](archive/PENDING-HARDWARE.md), rather than left as
`[~]` boxes scattered across six documents. They are one session's work — plug
the Pi in and run them — and each keeps its original ID, wording, and a link
back to the task it came from. The Pi was reconnected on 2026-09-09 and six of the ten
were measured; see the status note above.

Four of those ten had a comparison that did not need hardware, and it was
measured; `PENDING-HARDWARE.md` records what each showed so it is not
re-derived:

| item | measured off-vehicle | what the truck adds |
|---|---|---|
| C04.12 → H3 | cycle policy meets 4.0 Hz on every DID; interval misses by 26 % at 0.266 utilisation | 4 Hz achieved *and persisted* against the real ECM |
| C06.12 → H4 | continuity across an adapter fault: 0 sequences missing both sides | kernel/socket receive counters |
| C08.12 → H5 | 1 Hz vs 2 Hz with the real IF: 0 missed deadlines, +8–10 points of one core | Pi CPU, memory, thermal, event-loop delay |
| C10.12 → H6 | budget ladder 4→32 req/s: holds from 8 up, bus never the constraint | the same ladder on the truck |

The other six (C05.11, C09.12, C09.14, C09.16, C10.10, C10.11) ask what the
*vehicle* does; a scripted ECU cannot answer them at all.

## D01 — daemon throughput (opened 2026-09-09)

The H0–H6 measurements found one cause behind most of the symptoms: the daemon
is a single GIL-bound process doing more per tick than a tick affords. That
work is tracked separately in [`D01-daemon-throughput.md`](archive/D01-daemon-throughput.md),
not here — the C0x set is about collection correctness, this is about the Pi's
capacity to run it. D01.1/D01.2 (out-of-process scoring) are implemented and
**validated on the truck 2026-09-09** (D01.3): `compute` fell 17 % with the
scoring in its own process, but `compute_busy` misses did not — `run_tick`
serialises parent and child, so the overlap that would pay for the change
never happens. D01.9 (pipelining) is the missing half.

## D02 — what the 2026-09-10 drive found (opened 2026-09-10)

The first sustained drive on the truck closed H1, H7 and H9 and gave D01 its
post-scoring baseline (the tick is now inside budget: 659.7 ms median, 2.14 %
missed). What it also found is tracked in
[`D02-drive-findings.md`](archive/D02-drive-findings.md): batched-poll counters that
don't reconcile on a failed batch, Mode 22 batching switched off for want of
four response widths, MCP2515 controller overflow underneath the socket loss,
a child-scorer fallback that stalled a minute without being counted, and an
Isolation Forest that scored an all-missing vector for the entire drive.
Evidence:
[`data/analyses/2026-09-10-drive-h1-h7-h9.md`](../../../data/analyses/2026-09-10-drive-h1-h7-h9.md).

## Scope and evidence

The review inspected the current collection, scheduling, profile, storage,
and analysis paths. Four local reproductions established:

- Independently averaging two signals before comparison can report a stable
  1:1 relationship despite disagreement at every acquired sample.
- Eight readings acquired across 875 ms can receive one logical round stamp
  when the scheduler remains busy.
- A five-minute capture with no samples for its first four minutes can pass
  the stop-time quality evaluator because only the last minute is checked.
- The live-pipeline benchmark can report approximately 27,600 Hz while
  producing zero feature rows, because observations and ticks use different
  timestamp epochs.

These are regression cases, not measurements of the truck's current behavior.
The Pi was unreachable. A targeted run of collector, capture-quality,
ground-truth, and tuning-frame tests passed 101 tests; it did not cover these
new counterexamples. A broader run stalled in API tests and was interrupted.
No live rates, hardware capacity, or model accuracy were established.

## Task map

| ID | Task | Review coverage | Dependencies |
|---|---|---|---|
| C01 | [Acquisition-time comparison](archive/01-acquisition-time-comparison.md) ✅ | Finding 1: aggregate pairing and ignored acquisition time | Can start immediately; coordinate timestamp contract with C02 |
| C02 | [Explicit capture rounds](archive/02-explicit-capture-rounds.md) ✅ | Finding 2: unbounded round timestamps; fuel-map tuple scheduling | Can start immediately |
| C03 | [Capture-wide quality accounting](archive/03-capture-quality-accounting.md) ✅ | Finding 3: trailing-window verdict, cumulative losses, false exactness | C01 pairing and C02 round contracts for final tuple validation |
| C04 | [Hybrid diagnostic scheduling](archive/04-hybrid-diagnostic-scheduling.md) ✅ | Finding 4: transition waits and end-anchored cadence | Coordinate with C02 and C06 on the collector loop |
| C05 | [Trustworthy pipeline benchmark](archive/05-pipeline-benchmark.md) ✅ | Finding 5: stale-data benchmark and invalid capacity claims | Can start immediately; precedes optimization claims |
| C06 | [Lossless diagnostic discovery](archive/06-diagnostic-discovery.md) ✅ | Finding 6: untapped frames, discovery budget/session ownership | Can start immediately; coordinate with C04 |
| C07 | [Passive receive and queue handoff](archive/07-passive-receive-and-queueing.md) ✅ | Finding 7: blocking passive receive; collection overhead | C05 for performance evidence; C03 for loss reporting |
| C08 | [Inference scheduling and isolation](archive/08-inference-scheduling.md) ✅ | Finding 7: synchronous inference and cadence drift | C05 baseline; coordinate with C07 on event-loop measurements |
| C09 | [Polling plans and profile capacity](archive/09-polling-plans-and-profile-capacity.md) ✅ | Mode-specific recommendations: batching, priorities, DID weights, accurate demand | C02, C04, C06; C03 for delivery acceptance |
| C10 | [Sustained end-to-end validation](archive/10-sustained-validation.md) ✅ | Pi benchmarks and release evidence for all modes | Baseline after C05; final validation after relevant fixes |

✅ archived (complete). The vehicle measurements each of these deferred are in [`PENDING-HARDWARE.md`](archive/PENDING-HARDWARE.md).

Implementation ran in that order: C05 baseline repair; C01 and C02
correctness; C03 accounting; C04 and C06 diagnostic scheduling; C07 and C08
event-loop work; C09 profile optimization; C10 final validation. What remains
is one Pi session, not a sequence — the ten hardware items above are
independent of each other and each names its own procedure.

## Shared implementation rules

- Keep vehicle-specific addresses, session behavior, supported channels,
  measured payload widths, rates, and timing evidence in profile/config/data.
- Reuse existing Mode 01 batching and the existing storage batches. DID batch
  builders, splitters, and client/scan support also exist; only the active
  scheduler lacks integration. Do not implement a duplicate transport.
- Preserve raw acquisition evidence. A logical round, a successful response,
  a decoded channel, a persisted row, and a complete tuple are distinct units.
- Report unavailable inputs separately from insufficient coverage. Preserve
  tune-revision uncertainty and the current advisory arming behavior.
- Use the shared session resolver and `_` storage isolation conventions.
  Do not rewrite or delete historic sessions as a migration shortcut.
- Sweep callers when a signature or persisted schema changes. Update CLI,
  HTTP, MCP, dashboard types, and sidecar readers where affected.
- Work on `main`; preserve unrelated dirty files. Before any implementation
  commit run the whole-repo gate with the project virtual environment:
  `.venv/bin/ruff check .`, `.venv/bin/ruff format --check .`,
  `.venv/bin/mypy src`, and `.venv/bin/python -m pytest`. Dashboard changes
  additionally require `pnpm test` and `pnpm typecheck` in
  `src/dashboard/static`; rerun `TuningFeedback.test.ts` once on a flake.
- During iteration, run relevant tests with
  `.venv/bin/python -m pytest -m "not integration and not slow"` and selected
  paths; use `-n0` when debugging one test. Do not wrap API tests in GNU
  `timeout`. Commit only explicit paths belonging to the task.
- Hardware-dependent checkboxes remain incomplete until measured. Read
  `/status.vehicle_state` first. Replay a known-good session for controlled
  detector validation; use real capture for bus and receive-path validation.
  Replay cannot establish hardware throughput.

## Related existing work

- [Multi-PID polling roadmap](../archive/multi-pid-polling.md)
- [Capture profiles roadmap](../capture-profiles.md)
- [All-DID tuning hardening](../all-did-tuning-hardening.md)
- [Tuning analysis toolkit](../archive/tuning-analysis-toolkit.md)
- [Overnight queue and operator decisions](../OVERNIGHT-QUEUE.md)

When implementation closes an existing roadmap item, update that item's
checkbox and link the evidence. Do not mark an entire phase complete because
one overlapping defect is fixed.
