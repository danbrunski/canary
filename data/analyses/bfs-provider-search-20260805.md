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
