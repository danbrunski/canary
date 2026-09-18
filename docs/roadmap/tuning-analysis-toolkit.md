# Roadmap — tuning analysis toolkit: encode the gates, kill the curl

Status: **complete** (2026-08-05) — all 18 tasks across 5 phases. Straight to `main`, no feature branches.
Not yet queued in `OVERNIGHT-QUEUE.md` — operator decides ordering. Every
phase here is software-only (no truck needed) unless a task says otherwise.

## Why this exists

The 2026-08-03..05 tuning campaign produced real findings — and **four
retractions in three days**, every one caused by re-deriving analysis ad hoc
in scratchpad scripts instead of through code that enforces what the project
already knows:

| retraction | gate that would have caught it | where the gate lives today |
|---|---|---|
| "K responds 1:1" (`0ee0876`) | band-average over a *common* rpm window | prose in `engine-tuning-support.md` 3.3 |
| "fuelling unchanged +3.13→+3.10%" | open-loop filter (`TARGET AFR < 13.5`) | prose, same doc |
| "spark fell 0.7–1.6° in edited cells" (`0ee0876`) | never grid on `CAL/LD`; quote the noise floor | prose + memory |
| "UpRev streams via 0xAC+0x23" (`724c314`) | (not an analysis gate — read the replies) | — |

Meanwhile a same-tune null run measured the **noise floor** (per cell:
±1.5 % delivery error, ±1.25° spark) and nothing consumes it; the λ/φ
convention trap in PID 0x44 cost a −27 % phantom error (`78592f4`); and
three storage conventions for the same concept (raw `value_number`, hex
`value_text`, `payload` blobs) each bit at least once — decoded `ECM_RPM`
reads ×8 *by design*, DID values hide in `value_text`.

Separately, the operator workflow is curl-and-token gymnastics: arming
tonight's capture took four hand-typed HTTP calls (one rejected on a TTL
bound), the status endpoint reports a *lifetime-average* rate and a
filter-blind `active_requests` (both actively misled us, see `4c6a2a6`),
and `vehicle_state` reads `no_data` for the entire duration of every tuning
capture.

**Duplication warning, recorded so it stops happening:** the MCP server
already ships `start_job(kind="did_sweep")`, `diff_did_snapshots`,
`get_binned_analysis`, `get_session_timeseries`, and
`get_behavior_deviations --against`. All of these were re-implemented by
hand on 2026-08-04/05 because the MCP session was dead and nobody checked.
When the `canary` MCP connection dies, restart the client and reconnect —
falling back to scratchpad is how tools get written twice
(see `docs/mcp-server.md` for the full tool list; CLAUDE.md documents the
session-death quirk).

## Guiding constraints

- **Gates are code, not discipline.** Anything a function can refuse to do
  wrong (grid on load, pool closed-loop samples, compare across unequal rpm
  coverage) must be refused by the function, with a message naming the gate
  and the incident that created it. Callers who genuinely need the unsafe
  form get an explicit `unsafe_` escape hatch, not a default.
- **The noise floor is data, not lore.** Significance claims quote it or
  don't ship.
- **Vehicle-general.** Gates parameterize per profile (stoich AFR, rpm
  windows, the φ/λ verdict) — Xterra facts live in the vehicle profile's
  data files, never in `src/`.
- **Don't rebuild what exists.** Extend `query_telemetry` /
  `get_binned_analysis` / the digest hook rather than shipping parallel
  paths.
- Whole-repo gate before every commit; commit with explicit pathspecs
  (shared working tree).

## Phase 1 — Engineering-units reader (foundation; everything else consumes it)

- [x] **1.1 — One accessor over the three storage conventions.**
  `src/analysis/channels.py`: `read_channels(session_glob, specs) ->
  DataFrame` where a spec is `source:CHANNEL` (`obd:RPM`, `did:0x1225` /
  `did:ECM_AF_SENSOR_V_B1`, `decoded:ECM_RPM`, `can:0x1F9`). Returns
  engineering units: applies `bindings.yaml` / `dids.yaml` scale+offset
  (`apply_scaling` exists — route through it), parses DID `value_text` hex,
  decodes raw `payload` bytes for bound broadcast signals. Columns carry
  units. The ×8 `ECM_RPM` and hex-in-`value_text` traps die here.
  Acceptance: `decoded:ECM_RPM` agrees with `obd:RPM` (ratio ≈ 1.00) on a
  stored session; `can:0x1F9` byte-slicing agrees with `decoded:ECM_RPM`.
  Suggested scope cut: raw `can:` reads decode *bound* signals only —
  unbound payload archaeology stays in DuckDB.
  **DONE 2026-08-05** — `src/analysis/channels.py`. Verified on a stored drive:
  `decoded:ECM_RPM` vs `obd:RPM` ratio 1.0082 (the residual is sub-second
  sampling offset during a drive), `can:ECM_RPM` matches `decoded:ECM_RPM` to
  0.000 rpm. Deviation from the sketch: `can:` specs name a *signal*, not a
  CAN id — one frame carries several, so `can:0x1F9` could not say which.
