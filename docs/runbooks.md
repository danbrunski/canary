# CANary operator runbooks (Task 6.30)

Task-oriented "how do I…" recipes. Each gives the **dashboard path** and the
equivalent **CLI / `canaryctl`** command. `canaryctl` talks to the Pi's
authenticated control plane over HTTP (no SSH); it reads the host + token from
`~/.canary/env` or `--host` / `--token`. Read-only telemetry needs no token.

Honesty note: some flows are backend-complete but their dashboard panel or
live-ECU path is still pending — that's called out per runbook rather than
implied to work. Clear-DTC and proprietary diagnostic *tests* are deliberately
**not yet implemented** (Tasks 6.5 / 6.18); their safety contract is documented
here so the constraint is unambiguous.

---

## Collect data during this drive

Capture a labeled session for training/RE without SSH.

- **Dashboard:** Control view → *Captures* → Start (set purpose / vehicle
  state) → drive → Tag at events → Stop. (Control panel exists; richer
  capture UI is Phase 6B.)
- **CLI:**
  ```
  canaryctl capture start --purpose "highway baseline" --vehicle-state driving
  canaryctl tag "wot pull"
  canaryctl capture current
  canaryctl capture stop
  ```
- Pre-daemon / RE rig: `scripts/record.py` + `scripts/guided_session.py`
  (`rev` / `nt510` / `drive` protocols, physical-action tags).

## Debug a missing PID

"Why don't I see channel X?"

- **API:** `GET /obd/coverage` (no token) → each registry Mode 01 PID is
  `decoded` / `supported_not_decoded` / `unsupported`, plus poll tier,
  responded-recently, backed-off, and the explicit `undecoded_supported`
  gap list. `supported_not_decoded` = the ECU answers but CANary has no
  decoder yet (an honest gap, never hidden — Risk R7).
- A PID absent from the newest session's `obd:` channels and `unsupported`
  in coverage = the ECU doesn't report it on this vehicle. If `decoded` but
  stale, check `GET /status` → `health.data_freshness_seconds` and the
  scheduler/backoff state.

## Scan DTCs (stored / pending / permanent)

- **Decode/report (backend, no truck):** `src/decode/dtc_catalog.py` —
  `describe_dtc("P0420")` and `build_dtc_report(stored=…, pending=…,
  permanent=…, mil_on=…)` produce a described, timestamped, exportable
  snapshot (curated generic text + structural fallback for unknown/OEM
  codes).
- **Live read (Mode 03/07/0A) + dashboard panel:** pending — needs the
  in-truck ECU path. `CanObdClient.query_mode_03()` + `parse_dtc_payload`
  are the building blocks.

## Clear a DTC while parked

**Not implemented (Task 6.5).** When built, Mode 04 will be the *only*
write CANary makes and must be: behind the authenticated control plane,
parked-interlocked (never available while moving), explicitly confirmed in
browser/CLI, audit-logged, and followed by an automatic rescan. Until then
CANary is strictly read-only — use a dedicated scan tool to clear codes.

## Capture Nissan proprietary DIDs

- **Tooling (works today):**
  `scripts/record.py --obd-source canable --obd-dids 1208,F190 --obd-did-header 7E0`
  issues read-only Mode 22 reads over the Canable into `pids.jsonl`.
  `tools/scan_constant_dids.py --session C0` sweeps the `10 C0` DID space;
  `data/dids.yaml` + `src/decode/dids.py` decode known DIDs.
- **First-class live DID telemetry profiles + dashboard switching:** available
  in the Capture Profiles panel. `tuning-did` runs continuous Mode 22;
  `tuning-hybrid-research` uses the confirmed `10 81` fast exit to alternate
  4 Hz Mode 01 windows with Mode 22. `10 C0` remains mutually exclusive with
  Mode 01 while the session itself is open.
- **Do not generalize the ECM's CAN diagnostic protocol to other modules.** A
  2026-08-07 sweep found no Mode-22 response at guessed CAN request IDs
  `0x7E1`-`0x7E7` or `0x740`-`0x77F`; that proves only that those requests did
  not answer. The factory wiring shows the ABS on the normal pins 6/14 CAN
  network *and* on the pin-7 DDL2 K-line diagnostic path (`xterra_fsm/lan.pdf`
  LAN-6/54/62; `xterra_fsm/gi.pdf` GI-46/47; `xterra_fsm/brc.pdf` BRC-88).
  CONSULT-III supports both transports.
