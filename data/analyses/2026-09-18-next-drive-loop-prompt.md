# /loop prompt for the pull drive

Fill in DRIVE FACTS, then paste everything inside the fence after `/loop `.
Written 2026-09-18 from that day's drive log; the plan it points at is
`data/analyses/2026-09-18-next-drive-deploy-plan.md`.

```
OUTCOME for this drive: close the fuel workflow with ONE capture — promote or
falsify 0x1134 = target phi (D03.8), 0x1206/0x1207 = injector pulse and
0x1208 = air charge (D03.9), and measure the commanded-phi -> lambda transport
delay (D04.3); the fuel-cut boundary (D04.2) comes free from the same capture.
The pass/fail criteria are ALREADY WRITTEN in the newest
data/analyses/*-deploy-plan.md. Read them first and do not move them after
seeing data.

DRIVE FACTS (operator fills in; if blank, assume the conservative answer):
- Road and traffic allow WOT pulls / long uphill holds: [yes/no]
- Expected driving time: [minutes]
- Elevation change >= 800 m in one stretch: [yes/no, when]
- UpRev laptop along: [yes/no]
- Engine state at start: [off / idling / driving]

If pulls are NOT possible, say so in the log's first line and fall back, in
this order: closed-loop plateaus + in-gear/neutral coasts on fuel-pw-research
(D03.9 closed-loop cells, D04.2); the >= 10 kPa elevation span on default
(D04.8, 0x1208 vs baro, corpus altitude data for D03.20); otherwise the plain
default drive. Never claim an enrichment test ran without enriched samples.

Autonomous drive session. The operator is driving: never ask a question that
blocks you. You MAY post one-line maneuver requests in chat and in the Glance
("when safe: a 10 s 4th-gear WOT hold"), and you read what the operator says.
Decisions only they can make go in the log under "For the operator".
Pi: canary@192.168.137.2 when the cable is in (user/password canary/canary,
sudo allowed); if that is silent, resolve the WiFi address from Windows
(`powershell.exe Resolve-DnsName canary.local`) and ssh with
`-o HostKeyAlias=192.168.137.2`. Daemon: http://<pi>:8000, token in
~/.canary/env. MCP will not resolve canary.local from WSL.

## Resume from (first wake-up, in this order)
1. Newest data/analyses/*-deploy-plan.md: what is pending for the Pi, the
   predictions, the drive order.
2. docs/roadmap/tasks/D03-open-work.md "Suggested order" and D04.
3. Previous data/analyses/*-drive-log.md, newest first: incidents and causes.
4. Preconditions, each logged as met / not met: D03.18 (hybrid timeout stall)
   fixed? operator decisions D03.21 and the 0x1208/0x1206 catalog text taken?
   analyze_fuel_blocks in tools/ (else ~/canary-data/scripts-0918/)? a 3 Hz
   pull profile saved? If D03.18 is NOT fixed, ask for long holds (>= 10 s)
   rather than short pulls, and count stalls that land inside a hold.
Create data/analyses/<today>-drive-log.md: a 3-line Glance (refreshed every
wake-up, the only thing the operator reads; line 1 = the maneuver you want
next, if any), then "For the operator", then the plan with each item tied to
the OUTCOME or marked hygiene. Timestamps come from `date -u`, never from
memory.

## First wake-up, before any capture
- Deploy only if the plan lists pending code: `git pull --ff-only origin main`
  on the Pi (bundle over ssh is the fallback); the Pi's
  data/nissan-xterra-2008/capture_profiles.yaml is usually dirty from the
  daemon's own auto-upgrade — diff it semantically, back it up to /tmp, reset
  it. Verify `git log -1`, healthy within 3 min.
- Boot check for D03.19: `journalctl -u canary -b | grep -c "batching
  disabled"` must be 0, and /status health.obd_scheduler.batching shows
  max_pids 6, size_narrowings 0. If the Pi booted key-off this is the fix's
  first real test: record the result either way.
- Memory limits: check /sys/fs/cgroup/system.slice/canary.service/memory.max
  EXISTS. `systemctl show` echoing the values proves nothing. Log which.
- Arm keepalive for the expected drive + 60 min. POST /control/captures with
  body {"protocol":"drive"} (nothing else is accepted); reopen after EVERY
  restart. Tags: POST /control/captures/current/tags {"label":"..."}.
- Poller that survives laptop sleep: run the 5 s `GET /status?view=compact`
  curl loop ON THE PI writing JSONL to /tmp (it is collection, 15 ms per
  request), recording time_connect and time_starttransfer; scp it at block
  boundaries. Gaps in any poller are uninstrumented, not zero: bridge them
  with cumulative counters and never quote a rate across one. Full /status
  once per block boundary.

