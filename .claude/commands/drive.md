---
description: Standing protocol for an autonomous drive session against the live Pi. Use as `/loop /drive` followed by the drive's OUTCOME and DRIVE FACTS.
---

You are running an autonomous drive session: the operator is driving the
vehicle, the Pi is collecting, and you work from the laptop. The operator's
lines for THIS drive follow; everything after them is the standing protocol.

$ARGUMENTS

If the lines above name no OUTCOME, take it from the newest
`data/analyses/*-deploy-plan.md`. If DRIVE FACTS are missing, assume the
conservative answer (no WOT pulls, short drive, no external logger). If the
plan needs something the drive cannot give, say so on the log's first line
and fall back to what the plan lists, else to the plain `default` drive.
**Never claim an experiment ran without the samples it needs.**

Everything serves the OUTCOME. Monitoring and deploying are hygiene.
Discovery and code changes happen only when they move the outcome or fix a
defect seen this drive. The plain drive on the `default` profile is what the
models train on, so it wins ties.

## Talking to the operator

Never ask a question that blocks you. You may post one-line maneuver
requests in chat and in the Glance ("when safe: a 10 s 4th-gear WOT hold"),
and you read what the operator says — they do answer, and a sentence from
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
2. `docs/roadmap/tasks/D03-open-work.md` "Suggested order", and the other
   live task docs it points at.
3. Previous `data/analyses/*-drive-log.md`, newest first: incidents and
   their causes. Don't repeat them.
4. Log each precondition the plan names as met / not met.

Create `data/analyses/<today>-drive-log.md`: a 3-line **Glance** (refreshed
every wake-up, the only thing the operator reads; line 1 is the maneuver you
want next, if any), then **For the operator**, then the plan with each item
tied to the OUTCOME or marked hygiene, then a health table and work notes.
Timestamps come from `date -u`, never from memory.

**Predictions are written before the data.** If the plan has pass/fail
criteria, do not move them after seeing data; if it has none for a claim you
are about to test, write them into the log before the capture starts.

## First wake-up, before any capture

- **Deploy only if the plan lists pending code.** `git pull --ff-only origin
  main` on the Pi (a git bundle over ssh is the fallback). The Pi's
  `data/<vehicle>/capture_profiles.yaml` is usually dirty from the daemon's
  own built-in-profile upgrade: diff it semantically, back it up to `/tmp`,
  reset it. Verify `git log -1` matches what you pushed; healthy within
  3 min. If the operator already deployed, verify and move on.
- **Boot checks:** `journalctl -u canary -b | grep -c "batching disabled"`
  is 0 and `/status` `health.obd_scheduler.batching` shows the configured
  `max_pids` with `size_narrowings 0`. Memory limits: the file
  `/sys/fs/cgroup/system.slice/canary.service/memory.max` must EXIST —
  `systemctl show` echoing the values proves nothing. Log what you find.
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
   the RSS sum of the cgroup's pids). Flag counter resets. Log it.
3. A regression is diagnosed before anything else. Read `vehicle_state`
   before concluding anything about detectors; a burst of anomalies at an
   altitude or condition outside the corpus is out-of-distribution, not a
   fault — say which.
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
- Read every stop verdict: each `fail` clause is explained or becomes a
  task, never ignored.
- Isolated captures ≤ one third of driving time. Stop one before a stop that
  ends a block.
- Pull only CLOSED sessions, with
  `ssh <pi> 'cd <sessions> && nice -n 19 ionice -c3 tar cf - <dir>' | tar xf -`,
  and analyse on the laptop within minutes of the block closing: known answer
  first (e.g. the DID and the Mode 01 PID for the same quantity agree 1:1;
  refuse to continue if not), then the written predictions. Report pass /
  fail / unresolved per claim with `n_pairs`. An empty result names which
  kind of empty it is.

## Standing objectives

- **Verify one stored belief first** (memory, a catalog evidence block, a
  roadmap figure the outcome rests on) with the cheapest check that could
  falsify it. A falsification is the best result a drive can produce:
  correct the memory or doc the same day.
- **Desk analysis of data already on the laptop is the best use of warm-up
  and transit time.** Delegate it, and code fixes, to subagents so the loop
  stays light; review their diffs and re-run their tests yourself.
- **Discovery only in service of the outcome**, and always as candidates
  until a `compare_channels` 1:1 against a known answer promotes them. One
  passive `did_log` survey per drive at most, diffed against the previous.
- **Code: fixes for defects observed this drive, small, tested.** Refactors
  and design changes are desk work; queue them as roadmap tasks.
- **Deploying mid-drive is allowed** for a good fix, with a restart to check
  it — never while an isolated capture or a maneuver set is running. Hash
  verified, drive window reopened, unhealthy after 3 min → revert and
  redeploy, two failures → stop deploying. Say what the restart can and
  cannot prove.
- **Retire what you disproved.** Stale claims in memory and docs are
  corrected or deleted, not layered over.

## Hard rules

- Never write non-volatile ECU memory: no 0x2E, 0x3B, 0x31, 0x34–0x37.
  Don't clear DTCs. 0x27 requestSeed only.
- No analysis on the Pi while it collects. No replays on the Pi while the
  truck is collecting. No bus-budget raises.
- Never edit `bindings.yaml`, a feature spec, or the `default` / `fuel-map`
  profiles from the road. Catalog candidates at the lowest confidence rung,
  and only where a task says so. Never compare aggregates.
- Passive mode always with a TTL; disarm it when the block ends.
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
Then: stop the capture window if the Pi still answers; final summary = the
OUTCOME's state per claim (promoted, falsified, or unresolved and why),
beliefs verified or falsified, commits, candidates, the operator's decision
list, and what the next drive must produce; write that next-drive plan as
`data/analyses/<today>-next-drive-deploy-plan.md`; preserve the poller JSONL
and analysis scripts under `~/canary-data/`; commit with explicit pathspecs;
end the loop.

If a drive teaches you something durable about running drives, edit THIS
file — don't grow the pasted prompt.
