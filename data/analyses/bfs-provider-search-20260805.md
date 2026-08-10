# BFS provider search from existing captures — 2026-08-05

## Question

Can an already-recorded DID or passive CAN field provide the Base Fuel
Schedule (BFS) axis needed to look up commanded AFR in the current ROM's Fuel
Targets table, without requiring a new UpRev log?

## Ground truth and clock alignment

Two independent 2026-07-27 windows recorded UpRev and CANary simultaneously:

| CANary session | UpRev CSV | measured clock correction | RPM correlation |
|---|---|---:|---:|
| `2026-07-27T152444` | `UpRev_Log7-27-2026_9-59-48_AM.csv` | +1.3 s | 0.99982 |
| `2026-07-27T225711` | `UpRev_Log7-27-2026_5-02-24_PM.csv` | +1.6 s | 0.99989 |

The corrections are the values added to the filename-anchored UpRev clock.
They come from `src.analysis.clock_align.align_clocks`, not filename matching.
UpRev `B-FUEL SCHDL (ms)` is the reference.

## Passive broadcast sweep

Every one- and two-byte slice of every observed passive CAN frame was binned
at 200 ms and regressed against BFS. Within-window slope stability and the
second independent window were required; correlation alone was not accepted.

No field passed. The best physically plausible candidate remains
`0x233 byte 2` (`ECM_AIRFLOW_233_B2`):

| window | R2 | raw-to-BFS slope | offset | within-window slope verdict |
|---|---:|---:|---:|---|
| AM | 0.8904 | 1.29648 | 2.97389 ms | drifting, 33.94% |
| PM | 0.8267 | 1.15391 | 4.32210 ms | drifting, 30.52% |

An AM fit predicting the PM window gives R2 0.7676 and mean absolute BFS error
1.914 ms. That error spans meaningful Fuel Targets columns, so this byte is an
airflow-related cross-check, not an automatic BFS provider.

`0x23D byte 1` scores slightly higher in the AM window, but it is independently
confirmed accelerator-pedal voltage. Its apparent BFS fit is ordinary
driver-demand correlation and degrades in the PM window. It must not be
renamed or promoted.

## Other stored paths

- Mode-01 MAF is sparse during the paired windows and does not map stably to
  BFS in engineering g/s. This is consistent with the UpRev guide: “current
  MAF value” in the basic `MAF value * K` description refers to the ECU's
  internal MAF-table quantity, not necessarily the logged g/s output.
- The paired July windows contain no `DID_1208` or `DID_1209`, so they cannot
  bridge injector/fuel sibling DIDs to the BFS reference.
- No stored Parquet session contains `DID_1209`. The drive-sweep JSON artifacts
  contain snapshots of `0x1209`, but no simultaneous BFS/target reference.
  `0x1209` remains the highest-value candidate for a future narrow research
  capture, not a provider inferred from snapshots.
- The post-flash August 4 UpRev imports confirm that the current tune reaches
  BFS 17.8–18.3 ms and validate the supplied Fuel Targets orientation. They do
  not overlap dynamic DID capture: the AM logger stalls after 1.37 seconds and
  resumes after CANary's relevant window; the PM log has no paired CANary DID
  window.

## Existing all-DID captures

`_capture-tuning-did-2026-08-06T003148` contains no physical pull tags and
reaches only 3503 rpm (95th percentile 2799 rpm). Its throttle-sensor voltage
peaks at 3.24 V rather than the ~4.58 V range seen in the paired pedal/throttle
work. It is useful for measured AFR, injector-PW, cam, and rate analysis, but
not for reconstructing a WOT delivery-error surface.

`_capture-tuning-did-2026-08-06T034041` is engine-off data (RPM remains zero),
so it contains no fueling lookup evidence.

## Disposition

The existing corpus supports the ROM artifact and an honest partial report,
but not exact Canary-native commanded AFR for an all-DID drive. Keep BFS
unavailable in the semantic capability report. Do not substitute actual
injector PW, calculated load, pedal, throttle, or `ECM_AIRFLOW_233_B2`.

