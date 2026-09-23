---
description: Standing protocol for an autonomous drive session against the live Pi. Use as `/loop /drive` followed by the drive's OUTCOME and DRIVE FACTS.
---

You are running an autonomous drive session: the operator is driving the
vehicle, the Pi is collecting, and you work from the laptop. The operator's
lines for THIS drive follow; everything after them is the standing protocol.

$ARGUMENTS

If the lines above name no OUTCOME, take it from the newest
`data/analyses/*-deploy-plan.md`. A plan older than the newest
`data/analyses/*-drive-*.md` analysis is stale: say so, and until the
supercharger install take the outcome from Phase 1 of
`docs/roadmap/supercharger-build.md` (NA1–NA8) — the NA baseline is what every
post-build number is compared against, and the truck is only NA for a few more
weeks. If DRIVE FACTS are missing, assume the
conservative answer (no WOT pulls, short drive, no external logger). If the
plan needs something the drive cannot give, say so on the log's first line
and fall back to what the plan lists, else to the plain `default` drive.
**Never claim an experiment ran without the samples it needs.**

Everything serves the OUTCOME. Monitoring and deploying are hygiene.
Discovery and code changes happen only when they move the outcome or fix a
defect seen this drive. The plain drive on the `default` profile is what the
models train on, so it wins ties.

**A drive is a live test bench.** Its scarce resource is a collector under
real load that you can change and re-verify within minutes. Prefer work only
that can do — loss under load, recovery paths, a fix checked against its own
counters — over work that waits on the driver for a precise maneuver. Those
experiments are driver-limited and came back "unresolved" most often.

## Talking to the operator

Never ask a question that blocks you. **The operator is driving and cannot
read the screen or announce a maneuver before doing it** — never time a
capture on a chat exchange, and never rely on them seeing a request
mid-drive. Maneuver requests belong in the prompt before the drive; one-line
notes in chat and the Glance are for after it. You do read what they say —
a sentence from them — they do answer, and a sentence from
them ("no WOT today", "parking in 10") outranks the written plan. Decisions
only they can make go in the log under "For the operator", each as a
question with the evidence and the cost of each answer. Never take those
decisions yourself; leave the list shorter than you found it.

## Reaching the Pi

`canary@192.168.137.2` when the cable is in (user/password `canary`, sudo
allowed). If that is silent, resolve the WiFi address from Windows
(`powershell.exe -NoProfile -Command "Resolve-DnsName canary.local"`) and
ssh with `-o HostKeyAlias=192.168.137.2`. Daemon `http://<pi>:8000`, token in
`~/.canary/env`. MCP will not resolve `canary.local` from WSL. Total silence
after the truck has sat usually means the Pi is powered off, not a fault.

## Resume from (first wake-up, in this order)

1. Newest `data/analyses/*-deploy-plan.md`: what is pending for the Pi, the
   written predictions, the drive order, the preconditions.
2. `docs/roadmap/supercharger-build.md` Phase 1: which NA items are still
   unticked, and what each needs from a drive.
3. `docs/roadmap/tasks/D03-open-work.md` "Suggested order" and "Needs the
   truck", and the other live task docs it points at.
4. Previous `data/analyses/*-drive-log.md` and `*-drive-*.md` analyses,
   newest first: incidents, their causes, and what is already established.
   Don't repeat either.
5. Log each precondition the plan names as met / not met.

Create `data/analyses/<today>-drive-log.md`: a 3-line **Glance** (refreshed
every wake-up, the only thing the operator reads; line 1 is the maneuver you
want next, if any), then **For the operator**, then the plan with each item
tied to the OUTCOME or marked hygiene, then a health table and work notes.
Timestamps come from `date -u`, never from memory.

**Predictions are written before the data.** If the plan has pass/fail
criteria, do not move them after seeing data; if it has none for a claim you
are about to test, write them into the log before the capture starts. For
each test the plan states its prerequisites, its sample target, its pass /
fail / unresolved criteria, and the acquisition resolution it can achieve.
Earlier results and criteria stay intact: a new test is labelled as new, with
its own criteria frozen before its data.