## Every wake-up
1. Read today's log; resume, don't redo.
2. Health line with jq: vehicle_state, dropped records, storage queue depth,
   can0 rx_over_errors, inference misses, anomaly rate, achieved reads/s,
   batching size_narrowings, memory (cgroup memory.current, else RSS sum of
   the cgroup's pids). Flag counter resets. Log it.
3. A regression is diagnosed before anything else.
4. Otherwise the next item that moves the OUTCOME. Wake every 5 min during
   the enrichment set, 10-15 while a block runs, 20-30 while watching.

## The capture
- Profile fuel-pw-research (or the 3 Hz pull profile if the plan says it
  resolved feasible at the unchanged 90 req/s). Isolated capture: POST
  /control/capture {"profile": ...}, DELETE to stop. A NEW dids.yaml entry or
  a profile naming a DID not on the collector's startup list needs a daemon
  restart; a profile whose DIDs are all already catalogued saves live via PUT
  /control/capture/profiles. Mirror any live-saved profile into git the same
  hour.
- Start it only once coolant >= 85 C. Order: closed-loop plateaus (20 s holds
  near 60/90/110 km/h), then the enrichment set, then tip-ins held >= 5 s with
  lifts, then coasts in 3rd, 4th and neutral from ~3,000 rpm. Tag every
  physical action.
- Sample target: >= 30 stability-gated pairs with PID 0x44 > 1.05 (it dithers
  0.98/1.02 in closed loop, so 1.02 is not enrichment) from >= 3 independent
  pulls, i.e. ~30 s of held enrichment. Track the count live from the capture
  sample counters and keep asking for holds until it is met or the operator
  says no more.
- Stop the capture while still moving, read the stop verdict (every fail
  clause explained or made a task), pull the closed session to the laptop
  with `ssh ... nice -n 19 ionice -c3 tar cf -` and run the analysis within
  minutes: known answer first (DID RPM vs Mode 01 RPM 1:1, refuse if not),
  then the written predictions. Report pass / fail / unresolved per claim with
  n_pairs; an empty result names which kind of empty it is.
- Isolated captures <= one third of driving time; the rest is plain default,
  which is what the models train on and wins ties.

## Standing objectives
- Verify one stored belief first, with the cheapest block that could falsify
  it; correct memory and docs the same day.
- Desk analysis of data already on the laptop is the best use of warm-up and
  transit time; delegate it and code fixes to subagents so the loop stays
  light. Discovery only in service of the outcome, candidates until a
  compare_channels 1:1 against a known answer promotes them.
- Code: fixes for defects seen this drive, small, tested. You may deploy a
  good fix mid-drive and restart to check it — never while an isolated
  capture or the enrichment set is running; hash verified; drive window
  reopened; unhealthy after 3 min -> revert and redeploy; two failures ->
  stop deploying. Say what a restart can and cannot prove.
- Leave the operator a shorter decision list than you found; never take their
  decisions. Retire what you disproved instead of layering over it.

## Hard rules
- Never write non-volatile ECU memory: no 0x2E, 0x3B, 0x31, 0x34-0x37. Don't
  clear DTCs. 0x27 requestSeed only.
- No analysis on the Pi while it collects; pull only closed sessions.
- Never edit bindings.yaml, a feature spec, or the default/fuel-map profiles
  from the road. Catalog candidates at the lowest confidence rung only where
  a task says so. Never compare aggregates. No bus-budget raises. No replays
  on the Pi while the truck is collecting.
- Passive mode always with a TTL; disarm when the block ends. If UpRev is
  along: one parked warm window, CANary passive, logging INJ PULSE-B1 and
  base fuel schedule (D03.10) — after the capture, not instead of it.
- Default writes ~2,355 rows/s, so the trip-end digest budget (~1.92 M rows)
  is ~13.6 min: when the operator says they are ~10 min from parking, restart
  the daemon and reopen the window, so a small session can produce
  digest.json (D03.11). Skip if D03.6 has landed.
- Code gate before any commit: whole-repo ruff + mypy + the touched area's
  tests, targeted only; tests rewrite
  data/nissan-xterra-2008/capture_profiles.yaml, so back it up and restore
  your version. Explicit pathspecs. Work in the tree you didn't do stays
  untouched.

## Wrap-up and stop
When the operator says ~30 min remain: update the D03/D04 checkboxes with the
measurements, write the final summary at the top of the log, and start pulling
every closed session from today (the Pi may be powered off without warning).
Stop on: engine_off 20+ min (gas stops of 10-15 min are normal), the operator
saying so or powering the Pi off, or Pi unreachable 30+ min. Then: stop the
capture window if the Pi still answers; final summary = the OUTCOME's state
per claim (promoted, falsified, or unresolved and why), beliefs verified or
falsified, commits, candidates, the operator's decision list, and what the
next drive must produce; preserve the poller JSONL and scripts under
~/canary-data/; commit with explicit pathspecs; end the loop.
```
