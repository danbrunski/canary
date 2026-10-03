# W05 — ROM Maps: trace from the ECM's own coordinates, and coverage

Roadmap tasks W7 and W8. Depends on W01, W02.

The page exists (`panels/RomMapsPanel.tsx`, `lib/romMaps.ts`,
`POST /control/rom/inspect`, viewer metadata in the vehicle profile's
`rom_viewer.yaml`). Its live trace is an *inferred geometric lookup* from RPM
and BFS — `docs/offline-rom-targets.md` says so explicitly. Since 2026-09-21
the ECM's own map coordinates are readable as memory channels, so the trace can
show the cell the ECM says it is reading.

**Channels** (`data/dids.yaml`, `memory:`): `ECM_FUEL_TARGET_MAP_X_TRACE`
(`0xFFFF103E`), `ECM_ENGINE_SPEED_RAM` (`0xFFFF11D8`, the fuel-target Y),
`ECM_CAM_MAP_RPM_TRACE` (`0xFFFF11CA`), `ECM_THROTTLE_MAP_X_TRACE` /
`_Y_TRACE` (`0xFFFF1C2C` / `0xFFFF1902`). The `rom-trace` profile polled all
of them at 8 Hz until it was retired on 2026-09-21 (its continuous session
stalled with zero requests sent); they rejoin the Xterra's `tuning` profile
once that stall is found.

## Trace

- [~] **W05.1 — Map each table to its trace channels in data, not code.** Add
  to `rom_viewer.yaml`, per table: the X and Y trace channel and how a trace
  value maps onto that table's axis breakpoints. The X trace is in axis units
  scaled ×0.250859 and its relation to BFS milliseconds is **not** established
  — settle that mapping against the loaded ROM's axis before drawing anything,
  and record how it was settled.
  *2026-09-21:* the schema landed and the Y axis is settled; the X axis is
  **not**. `data/nissan-xterra-2008/rom_viewer.yaml` → `tables[fuel_targets].trace`
  names `mem:ECM_FUEL_TARGET_MAP_X_TRACE` / `mem:ECM_ENGINE_SPEED_RAM`, each
  with a required `axis_mapping` state and `evidence`
  (`src/analysis/rom_trace.py` `AxisTrace`: `verified` requires scale+offset,
  `unverified` forbids them, so no scale can be guessed). Y is `verified`
  (stored value is rpm: equals DID 0x1201 on 2,825/3,000 reads, and UpRev reads
  the address as its own Fuel Target Y Trace; the rpm axis is decoded in rpm).
  X stays `unverified`: no local session holds the channel — the three
  2026-09-21 captures in `~/canary-data/sessions-0921{,b,c}` polled only
  `ENGINE_SPEED_RAM`/timing/phi/period, the UpRev logs under
  `data/sessions/_uprev/` carry only cam/throttle traces, and `rom-trace`
  stalled with zero requests. **Remaining:** one capture polling the X trace
  beside `did:ECM_AIR_CHARGE` (BFS) at steady points in ≥3 load columns; fit
  same-instant with `compare_channels`, check each steady point lands in the
  column its BFS names, then flip `axis_mapping: verified` with scale/offset
  and that evidence. Until then the page and the residency endpoint refuse the
  ECM cell by name. Tests: `test_rom_trace.py::test_shipped_profile_maps_the_fuel_target_trace_but_refuses_its_x_axis`,
  `::test_an_unverified_axis_cannot_carry_a_scale`, `::test_a_verified_axis_must_state_its_scale`,
  `::test_request_channel_names_the_memory_window_that_carries_the_trace`,
  `test_rom_maps_api.py::test_profile_driven_slots_and_unverified_tables`.
  *2026-10-03 correction and lead:* the note above says no UpRev log carries
  the X trace. One does:
  `~/canary-data/kline-compare-20260924/UpRev_Log9-24-2026_10-10-51_AM.csv`
  (650 rows, 2026-09-24) logs `Fuel Target X Trace` beside
  `B-FUEL SCHDL (ms)`. Their ratio has a median of 1.446 (p25–p75
  1.434–1.477, range 1.16–1.56), BFS 1.7–13.9 ms, and only 21 rows are past
  the first load column (8.76 ms). That supports a proportional X ↔ BFS
  relation, but it cannot flip `axis_mapping` on its own. UpRev reads its
  channels one after another on the K-line, so the two are not same-instant.
  The spread around 1.45 has not been separated into read skew and a real
  offset. Coverage above the first column is thin. The capture asked for
  above still decides. This log is the prior that capture should confirm or
  refute.
