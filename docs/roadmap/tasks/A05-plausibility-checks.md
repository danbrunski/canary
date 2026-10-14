# A05 — deterministic plausibility checks

**Depends on:** A03.1's "sample arrived" flag for the stuck and freshness
checks; A02.4 to evaluate. No retrain. Follow
[A00](A00-anomaly-detectors-README.md).

Gross sensor faults are the cheapest thing to catch, and the current
detectors miss them. In the 2026-09-20 fault-injection benchmark the
Isolation Forest caught these (injected faults per kind: 50):

| Fault | Caught |
|---|---|
| Spike | 2 |
| Scale | 4 |
| Dropout | 5 |
| Stuck | 7 |
| Drift | 8 |

In one run it never flagged an RPM spike of five times the channel maximum
plus 1,000.

An isolation tree cannot tell "slightly beyond the training range" from "far
beyond it". Its split points come from the training data, so any value past
the training maximum takes the same path as the maximum itself. A
per-channel rule catches these faults by construction and says which channel
failed.

This is the OBD-II monitor pattern (per-component checks gated on operating
conditions, confirmed on repeat), and the second brain's deterministic layer.
Build it once, as channel metadata plus one evaluator, not as per-channel
code.

## Checklist

- [x] **A05.1 — One place for each channel's physical limits.** Limits
  already exist as data: `plausible_min`/`plausible_max` on broadcast
  bindings (`src/decode/bindings.py`) and memory channels
  (`src/analysis/memory_verification.py`), plus the encoding range of each
  Mode 01 PID. Today they are used only by analysis and verification.
  - Expose one resolver: channel → physical limits, slew limit, expected
    sample interval (from its poll tier), and the operating modes where each
    applies.
  - Keep the values in profile/catalog data; the Xterra's numbers go in its
    files, not in shared code.
  - A channel with no stated limit reports "unchecked", not "ok".
  - Evidence: `src/plausibility/limits.py` — `resolve_limits` merges binding,
    DID and memory `plausible_min/max` (decoded bounds converted to the raw units
    `decoded:` values arrive in), the J1979 Mode 01 encoding range computed from
    the registry's own decoders, `fsm.PHYSICAL_LIMITS`, and the vehicle's
    `data/nissan-xterra-2008/plausibility.yaml` (tier intervals, group silence,
    tighter ranges, slew/stuck rules, enabled modes, confirmation policy, each
    with a measured `reason`); tightest bound wins and names its source.
    `ChannelLimits.coverage()` reports `unchecked: <why>` per check, never `ok`.
    Mode names the segmenter cannot produce, unscaled decoded channels given
    engineering rules, and a missing confirm policy are load errors.
    Tests in `tests/test_plausibility.py`: `test_decoded_binding_limits_are_converted_…`,
    `test_the_tightest_bound_wins_and_names_its_source`,
    `test_a_channel_with_no_stated_limit_reports_unchecked_not_ok`,
    `test_profile_mode_names_must_exist_in_the_segmenter`,
    `test_the_xterra_profile_resolves_against_its_catalogs_and_modes`.