- **Per-wheel speeds + the seven unpinned DID scales:**
  `docs/abs-wheel-speed-and-did-gap-protocol.md`. Read the Results section
  first; the original plan's Phases 1-4 are superseded. `0x6F7` A/B/C are
  *not* per-corner — do not difference them to infer slip — and `0x6F7` bytes
  0-1 read a confident zero below ~4 km/h.

## Run another scan tool alongside canary

Expect canary to lose Mode 01 while the other tool polls the **ECM over CAN**.
Measured 2026-08-07 with a Z1 on an OBD-II Y-cable: while its generic OBD menu
polled, `0x7E8` responses to canary went to **zero** and the daemon backed off
from 6.84 to 2.88 Hz. This ECM serves one tester session and the second arrival
wins. Recovery is automatic once the other tool stops. The Z1's ABS screen is
different: it transmitted no CAN frames while live wheel speeds updated. It
either used pin-7 K-line—the factory-wiring-favored explanation—or decoded
existing passive CAN traffic. Do not assume that merely opening ABS data
monitor contests the ECM.

- Broadcast decode is unaffected, so `decoded:*` channels keep flowing.
- `vehicle_state` used to misreport this as `engine_off` — "plugged in, key
  out" — with the engine running. Fixed 2026-08-07; it now falls back to the
  broadcast rpm whenever the OBD rpm is stale, not merely absent.
- In the tested generic OBD menu, tell the two apart by ISO-TP padding on
  `0x7DF`: canary pads `0x00`, the Z1 pads `0xFF`.

## Probe for a module at an unknown address

`POST /control/obd_request` **cannot distinguish a negative response from
silence** — both return `responded: false` with a null payload. Verified by
pointing it at the ECM with DID `0x1101`, which is known to answer
`7F 22 80`. Until that is fixed, pair every probe with
`POST /control/can_activity` and look for the module's response id on the
wire; a bare `responded: false` is not evidence that nothing is there. This
endpoint is a CAN/ISO-TP probe and cannot discover a module whose diagnostic
path is on DLC pin 7.

## Run a parked diagnostic / actuator test

**Not implemented (Task 6.18).** Contract for when it lands: allow-listed
tests only, parked-interlocked, authenticated, countdown + confirmation,
audit log, timeout, post-test restore + rescan. **No** arbitrary raw
CAN/UDS send endpoint will ever be exposed.

## Label a false alarm

Mark an anomaly's verdict so tuning/retraining can use it.

- **Dashboard:** Anomaly timeline → an event already shows its current
  verdict inline (the `/anomalies` feed is feedback-annotated); the
  click-to-label control is Phase 6B.
- **API:**
  ```
  POST /control/feedback   {"event": <anomaly event>, "label":
      "false_positive", "reason": "A/C compressor cycle"}
  GET  /control/feedback   # current verdicts + summary
  ```
  Labels persist append-only next to the anomaly log (full relabel chain
  kept) and export via `feedback_to_csv` for training-set curation.

## Tune a threshold / suppress a known-benign alert

- **API:** `GET /control/tuning` → edit → **estimate before committing**
  `POST /control/tuning/estimate` (before/after FP count over recent
  anomalies, Risk R11) → `POST /control/tuning` (auto-versioned, audited,
  takes effect on the next inference tick) → `POST /control/tuning/revert
  {"version": N}` to roll back (revert is itself a new audited version).
- Suppression counts surface in `GET /status` →
  `emitter_counters.suppressed_tuning`.

## Author or review an accepted-baseline override

`accepted_baselines.yaml` (`docs/roadmap/online-training-strategy.md`
Phase 2) declares "this engine state is known and accepted going
forward" — a *recentering with a one-sided band*, not a mute. Use this
instead of a tuning suppression when the channel has a genuine,
diagnosed, ongoing fault you're not fixing (a weak injector, a lazy
thermostat) and you want to keep watching it get *worse* while ignoring
the already-known offset. A tuning suppression is the right tool for
noise (A/C compressor cycling); an override is the right tool for a
named, provenanced fault.

- **Author one (operator-only — never an unattended/overnight task):**
  edit the profile-scoped `<data>/<vehicle-slug>/accepted_baselines.yaml`
  (schema + a commented worked example in
  `src/decode/accepted_baselines.py`). Required fields: target (`channel`
  **or** `behavior_model`, never both), `band` (`tolerance`,
  `worse_direction`, `worse_warning`/`worse_critical`,
  `better_direction: ignore` — the only value today), and `provenance`
  (`declared_by`, `declared_at`, `review_by`, `reason`). A `channel`
  target also needs `accepted_center` (there's no fitted surface to read
  a center from); a `behavior_model` target's center is always the
  surface's own fit — never set `accepted_center` there.
  `scripts/disposition_report.py`'s Quarantine tier is where a channel
  persistently outside the this-engine baseline shows up as an override
  candidate in the first place.