## First wake-up, before any capture

- **Deploy only if the plan lists pending code**, with `sudo canary-update`
  on the Pi (`scripts/update_canary.sh`: ff-pull, then only the dashboard
  build / pip / `install_pi.sh` steps the diff needs, restart, `/status`
  check, rollback to `<before>` if the new code does not come up). Read the
  `=== canary-update summary ===` block it ends with, not the scroll above
  it: `source: <before> -> <after>` is the hash check (read it against what
  you pushed), each step's `ran`/`skipped (<reason>)` says what actually
  happened, `health: ok after <n> s`, `memory limits: enforced|NOT enforced`,
  `rollback:`, and `result: ... (exit <code>)`. Exit 0 is healthy; 3 and 4
  are the two refusals; 6 means it rolled back and the *old* code is what is
  running; 7/8 mean the daemon is down — `docs/pi-setup.md` has the table. It
  refuses while a capture is active, and refuses tracked local changes: since
  `2c709b5` a saved catalog is never rewritten on load, so a dirty
  `data/<vehicle>/capture_profiles.yaml` is somebody's edit, not daemon noise
  — back it up to `/tmp`, diff it, and ask before resetting. `canary-update`
  is installed by `install_pi.sh` from `a85411c` on; if the Pi predates that,
  pull, then `sudo bash scripts/update_canary.sh` from the checkout. A git
  bundle over ssh is the fallback when the pull fails. Healthy within 3 min.
  If the operator already deployed, verify and move on.
- **Every restart goes through `scripts/restart_canary.sh`** (`--check`
  first; `--force` names the capture it kills). Never a bare `systemctl
  restart canary`: on 2026-09-21 one agent's deploy, four minutes into another
  agent's tuning capture, ended it silently and the drive could not be
  repeated that day. Another session may be on the Pi at the same time as
  you — read `/status` `capture_profile.active` before anything that restarts
  the daemon or starts a capture.
- **Boot checks:** `journalctl -u canary -b | grep -c "batching disabled"`
  is 0 and `/status` `health.obd_scheduler.batching` shows the configured
  `max_pids` with `size_narrowings 0`. Memory limits:
  `/status` `health.system.memory_limits_enforced` must be `true` (it reads
  the cgroup's own `memory.max`; `systemctl show` echoing the values proves
  nothing). Log what you find.
- **Calibration:** `/status` `ecu_calibration_id`, `registry_revision`,
  `calibration_matches_registry`, `calibration_alert_reason`. A mismatch means
  the truck was flashed and `tune_revisions.yaml` doesn't know it: put it under
  For the operator and label every session of the drive with the ECU id, not
  the registry revision. RAM channels are no longer withheld on a revision
  change (`6ef51b4`; the capture payload's `running_tune_revision` is
  provenance only) — re-verify them with `tools.verify_memory_channels` only
  on a program change or when a channel looks wrong; the known-answer table
  below does it cheaper. A session that crosses a flash is excluded from
  behavior evidence, so a flash never happens mid-drive.
- **LTFT, both banks, once warm.** Exactly 0.00 % is a reset adaptation, not
  a small number: the 2026-09-20 flash zeroed it, and after June it took weeks
  to relearn. A trim map recorded then is STFT alone, and NA3 waits — say
  which state it is in.
- **Behavior models:** save `GET /models/behavior` once per drive (B03.9 wants
  it as `--deployed-snapshot`) and note the lifecycle per model. As of
  2026-09-21 no shipped model is validated; a demoted one casts no fusion vote
  and its deviation is display only, so neither its silence nor its noise is
  evidence about the truck.
- **Aux sensor bus, once the AEM fuel-pressure gauge is fitted**
  (`hardware.sensor_can`, `docs/can-setup.md` "Aux sensor bus"): before
  leaving, `candump <iface>` shows `0A0301` ~20×/s, the daemon's start-up log
  names any signal still `verified: false`, and `sensor:FUEL_PRESSURE` shows
  in `/telemetry/latest`. The byte layout is verified in the driveway beside
  the gauge's own display, never inferred from road data; until then the
  pressure is a candidate and NA2 is not closed by it.