- [~] **A05.2 — Range check.** Flag a value outside its physical limits, per
  sample, naming the channel, value and limit.
  - Check against the raw sample, before resampling or forward-fill.
  - The first run should reproduce the known answer: every injected spike and
    scale fault in the 2026-09-20 benchmark is caught.
  - Done: the range check runs on every arrived sample *before* the quality gate
    (`LiveFeatureSource.sample_observer`, called ahead of `is_physically_valid`;
    offline, `src/plausibility/offline.py` rebuilds the arrived-sample stream),
    naming channel, value, limit and its source (`test_range_detection_is_pending_…`,
    `test_the_live_source_hands_every_value_to_the_observer_before_the_quality_gate`).
    It reproduces one known answer: the pre-`d947afe` decoder stored PID 0x0E's
    0xFF "not available" as 63.5 degBTDC, and the check confirms exactly that
    (14 confirmed records) on the 10 calibration drives recorded before the fix
    reached the Pi, and on no later drive.
  - Remainder — the benchmark's known answer does **not** reproduce, and the
    claim behind it was wrong: on its two drives (`2026-08-07T164617`,
    `2026-09-18T173749`, 5 runs x 5 channels, seed 42, arrived samples) spikes
    confirmed 45/50 and scales 20/40 graded (10 no-ops: SPEED at 0 x 5). Every
    miss is RPM or THROTTLE_POS at idle, where the fault stays physically possible:
    `2026-08-07T164617` never leaves idle, so 5 x max + 1,000 = 5,187.5 rpm is
    inside 0-7,000 and its 4,487 rpm step in 1.03 s is inside the 4,500 rpm/s
    slew; idle RPM x 5 ~ 3,500 rpm and closed throttle x 5 ~ 75 % likewise. A
    single-channel range cannot see these; see Operator decisions (A05.8).
  - **Operator decision 2026-10-13 (A05.8):** *the committees own RPM/THROTTLE
    faults at idle and stuck-at-cruise; plausibility keeps single-channel
    range/slew.* The known answer is therefore restated: every injected spike
    and scale fault *whose values leave the channel's range or slew envelope*
    is caught. The in-envelope idle misses are the committees' (A05.8 (a)). The
    restated answer holds where it has been run: on the committed 08-03 slice
    (`scripts/evaluate_committee_coverage.py`, A05.8 evidence), every spike and
    scale fault is plausibility-owned and confirmed, 25/25 each. The 5,187.5 rpm
    idle spike is classified as in-envelope (committee-owned) by
    `test_committee_coverage.py::test_the_idle_rpm_spike_a05_2_described_is_the_committees`.
    **Remaining:** run `scripts/evaluate_committee_coverage.py` on the two
    benchmark drives (`2026-08-07T164617`, `2026-09-18T173749`, in
    `~/canary-data`, not on the machine this ran on). That confirms by
    measurement that each of the 5 spike and 20 scale misses is in-envelope (owner `committee`)
    and that plausibility-owned spikes and scales confirm at 100 %. The by-hand
    analysis above says so; it has not been run through the classifier.
- [x] **A05.3 — Stuck check.** Flag a channel whose value has not changed
  across N consecutive *arrived* samples (A03.1), where the channel is
  expected to move in that operating mode.
  - Coolant at warm idle legitimately holds, and must not be flagged.
  - N and the "expected to move" modes are channel metadata.
  - Evidence: `PlausibilityEvaluator.observe` — `samples` consecutive identical
    arrived samples spanning `min_seconds`, only in the rule's `modes`, re-detected
    each time the run is as long again. A03.1's arrived flag does not exist yet;
    "arrived" is defined directly: one `observe()` call = one decoded collector
    reading or CAN frame, never a forward-filled value. N, `min_seconds` and the
    modes are profile data measured on calibration holds; coolant (warm idle) and,
    after the first evaluation pass, RPM and SPEED (steady cruise holds them exactly) carry
    no rule. Tests: `test_stuck_fires_only_where_the_channel_is_expected_to_move`,
    `test_stuck_needs_the_duration_as_well_as_the_count`,
    `test_coolant_holding_at_warm_idle_is_not_stuck_in_the_real_profile`.
- [x] **A05.4 — Freshness check.** Flag a channel that has not produced a
  sample within its poll tier's expected interval × k while its source is
  otherwise answering.
  - This is distinct from a whole source going stale, which
    `vehicle_state` already reports.
  - Reuse the existing `__gap`/freshness signals, don't duplicate them.
  - Evidence: `PlausibilityEvaluator.check_freshness` — age is the resampler's
    `__gap` age (`now - last arrival`) with the time the channel's answer group was
    suspended taken out (group answering clock; `groups.obd.silence_seconds: 4.0`,
    measured bimodal gaps), so a Mode 22 window or key-off is not a per-channel
    fault and a whole source going stale stays `vehicle_state`'s. Threshold = tier
    interval x `freshness_k` (fast: 33 s x 3, the scheduler's 30 s miss backoff;
    the priority PIDs retry at 10 s since 2026-10-13, and the 33 s is re-measured
    once drives recorded that way exist — A05.8).
    Only `collection.obd_priority_channels` are checked (others were measured absent
    176-1,912 s of answering time on clean drives), a channel needs 3 arrivals
    first, DIDs/RAM/broadcast report `unchecked` with the reason. The evaluator keeps
    its own last-arrival per channel because `_last_observed_at` skips values the
    quality gate rejects. Tests: `test_freshness_flags_one_silent_channel_…`,
    `test_freshness_ignores_a_whole_group_going_quiet_…`,
    `test_suspended_group_time_does_not_age_a_channel`,
    `test_a_channel_seen_once_is_never_stale`,
    `test_non_priority_mode01_channels_report_freshness_unchecked`.
