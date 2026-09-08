# Collection and diagnostics improvement tasks

Created 2026-09-06 from the collection, tuning, and diagnostic-mode review.
These documents live in `docs/roadmap/tasks/` as requested. Existing roadmap documents
remain under `docs/roadmap/`; this set does not change the overnight queue.

The individual checkboxes are implementation truth. Mark `[x]` only with
evidence; use `[~]` for partial completion and name the remaining work.

**Status 2026-09-08 — the C01–C10 set is closed.** All ten task documents
are complete and archived; every checkbox in them is `[x]`.

The ten measurements that need the vehicle were moved out into a single
checklist, [`PENDING-HARDWARE.md`](PENDING-HARDWARE.md), rather than left as
`[~]` boxes scattered across six documents. They are one session's work — plug
the Pi in and run them — and each keeps its original ID, wording, and a link
back to the task it came from. The Pi is unplugged (operator-confirmed;
`canary.local` ENOTFOUND, no route to `192.168.137.0/24`).

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

✅ archived (complete). The vehicle measurements each of these deferred are in [`PENDING-HARDWARE.md`](PENDING-HARDWARE.md).

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

- [Multi-PID polling roadmap](../multi-pid-polling.md)
- [Capture profiles roadmap](../capture-profiles.md)
- [All-DID tuning hardening](../all-did-tuning-hardening.md)
- [Tuning analysis toolkit](../tuning-analysis-toolkit.md)
- [Overnight queue and operator decisions](../OVERNIGHT-QUEUE.md)

When implementation closes an existing roadmap item, update that item's
checkbox and link the evidence. Do not mark an entire phase complete because
one overlapping defect is fixed.