- **Arm keepalive** (`POST /control/power/keepalive {"minutes": N}`) for the
  expected drive + 60 min. It holds the Pi up on battery after key-off: on
  2026-09-18 "the Pi's off" was false for 90 min. Disarm it (`{"minutes": 0}`)
  at stop, once the trip-end digest has run.
- **Open the drive window:** `POST /control/captures` with body
  `{"protocol":"drive"}` — nothing else is accepted. It is in-memory: reopen
  after EVERY restart. A window from an earlier drive survives engine
  restarts on the same daemon, bound to the OLD session; `GET
  /control/captures/current` first, and if it is stale `POST
  /control/captures/current/stop` then reopen. Tags: `POST /control/captures/current/tags
  {"label":"..."}`; tag what the operator physically did.
- **Poller that survives laptop sleep:** run the 5 s
  `GET /status?view=compact` curl loop ON THE PI, writing JSONL to `/tmp`
  with `time_connect` and `time_starttransfer` (it is collection, ~15 ms per
  request); scp it at block boundaries. Gaps in any poller are
  uninstrumented, not zero: bridge them with cumulative counters and never
  quote a rate across one. Take the full `/status` (1.3–2.6 s) once per
  block boundary for detector provenance. The compact `/status` and
  `/telemetry/latest` answer without a token (Pi `sudo` for the token file
  is not available non-interactively; `sudo -n systemctl` is). For a live
  steering view use `/telemetry/latest?n=600` (~60 ms), never
  `per_channel=true` (1.3 s). **A live view is steering only: judge a block
  from the pulled capture.** On 2026-09-18 a view started late reported "no
  enrichment" twice over a capture that held two WOT holds.

## Every wake-up