- [x] **A05.5 — Rate-of-change check.** Flag a step between consecutive
  arrived samples that exceeds the channel's physical slew limit, scaled by
  the real time between them. Example: coolant cannot move 20 °C in a
  second.
  - Evidence: `|delta| > per_second * max(dt, update_interval_seconds) +
    allowance` against the previous arrived sample, `dt` the real time between them
    (floored at the source's refresh: two RPM reads 6 ms apart straddled one ECM
    refresh and stepped 450 rpm on calibration drives). Xterra limits measured:
    RPM 4,500 rpm/s, SPEED 40 km/h/s, coolant 2 degC/s, IAT 5 degC/s, baro
    0.5 kPa/s. Tests: `test_slew_is_scaled_by_the_real_time_between_arrived_samples`,
    `test_slew_floors_dt_at_the_sources_refresh_interval`.
- [x] **A05.6 — Enable conditions and confirmation.** Each check runs only in
  the operating modes where it is valid.
  - A single detection is *pending*; a repeat inside a configured window (or
    on the next drive, for slow checks) *confirms*.
  - Only a confirmed detection alerts, the same way OBD-II requires a repeat
    detection before lighting the malfunction indicator lamp (MIL).
  - Both states are stored.
  - Evidence: every check has per-channel `modes` (range/slew default: all;
    stuck: required; key-on broadcast states — coolant -40 degC, wheel speed
    253.9 km/h, the crank-period counter at engine stop — gated to running modes).
    First detection = `pending`; `repeats` detections at least
    `min_separation_seconds` apart inside `window_seconds` = `confirmed`;
    `across_drives` carries the previous drive's pendings (by write order, not
    session name) and confirms on the first repeat. Both states are written to the
    evidence JSONL; only `confirmed` sets `alerts` (warning log, `/status`
    `open_confirmed`). Tests: `test_range_detection_is_pending_then_confirmed_…`,
    `test_a_pending_detection_expires_outside_its_window`,
    `test_range_check_runs_only_in_its_enabled_modes`,
    `test_a_modes_only_range_rule_gates_the_catalog_bound`,
    `test_carried_pending_confirms_on_the_next_drive_for_across_drive_checks`,
    `test_carried_pending_is_ignored_for_in_drive_checks`.
- [x] **A05.7 — Emit evidence, not just alerts.** Every detection writes a
  record naming the channel, check, value, limit, operating mode and sample
  times. This is the shape the second brain's evidence store will use
  ([ECU second brain](../ecu-second-brain.md), capability 2). Dashboard text
  is rendered from the record.
  - Evidence: `src/plausibility/evaluator.py` `EvidenceRecord` (schema
    `plausibility-evidence/1`: state, channel, check, value, limit, limit kind and
    source, unit, engineering value/limit for raw `decoded:` channels, operating mode,
    sample times, detected-at, session, detection count, detail); `text` is rendered
    by `render_evidence` from the record. Live file
    `<anomalies_dir>/plausibility-evidence.jsonl`, replays
    `<anomalies_dir>/_replay/<id>.plausibility-evidence.jsonl`. Test:
    `test_evidence_round_trips_and_text_is_rendered_from_the_record`.