- [x] **1.2 — Same-instant pairing.** `read_aligned(session, specs,
  grid="1s", require_all=True)` — the stale-join guard (G.4) as the
  default: a row exists only where *every* requested channel has a fresh
  sample in the grid cell. `require_all=False` keeps NaNs; nothing ever
  forward-fills silently. Any `max_staleness` tolerance is explicit opt-in
  and carries per-cell age columns.
  **DONE 2026-08-05** — `read_aligned`; `max_staleness` emits `<spec>__age_s`
  so a carried-forward value always shows its age.
- [x] **1.3 — Convention registry for PID 0x44.** The decoder deliberately
  stores the raw ratio (`78592f4`). Add per-vehicle-profile metadata —
  `afr_calibration.yaml` is the natural home:
  `commanded_equiv_ratio_convention: phi | lambda | unverified`.
  `read_channels` exposes a derived `obd:COMMANDED_AFR` only when the
  convention is recorded; `unverified` raises with instructions to check at
  WOT. Record `phi` for the Xterra with the WOT evidence from `78592f4`;
  the example config ships `unverified`.
  **DONE 2026-08-05** — `AfrCalibration.commanded_equiv_ratio_convention` +
  `commanded_afr()`; derived `obd:COMMANDED_AFR` spec in the reader; `phi`
  recorded for the Xterra with the WOT evidence. `unverified` raises with
  instructions rather than guessing.
- [x] **1.4 — Expose through the existing query path.** `query_telemetry`
  (MCP) and `/analysis/binned` gain an engineering-units option routed
  through the reader, so remote/agent callers get real units without SSH.
  Naming note: prefer `units="engineering"` over `decoded=` — `decoded:`
  already names the broadcast-decoder source namespace.
  **DONE 2026-08-05** — `units=` on `GET /analysis/binned` and the
  `get_binned_analysis` MCP tool, rescaling `decoded:` rows via
  `rescale_channel_rows`. Scope limit, documented in both docstrings: `did:`
  channels cannot reach this path at all — the query layer filters on
  `value_number` and DID values live in `value_text` — so those stay on
  `read_channels`. Closing that would mean a second query engine, which the
  roadmap's own constraint forbids.

## Phase 2 — Tuning analysis module (the gates, as code)

- [x] **2.1 — `delivery_error(sessions, *, profile)`.** Measured-vs-commanded
  AFR with every gate baked in: open-loop-only (`FUEL_STATUS == 4` **and**
  enrichment target strictly below stoich, so DFCO is excluded by
  construction), same-instant pairing via 1.2, commanded AFR via the 1.3
  convention registry, **band-averaged over the intersection of rpm
  coverage** (refuses to compare datasets whose common window is narrower
  than a configurable minimum), per-band n and the pooled-vs-band split
  reported side by side. Output cites which gates filtered how many samples
  — the "116 stoich seconds diluted your effect" failure becomes visible
  instead of silent. Measured-AFR source is a parameter (λ path vs.
  DID-voltage path) so the 3.8 % dispute stays visible rather than being
  silently resolved either way. Acceptance: reproduces the hand-computed
  +0.72 % band-averaged result on `_tuning-2026-08-05T142720`.
  **DONE 2026-08-05** — `src/analysis/tuning.py`. On the reference session it
  gives n=16 (identical), pooled +1.15 % vs the hand +1.17 %, band-averaged
  +0.69 % vs +0.72 %. The residual is conversion order — this converts each
  sample's commanded ratio to AFR *then* averages, where the hand analysis
  averaged the ratio first; per-sample is the more correct order, and the
  difference is far inside the ±1.5 % floor. Cell aggregation turned out to
  matter (last-vs-mean within a cell moved the band average 0.58 → 0.72 %),
  so it is now an explicit `cell_agg` parameter rather than a hidden default.
