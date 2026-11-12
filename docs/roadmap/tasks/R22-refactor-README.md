# R22 — Codebase review, 2026-11-12: refactor, physics and skills

The third whole-repo review, after R00–R09 (September) and R10–R21
(2026-10-14/15, both archived). It asked three things: where the code has
smells, oversized files, repeated code or avoidable cost; whether the physics
models are accurate; and whether the `engine-management` and
`vehicle-nissan-xterra-2008` skills need updating. This file is the index.

**Nothing in these files has been implemented.** Writing them is not evidence
that anything landed. This order does not modify
[OVERNIGHT-QUEUE.md](../OVERNIGHT-QUEUE.md).

## Task index

| Task | What it covers | Items | Size / risk |
|---|---|---|---|
| [R23](R23-collection-and-storage-hot-path.md) | Collection and storage: the parquet writer, corpus walks on request paths, the OBD loop, capture profiles | 15 | M–L / collection timing, the Pi's hot path |
| [R24](R24-shared-primitives.md) | The same small helper written many times, across packages: refusals, strict models, YAML loading, time, hashing, binning, guards | 10 | M / stored hashes and one output change (R24.6) |
| [R25](R25-simulation-twin-and-plant.md) | ROM-edit simulation, the twin and the plants: structure and cost | 12 | L / numerical, evidence |
| [R26](R26-tuning-workbench-signals-vehicle.md) | Tuning and workbench analysis, `signals/`, `vehicle/` | 12 | M / broad import surface |
| [R27](R27-detection-stack.md) | Models, preprocessing, the live feature path, training entries | 12 | M–L / serve path and the deploy path |
| [R28](R28-dashboard-frontend.md) | Dashboard frontend: render rate, API types, the ROM Editor tree | 9 | M / UI behavior |
| [R29](R29-daemon-control-api-mcp.md) | Daemon, control plane, HTTP routes, MCP | 16 | M–L / the Pi's memory, the write guard |
| [R30](R30-scripts-tools-tests-gates.md) | Scripts, tools, test scaffolding, the shell gates | 13 | M / the push gate |
| [M01](M01-physics-model-accuracy.md) | Physics accuracy: trip and economy, the dyno, IMU, fuel plant and pressure, λ | 20 | Deep / changes stored numbers |
| [L01](L01-skill-updates.md) | The two skills, checked against the code, the tracked documents and the running tune | 27 | Light, mostly; text only |

## Defects found on the way

A refactor review is not a bug hunt, but these turned up and should not wait
for the structural work around them. Each is one item in its file.

| Where | What |
|---|---|
| M01.1 | 0–60 mph and quarter-mile times include the time spent stationary before the launch |
| M01.2, M01.3 | Trip distance is 8 % low (uncorrected ECM speed) and fuel volume about 3.7 % low (E0 constants on E10); reported mpg ≈ 0.955 × true |
| M01.7 | The dyno's two-heading pitch offset is time-weighted; road-load `A` reads 9 % low on a ±0.5 % crest |
| R29.1 | Trip summary, `/analysis/binned` and four MCP tools run whole-session analysis in-process on the Pi with no capture deferral |
| R29.2 | A capture-stop recompute can overlap a digest or workbench child; "one analysis child at a time" does not cover it |
| R29.4 | The MCP `get_binned_analysis` tool lost its `units` parameter and the snapshot test pins the loss |
| R29.14 | The non-volatile write guard does not check broadcast rpm; a just-started engine can still read `ignition_only` for up to 5 s. Not wired to any write yet |
| R27.1 | The IF trainer accepts a blank calibration-override reason from any caller that is not the CLI |
| R30.1 | `scripts/deploy_truck.sh` restarts the daemon without the capture guard |
| R26.2 | The tuning digest reports a failed read as "no WOT pair measured" |
| R25.6 | `plant_validation`'s two verdict functions disagree on what "no signed pair" means |

## Suggested order

1. **The defects above**, smallest first: M01.1, R29.4, R30.1, R27.1, R26.2.
   Then R29.14's failing test and fix, and R29.1 / R29.2 together.
2. **R24.8** (source-scan guards) and the shrink-only guards in R30.2 and
   R30.8. They are tests, they change no code, and they stop the regrowth the
   rest of this review removes. Two earlier reviews closed items on a one-off
   grep and both counts are back.
3. **The trip path as one change**: M01.3(a), then M01.2, M01.3(b), M01.4,
   after R27.6 has extracted `TripIntegrals`.
4. **Hot-path costs, each after its measurement on the Pi**: R23.1–R23.3,
   R27.3, R27.4, R29.9, R26.1 with R26.3, R28.1.
5. **R24.3 and R24.1 with R29.3**: shared helpers with the most callers
   waiting on them.
6. **File splits** (R25.6, R26.4, R26.5, R27.7, R29.7, R29.10, R29.13,
   R30.11), each when its file is next touched or when a neighboring item
   has settled.