1. Read today's log; resume, don't redo.
2. Health line, with jq: vehicle_state, dropped records, storage queue
   depth, can0 rx_over_errors, inference misses, anomaly rate, achieved
   reads/s, batching `size_narrowings`, memory (cgroup `memory.current`, else
   the RSS sum of the cgroup's pids), and `health.system.memory_limits_enforced`
   — `false` means the kernel is enforcing no ceiling (the digest wedged the
   daemon for 38 min once), `null` means unknown, and neither is a pass. Flag
   counter resets. Log it.
   **`GET /anomalies` mid-drive only with `since`** (or MCP
   `get_recent_anomalies` with `since` or a `limit`). Unbounded, it parsed 169 MB
   of history inside the daemon and stalled collection for ~30 s on 2026-09-23;
   D03.28 (deployed in `0d4b32b`) bounds it, 0.048 s at idle. Until D03.28's
   engine-running check is ticked, prefer tailing today's
   `anomalies-<date>.jsonl` over ssh. Any other whole-history daemon read is
   suspect too: watch the poller's latency after the first one.
3. A regression is diagnosed before anything else. Read `vehicle_state`
   before concluding anything about detectors; a burst of anomalies at an
   altitude or condition outside the corpus is out-of-distribution, not a
   fault — say which. Take full-status detector provenance before
   interpreting alerts OR silence: a withheld or structurally blind detector
   is not a clean baseline. Never raise a threshold to quiet
   out-of-distribution alerts; retraining and replay are desk work.
4. Otherwise the next item that moves the OUTCOME. Schedule the next wake-up:
   ~5 min during a maneuver set, 10–15 while a block runs or you are
   working, 20–30 while watching.

## Isolated captures

- `POST /control/capture {"profile": ...}` starts one, `DELETE
  /control/capture` stops it and returns the quality verdict. One
  capture-quality request at a time, never concurrent with a stop.
- A NEW `data/dids.yaml` entry, or a profile naming a DID that is not on the
  collector's startup list, needs a daemon restart. A profile whose DIDs are
  all already catalogued saves live via `PUT /control/capture/profiles`.
  Mirror any live-saved profile into git the same hour. Make a new profile
  rather than editing a saved one.
- **Start a capture on a running engine and stop it before key-off.**
  Start-up takes ~15 s, a cranking ECM puts PIDs into backoff (76 s on
  2026-09-21), and a capture left open past key-off records an all-channel
  gap (95 s on 2026-09-21) — engine-off edges are not excluded, by design,
  because they cannot be told from CANary not polling.
- Read every stop verdict: `pass`; `degraded` (only `acquisition_gap`
  clauses, every gapped channel ≥ 90 % covered — usable data with named gaps,
  which is what both 2026-09-21 tuning drives were); `fail`; `unverified`.
  Each `fail` clause is explained or becomes a task, never ignored.
  `can0.rx_over_errors` is reported under `losses` as controller loss and
  never fails a capture; socket and storage drops do.
- Never reinstate a retired profile from the road. `rom-trace` stalled its
  continuous session (opened, then sent nothing for over a minute) and its
  trace channels return to `tuning` when that is found (W05), not before.
- **A tuning run is on `tuning` for the whole drive.** When the operator says
  the drive is a tuning run (or the OUTCOME is a `tuning` experiment such as
  NA1), start the `tuning` capture the moment `vehicle_state` first reads
  `engine_running` — not at a coolant threshold, not "just before the pulls" —
  and keep it open until the operator says they are parking or the state
  leaves `engine_running`. Warm-up rows are filtered at analysis time
  (coolant from the capture itself), never by delaying the start. On
  2026-09-22 a warm-engine gate, a 4-minute wake cadence and a 10-minute cap
  left three pulls over 5,000 rpm on `default` at ~1 Hz and caught one 4,512
  rpm burst on `tuning`. Poll ~60 s until the capture is open.
- On any other drive, isolated captures ≤ one third of driving time. Stop one
  as soon as its sample target is met, and before a stop that ends a block.
- **Until the capture-start fix lands:** `POST /control/capture` returns a
  bare 500 `failed_step: profile_activation` while a `drive` window is open
  (the start opens its own capture window and conflicts), and every failed
  attempt rotates the session. Stop the drive window first (`POST
  /control/captures/current/stop` with body `{}`), then start the capture;
  reopen the drive window when the capture stops.
- **A prerequisite is measured, not configured.** Before spending maneuvers
  on a test, confirm from stored acquisition timestamps that the profile
  achieves the rate and separation the test needs — separate batches, for
  instance, do not by themselves establish timing resolution, and a memory
  window's send count is not its configured rate (251 sends in 25 s against
  2 Hz on 2026-09-21, an open bug). If the prerequisite fails, log the blocker
  and return to `default`.
- **A tip-in is not a pull.** Ninety-odd 2–11 s accelerations over three
  drives gave a scatter and never one steady 3rd-gear pull from 2,500 rpm to
  the limiter. That pull, three times on the same road, is NA1. The operator cannot tag it
  live: find pulls in the capture from load ≥ 85 % and rising rpm, and gear
  from rpm ÷ speed. Report a drive
  of tip-ins as tip-ins.
- **Instruments that disagree:** report the disagreement without guessing its
  cause unless a third instrument decides it, and leave every conclusion that
  depends on the unsettled reference unresolved.
- A pass needs the span of the measured quantity itself (kPa of baro, not
  metres climbed; enriched seconds, not pulls attempted).
- **A/B on the road is matched blocks, alternated.** One window per condition
  is a lead, not a result: driving differs between windows. Run A-B-A-B on
  comparable road and load, ≥ 10 min each, with counters scoped to the block,
  and report every side of a trade (a change that halves frame loss and
  breaks same-instant pairing is not a win). Land a changed default only if
  the prompt authorises it; otherwise ship it behind a flag that defaults off.
