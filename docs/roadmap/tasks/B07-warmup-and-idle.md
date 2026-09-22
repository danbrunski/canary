# B07 — warmup validation and idle coverage

**Depends on:** B01/B02; B03 demotes the old catalyst artifact and handles
publication. Follow [B00](B00-behavior-models-README.md).

Keep the useful warm-idle baseline. Rebuild confidence in coolant/catalyst
warmup from actual cold starts rather than carrying old fits forward. The
review's catalyst exceedances establish a failed expectation, not a failed cat.

## Checklist

- [x] **B07.1 — Verify warmup measurements before modeling them.** Check
  coolant, run time and catalyst-temperature availability, scaling, freshness,
  resets and plausible trajectories against known-answer captures/catalogs.
  Establish whether the catalyst channel is measured or ECU-estimated before
  making independent-health claims. Preserve the old 30/80 °C artifact and
  its failed September result as regression evidence, not a baseline to bless
  by merely applying the newer 45/115 °C bands.
  *Done 2026-09-22* on a committed slice of the 2026-09-18 cold start
  ([`src/analysis/warmup_checks.py`](../../../src/analysis/warmup_checks.py),
  `tests/test_warmup_inputs.py`, report
  [2026-09-22](../../../data/analyses/2026-09-22-lambda-delivery-and-warmup-inventory.md)):
  **run time** is a seconds-since-start clock — 164 reads, median rate 0.992 s
  per wall second, 0 resets, 0 gaps > 30 s, first answer 20 s (which is what
  places this start), max 971 s. **Coolant** identity is not re-derived: the
  ground-truth suite already asserts the broadcast binding and PID 0x05 agree
  1:1. **Catalyst temperature is ECU-estimated, not measured** — three
  independent per-sample indicators, two required for the verdict: its first
  reading is 0 °C while intake air reads 16 °C at the same instant, the two
  banks return identical values on 78 % of 45 same-instant pairs, and readings
  land only on a 4 °C grid (PID resolution 1 °C). Consequence carried into
  B07.5: a qualified output there is a temperature-trajectory deviation, never
  a conversion-efficiency or aging claim. The legacy artifact is preserved as
  regression evidence (version 4, bands 30/80 °C, `validation_sample_count: 0`,
  quarantined to `display`) and
  `::test_the_legacy_catalyst_artifact_is_preserved_and_quarantined` fails if
  the newer 45/115 °C bands are applied to it. *Correction to the B02 report:*
  `cat_warmup`'s eleven May training sessions **are** present locally under
  `~/canary-data/<name>`; the report listed them as missing, which is why its
  catalyst row carries no baseline.
- [x] **B07.2 — Inventory independent genuine cold starts.** Record initial
  coolant, ambient/IAT, runtime continuity, stopped/moving history and load
  coverage per physical drive. Exclude warm restarts and mid-session fragments
  from cold-start evidence. Distinguish missing channels/sidecars from a drive
  that simply never contained an eligible start.
  *Done 2026-09-22:* [`src/analysis/cold_start_inventory.py`](../../../src/analysis/cold_start_inventory.py)
  and [`scripts/inventory_cold_starts.py`](../../../scripts/inventory_cold_starts.py),
  judged per **engine run** by the serving rule (`EngineStartTracker`, not a
  copy) and grouped into drives by parsed start time (`group_drives`). Over
  `~/canary-data` (170 session directories, replays excluded, duplicate pulls
  deduplicated; 21 s): **12 cold starts** in 12 independent drives, initial
  coolant 5-39 °C / IAT 8-39 °C, all in non-capture sessions; 30 warm restarts
  and 52 starts with **unknown** initial temperature (recording began
  mid-run) excluded by name; 24 sessions with no polled rows and 28 missing
  the contract channels reported as their own reasons. Ambient is *missing*,
  not zero — no profile in this corpus polls `AMBIENT_AIR_TEMP` — and only 6
  of the 12 can say the vehicle was stopped beforehand. **The sidecar
  over-reports:** of 122 sidecars, 15 disagree with the serving rule and 14 of
  those claim a `cold_start` the contract refuses (e.g. `2026-08-05T031055`,
  whose RPM never leaves 0 — key-on, engine off); 11 predate `f8b7d38`. A
  recipe gated only on `require_session_events=("cold_start",)` would train on
  those 14. Evidence: report §6, `tests/test_warmup_inputs.py` (committed
  cold-start slice, mid-drive and warm-restart cases, two runs in one
  recording, drive grouping, the microsecond-timestamp regression for the
  start window, and the per-directory deduplication the scan needs).