- [x] **2.2 — `compare_sessions(a, b, value, axes)`.** Cross-flash / A-B
  deltas on a matched grid. **Refuses `ENGINE_LOAD`/`CAL/LD` as an axis**
  (message cites the +1.43-points-at-fixed-airflow measurement and points
  at `rpm×MAF` / `rpm×BFS`); resolves aliases so `obd:ENGINE_LOAD` and
  `did:` load equivalents are equally refused. Reports per-cell deltas
  with n, and — when a floor exists (2.3) — delta ÷ floor. BFS axis may use
  `did:ECM_AIR_CHARGE` (0x1208, then named `ECM_INJECTOR_PW_B1`) unscaled (scale is null) with the raw-counts
  caveat carried in the output. `unsafe_axes=` overrides with a logged
  warning.
  **DONE 2026-08-05** — refusal covers `obd:ENGINE_LOAD`, bare `ENGINE_LOAD`
  and the `CALCULATED_LOAD`/`ABSOLUTE_LOAD` aliases. Two extra gates emerged
  from real data: an empty matched frame and a zero-spread axis both used to
  surface as cryptic pandas errors, and now name what is wrong.
- [x] **2.3 — Noise floor as an artifact.** `data/<profile>/noise_floor.yaml`:
  per-channel, per-cell sd measured from designated null-run pairs, with
  provenance (which sessions, which day). Seed it from the 2026-08-04 AM/PM
  pair (delivery error sd 1.49 %, spark sd 0.64°/0.75° — see
  `engine-tuning-support.md` 3.3/3.5) by running the recompute against the
  stored sessions, recording recomputed values plus provenance rather than
  copying the prose numbers. `record_null_run(a, b)` recomputes it; 2.1/2.2
  load it and annotate every delta with `× floor`. A delta under 2× prints
  as *not distinguishable from noise* — the phrasing that was missing from
  three confident claims. A missing floor degrades to `floor: unavailable`,
  never an invented one.
  **DONE 2026-08-05** — `src/analysis/noise_floor.py`, seeded by recomputing
  against the imported UpRev null pair rather than copying the prose numbers:
  spark sd **0.483°** (campaign 0.64), delivery sd **1.250 %** (campaign 1.49),
  5 cells each, means +0.069 / −0.338 against +0.01 / −0.36. Two findings from
  doing the recompute properly: the floor is only valid for the conditions it
  was measured under (across *all* conditions spark spread is 7.7°, because
  idle timing swings 17–45°), and it needs a 50 ms grid — UpRev rows are
  synchronous by construction, so a 1 s grid discards most of them and left
  zero matched cells.
- [x] **2.4 — Scale-stability validator.** The decisive binding test
  (`c341570`) as a reusable function: fit linear map on window A, predict
  window B, report OOS R², refitted-scale drift, and verdict against the
  0.10 % `ECM_RPM` control. Regression-test against the recorded 0x23D
  outcomes (pedal +0.24 % stable, airflow −6.5 % unstable). Used by future
  binding/DID promotions instead of each one re-inventing the arithmetic.
  **DONE 2026-08-05** — `scale_stability()`; regression test reproduces both
  0x23D verdicts (pedal `stable`, airflow `unstable` despite r ≈ 0.98).
- [x] **2.5 — CLI + MCP surface.** `scripts/tuning_report.py
  --delivery-error --compare A B ...` for the laptop; MCP
  `get_delivery_error` / `compare_tuning_sessions` (analysis-only) for agents,
  following the `get_behavior_deviations` local-tool pattern (requires
  `CANARY_CONFIG`, no daemon round-trip). Docs: `docs/mcp-server.md` table
  + a "tuning analysis" subsection in `docs/headless-api.md` cross-linking
  here.
  **DONE 2026-08-05** — `scripts/tuning_report.py` (`delivery-error`,
  `compare`, `null-run`) and MCP `get_delivery_error` /
  `compare_tuning_sessions`, both local-tool pattern, returning
  `{ok: false, error}` on a gate refusal so a refusal reaches an agent as a
  message rather than a stack trace. Documented in `docs/mcp-server.md`.

## Phase 3 — Capture orchestration: one verb, honest telemetry