- [~] **A05.8 — Evaluate on A02's harness.** Required results:
  - 100 % of the plumbing faults (spike, scale, stuck, dropout, drift at
    today's magnitudes);
  - zero confirmed alerts on clean held-out drives;
  - the recall-versus-severity curve on A02.6's realistic faults, reported
    honestly, since range checks will miss subtle faults by design.
  - Measured with `scripts/evaluate_plausibility.py` (the live evaluator over
    arrived samples; A02.6's realistic faults via `synthetic_fault_eval.inject_realistic_fault`,
    imported, not edited), evidence `data/analyses/2026-09-23-a05-plausibility-evaluation.json`. Numbers measured on calibration
    drives (2026-07-18..08-07); evaluation drives: 38 drives / 14.27 h and 17
    captures / 4.36 h, meant as held-out but used to revise rules (below).
    - Clean: **0 confirmed on a second pass over the same 38 drives (rules revised
      on them), not held-out** — plus 0 on 17 captures. The first pass on these
      drives confirmed 4 (RPM/SPEED "stuck" at steady cruise, THROTTLE_POS
      freshness in a capture that polled it once, the crank-period counter at
      engine stop) and also had pending ENGINE_LOAD/THROTTLE_POS stuck detections;
      the A05.3/A05.4/A05.6 changes (no RPM/SPEED stuck rule, a stuck duration
      floor, 3 arrivals before freshness, the period counter gated to running
      modes) removed them. The margin is thin: the second pass's 1 pending is
      `2026-09-18T180630` THROTTLE_POS holding 8.63 % for 49 samples / 49.0 s in
      `city`, over its 30-sample / 40 s rule; a second such hold within 600 s
      would confirm. Calibration drives (78, 35.9 h): 14 confirmed, all the
      stored 63.5 degBTDC timing sentinel (A05.2).
    - Plumbing faults at today's magnitudes, 5 benchmark channels x 5 runs on the 38
      evaluation drives, confirmed / graded (no-ops excluded and counted): spike
      790/810 (97.5 %), scale 552/734 (75 %), drift 590/815 (72 %), stuck 75/641
      (12 %), dropout 74/815 (9 %). Misses are by construction: spike/scale inside
      the physical range at idle (A05.2); stuck only where the channel cannot hold
      (none on RPM/SPEED/coolant); freshness only on the priority PIDs, and a
      one-tenth-of-drive window is often shorter than 99 s + 33 s of Mode 01
      answering time.
    - Realistic faults (A02.6), 2 windows per kind per drive, confirmed / graded at
      every severity: maf_gain 0/40, lazy_o2 0/40, bank_imbalance 0/40, idle_air_leak
      0/25, thermostat 0/43 (pending slew detections 34/43 at 10 degC and 40/43 at
      15 degC, all at the injection's instantaneous end, an artefact of the
      injection). Range checks miss subtle faults by design, as expected.
  - Remainder: (1) **100 % of plumbing faults is not met** — the operator
    decision is now made (below); what remains is named under it.
    - **Operator decision 2026-10-13:** (a) *the committees own RPM/THROTTLE
      faults at idle and stuck-at-cruise; plausibility keeps single-channel
      range/slew.* "100 % of plumbing faults" is restated per detector:
      plausibility is judged on the faults inside its single-channel scope,
      the committees on the cross-channel cases. (b) *Shrink the scheduler's
      30 s miss backoff for the priority PIDs to ~10 s.*
    - **(b) done in the scheduler.** `PRIORITY_MISS_BACKOFF_SECONDS = 10.0`
      (`src/collection/obd_scheduler/cadence.py`): a priority request
      (`collection.obd_priority_channels`, or the active capture profile's own
      `priority_channels`, resolved by `request_is_priority`, the same rule slot
      selection uses) that has answered before retries 10 s after a miss;
      others keep 30 s, never-answered probes keep the 30 s → 900 s escalation,
      and a focused capture's 1 s retry now escalates only to 10 s for a priority
      channel. A policy constant beside `AUTO_MISS_BACKOFF_SECONDS`, not a
      vehicle fact. `priority` is a required argument of
      `_update_response_state`/`_miss_backoff_seconds`, so no call site can leave
      it out. Pinned: `test_can_obd_collector.py::test_a_priority_request_that_answered_retries_after_ten_seconds`,
      `::test_a_focused_capture_priority_miss_escalates_only_to_the_priority_ceiling`,
      and on the fake clock through the real loop
      `test_default_profile_core_rate.py::test_a_priority_pid_that_goes_silent_is_re_asked_every_ten_seconds[6|1]`.
      There THROTTLE_POS (priority) and SPEED both go silent while RPM answers:
      THROTTLE_POS is re-asked every 11.0 s batched or 12.0 s unbatched (10 s +
      the 1 s round or timeout), SPEED every 31-32 s. All four tests fail with
      the constant set back to 30.
      **The dropout bound the docs cite has not moved yet.** It is set by
      `plausibility.yaml`'s fast tier (33 s x `freshness_k` 3 = 99 s, then
      confirmation on the repeat, ~99 s + 33 s of answering time), and that 33 s
      was measured on drives recorded under the 30 s back-off. Every stored drive
      was. A shorter interval now would read their routine back-off gaps (up to
      69.1 s) as dropouts. The file keeps 33 s with the reason updated, and
      `test_plausibility.py::test_no_tier_interval_is_shorter_than_the_priority_miss_backoff`
      makes an interval below the back-off a failing test. Expected after
      re-measurement, not measured: ~10 s + the 1.0-2.7 s routine interval
      ≈ 13 s, so ~39 s to pending and ~52 s to confirmed.
      **Remaining (truck):** drives recorded with the 10 s back-off, then
      re-measure the priority PIDs' longest routine answering-time gap and set
      `tiers.fast.interval_seconds` from it.
    - **(a) measured, not assumed:** `scripts/evaluate_committee_coverage.py` grades
      both detectors on A05.8's own injections (same seed, generator and kind order
      as `evaluate_plausibility.py`, so the same windows and magnitudes). Committees
      run as replay runs them (`evaluate_committee_frame` over
      `resample_and_segment`'s frame, the fault applied to the cells where the
      channel arrived). Each fault gets an owner: `plausibility` if its values leave
      the resolved range or slew envelope (mode gates ignored, so generous to
      plausibility), if it is stuck on a channel with a stuck rule, or if it is a
      dropout where freshness runs; `committee` if it is an in-envelope
      spike/scale/drift on RPM/THROTTLE_POS, or stuck on RPM/SPEED; `unassigned`
      otherwise, reported and never folded into either denominator.
      Evidence:
      [`2026-10-13-a05-committee-coverage-slice.json`](../../../data/analyses/2026-10-13-a05-committee-coverage-slice.json).
      This is the committed 08-03 ground-truth slice: 420 s of driving, 5 channels
      x 5 kinds x 5 runs, `--assume-gps-fix` because the slice was cut without
      sats/HDOP. The 38 evaluation drives are not on the machine this ran on.
      Results:
      - **Committees cover no RPM or THROTTLE_POS fault at all.** No committee
        declares `obd:RPM` or `obd:THROTTLE_POS` as a member, so every fault on
        them is `uncovered` and the committee catch is 0 by construction. The
        only benchmark channel a committee reads is `obd:SPEED`
        (`SPEED_SOURCE_DIVERGENCE`).
      - Speed committee, on SPEED's faults: spike 1/5, drift 2/5, scale 3/5,
        stuck 1/5, dropout 0/5 (a dropout is `stale`, not a departure). It raised
        0 flags on the clean slice. The partial catch comes from the tolerance: Mode
        01 SPEED arrives every ~2 s, and a run needs ≥ 5 departed verdicts with
        ≤ 3 s between them. Ticks that fail the 0.05 g acceleration conditions
        give no verdict, so the run often restarts.
      - By owner, confirmed or caught / graded:
        - plausibility-owned: spike 25/25, scale 25/25 and drift 25/25 (the slice
          drives, so every RPM/THROTTLE spike/scale/drift left the envelope);
          stuck 0/10 (THROTTLE_POS and ENGINE_LOAD rules against 40 s windows);
          dropout 0/10 (RPM and THROTTLE_POS, 40 s windows under the 99 s
          threshold).
        - committee-owned: stuck 1/10 (SPEED 1/5; RPM 0/5, uncovered). No
          in-envelope RPM/THROTTLE fault arises on a driving slice, so the idle
          case is graded only by the known-answer test below.
        - unassigned: dropout 15 (COOLANT_TEMP, ENGINE_LOAD and SPEED; freshness
          watches only the priority PIDs) and stuck 5 (COOLANT_TEMP).
      - Known answer, A05.2's idle case:
        `test_committee_coverage.py::test_the_idle_rpm_spike_a05_2_described_is_the_committees`.
        A 5,187.5 rpm spike from 700 rpm at 1.03 s spacing stays inside the Xterra
        range and slew envelope, so it is the committees' fault. The same spike
        at 7,500 rpm breaches the range and is plausibility's. Also
        `::test_ownership_follows_the_decision_and_names_what_neither_owns` and
        `::test_on_the_real_slice_no_committee_reads_rpm_and_speed_is_caught_in_part`
        (pins the counts above).
      **Remaining:** (i) a committee that reads RPM and THROTTLE_POS. None exists,
      so the faults the decision gives the committees are caught by nothing today.
      The resampled frame already carries broadcast copies
      (`decoded:ECM_RPM`, `decoded:ECM_THROTTLE_SENSOR_V`) that could pair with
      the Mode 01 PIDs. Their relations and tolerances must be measured on
      known-good drives, as H04's rules require (H04 work). Note that a
      broadcast/Mode 01 pair of one sensor sees plumbing faults, not sensor
      faults. (ii) Run `evaluate_committee_coverage.py` on the 38 evaluation
      drives (`~/canary-data`, the A05.8 split) to get per-owner recall where
      idle and cruise actually occur. (iii) The unassigned faults (dropout of
      non-priority PIDs, coolant stuck, in-envelope drift on unnamed channels)
      are covered by neither detector under the decision.
    (2) ~~A02's harness itself was not used~~ — **closed in `b037a51`**: A02
    added a `RawStreamDetector` protocol and `PlausibilityDetector`
    (`src/models/evaluation_detectors.py`), and `plausibility` is a default
    baseline in `scripts/evaluate_detectors.py`, scored on the same rows as
    every other detector. The A02.11 table reads plausibility 0 FA/eh (0 %
    withheld) on 13 in-era held-out drives, and 0 recall on the five
    realistic (sub-range) faults, as designed
    ([baseline](../../../data/analyses/2026-09-23-a02-detector-baseline.md)).
    Those drives predate this rule set, so they are not unseen by it and (3)
    stands.
    (3) **Zero confirmed on unseen drives** — re-run
    `scripts/evaluate_plausibility.py --clean` on drives recorded after this rule
    set was frozen.
    **Met 2026-09-26: 0 confirmed on 12 unseen sessions, 1 pending**
    ([evidence](../../../data/analyses/2026-09-26-a05-plausibility-unseen.json)).
    - **Rules unchanged since the freeze:** `git diff 06a3188 HEAD` over
      `src/plausibility`, `plausibility.yaml` and the catalogs' `plausible_*`
      bounds shows only the confirmed-only `alert_sink`, the catalog
      `fingerprint`, the decoded `display_unit` and the P01/P02.8 offline
      reader changes. No limit, mode gate or confirmation policy moved.
    - **Sessions** (each starts after 2026-09-23T07:09Z; none revised the
      rules; 2.553 h in total, 1.867 h live and 0.686 h capture):
      `2026-09-23T145645`, `150658` and `162918`;
      `_capture-tuning-2026-09-23T142926` and `164007`; the 09-24 set in
      `~/canary-data/kline-compare-20260924` (`160323`, `161109`, `161151`,
      `161244`, and captures `161055`, `161137`, `161230`). Command:
      `--config canary.example.yaml --profile nissan-xterra-2008 --clean
      <12> --workers 4`. Every session reads `scored_quiet`.
    - **Which kind of quiet.** Real mode labels cover 1.877 h (`150658`,
      `162918`, `164007`, `160323`). There, every check ran, with 36–46
      channels range-checked. The other 0.676 h carries no mode gate. In
      `142926` the segmenter fell back to `all` because the tuning profile
      has no `THROTTLE_POS`. `145645` is almost all `unknown`: 22 OBD rows
      in 613 s, engine not running. The six short 09-24 sessions (183 s)
      have no OBD rows at all, only CAN, decoded, GPS and `kline` rows, and
      got `all` with 15 channels. In those sessions only
      ungated range checks ran. Stuck checks and the engine-running range
      gates did not.
    - **The margin is thin again.**
      `_capture-tuning-2026-09-23T164007` has one pending:
      `obd:mem:ECM_ENGINE_PERIOD` read 32,329 counts (limit 20,000) at
      16:52:33.100Z in `city`. RPM fell from 850 to 0 over 16:52:31–34Z, so
      this is an engine stop while the mode label still read `city`. The
      period counter at engine stop is the same shape the first pass
      confirmed. A second read of 32,329 at 16:52:34.028 came 0.928 s
      later, under `confirm.range.min_separation_seconds` (1.0 s). That is
      the only thing that kept it from confirming. The A05.6 running-mode
      gate does not cover the seconds before the mode label changes.
    - Criterion (1) was decided on 2026-10-13 (Remainder (1) above). The box
      stays `[~]` for that item's named remainders: a committee that reads
      RPM/THROTTLE_POS, the per-owner run on the 38 evaluation drives, and the
      re-measured fast tier from drives recorded with the 10 s back-off.