- **Where it applies:** the live behavior detector consults
  `AcceptedBaselines.active_override_for` every tick
  (`src.daemon.inference._observe_behavior`) — the worsening side still
  trips warning/critical, the improving side never alerts, and the fired
  event's `detail.override_reason` echoes the declared `reason` so a
  recentered/suppressed channel always reads back *why*.
  `src.decode.behavior_decoder.deviation_for_override` is consulted
  directly by `_observe_behavior`'s channel-override branch for a
  raw-channel target evaluated on its own (no behavior model/surface in
  the loop) — the same per-tick call graded against `accepted_center`
  instead of a fitted expectation.
- **Review before it lapses:** `review_by` is non-negotiable — past that
  date the override goes inert (stops recentering/suppressing) rather
  than silently masking a *second*, newly-failing fault. `GET
  /status` → `accepted_baseline_overrides` is always present
  (`{"active": N, "overrides": [...]}`); the dashboard's StatusStrip
  shows "N accepted-baseline overrides active" at all times, expandable
  to each override's provenance + `review_by`, with an expired one
  flagged "needs re-confirmation" instead of dropped from view. Re-review
  and either bump `review_by` (still accepted) or delete the entry
  (fixed, or no longer acceptable) once flagged.

## Check whether the corpus is ready to retrain

- **CLI:** `scripts/dataset_health.py` (text) or `--json` (exit 2 = not
  ready). Per-session coverage/mode/value-range/gap/quality + a
  go/no-go verdict with reasons.
- **API:** `GET /control/datasets/health`.

## Export a training dataset

- **CLI:**
  ```
  scripts/export_dataset.py --session <id> --session <id> \
      --format parquet --format feature_matrix --resample-hz 1 \
      --purpose "lstm baseline"
  scripts/export_dataset.py --mark-candidate <id> --purpose "clean highway"
  ```
- Every export carries `dataset_manifest.json` (vehicle, config hash,
  channel schema, sessions, tags, time range, data-quality warnings —
  Risk R12) so a copy taken off the Pi stays reproducible.

## Retrain and publish a model

- **CLI (on the Pi or laptop):**
  ```
  scripts/retrain.py --days 14            # IF + LSTM, ledger-recorded
  scripts/ledger.py list                  # training-run history
  scripts/ledger.py publish <run_id> --status active
  ```
  The daemon's `ModelManager` hot-swaps a new artifact on its next poll
  (no restart) once the feature schema matches.
- **Remote (laptop → Pi, no SSH):**
  ```
  canaryctl jobs start retrain_all --days 14
  canaryctl jobs logs <job_id>
  canaryctl models upload <artifact> <manifest>
  ```
- Companion / offline: artifacts + history live in the local library
  (`~/.canary/library`, Task 6.32); the publish queue is Task 6.37.

## Online training: enable the scheduler, promote, roll back, re-declare golden

Phase 3 of `docs/roadmap/online-training-strategy.md` ("closing the loop") —
a config-gated daemon task that runs `retrain.py` on a cadence, a
champion/challenger promotion gate, and a frozen golden baseline to measure
drift against. Every step below is an *explicit operator action*; nothing in
this runbook happens automatically except the scheduled retrain call itself
(and even that always lands `unpublished`, never `active`).