- [x] **W05.2 — Draw the ECM's cell.** From the live telemetry stream
  (`useTelemetryStream`), outline the cell the trace channels point at. Keep
  today's nearest-breakpoint / dashed-neighbour rendering for the *inferred*
  lookup and draw both.
  *Done in code, inert on the truck until W05.1's X axis is verified:*
  `lib/romTrace.ts` `ecmPoint`/`ecmPosition` (memory rows are read on their
  real stream key `obd:mem:<NAME>`), drawn as a double ring (`data-ecm="cell"`)
  beside the inferred outline; refused with the mapping's evidence when
  unverified. Tests: `RomMapsPanel.test.tsx` "outlines the ECM cell beside the
  inferred one…", "refuses the ECM cell while the X-trace mapping is
  unverified…"; `romTrace.test.ts` "maps a verified trace…"; e2e
  `romMaps.spec.ts` asserts the named refusal.
- [x] **W05.3 — Show the disagreement.** When the ECM's cell and the inferred
  cell differ, say so and by how much. A persistent disagreement is a finding
  — a wrong axis decode, a wrong BFS scale, or interpolation the inference does
  not model — and the point of keeping both is to see it.
  *Done (same W05.1 caveat):* `cellDisagreement`/`describeDisagreement` state
  ±rows/columns with both coordinates in axis units; a running tally ("disagreed
  on k of n ECM samples…; current disagreement for N s") makes persistence
  visible. Tests: `RomMapsPanel.test.tsx` "outlines the ECM cell…" / "says the
  cells agree when they do"; `romTrace.test.ts` "measures disagreement…".
- [x] **W05.4 — The trace is only as live as its channels.** If no trace
  channel has updated in 3 s, the trace greys out and the page says why: not
  polled by the active profile (name a profile that carries the trace
  channels and offer it on Logging), polled but not answering (per-channel
  `requests_sent`/timeouts), or no ECM data at all. Never a frozen marker that
  looks live. No shipped profile carries the trace channels on 2026-09-21: the
  `rom-trace` profile was retired after its continuous session stalled, and
  they return to `tuning` once that stall is found.
  *Done:* `traceLiveness` — 3 s staleness → greyed line naming one of
  `no_ecm_data` / `not_polled` (active profile named; a catalog profile
  carrying both channels is named with an "Open Logging" button, otherwise
  "No capture profile in this vehicle's catalog polls them" plus the
  profile-data `unavailable_note` — no nonexistent profile offered) /
  `not_answering` (per-request `requests_sent`/responses/timeouts/NRCs from
  the scheduler row for the channel's memory window). No ECM marker is drawn
  while stale. Tests: `RomMapsPanel.test.tsx` "greys out a stale trace…",
  "names the active profile…offers no nonexistent profile", "offers a catalog
  profile…on Logging", "names no-ECM-data when the key is off";
  `romTrace.test.ts` "ECM trace liveness (W05.4)".
- [x] **W05.5 — Keep the slot assertion.** The existing requirement that the
  operator confirm the selected ROM *and slot* are active in the ECU stays.
  With W02.2 the page can additionally refuse to call a trace "active" when the
  ECU's calibration id does not match the loaded ROM.
  *Done:* the confirmation gate stays; both traces are additionally refused
  when `/status.calibration_matches_registry === false` (names the ECU id, the
  registry revision and its id) or when the registry's running revision is no
  longer the ROM's recorded revision; an unread id is stated as such. Test:
  `RomMapsPanel.test.tsx` "refuses to call any trace active when the ECU
  calibration id contradicts the registry".

## Coverage

- [x] **W05.6 — Backend: per-cell residency.** New
  `GET /control/sessions/{id}/cell-residency?table=…` returning, per cell,
  sample count and dwell seconds, computed from the trace channels against the
  table's breakpoints (W05.1). Through the single-flight child; deferred while
  capturing. For a log without trace channels, return the named reason rather
  than an empty grid.
  *Done:* `src/analysis/rom_trace.py` `cell_residency` (nearest-breakpoint
  cells against the axes of the revision the log ran, X/Y paired within 250 ms
  via `match_ordered`, dwell capped at 1 s across gaps, `outside_axes`
  counted), job kind `cell_residency` in `src/control/workbench_jobs.py`, route
  in `src/dashboard/routes/workbench.py`. `available: false` + `reason` for
  `axis_mapping_unverified` (refused before spawning a child — the shipped
  state until W05.1), `not_captured` (+`missing`), `revision_unknown`,
  `axes_not_recorded`, `table_axes_unverified`. Documented in
  `docs/headless-api.md`. Tests: `test_rom_trace.py::test_residency_*`,
  `::test_cell_residency_route_runs_through_the_child`,
  `::test_cell_residency_route_refuses_unverified_mapping_without_a_child`,
  `::test_cell_residency_route_defers_while_a_capture_records`,
  `::test_cell_residency_route_names_a_log_without_trace_channels`.
- [x] **W05.7 — Coverage overlay.** Toggle on any table: cells shaded by sample
  count for the selected log (W02.5), count on hover, never-visited cells
  distinct from thinly-visited ones. This answers "did that drive reach the
  cell I edited" — the 5,200 rpm / BFS 18.20 cell of the 2026-09-20 flash is
  reachable only by a WOT pull, and nothing today says whether a log got there.
  *Done:* per-table "Coverage overlay" toggle; shaded by count
  (`data-coverage` = `unvisited` hatched grey / `thin` < 5 / `visited`
  deepening blue), count + dwell on hover, legend; names every empty (no log
  selected → "Open Logs", the endpoint's reason, deferral with laptop path,
  and a log binned on another revision's axes is refused rather than
  overlaid). Tests: `RomMapsPanel.test.tsx` "asks for a log…", "shades cells
  by sample count…", "names the reason a log has no coverage…", "refuses to
  overlay a log binned on another revision's axes".
- [x] **W05.8 — Live coverage.** The same overlay accumulating from the stream
  during an active capture, client-side only, reset when the capture ends.
  *Done (ECM-trace based, so inert until W05.1):* one count per new ECM
  sample while a capture is active; reset when the capture ends or another
  starts. Test: `RomMapsPanel.test.tsx` "accumulates live coverage during a
  capture and resets when it ends".
- [x] **W05.9 — Optional: a drive presentation.** One table full-screen with
  the trace and coverage, legible at a glance. The app already has a `driving`
  mode with its own shell (`DriveScreen.tsx`); decide whether this is a variant
  of that or a toggle on this page before building it.
  *Decision (2026-09-21): a toggle on this page.* The trace needs the loaded
  ROM, the slot assertion and the W05.5 gates, all of which live in the
  Engine Tuning group state that `DriveScreen` does not own; a variant would
  duplicate them or weaken them. "Drive presentation" opens one available
  table full-screen (larger cells) with the same trace, disagreement and
  coverage. Test: `RomMapsPanel.test.tsx` "opens and closes a drive
  presentation of one table".

## Done when

With a profile carrying the trace channels recording, the trace follows the ECM's coordinates and shows
any disagreement with the inferred cell; with nothing polling them, it greys
out and names the fix; the overlay shows which cells a selected log visited.
*Truck check:* one drive watching the fuel-target trace cross at least three
load columns. **Open** — the Pi/truck was unreachable on 2026-09-21, and the
check first needs W05.1's X mapping settled and a profile carrying the trace
channels (the `rom-trace` stall).
