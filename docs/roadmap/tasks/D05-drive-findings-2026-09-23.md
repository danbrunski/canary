# D05 — Drive findings, 2026-09-23 afternoon

A separate file on purpose: `D03-open-work.md` was mid-edit by another session
(D03.29) when these were found. Same conventions as D03: `[x]` only with the
measurement in hand and the session named. Fold into D03 when it is free.
Evidence: [drive log, "Drive 2"](../../../data/analyses/2026-09-23-drive-log.md).

- [x] **D05.1 Six of eight `tuning` DIDs were silent for the first 37.5 s of a
  capture.** `_capture-tuning-2026-09-23T164007` started at 16:40:07.5Z with the
  engine warm and running on `default`. First stored row, seconds after start:

  | channel | first row |
  |---|---|
  | DID 0x1206 (`ECM_INJECTOR_PULSE_1`), DID 0x1209 (`ECM_MAF_GPS`), all four RAM channels | 0.04 |
  | DID 0x1207, 0x1208, 0x1219, 0x1225 | 37.53 |
  | DID 0x120F, 0x1226 | 38.03 |

  For those 37.5 s the two live DIDs ran at 2 Hz each (77 rows apiece), so the DID
  sweep ran but with two members only. The journal has no DID, window or session
  line in that span. The capture verdict reports it as `missing_initial_s`
  ≈ 37.6 on each of the six, and it is why their coverage is ~0.91 against ~0.96
  on Mode 01.
  - **Not the same as the fixed defect.** `cdd355e` removed a 27.6 s wait-out of
    the old profile's window at a profile change, and was verified at 0.16 s on
    2026-09-22 (KOEO). The morning `tuning` start today (14:29:26Z) had every DID
    within 0.7 s. And the two DIDs that did answer are not simply `default`'s
    members: 0x1209 is in `tuning` only.
  - [x] Reproduce at a desk against a scripted ECU: switch `default` → `tuning`
    at several points in the default DID cycle, and assert every profile DID's
    first request goes out within one `did_cycle_seconds` (0.5 s).
    **Reproduced exactly (desk, fake clock, 2026-09-23).**
    `tests/test_capture_start_latency.py::test_every_profile_did_starts_within_one_cycle_of_a_switch_from_default`
    switches at 8 points of `default`'s 60 s window grid (0.1 / 0.15 / 0.25 s
    into a window, then 1, 15, 22.5, 45, 59 s after it). Before the fix all 8
    failed. At 22.5 s the four `default` DIDs first went out 38.0 s after the
    switch, and the `tuning`-only DID (stands in for 0x1209) at 0.006 s. The
    number matches the truck (37.53/38.03 s); the shape does not quite: on the
    truck 0x1206 (`ECM_INJECTOR_PULSE_1`), which the committed starter `default`
    polls, also answered at 0.04 s, where the repro puts every `default` member
    at ~38 s. **Not explained offline** (see the morning start below): either
    0x1206's due time was already past (never reached in the last `default`
    window, or re-anchored by an earlier miss), or the Pi's saved `default`
    catalog differs from the starter. After the fix all 8 pass, each DID ≤ 0.2 s.
    Checked against the pulled session: 0x1206 and 0x1209 have `round_id` 0 at
    16:40:07.537Z; the other six first appear at 16:40:45.029Z and 16:40:45.529Z.
  - [x] Find what admits only two members for ~37 s. Candidates to check, not
    conclusions: miss back-off carried over from `default` (six DIDs benched at
    once would fit D03.18's "benched 30–48 s"), rate tiering, or window budget.
    **Cause: cadence carried over, not back-off.** Under `default` a windowed DID's
    `next_due_at` sits on a 60 s grid (`obd_did_window_interval_seconds`).
    Nothing in the generation-change path touched per-request state, so
    `tuning`'s 0.5 s windows opened with only the members that were already due.
    Those were the DIDs `default` does not poll (0x1209), the RAM windows, and
    on the truck 0x1206 too (a `default` member; see sub-box 1 for why that part
    is not explained).
    `docs/sustained-validation.md` finding 3 had reported the same defect for
    Mode 01.
    **Fixed at source:** `obd_scheduler.replan_for_generation`, called once per
    generation change in `run_can_obd_loop`. Every selected pid/did/memory member
    becomes due now. A miss back-off is re-derived under the new profile's policy
    (new `_PollRequestState.missed_at`), so a `default` 30 s bench becomes the
    capture's 1 s retry. Neither value is ever lengthened.
    **A second defect of the same class turned up while reproducing and is fixed
    too.** A switch landing inside a `default` window or its exit grace left the
    new cycle-mode profile with no grid anchor, so
    `DidWindowController._schedule_next_window` fell through to the interval
    policy and deferred the first window 60 s (0.1 s and 0.15 s cases).
    Tests that fail without the fix and pass with it:
    `test_a_did_backed_off_under_default_is_not_benched_into_the_capture` and
    `test_a_slow_tier_pid_starts_within_one_cycle_of_the_switch[0.05|1.0|2.0|3.0]`.
    **Morning start (14:29:26Z, every DID within 0.7 s): not explained offline.**
    Under this mechanism a prompt start needs every `default` DID's grid point
    to fall at the switch. The drive log gives no cause for that. The
    pre-capture `default` session and the journal are not on the laptop, so
    the morning grid phase cannot be read.
  - [x] Add a counter or a log line when a profile DID is withheld at capture start,
    so the next occurrence says why. An empty channel must say which kind of empty.
    **`ProfileStartLedger` on the window controller.** For every Mode-22/RAM
    member of the profile now in force it records the seconds to the first
    request (`first_request_s`, filled in however late). One cycle after the
    first window may open (an ECU session lapse is not counted), every member
    not yet requested gets a reason: `backoff` (`for_s`, `missed_responses`),
    `cadence` (`due_in_s`), `no_window` or `budget`. The ledger logs once
    (`profile tuning: N of M Mode-22 window members withheld at profile start:
    …`) and counts by reason. Where it shows up:
    `/status.health.obd_scheduler.did_profile.profile_start` and the top-level
    `obd_scheduler.profile_start_withheld`, which the compact `/status` view
    carries too (`_COMPACT_SCHEDULER_FIELDS`;
    `tests/test_dashboard_api.py::test_status_compact_view_omits_the_per_channel_bulk`).
    Tests: `test_profile_start_ledger_names_what_withheld_a_member` (a
    never-answered DID shows up as `backoff`, 55 s left, 2 misses, logged once)
    and `test_a_start_the_ecu_never_answers_is_reported_as_no_window`.
  - [x] Verify on the truck: three `tuning` starts from a running `default`, every
    DID's first row within 1 s.
    **Verified 2026-09-24 16:19–16:22Z** (Pi at `2971d79`, engine running, warm
    idle, passive off, UpRev unplugged): three starts at different points of
    `default`'s cycle. In each, the ProfileStartLedger resolved with nothing
    withheld and all 10 members (8 DIDs + 2 RAM) first requested within
    0.106–0.112 s. In the stored captures `_capture-tuning-2026-09-24T161939`,
    `…162032` and `…162142`, every DID's first row is 0.17 / 0.14 / 0.23 s after
    the capture's first row. The earlier starts at 16:10–16:13Z ran with
    passive mode armed by another session (no CAN transmit), so they don't
    count and their three captures are void.

- [~] **D05.2 The trip-end digest summarised the wrong session.** Handed to the
  D03.29 owner on 2026-09-23, who folded it into D03.29 as a sub-item; tracked
  there, listed here only so the finding is not lost. The GPS trip end fired at
  16:57:33Z, 300 s after the last movement, and wrote `digest.json` for
  `2026-09-23T165316`. That is the 257 s parked `default` session the writer
  rotated to when the capture stopped. The drive itself, in
  `_capture-tuning-2026-09-23T164007`, was never digested.
  *Desk fix landed in `0d4b32b` (D03.29, deployed to the Pi 2026-09-23):* a
  trip's digest now goes to the session its stop was seen in, captures
  included, with a `trip` block naming every spanned session, and neither the
  edge nor the shutdown fallback digests a parked session the trip never
  touched. `tests/test_trip_speed_sources.py::test_a_capture_stopped_after_the_stop_digests_the_capture_not_the_parked_session`
  replays this exact sequence (`ended == [capture]`). **Still owed:** one real
  capture drive whose `digest.json` lands in the `_capture-*` directory.
