# B07 — warmup validation and idle coverage

**Depends on:** B01/B02; B03 demotes the old catalyst artifact and handles
publication. Follow [B00](B00-behavior-models-README.md).

Keep the useful warm-idle baseline. Rebuild confidence in coolant/catalyst
warmup from actual cold starts rather than carrying old fits forward. The
review's catalyst exceedances establish a failed expectation, not a failed cat.

## Checklist

- [ ] **B07.1 — Verify warmup measurements before modeling them.** Check
  coolant, run time and catalyst-temperature availability, scaling, freshness,
  resets and plausible trajectories against known-answer captures/catalogs.
  Establish whether the catalyst channel is measured or ECU-estimated before
  making independent-health claims. Preserve the old 30/80 °C artifact and
  its failed September result as regression evidence, not a baseline to bless
  by merely applying the newer 45/115 °C bands.
- [ ] **B07.2 — Inventory independent genuine cold starts.** Record initial
  coolant, ambient/IAT, runtime continuity, stopped/moving history and load
  coverage per physical drive. Exclude warm restarts and mid-session fragments
  from cold-start evidence. Distinguish missing channels/sidecars from a drive
  that simply never contained an eligible start.
- [ ] **B07.3 — Apply the existing warmup decision.** Re-run
  `count_usable_cold_start_sessions`/`choose_warmup_confounding_strategy`.
  Follow the approved rule: at least eight usable cold-start sessions with
  load/IAT spread supports adding context; otherwise use the documented wider
  bands and limitations. B02 validation still applies: insufficient evidence
  means no trusted alerting model, not automatic qualification by broad bands.
- [ ] **B07.4 — Fit coolant warmup and cold-start idle appropriately.**
  Reuse current recipes with B01 cold-start/initial-temperature contracts.
  Restrict an idle curve to actual stationary eligible operation; moving
  warmup data must not redefine fast idle. Evaluate across distinct initial
  temperatures/load regimes, with independent held-out starts and labelled
  synthetic delayed warmup/idle-offset controls.
- [ ] **B07.5 — Decide whether catalyst warmup earns reinstatement.** Require
  verified input semantics, multiple held-out cold starts, meaningful error
  bounds and useful sensitivity after load/ambient context. Keep the current
  artifact quarantined if those criteria fail. Describe any qualified output
  as temperature-trajectory deviation; conversion efficiency/aging needs
  independent diagnostic evidence beyond this channel's trajectory.
- [ ] **B07.6 — Validate the retained warm-idle baseline's envelope.** Include
  settled warm idle, recent-motion/transient idle and available accessory-load
  observations, tagged by physical action. Check RPM bias/spread and normal
  load steps. Document which modes the baseline covers: an `idle` RPM ceiling
  must not be advertised as detection of all elevated idle. Assess a separate
  idle-roughness or broader idle-state residual only if the data and diagnostic
  need support it; avoid self-gating the target fault out of its own evaluation.
- [ ] **B07.7 — Report and publish the qualified subset.** Archive B02 reports,
  preserve explicit lack of coverage and use B03's rollout. Verify cold-start
  context reset across session/replay boundaries and unknown context after
  mid-drive daemon startup. Leave missing cold-start/accessory evidence named
  and unchecked rather than counting code completion as validation.

**Done when:** the retained idle baseline has a measured applicability envelope,
each warmup candidate either meets held-out criteria or remains visibly withheld,
and the legacy catalyst fit cannot reappear as trusted through carry-over.