- [~] **B07.3 — Apply the existing warmup decision.** Re-run
  `count_usable_cold_start_sessions`/`choose_warmup_confounding_strategy`.
  Follow the approved rule: at least eight usable cold-start sessions with
  load/IAT spread supports adding context; otherwise use the documented wider
  bands and limitations. B02 validation still applies: insufficient evidence
  means no trusted alerting model, not automatic qualification by broad bands.
  *Decision re-run 2026-09-22:* 6 of the 12 cold-start drives carry the
  required load/IAT spread during warmup against a floor of 8, so
  `choose_warmup_confounding_strategy` returns **`widen_bands`** — the same
  answer as the July note, now from a current per-drive count (report §7).
  **Remaining:** applying it is a refit — deferred: needs the operator's
  corpus choice (2026-09-22), and the warmup recipes additionally cannot
  refit until D03.22 relabels the corpus (event gating rejects sidecars older
  than `f8b7d38`); 2 more usable cold-start drives are needed for a context
  axis.
- [ ] **B07.4 — Fit coolant warmup and cold-start idle appropriately.**
  Reuse current recipes with B01 cold-start/initial-temperature contracts.
  Restrict an idle curve to actual stationary eligible operation; moving
  warmup data must not redefine fast idle. Evaluate across distinct initial
  temperatures/load regimes, with independent held-out starts and labelled
  synthetic delayed warmup/idle-offset controls.
  *Deferred: fitting — needs the operator's corpus choice (2026-09-22) and
  the D03.22 relabel; held-out starts exist (12 drives) but only 6 with
  load/IAT spread.*
- [ ] **B07.5 — Decide whether catalyst warmup earns reinstatement.** Require
  verified input semantics, multiple held-out cold starts, meaningful error
  bounds and useful sensitivity after load/ambient context. Keep the current
  artifact quarantined if those criteria fail. Describe any qualified output
  as temperature-trajectory deviation; conversion efficiency/aging needs
  independent diagnostic evidence beyond this channel's trajectory.
  *Deferred: needs a fit. One input it lacked is now settled — B07.1 shows
  the channel is ECU-estimated, so any qualified output is a temperature-
  trajectory deviation only. Its eleven May training sessions ARE in the
  local corpus (contrary to the B02 report), so a baseline is computable
  once fitting is allowed.*
- [ ] **B07.6 — Validate the retained warm-idle baseline's envelope.** Include
  settled warm idle, recent-motion/transient idle and available accessory-load
  observations, tagged by physical action. Check RPM bias/spread and normal
  load steps. Document which modes the baseline covers: an `idle` RPM ceiling
  must not be advertised as detection of all elevated idle. Assess a separate
  idle-roughness or broader idle-state residual only if the data and diagnostic
  need support it; avoid self-gating the target fault out of its own evaluation.
  *Deferred: needs evaluation against the published artifact plus accessory-
  load observations that the corpus does not carry (no tagged A/C or
  electrical-load states).*
- [ ] **B07.7 — Report and publish the qualified subset.** Archive B02 reports,
  preserve explicit lack of coverage and use B03's rollout. Verify cold-start
  context reset across session/replay boundaries and unknown context after
  mid-drive daemon startup. Leave missing cold-start/accessory evidence named
  and unchecked rather than counting code completion as validation.
  *Deferred: publication (operator's corpus choice, 2026-09-22).*

**Done when:** the retained idle baseline has a measured applicability envelope,
each warmup candidate either meets held-out criteria or remains visibly withheld,
and the legacy catalyst fit cannot reappear as trusted through carry-over.