- [x] **3.1 — Instantaneous achieved rate.** `/status.obd_scheduler` gains
  `achieved_hz_60s`: per-channel reads in the trailing 60 s window (ring of
  timestamps per channel; cheap). The lifetime `channel_reads_per_second`
  stays but is renamed/labelled `lifetime` so nobody reads it as current
  again. `active_requests` either applies the profile filter or is
  explicitly renamed `active_requests_unfiltered`. Dashboard bindings
  updated in the same change (`pnpm test` + `typecheck` gate applies).
  **DONE 2026-08-05** — `achieved_hz_60s` (per-channel deque over a 60 s
  window), `active_requests_unfiltered` alongside the original key, and the
  lifetime average relabelled `channel_reads_per_second_lifetime`. No
  dashboard change was needed after all: the frontend never bound either
  field, so the pnpm gate does not apply here.
- [x] **3.2 — `vehicle_state` from broadcast during captures.** The
  classifier currently reads OBD freshness from the storage session, which
  rotates at capture start → `no_data` for every capture (bug found
  2026-08-04). During an active capture profile, classify from decoded
  broadcast channels (`ECM_RPM` at ~100 Hz never stops; the binding is
  `confirmed`). Also fixes the all-DID case, where Mode 01 is dark *by ECU
  design*. Acceptance: arm a capture during replay → state stays
  `engine_running`.
- [x] **3.3 — `arm_tuning_capture` / `disarm_tuning_capture` MCP verbs.**
  One call: activate profile (by name — `tuning-did`, `fuel-map`), set
  `rate_hz`, set bus budget with TTL, then **verify** by sampling
  capture-scoped `achieved_hz_capture` after ~20 s and reporting the actual
  observation interval, sample count, and maximum gap in the response. Disarm
  reverts profile + the exact prior budget state and returns `ended_capture_dir`.
  The daemon owns this transaction at `/control/capture`; the MCP verb makes
  no follow-up state-changing calls. Repeating the same arm, or immediately
  retrying disarm after a lost response, is idempotent. Expose `set_bus_budget`
  standalone too (MCP write-tool, same auth gating as `start_capture`).
- [x] **3.4 — TTL ergonomics.** The `bus-budget` TTL cap (3600 s) rejected a
  4-hour request mid-arming; `null` disables expiry but silently. Accept
  `ttl_seconds` up to 6 h for capture-scoped overrides (the capture
  teardown already reverts budget — the TTL is a backstop, not the
  mechanism), and include `ttl_remaining` in `arm` responses.
  **DONE 2026-08-05** — cap raised 1 h → 6 h; the arm response carries the
  whole `bus_budget` snapshot, `ttl_remaining_seconds` included.

## Phase 4 — UpRev importer upgrades (ground truth without the guessing)

- [x] **4.1 — Clock alignment by RPM cross-correlation.** Port the
  scratchpad method (peak r = 0.9998–0.9999 on a 100 ms grid; measured
  offsets −1.3 s / −1.6 s) into `tools/import_uprev_log.py`: given
  `--align-session <id>` (or auto-detected overlap from the session
  catalog), refine `--started-at` by the measured lag and record
  `{clock_offset_seconds, alignment_r}` in the import manifest. Refuse to
  claim alignment when peak r < 0.99 — that threshold is what exposed the
  7-29 dead overlap. Validate against the 2026-07-27 pair with its known
  offsets.
  **DONE 2026-08-05** — `src/analysis/clock_align.py` + `--align-session`.
  Validated exactly: the 2026-07-27 pair recovers 1.30 s / 1.60 s at
  r = 0.99982 / 0.99989, and the 2026-07-29 pair is *refused* with 0
  overlapping samples. Sign convention is "offset to add to the candidate's
  timestamps"; the scratchpad reported the opposite shift, same relationship.
  Those July sessions predate decoded persistence, so alignment falls back to
  `can:ECM_RPM` when `decoded:` is absent.
- [x] **4.2 — Auto-import sweep (laptop-side).** The UpRev logs directory is
  on the Windows side (`~/UpRev Logs/`); a small
  `tools/watch_uprev_logs.py --once` (cron/manual, not a daemon) imports
  new CSVs with the filename hint, then applies 4.1 when an overlapping
  session exists. Local-only, per the importer's existing design.
  **DONE 2026-08-05** — `tools/watch_uprev_logs.py`, skipping anything whose
  `original_filename` already appears in an `_uprev` manifest. Dry run over
  the real log directory correctly proposes the 9 un-imported logs and skips
  autosaves, whose filenames carry no parseable stamp.
