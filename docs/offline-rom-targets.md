# Offline Fuel Targets editing

## Dashboard viewer

Open **Workbench → Engine Tuning → ROM Editor** (`#wb/tuning-rom-editor`), enter
the dashboard control token, and pick a stored image or open a `.OsirisROM` or
plain 1 MiB image. Opening a file adds it to the ROM library, kept by its
plain-image hash. The library accepts any valid image (a plain image of the
vehicle's exact size, or an `.OsirisROM`; UpRev's backup `.bin` and
anything else are refused by name), so a historical or experimental image can
be looked at and edited. What is gated is the live overlay: its confirmation and
its trace are available only for the image the flash history records as
running now (`matches_recorded_revision`), and any other image is view-only
there.
(The page that used to do this, **ROM Maps** at `#wb/tuning-rom-maps`, was merged
into the editor on 2026-10-26: `#wb/tuning-rom-maps` and `#wb/live-rom-maps`
still open the editor. The old upload-without-saving flow is not in the
dashboard any more; `POST /control/rom/inspect` still loads an `.OsirisROM`
in memory without storing it, for scripts.)

The editor shows every table the image's map declares but the axes, in every
map slot: Fuel Targets, Fuel Compensation, both ignition timing trims (Timing
Main trim A, Timing High Det trim B), the base timing maps and the like. A
table UpRev keeps a copy of in each map slot is one family in the table tree,
opened to pick **Map 1** to **Map 5**; a table with one copy stands alone.
**Show tables** narrows the tree to the editable tables or the view-only ones.
Which tables take an edit is the map's word (`editable: true` in
`rom_map.yaml`). Of a slotted table, Map 1 is the editable copy: edits and the
simulator cover Map 1 only, and the other slots are for looking at. A table
with one copy (every table of a stock image without map slots, such as the
2013's) is edited in place. A vehicle without a simulator still previews an
edit, without saying what it reaches. **Download spec** appears only when
every edited table is `confidence: verified`; an edit to an inferred table is
previewed but never exported. **Export to library** (beside Download spec)
saves the edited image as a new ROM Library entry: its checksum words
recomputed, written and re-verified, and its provenance (parent image, edit
spec hash, map hash, calibration id) on the entry. It downloads as a plain
`.bin` like any entry. The export is refused when an edited table is only
inferred, when the parent image's own checksum fails, or when the image's
calibration is not the one the map describes. Exporting writes nothing to the
ECU. A table
whose decode is unconfirmed is shown by its raw counts, with its confidence
and evidence named. Titles and axes follow UpRev's tuning guide pp. 9–10; the
axis labels the editor shows are labels only, and every edit and the simulator
still read the raw axis bytes.

The numeric gradient runs green (low) through yellow/orange to red (high),
with numbers, axes and a legend; it is not a safety rating. A scalar or a
table whose cells are all equal has no range to shade and is left plain.
Hover a cell for its raw count and full-precision value.

The **Live overlay** checkbox (off by default; nothing is fetched until it is
on) puts the live marks on the tables the vehicle maps a trace to (Fuel
Targets on the Xterra, in Map 1). It reads `GET /control/rom/library/{sha}/trace`,
which carries the vehicle's `rom_viewer.yaml` trace definitions and live
providers. A vehicle without one has no overlay and the editor works as before.
To trace, confirm that the selected ROM **and selected slot** are active in
the ECU. This assertion is not an ECU map-switch command or automatic slot
detection. Historical/mismatched ROMs remain view-only. The nearest RPM/BFS
breakpoint gets a solid outline/dot; surrounding cells get dashed outlines.
This is an **inferred geometric lookup**, not proof of ECU interpolation or
active cell selection. Pedal enrichment and other overrides are not modeled;
the independently read final commanded AFR is shown separately when fresh.
The lookup compares live BFS (ms) and rpm with the table's displayed axes, so
a table whose axes are not labeled in those units gets no cell and the page
says why.

Beside it the overlay draws the **ECM cell** (double ring) from the map-trace
memory channels — the ECM's own coordinates — and states any disagreement
with the inferred cell in rows/columns. Each table's trace channels and
whether their mapping onto the axes is established live in `rom_viewer.yaml`
(`trace.x/y.axis_mapping`); an `unverified` axis carries no scale and the ECM
cell is refused with its evidence. A verified axis names the breakpoints it is
compared with (`axis_units`). Since 2026-10-04 (W05.1) both fuel-target axes
are verified from the ECM program: the X byte's raw count, with its catalog
decode (raw × 0.250859) undone, lands on the ROM's raw BFS axis bytes, and the
rpm word lands on the rpm axis in rpm. The lookup clamps past either end
(`trace.out_of_range`), so there the ECM cell is the edge cell and the overlay
says so. Pedal-switch enrichment moves the ECM's read to the last load column
without moving the X trace, so neither marker shows it. The ECM trace greys out after
3 s without samples and names why: no ECM data, not polled by the active
profile (naming a catalog profile that carries the channels, if any), or
polled but not answering (per-request counters). Tracing is also refused when
`/status` reports an ECU calibration id that contradicts the flash history.
A per-table **coverage overlay** shades cells by sample count for the log
selected on Logs (`GET /control/sessions/{id}/cell-residency`) or, during a
capture, accumulates live (while it is on, shading is by sample count in place
of value); "Drive presentation" shows one table full-screen.

Tracing clears on stale/missing inputs (>2.5 s), RPM/BFS skew >750 ms,
out-of-axis points, fuel-cut sentinel, replay, or missing/stale (>15 s)
engine-running status. Channel IDs and BFS scaling come from confirmed
vehicle-scoped tuning providers, not hardcoded frontend guesses. Fuel
Compensation must not use throttle, calculated load, or BFS in place of its
unresolved Volumetric Flow Ratio input.

The library routes have no writer or flash integration.

This tool exports or edits **Map 1 Fuel Targets only** for the independently
verified ROM hashes recorded in the vehicle's AFR calibration file. It has
no hardware connection or flash commands. Fuel Compensation, K, timing,
cam tables, other slots, and other ROM builds are not yet supported.

## Export the current map

From the repository root:

```sh
.venv/bin/python scripts/rom_targets.py export \
  '/mnt/c/Users/thebo/Desktop/Uprev/Tuned ROMs/ZS44A-Tuned-20260920.OsirisROM' \
  --definition data/nissan-xterra-2008/rom_targets.yaml
```

JSON includes the source hash, revision, slot, RPM/BFS axes, raw cells,
AFR values, and conversion formula. No files are written by export.

## Propose and review a patch

A patch is JSON with `schema_version: 1`, the exported `source_sha256`,
`table: "fuel_targets"`, `map_slot: 1`, and a nonempty `changes` array.
Each change requires zero-based `row`/`column`, matching `rpm`/`bfs_ms`
axis labels, `old_raw`, `new_raw`, and a nonempty `rationale`.

The model should propose exact raw counts with explicit before/after AFR,
not silently round a requested AFR. The relation is **1881.6 / raw**; larger
counts mean richer targets. A byte-range-valid edit is not a claim that the
requested mixture is appropriate. Keep tuning evidence and calibration
judgment separate from file-format validation.

`apply` without `--output-dir` is a dry run. To create review artifacts,
specify a **new directory outside the repository**. Existing directories
are refused, inputs are never overwritten, and no proprietary ROM content
belongs in Git. Each bundle contains:

- `candidate.OsirisROM`: the complete candidate file.
- `review.json`: hashes, per-cell changes, AFRs, rationale, checksum values,
  and every changed image offset.
- `patch.json`: the exact requested patch.

Before emitting a candidate, the tool verifies the known source hash,
complete target table, both axes, checksum-style markers, the file's own
integrity checks, internal checksums, that the written file loads back to the
same image, that everything else in the file is unchanged, and absence of
changes outside requested cells/checksum words.
It rejects duplicate cells, wrong old values, wrong axes/slots, and unknown
patch fields. Vehicle-specific offsets/checksum ranges are profile data.

## Known-answer reproduction (not a new tuning recommendation)

```sh
.venv/bin/python scripts/rom_targets.py apply \
  /home/dan/Uprev/ROMs/ZS44A-Tuned.OsirisROM \
  --definition data/nissan-xterra-2008/rom_targets.yaml \
  --patch data/nissan-xterra-2008/rom-targets-known-edit.json \
  --expect-rom '/mnt/c/Users/thebo/Desktop/Uprev/Tuned ROMs/ZS44A-Tuned-20260920.OsirisROM' \
  --output-dir /tmp/canary-rom-review-example
```

The shipped patch applies **only to the old source hash** and reproduces
the operator's already-flashed 5200 RPM / BFS 18.20 edit. It is not a patch
to apply again to the current tune. Actual validation on the supplied files
produced byte-for-byte equality with the UpRev file, SHA-256
`03d00051a59b86251ac0d5d9cf2ac9651682cfcba978edce81f07371f1c128a2`.
Only image offsets `0x9598`, `0x95a0`, and `0xe025c` changed.

Reopen candidates in UpRev and compare the table against the report before
approving them. Automated GUI reopening has not been performed here; the
known-answer file is byte-identical to the file already saved/flashed by
UpRev. No additional flash is needed to validate this reproduction.

New candidate hashes are not automatically approved as future baselines.
After independent UpRev review, record any accepted/flashed revision and
its extracted table explicitly. The tool never updates the active tune as
a side effect of producing a file.

## Revision and deployment scope

The local registry now records `ZS44A-20260920` from
**2026-09-21T03:08:45.954035Z**, the last successful post-flash range CRC
response (capture frame 42353). The original `ZS44A-k106` record and target
table remain intact. Current-map selection points to the new revision.
Sessions starting earlier retain the previous inferred revision; a session
spanning the flash must be split before comparison. Explicit capture
manifest revisions still take precedence and conflicts remain visible.

These changes were deployed to the Pi on 2026-09-21 (see below). The revision
guard this paragraph used to describe — memory channels withheld until
re-verified on the new revision — was removed the same day (`6ef51b4`): RAM
addresses survive calibration flashes, and `verified_tune_revision` is now
provenance only. This tool still does not relabel RAM-address evidence.
The saved capture/daemon session also needs the correct revision when live
collection resumes; updating historical manifests is not part of this work.

## Validation performed

- 77 focused ROM/AFR/tuning-analysis/digest tests passed; the existing
  large local-corpus reference capture test was deselected after a separate
  run spent several minutes reading Parquet. Synthetic file-format and patch
  tests contain no production ROM bytes.
- Real old -> new reproduction matches the complete UpRev file;
  an in-memory new -> old inverse edit also reconstructs the original
  file byte-for-byte. The inverse was not written to disk.
- Full `mypy src` passed (291 files); touched Python files pass Ruff lint
  and format checks; `git diff --check` passed.
- The broader non-integration/non-slow run was interrupted after 3,914
  passes, one skip and three timeout failures while local-corpus work
  remained. All three timeout failures passed on a serial rerun. This is
  **not** a completed full commit gate.
- At that validation stage no commit, push, Pi deployment, GUI reopen or
  ECU flash was performed.

## Dashboard deployment — 2026-09-21

Deployed an explicit working-tree bundle and built dashboard to
`canary@192.168.137.2:/home/canary/canary`, based on commit
`5b7a77950a6126adf5623c392603ab6349995371`. This is not a committed release.
The existing installation/dependencies were retained for this incremental
code-only update; the full machine installer was not rerun. Backup of the
affected directories: `/home/canary/rom-viewer-predeploy-20260921.tar.gz`.

- 248 frontend tests passed; TypeScript checks and production build passed.
- Chromium end-to-end test passed, including file upload, colored table,
  confirmation-gated trace, five-slot navigation, and mobile rendering.
- 41 focused backend tests passed; mypy passed for 292 source files.
- Restarted `canary`; authenticated inspection of the real current ROM
  returned `ZS44A-20260920`, matching the recorded revision, five slots,
  and only the verified Map 1 Target AFR table available.
- Verified the served build `index-Bnkf4OtU.js` and resumed
  `engine_running` telemetry. No ROM was persisted on the Pi or flashed.
- Full repository commit gate remains incomplete as described above.
