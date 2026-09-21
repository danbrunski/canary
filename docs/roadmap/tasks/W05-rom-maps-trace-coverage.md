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

- [ ] **W05.1 — Map each table to its trace channels in data, not code.** Add
  to `rom_viewer.yaml`, per table: the X and Y trace channel and how a trace
  value maps onto that table's axis breakpoints. The X trace is in axis units
  scaled ×0.250859 and its relation to BFS milliseconds is **not** established
  — settle that mapping against the loaded ROM's axis before drawing anything,
  and record how it was settled.
- [ ] **W05.2 — Draw the ECM's cell.** From the live telemetry stream
  (`useTelemetryStream`), outline the cell the trace channels point at. Keep
  today's nearest-breakpoint / dashed-neighbour rendering for the *inferred*
  lookup and draw both.
- [ ] **W05.3 — Show the disagreement.** When the ECM's cell and the inferred
  cell differ, say so and by how much. A persistent disagreement is a finding
  — a wrong axis decode, a wrong BFS scale, or interpolation the inference does
  not model — and the point of keeping both is to see it.
- [ ] **W05.4 — The trace is only as live as its channels.** If no trace
  channel has updated in 3 s, the trace greys out and the page says why: not
  polled by the active profile (name a profile that carries the trace
  channels and offer it on Logging), polled but not answering (per-channel
  `requests_sent`/timeouts), or no ECM data at all. Never a frozen marker that
  looks live. No shipped profile carries the trace channels on 2026-09-21: the
  `rom-trace` profile was retired after its continuous session stalled, and
  they return to `tuning` once that stall is found.
- [ ] **W05.5 — Keep the slot assertion.** The existing requirement that the
  operator confirm the selected ROM *and slot* are active in the ECU stays.
  With W02.2 the page can additionally refuse to call a trace "active" when the
  ECU's calibration id does not match the loaded ROM.

## Coverage

- [ ] **W05.6 — Backend: per-cell residency.** New
  `GET /control/sessions/{id}/cell-residency?table=…` returning, per cell,
  sample count and dwell seconds, computed from the trace channels against the
  table's breakpoints (W05.1). Through the single-flight child; deferred while
  capturing. For a log without trace channels, return the named reason rather
  than an empty grid.
- [ ] **W05.7 — Coverage overlay.** Toggle on any table: cells shaded by sample
  count for the selected log (W02.5), count on hover, never-visited cells
  distinct from thinly-visited ones. This answers "did that drive reach the
  cell I edited" — the 5,200 rpm / BFS 18.20 cell of the 2026-09-20 flash is
  reachable only by a WOT pull, and nothing today says whether a log got there.
- [ ] **W05.8 — Live coverage.** The same overlay accumulating from the stream
  during an active capture, client-side only, reset when the capture ends.
- [ ] **W05.9 — Optional: a drive presentation.** One table full-screen with
  the trace and coverage, legible at a glance. The app already has a `driving`
  mode with its own shell (`DriveScreen.tsx`); decide whether this is a variant
  of that or a toggle on this page before building it.

## Done when

With a profile carrying the trace channels recording, the trace follows the ECM's coordinates and shows
any disagreement with the inferred cell; with nothing polling them, it greys
out and names the fix; the overlay shows which cells a selected log visited.
*Truck check:* one drive watching the fuel-target trace cross at least three
load columns.