- [x] **4.3 — Era/tune registry.** Imports and capture sessions both carry a
  `tune_revision` (the capture route already accepts one). Add the registry
  file (`data/<profile>/tune_revisions.yaml`: revision → flash time, K,
  notes, ROM file name) and have 2.1/2.2 label outputs by revision, so
  "delivery error by tune revision" is a groupby, not archaeology. Backfill
  the 2026-07/08 flash history from `engine-tuning-support.md`'s timeline.
  **DONE 2026-08-05** — `src/analysis/tune_revisions.py` +
  `data/nissan-xterra-2008/tune_revisions.yaml`; `delivery_error` carries
  `tune_revision` and `compare_sessions` carries both sides plus a
  `same_revision` flag (equal revisions mean the spread *is* noise). Two
  boundaries are honestly approximate — the K1.05 and K1.06 flash times were
  never recorded, so the yaml says so rather than implying precision.

## Phase 5 — Post-drive tuning digest

- [x] **5.1 — Digest flavor for capture sessions.** The trip-end hook
  already writes `digest.json` per session. For sessions with a capture
  profile, add a tuning section: WOT inventory (seconds by rpm band × load,
  with AFR-pair counts), achieved per-channel Hz vs. requested, delivery
  error via 2.1 with floor annotation, cam commanded-vs-measured summary
  when the DID channels are present. Rendered by the existing
  `get_session_digest` path — no new endpoint.
  **DONE 2026-08-05** — `src/analysis/tuning_digest.py`, wired into
  `compute_digest` as a `tuning` block that is `None` for ordinary drives (so
  a reader can tell "not a capture" from "a capture that found nothing") and
  never raises — a digest is a report, not a gate. Verified end-to-end against
  the real 2026-08-05 capture: 27 WOT seconds, n=16, band-averaged +0.45 %,
  tune revision ZS44A-k106. Achieved rate is computed from the stored rows
  rather than read off `/status`, so it still works long after the drive and
  reports what was actually written.
- [x] **5.2 — Delta vs. previous capture.** When an earlier session shares
  the same profile + tune revision, the digest appends the 2.2 comparison
  (matched axes, floor-quoted). This is the "did the flash do what I meant"
  answer, produced without anyone typing SQL at 10 pm.
  **DONE 2026-08-05** — matches on profile **and** tune revision, because
  across a flash boundary the delta is the flash rather than drift. Quotes
  `delta_sigma` + `distinguishable` against the measured floor, or `None` when
  no floor exists.

## Explicitly out of scope

- **More DID/sweep tooling** — `start_job(did_sweep)` + `diff_did_snapshots`
  exist; use them. The only sweep follow-up is extending the range past
  `0x1400` (an argument, not a tool).
- **Knowledge-base MCP / campaign-facts search** — the roadmap docs + memory
  are doing this job adequately.
- **Multi-vehicle speculation** — profiles already partition; nothing here
  hard-codes the Xterra beyond its data files.
- **Aftermarket wideband** — hardware, operator's call, and the only path to
  *absolute* AFR truth. Every tool above sharpens the comparison between two
  internal paths that could both be wrong (the 3.8 % λ-vs-voltage
  disagreement, `78592f4`). Recorded here so the software list is honest
  about what it cannot settle.

## Suggested order and why

1 → 2 (reader feeds gates; together they would have prevented three of the
four retractions) → 3 (next capture benefits; 3.2 is the standing
`vehicle_state` bug) → 4 → 5. Phases 1–2 are ideal overnight-queue
candidates: pure library code, testable against sessions already on disk
(`_tuning-2026-08-05T142720` has known-correct hand-computed answers to
assert against: +0.72 % band-averaged delivery error, 8.3 Hz fast-tier
rate). Phase 3.2's acceptance runs on replay, no truck. Phase 4.1 validates
against the 2026-07-27 overlap pair with its known offsets.

## Cross-links

- [`engine-tuning-support.md`](./engine-tuning-support.md) — the campaign
  this serves; 3.3/3.5 hold the measured gates and floors
- [`capture-profiles.md`](./capture-profiles.md) — profile/capture
  mechanics this orchestrates
- [`multi-pid-polling.md`](./multi-pid-polling.md) — rate/batching
  substrate; its 4.x validation tasks pair with 3.1 here
- `data/analyses/did-sweep-20260804-koeo-baseline.md` — session
  exclusivity, DID identifications, the λ/φ finding
- `docs/mcp-server.md` — existing tool surface (check before building)