The next evidence-efficient capture is a narrow BFS research profile containing
`0x1209`, `0x1208`, RPM, throttle/pedal evidence, and passive CAN. A successful
UpRev BFS trace is optional corroboration; without it, a deliberately changed
ROM target region can provide Canary-native causal identification.

## Update 2026-08-10 — BFS as a *derived* quantity, not a found one

The searches above ask "which stored field **is** BFS". Both came back empty
(passive sweep here; all-DID rung sweep 2026-08-08). Since moving off UpRev
logging is an objective rather than a convenience, the next question is whether
BFS can be **computed** from quantities CANary already has.

**Hypothesis.** BFS is a per-injection base fuel time, so it should track air
mass per intake event, not raw airflow. The earlier attempt above regressed BFS
against MAF in engineering g/s and found no stable map — but g/s is a *rate*,
and dividing by rpm is the difference between the two. For a V6,
`air_per_event = MAF_gps * 20 / rpm`.

**First pass, 12 UpRev logs carrying BFS and MAF g/s on the same K-line
transport** (single clock, so no cross-source join; restricted to steady state,
`|d rpm/dt| < 100 rpm/s` and `|d MAF/dt| < 5 g/s²`, because UpRev is
rotation-polled and its channels do not share a timestamp):

| log | n | r² | slope ms/(g/event) | offset ms | within-log slope drift | median err |
|---|---:|---:|---:|---:|---:|---:|
| 7-26 4:53 PM | 1181 | 0.897 | 24.53 | +0.552 | 78.5 % | 1.5 % |
| 7-29 6:10 PM | 133 | 0.996 | 30.83 | +0.020 | 64.5 % | 1.4 % |
| 8-08 4:48 PM | 1795 | 0.887 | 26.89 | +0.457 | **4.6 %** | 1.7 % |
| 8-08 5:12 PM | 4271 | 0.907 | 26.82 | +0.429 | **5.1 %** | 1.6 % |
| 8-08 5:26 PM | 2543 | 0.958 | 28.85 | +0.172 | 152.2 % | 1.0 % |
| 8-08 6:56 PM | 1596 | 0.965 | 28.99 | +0.195 | 15.4 % | 1.1 % |
| autosave 7-27 | 1572 | 0.994 | 29.50 | −0.025 | 37.6 % | 1.5 % |

**Verdict: promising, NOT yet a provider.** Within a log the fit predicts BFS
to **1.0–1.7 % median error**, which is the same order as the delivery-error
noise floor (1.25 %). But the slope spans **24.5–30.8 across logs (23 %)** while
K moved only ~1 % over the same period, so something other than K dominates the
scale, and within-log drift reaches 152 % on the drives. A constant cannot be
pinned from this.

**Do not promote this to a semantic BFS provider on these numbers.** Ranking by
r² would have — six of seven logs clear 0.88 — which is exactly the failure mode
`scale_verdict` exists to catch.

**What to try next, in order.**

1. The UpRev guide says "MAF value" in `BFS = MAF value × K` is the ECU's
   **internal MAF-table quantity**, not the logged g/s. Fit
   `air_per_event → BFS` with IAT as a second term: a density correction is the
   most likely missing factor and IAT is logged on both sides.
2. Separate open- from closed-loop samples before fitting. Base schedule is
   pre-correction, so closed-loop rows may carry a different effective scale.
3. The two lowest-drift logs (8-08 4:48/5:12 PM, both ≈ 4.6–5.1 %) are the
   steady rung captures. If the residual scale is an artifact of transients
   surviving the steady-state gate, a rung-only protocol across a wider rpm ×
   load span should pin it — and that protocol also runs CANary Mode 22
   simultaneously, so `ECM_INJECTOR_PW_B1` can be fitted in the same window.
4. Injector pulse width (`0x1208`, CANary-native, already carrying a fuel-mass
   constant of 914943 counts/g from n=411) is the other candidate for BFS's
   *job* — confirming a fuel edit landed — even if it never reproduces BFS's
   *value*. Test both from one capture; they need the same window.