- **Enable the scheduler (`online_training:` block, ships `enabled: false`
  — a hard pre-made decision, not a bug):**
  ```yaml
  online_training:
    enabled: true
    interval_seconds: 86400      # daily
    window_days: 30              # rolling --since window, not a fixed corpus
    poll_interval_seconds: 5
  ```
  Requires a daemon restart to pick up (like any other structural config
  change — see `poll_config`'s hot-reload limits). Once enabled,
  `run_online_training_scheduler` (`src/daemon/online_training.py`) submits
  a `retrain_all` job through the existing `JobController` every
  `interval_seconds` — the same subprocess-isolated mechanism the
  dashboard's manual "Retrain" button already uses, so a scheduled retrain
  never runs on the daemon's event loop or blocks live capture.
  - **systemd-timer alternative** (documented here, not installed by any
    script in this repo — this repo doesn't install systemd units from
    code): a `canary-retrain.timer` firing `scripts/retrain.py --since
    $(date -d '30 days ago' +%Y-%m-%d)` on the same cadence is equally
    valid if you'd rather keep the daemon itself simpler and drive retrains
    from cron/systemd instead. Either path must always reserve a
    calibration holdout — never add `--no-calibration-holdout` to a
    unit/timer meant to run unattended.
- **Observe a scheduled run:**
  ```
  scripts/ledger.py list                    # unpublished runs show up here
  tail -f data/control/jobs/<job_id>.log     # or canaryctl jobs logs <job_id>
  ```
  A window too small to spare a calibration holdout (Task 3.1's guard)
  makes `retrain.py` exit with a distinct code rather than degrading to
  in-sample calibration — the scheduler logs that as a routine skip
  (`online-training: scheduled retrain skipped`), not an error. A genuine
  trainer failure logs at `ERROR` instead — check the job log for the
  underlying exception.
- **Promote a challenger** (never automatic — this is the explicit action
  the gate requires):
  ```
  scripts/ledger.py promote <run_id>
  ```
  Prints the full gate verdict (calibration stamp, contamination sanity,
  a quarantine-fraction trend check against recent scheduled runs, recall
  non-regression vs golden on the fault-injection benchmark, held-out FP/hr
  budget, holdout score agreement) and only sets the run `active` if every
  check passes. An artifact whose manifest is stamped `calibration:
  in_sample` is categorically rejected — no override flag. No golden run
  declared yet means promotion always fails closed (see below).
  - **The held-out FP/hr check is an alert-rate budget, not a verdict on
    its own** (2026-07-20 review amendment): it runs on *unlabeled* held-out
    data, so a breach doesn't mean "the challenger is worse" — it can just
    as easily mean the held-out window contains a genuinely developing
    fault the challenger is (correctly) alerting on more than golden did.
    Treat a breach as "go read the alerts the held-out window produced";
    only block/roll back once you've actually looked, not on the number
    alone. Same posture, independently, for a **sustained rise** in the
    quarantine-fraction trend check — it flags "this window's health may be
    shifting," a prompt to inspect the disposition report over the
    candidate's input sessions, not an automatic contamination verdict
    (that's what the flat `contamination_sanity` reject-fraction ceiling
    is for).
- **Roll back:** promotion doesn't add a new mechanism here — the
  pre-existing ungated escape hatch still applies:
  ```
  scripts/ledger.py publish <run_id> --status rolled_back
  ```
- **Declare (or re-declare) the golden baseline** (an explicit,
  per-vehicle-profile operator assertion — **never** auto-set by any code
  path, including a scheduled retrain or a promotion):
  ```
  scripts/ledger.py golden <run_id>
  scripts/ledger.py golden-status <model_type>   # live-vs-golden divergence
  ```
  `golden-status` surfaces "normal has drifted a lot — why?": the current
  active run's held-out clean-data alert rate vs the frozen golden run's,
  per detector. A large divergence is a prompt to go look, not itself an
  alert. Declaring the *actual* golden run for the real vehicle profile is
  an operator morning task (Round-2 pre-made decision) — nothing overnight
  declares one, and none is declared on this corpus yet.
  - `ledger.py golden <run_id>` also re-freezes that model_type's
    disposition baselines (Task 3.7) from the run's recorded input
    sessions — median/MAD per `(channel, operating_mode)` for continuous
    channels, the observed value set for discrete/bitmask ones (Task 1.6).
    `classify_dispositions` then screens future candidate windows against
    *those* frozen numbers instead of recomputing from each window (which
    can't see a slow drift that's dominating the very window being
    screened — it just becomes the new "normal"). Best-effort: an
    unreadable/archived input session just means the freeze is skipped
    this time, falling back to window-local screening for that model_type.
  - **Re-declaring golden periodically is routine seasonal maintenance,
    not an anomaly response** (2026-07-20 review amendment): winter intake
    temps, a fuel-blend change, and similar legitimate drift will
    eventually push any honest challenger outside agreement-with-golden
    tolerance — that's expected adaptation, not a sign golden itself is
    stale from decay. Re-declare on a seasonal cadence (or after a known
    vehicle-state change — see the vehicle ledger's `corpus_boundary`
    events) the same deliberate way you'd re-baseline any calibrated
    instrument, not only when something looks wrong.

---

See also: `docs/headless-api.md` (endpoint reference + SSH-tunnel dev),
`docs/can-re-workflow.md` (RE capture + analysis), `docs/pi-setup.md`
(deployment), `docs/companion-app-plan.md` (offline workflow).