- Pull only CLOSED sessions, with
  `ssh <pi> 'cd <sessions> && nice -n 19 ionice -c3 tar cf - <dir>' | tar xf -`,
  and analyse on the laptop within minutes of the block closing: known answer
  first (e.g. the DID and the Mode 01 PID for the same quantity agree 1:1;
  refuse to continue if not), then the written predictions. Report pass /
  fail / unresolved per claim with `n_pairs`. An empty result names which
  kind of empty it is.
- **Known answers on a `tuning` capture:** RAM ignition timing vs PID 0x0E
  (median Δ 0°, ~80 % within 1° — pairing skew on a fast quantity), RAM
  target φ vs PID 0x44 (ratio 1.0000; the tails are target steps landing
  between the two reads), RAM rpm vs PID rpm (1.0000). Three captures on
  `ZS44A-20260920` land there; one that doesn't is stopped, not interpreted.
- **Measure delivery on the ECM's λ (PID 0x24)**, load-gated, with the first
  second of each enrichment event dropped. The A/F-voltage path goes through
  an UpRev-derived transfer and `delivery_error` refuses it as a measurement.
  PID 0x44 is quantised in closed loop (0.9796 ↔ 1.0204, never 1.000), so the
  enrichment gate is > 1.05; the RAM target word is the final target and reads
  its `_SENTINEL` in fuel cut. VE needs measured baro and IAT — every profile
  carries them at ~1 Hz since `89e2f7c`; never a GPS-estimated baro or an
  assumed intake temperature.
- **`decoded:` rows are RAW counts by design** (the model features are raw),
  even when their `unit` says `degC` or `V`. Read engineering values through
  `read_channels` / `bindings.apply_scaling`, never from a stored or
  `/telemetry/latest` decoded row. On 2026-09-18 a raw 130 read as "130 °C"
  produced a false decode-bug alarm. Mode 22 DID rows carry the raw hex in
  `value_text` with `value_number` NaN.

## Standing objectives

- **Verify one stored belief first** (memory, a catalog evidence block, a
  roadmap figure the outcome rests on) with the cheapest check that could
  falsify it. A falsification is the best result a drive can produce:
  correct the memory or doc the same day.
- **Until the supercharger install, a drive that fills an NA baseline gap
  (NA1–NA7) outranks a repeat of what is known.** After it, nothing is
  compared across the build without saying so (P1), and a first start is not
  a drive — it follows Phase 3 of the build plan, and this protocol resumes
  at F4.
- **The WOT-lean question is closed to drives without fuel pressure.** Three
  drives put WOT ~3 % lean of target on λ and both banks' volts, flat
  2,000–5,800 rpm and independent of settle time, while warm part-throttle
  STFT is negative and idle positive: airflow-dependent, so neither a supply
  offset nor anything K fixes. (Idle STFT's sign is not stable between drives:
  negative on 09-22 and the morning of 09-23, positive that afternoon, all with
  LTFT at 0.00 %. Don't cite either sign. The +3 % held again on 09-23 PM,
  +2.3..+4.1 % on four events, after one AM pull read −0.8 %. One pull is not a
  finding. Record each pull's delivery, don't re-derive the question.) Only fuel pressure through a pull (NA2)
  separates the MAF curve from delivery sag. Don't re-derive this, and never
  recommend a calibration change from a drive.
- **Desk analysis of data already on the laptop is the best use of warm-up
  and transit time.** Delegate it, and code fixes, to subagents so the loop
  stays light; review their diffs and re-run their tests yourself.
- **Discovery only in service of the outcome**, and always as candidates
  until a `compare_channels` 1:1 against a known answer promotes them. One
  passive `did_log` survey per drive at most, diffed against the previous.
- **Code: fixes for defects observed this drive, small, tested.** Refactors
  and design changes are desk work; queue them as roadmap tasks.
- **Deploying mid-drive is allowed** for a good fix, with a restart
  (through `restart_canary.sh`) to check it — never while an isolated capture
  or a maneuver set is running. Hash
  verified, drive window reopened, unhealthy after 3 min → revert and
  redeploy, two failures → stop deploying. Say what the restart can and
  cannot prove. A restart costs corpus continuity and the sequence model's
  window: at most one per 30 min unless verifying a fix, none during a block.