- [~] **A05.9 — Run it live and measure the cost.** Wire the evaluator into
  the live tick (or the collector's decode path for raw-sample checks).
  Measure per-tick cost on the Pi against the missed-tick budget before
  enabling it on a drive (Pi item).
  - Done: `plausibility.enabled` (`src/config.py`, default **off**) wires the
    evaluator into the live path — samples through `LiveFeatureSource.sample_observer`,
    freshness + cost in `LiveInferencePipeline._snapshot` (`src/daemon/plausibility_live.py`).
    Per-tick cost is `/status` `detectors.inference.stages.plausibility` (every sample
    observed since the last tick + the freshness pass) and the evaluator block is
    `detectors.inference.plausibility`; enabled without a `plausibility.yaml` it is
    withheld with the reason. Laptop: 1.84 us per sample; replaying
    `2026-09-18T173749` (674,052 records, 593 s) through the live source added
    4.2 ms of loop time per 1 s tick, stage median 2.5 ms / p95 3.3 ms.
    `/status` `detectors.inference` now always carries a `plausibility` key: `null`
    when off, `{enabled: true, withheld: <why>}` when enabled but the file is
    missing, malformed or the evaluator failed to start, the evaluator block when
    running.
    Tests: `test_plausibility_is_off_by_default_and_withheld_without_a_vehicle_file`,
    `test_an_enabled_evaluator_with_a_broken_file_is_withheld_not_off`,
    `test_an_unexpected_start_failure_is_withheld_not_off`,
    `test_live_plausibility_records_cost_and_evidence_per_tick`.
  - *Alert routing done 2026-09-25 (desk).* `PlausibilityEvaluator(alert_sink=)`
    receives confirmed records only — the pending/confirmed split is decided in
    the evaluator, so no caller can route a pending detection by forgetting to
    filter. `AnomalyEventEmitter.emit_plausibility` turns one into an
    `AnomalyEvent(source="plausibility")` (new `AnomalySource`; `detail.cause` is
    the evidence sentence, `detail.plausibility` the whole record, `score` 1.0 with
    no threshold like `dtc`, dedupe per `plausibility:<channel>:<check>`) through
    `_emit`, so tuning, dedupe, role, the anomaly log/broadcast and the ntfy push
    (never during replay, per-session cap) apply as for every source. A pending
    record is refused twice: `AnomalyEvent.from_plausibility` raises and
    `emit_plausibility` counts it `suppressed_not_anomaly`. An alert-sink failure
    never costs the evidence record. Tests (`tests/test_plausibility.py`):
    `test_a_confirmed_detection_is_an_anomaly_event_and_a_push`,
    `test_a_pending_record_can_never_become_an_event`,
    `test_plausibility_events_dedupe_per_channel_and_check_and_never_push_in_replay`,
    `test_an_alert_sink_failure_never_costs_the_evidence`.
  - *Daemon wiring done 2026-09-25 (desk).* `LivePlausibility.__init__` passes
    `alert_sink=` to `PlausibilityEvaluator`; `LivePlausibility.build` feeds it
    `components.emitter.emit_plausibility`. No `DetectorRolesConfig` key is
    needed: roles default to `alert` for any source not in `by_source()`, which
    is right for the deterministic layer. Plausibility is not capture-profile
    gated (R03.12c). Test (real `build_components`, the Xterra
    `plausibility.yaml`):
    `tests/test_plausibility_live_alerts.py::test_a_confirmed_detection_reaches_the_emitter_and_replay_never_pushes`
    — two out-of-range SPEED samples under an active `tuning` profile give one
    `source: plausibility` event in the anomaly log and one push; in replay mode
    the event is emitted and nothing is pushed.
  - Remainder: the Pi measurement against the missed-tick budget before
    enabling it on a drive.

## Acceptance

A05.1–A05.8 are desk work over stored sessions and replay. A05.9 needs the
Pi. Sensor committees (`src/analysis/committees.py`) remain the cross-sensor
check, and nothing here replaces them. A committee disagreement and a
plausibility failure on the same channel are two pieces of evidence, not one.