7. **L01** in its own order; text only and independent of the rest, except
   L01.23's last bullet, which waits on M01.

## Shared implementation and acceptance rules

These restate the R10 rules, which held up, and add three.

- **Behavior unchanged** unless a separately named defect has its own
  reproducer, expected output and deliberate commit. A file split does not
  change thresholds, defaults, pairing, request ordering or evidence
  denominators. Items that change an output say so (R24.6, most of M01).
- **Inventory the callers with `rg`** before moving anything: tests, tools,
  monkeypatch targets, lazy imports and pickled types. Keep old public paths
  as direct re-exports during the migration, with an `is`-identity test where
  R13/R14 used one. Internal modules import the owning module, never the
  compatibility facade.
- **A persisted hash or file format does not change.** Pin it with a fixture
  per caller before touching the code that produces it (R24.5, R24.10, the
  trip-summary sidecar).
- **Measure before optimizing.** Every cost quoted in these files was measured
  on the laptop, usually on synthetic input, and is a ratio, not a Pi number.
  An efficiency item starts with a measurement on the Pi (not while it
  collects) or on a stored session, recorded in the checklist, and ends with
  the same measurement.
- **Serve-path changes are replayed.** Replay a fixed session before and after
  and compare scores, per `AGENTS.md`. Anything under `scripts/ml_paths.txt`
  also runs `pytest -m ml_training` by hand.
- **A new package or module gets a layer** in `tests/test_layering.py` before
  anything imports it. Do not defer an import into a function to pass the
  test.
- **New: a regrowth guard closes the item, not a count.** Where the finding
  is "N copies of X", the acceptance is a test that fails on copy N+1 (an
  allowlist that may only shrink is fine), not "grep returns 0 today".
- **New: a constraint the review learned goes in a schema.** M01.8's air
  density and M01.14's limitation are required fields, not comments.
- **New: other sessions work in this tree.** Run `git status` before each
  item; a file dirty with someone else's work is theirs, and an item that
  needs it waits. During the review the capture-ledger work (D03.70, landed
  as `7e6fe270`) held the OBD loop, `daemon/health.py` and
  `tests/slow_tests.txt`; when implementation began, the injector-pulse and
  firmware-decode work (F01) held `analysis/injector_pulse.py` and several
  profile YAMLs. Commit with explicit pathspecs.

Dispatch tiers (`CLAUDE.md`): the source-scan guards and most of L01 are
Light with a worked prompt; file splits behind a facade are Standard; R23,
R25, R29.1–R29.2 and R29.11–R29.12 are Heavy; all of M01, R27.3–R27.5,
R27.9, R29.14 and anything touching decode or model code are Deep.

## How the review was done, and what it did not cover

A mechanical scan (file and function sizes, exact-duplicate function bodies,
class sizes) across about 586,000 lines, then nine parallel area reviews:
seven for structure and two for physics. Each reported with its evidence, and
the main claims of each were re-checked against the code (by reading the cited
function, re-running the grep, or re-running the reviewer's script) before
the task file was written. Two physics errors were reproduced by executing
the repo's functions (M01.1, M01.7).

What that does and does not establish:

- Structural claims (a copy exists, a parameter is missing, a function has no
  caller) were verified by reading or grep on 2026-11-12.
- Costs are laptop microbenchmarks. None was measured on the Pi.
- Physics sizes are derivations at stated operating points. No recorded
  session was read.
- The full test suite, the push gate and the frontend checks were not run;
  nothing was changed that they would test.
- Each file ends with a "Not reviewed" list. Absence of a finding there means
  the code was not read, not that it is clean. Spark and knock physics,
  `ecu_state_models.py`, `src/datasets/`, `src/provisioning/` and
  `src/validation/` are the largest unread areas.

## Operator questions

Collected from the files; each blocks only its own item.

1. ~~Is the tank E10?~~ Answered 2026-11-12: usually E10, whatever standard
   pump gasoline is available; 91 octane with the supercharger. Recorded in
   M01.3(b), which is now unblocked.
2. Was the AEM gauge's "0 on the face, 0.574 V out" reading taken with the
   rail fully bled? (M01.15)
3. `plant_validation`: does "no signed pair" pass or fail? (R25.6)
4. `afr_error_surface` has no caller but its tests. Keep or delete? (R26.5)
5. Does anything still use `/control/tuning-mode`, or is `/control/capture`
   the one lifecycle? (R29.7)
6. `tools/watch_uprev_logs.py`, `scripts/migrate_to_profiles.py`,
   `scripts/decode_flash.py`: keep or delete? (R30.7)
7. Where does one ECM program's model live (`ecu_state_models.py`)? (R25.12)
8. The fuel-pump change (2026-10-10..26) has no `labelled_events.yaml` entry;
   is it a `corpus_boundary`? (L01)
9. Does the Fuel Comp fit keep purge-active samples on purpose? (M01.18)