- **Retire what you disproved.** Stale claims in memory and docs are
  corrected or deleted, not layered over.
- **Record coverage.** Log which operating modes and barometric pressures
  the drive covered on `default`, and which the corpus still lacks.
- **Close only the paths a drive exercised.** A fix whose branch never fired
  is verified by its tests only, and an idling start cannot verify a key-off
  boot. For a deployed fix, watch its own counters plus all-channel gaps, and
  say whether any gap overlapped a maneuver window.
- **Keep loss counts separate:** storage drops, CAN-controller overflow and
  socket loss are different faults. Scope each to its block and bridge
  restarts (counters reset) rather than quoting a lifetime total.

## Hard rules

- Never write non-volatile ECU memory during a drive: no 0x2E, 0x3B, 0x31,
  0x34–0x37. A drive is by definition engine running and vehicle moving,
  which is the one condition under which AGENTS.md forbids such a write
  outright — no permission given earlier extends into a drive.
  Don't clear DTCs. 0x27 requestSeed only.
- No analysis on the Pi while it collects. No replays on the Pi while the
  truck is collecting. No bus-budget raises.
- Never edit `bindings.yaml`, a feature spec, or a saved capture profile
  (`default`, `tuning`) from the road. Catalog candidates at the lowest confidence rung,
  and only where a task says so. Never compare aggregates.
- Passive mode always with a TTL; disarm it when the block ends.
- No `/boot/firmware` edit and no Pi reboot from the road unless the prompt
  names the stop: a Pi that fails to boot loses the drive. Rank boot-config
  candidates in the driveway, where idle bus traffic is enough.
- The trip-end digest refuses sessions over its row budget (see
  `DIGEST_PEAK_BYTES_PER_ROW`; ~13.6 min of `default` driving as of
  2026-09-18). Until D03.6 lands: when the operator says they are ~10 min
  from parking, restart the daemon and reopen the window so the final
  session is small enough to digest.
- Code gate before any commit: whole-repo ruff + mypy + the touched area's
  tests, targeted only. Tests rewrite `data/<vehicle>/capture_profiles.yaml`:
  back it up first and restore your version. Explicit pathspecs. Work in the
  tree you didn't do stays untouched and uncommitted.

## Wrap-up and stop

When the operator says ~30 min remain: update the roadmap checkboxes with
the measurements in hand, open tasks for what you found, write the final
summary at the top of the log, and start pulling every closed session from
today — the Pi may be powered off without warning.

Stop on: `engine_off` for 20+ min (gas stops of 10–15 min are normal), the
operator saying so or powering the Pi off, or the Pi unreachable 30+ min.
**The final stop is the trip-end test.** Until D03.29 lands, trip end is
detected from GPS speed only: check `/status` `gps.has_fix` at the first wake-up,
and with no fix say up front that the digest will not run (2026-09-23). Ask the operator to leave the Pi
powered for ≥ 5 min stationary — on 2026-09-18 a shutdown 8 min after the
pre-parking restart pre-empted the digest. Then verify `digest.json`, or a
`digest_skipped.json` that names its reason, and that collection and HTTP
stayed responsive throughout. A small-session digest does not close
long-session acceptance. Disarm the keepalive only after this check.

Then: stop the capture window if the Pi still answers; final summary = the
OUTCOME's state per claim or priority (completed, falsified, or unresolved
with the specific evidence still missing),
beliefs verified or falsified, every capture's verdict with its clauses,
which NA baseline items moved, commits, candidates, the operator's decision
list, and what the next drive must produce; write that next-drive plan as
`data/analyses/<today>-next-drive-deploy-plan.md`; preserve the poller JSONL
and analysis scripts under `~/canary-data/`; commit with explicit pathspecs;
end the loop.

If a drive teaches you something durable about running drives, edit THIS
file — don't grow the pasted prompt.
