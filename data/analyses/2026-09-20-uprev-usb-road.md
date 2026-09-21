# UpRev USB/K-line live road experiment — 2026-09-20

## Conditions and instruments

- Vehicle: 2008 Nissan Xterra, engine running, sustained road drive with
  natural acceleration/deceleration; GPS showed about 100 km/h near the first
  full scan and later windows covered wider load/RPM changes.
- Cable: Cipher/UpRev `0403:ccb8`, serial `12345678`, attached from Windows to
  WSL through USBIP and opened directly with PyUSB.
- CANary: Pi session `2026-09-20T175050`. Active CAN diagnostics were put in
  TTL-bounded passive mode because CAN and K-line share ECM diagnostic-session
  state. Broadcast CAN, decoded broadcast rows, GPS, and storage stayed live.
- Status interpretation: `/status.vehicle_state` continued to say
  `engine_running`, but its OBD RPM/coolant/run-time fields became stale by
  design under passive mode. The evidence that collection remained live is
  `can_freshness_seconds=0`, fresh GPS, and zero telemetry-sink drops.
- Pairing: every claimed cross-instrument relationship below uses
  `src.analysis.compare.compare_channels` or
  `src.analysis.broadcast_discovery.sweep_session`, never window aggregates.
  Cable/PI timestamps are stored host-receive timestamps. Policies were 50 ms
  for decoded references and 20 ms for raw broadcast sweeps; actual median
  separation was 2-3 ms and p95 about 5 ms.

Raw cable captures are quarantined from training at:

```text
/data/canary/_uprev-usb/2026-09-20/
```

Local analysis reports during the experiment were written under `/tmp` as
`uprev-*-analysis-20260920.json`.

## Transport/session result

The Linux path is live, not just a parser. It performed the FTDI/UPRV cable
handshake, the captured cold ECM bootstrap, support-mask discovery, individual
DID reads, dynamic-local-ID definitions, and repeated stream polls.

The eight support masks advertised 75 readable DIDs:

```text
1101 1102 1103 1106 110A 1110 1111 1114 1115 1117 111A 111B
1123 1124 1125 1126 112B 112D 112E 1135 1138 1139 1144 1145
1146 1147 1148 114E 114F 1159 115A 1163 117B
1201 1203 1204 1206 1207 1208 1209 120B 120D 120E 120F 1210
121A 121B 121C 1221 1222 1223 1225 1226 122A 122B 1246 1247
1248 1249 124A 1250 1302 1303 1304 1305 1306 1307 1308 1309
130A 130B 130C 130D 130E 1313
```

Every advertised DID answered an individual read and therefore has a measured
one- or two-byte response width. One definition containing all 75 DIDs timed
out; seven definitions of at most 12 DIDs streamed all of them cleanly. The
main runs produced 36,071 usable stream replies, including 15,000 consecutive
mixed-DID replies in the longest run.

One run ended after 871 samples with an FTDI bulk-write timeout. The cable was
still enumerated and the next ping first returned the final queued stream
reply, then `06`. Recovery was therefore a stale-reply ordering defect, not a
detached cable or dead ECU. `UpRevUsbTransport.cable_ping()` now drains complete
checksum-valid stale replies until the ping acknowledgement; the regression
test uses the exact observed stream shape. A 3,000-reply retry and the later
15,000-reply run both completed cleanly.

## Confirmed telemetry

### DID 0x1201 — coarse RPM

The captured UpRev parameter definition identifies `0x1201` as engine RPM at
`raw * 12.5`. The live 15,000-sample run provided the independent known-answer
check against broadcast RPM:

```text
did:0x1201 -> decoded:ECM_RPM
n=14,991, r2=0.99024, slope=0.995552, offset=15.917 rpm
scale drift=1.32% (stable), difference=+4.918 +/- 36.42 rpm
separation median/p95/max=3/5/49 ms
```

The older four-state values now also make physical sense without a fitted
parameter: raw 60 = 750 rpm and raw 193 = 2,412.5 rpm, versus parallel Mode 01
readings of about 756 and 2,418 rpm. `data/dids.yaml` now records this as
`ECM_RPM_COARSE`, high confidence.

### DID 0x121A and broadcast 0x2D1 — vehicle speed

The raw broadcast sweep found that `0x2D1` bytes 1-2 little-endian are the same
counter as DID `0x121A`. The DID is `raw * 0.1 km/h`; after applying that scale
to both paths, the direct engineering comparison is:

```text
did:0x121A -> can:VEHICLE_SPEED_2D1
n=14,975, r2=0.99908, slope=0.999823, offset=0.0969 km/h
scale drift=0.92% (stable), difference=+0.081 +/- 0.241 km/h
separation median/p95/max=2/5/20 ms
```

An independent comparison to confirmed `WHEEL_SPEED_6F7_C` is also stable
(`r2=0.99894`, slope 0.990818). The roughly -0.70 km/h difference is between
two ECU/ABS speed variants; both inherit the already documented under-read
versus GPS. The legacy one-byte `MOTION_SIGNAL_2D1_1` binding remains only for
old model-manifest compatibility; new consumers use `VEHICLE_SPEED_2D1`.

### DID 0x122A — vehicle speed clamped at 30 km/h

In the 15,000 same-reply samples, `0x122A` and `0x121A` have r2=0.99990 with a
stable 1.777300 slope. Eleven independent 2026-08-05 moving snapshots reproduce
`0x122A = round(0x121A * 16/9)` within two counts. At KOEO/stopped, however,
`0x121A=0` and `0x122A=533`. Thus `0x122A` is the same speed expressed at
`raw * 0.05625 km/h`, with an intentional floor at 29.98 km/h.

A final live-idle stream reproduced the boundary directly for 200 consecutive
replies: coarse RPM raw 58-61 (725-762.5 rpm), `0x121A=0`, and `0x122A=533`.

The direct check against confirmed `WHEEL_SPEED_6F7_C` gives n=14,975,
r2=0.99930, slope 0.991213, offset 0.0090 km/h, stable 0.72% drift, and
difference -0.763 +/-0.219 km/h. The catalog name is
`ECM_VEHICLE_SPEED_MIN_30`; it is an internal table/logic coordinate and must
not be used as actual low-speed telemetry.

### Target AFR RAM word — final commanded phi and fuel cut

Later Windows captures showed that UpRev's Target AFR item uses two read-only
absolute-RAM byte descriptors, `0xFFFF26BE` then `0xFFFF26BF`, rather than a
DID. The Linux implementation reproduced those descriptors and then proved
that a dynamic definition may mix them with ordinary DID descriptors. The Pi
was unreachable during this later phase, so the known-answer checks were made
entirely within the same K-line reply rather than against stale Pi telemetry.

A 5,000-reply stream paired the RAM word with RPM, throttle, air charge,
injector pulse, and wideband voltage. All 98 replies with RAM word `0x0000`
also had injector pulse `0x1206` raw 54-57, the independently established
DFCO floor, at 2,687-2,950 rpm with closed throttle. Zero therefore denotes
fuel cut/no target rather than an AFR of zero.

The stronger known-answer stream paired the word with the already researched
rich-side target DID `0x1134`. Across 5,000/5,000 same-reply observations, with
no exceptions:

```text
0x1134 raw = max(64, floor(target_ram_word / 256))
```

The run covered words `0x0000`, `0x4000`, `0x4080`, `0x4100`, `0x4150`,
`0x4180`, `0x4380`, and `0x4A80`. The last gives
`0x4A80 / 16384 = 1.1640625` and DID raw 74, independently reproducing the
Mode-01 commanded-phi value previously observed with `0x1134=74`. This pins:

```text
phi = target_ram_word / 16384
target_AFR = fuel_stoich_AFR / phi   (nonzero words only)
0x0000 = fuel cut / no AFR target
```

At gasoline stoich 14.7, `0x4A80` commands 12.63 AFR. DID `0x1134` is the
low-resolution rich-side table view and clamps to stoich during fuel cut; it
is not the final commanded target. The captured RAM addresses are specific to
this vehicle's ROM/calibration and are not shared-code constants.

The later Linux JSONL captures and comparison output were preserved beside
the Windows PCAPNG evidence as `linux-target-*.jsonl/json` in the external
capture directory documented by `docs/uprev-usb-kline-research.md`.

A later Windows combined capture independently exercised the reverse mixed
descriptor order: both Target AFR RAM bytes first, then injector B2, A/F B1,
A/F B2, BFS, and injector B1. All 387 replies were checksum-valid. Its 131
zero-target observations put both injector pulses at 0.45-0.49 ms and both
wideband sensors at the 5.065 V lean rail, reproducing the fuel-cut
interpretation with both banks in the same ECU reply.

### DIDs 0x1206/0x1207 — injector pulse width and scale

The isolated operator-selected definitions directly identify `0x1206` as
Bank 1 and `0x1207` as Bank 2. An UpRev autosave CSV overlapping the Bank 2
PCAP supplies the engineering-unit known answer. All 481 ordered display rows
equal the corresponding raw reply times `0.01` within the CSV's 0.05 ms
precision. `compare_channels` at a 10 ms policy gave 312 same-instant pairs,
`r2=0.995112`, slope `0.0100504 ms/count`, and offset `-0.00325 ms`; this pins
the intended conversion to `raw * 0.01 ms` for both bank registers. Injector
dead time is still not established by this result.

### Diagnostic-CAN parity and the remaining integration gap

All values in the combined UpRev fuel stream are available over CAN diagnostics,
not merely over the USB cable. DIDs `0x1206`, `0x1207`, `0x1208`, `0x1225`, and
`0x1226` are already handled by CANary's Mode 22 collector. The final Target AFR
word is also readable: after entering session `0xC0`, a service `0x23` KWP32
request with body `FF FF 26 BE 00 02` returned positive payload `4000` through
the Pi's ISO-TP CAN client. The KWP24 and UDS-ALFID request forms returned NRC
`0x12`. Evidence is preserved on the Pi at
`/data/canary/_uprev-usb/2026-09-20/can-target-afr-memory-probe.json`.

The remaining gap was software integration, not ECU or transport access, and
it is now closed: CANary models a recurring ReadMemoryByAddress channel
(`memory:` in `data/dids.yaml`, `mem:<NAME>` telemetry, selected per capture
profile). It is scheduled inside the Mode-22 session window, because service
0x23 is gated by the same session on this ECM, and it is withheld unless the
running calibration matches the one the address was verified against -- a RAM
address is an artifact of one ROM build, not a contract like a DID number.
The fuel-cut word decodes to no measurement rather than to phi 0.

`0x1134` therefore reverts to what it is: the low-resolution, rich-side
clamped view that cannot distinguish fuel cut from stoich. Use it only when
the memory channel is withheld, and never present it as the final target.

No equivalent passive-broadcast fields are claimed. These observations use the
ECM's active diagnostic endpoint. A simultaneous transport trial also showed
that an active K-line stream starves active CAN diagnostics (Mode 01 produced
only the opening seconds, then no samples for about 132 seconds), although
passive CAN frames remain available. Collection must choose one active diagnostic
transport at a time.

The new read-only `tools/can_memory_log.py` then exercised the recurring CAN
path. A 32-byte read at `0xFFFF26B0` completed 100/100 responses in 5.8 s. An
interleaved run added DIDs `0x1206`, `0x1207`, `0x1208`, `0x1225`, and `0x1226`;
the long capture completed 1,000/1,000 memory reads and 5,000/5,000 DID reads
in 35.2 s. The memory contains two similar 14-byte bank records, including
copies of the `0x4000` target-like word. Their changing fields remain unknown:
all `compare_channels` scale verdicts were `drifting` or `unresolved`. The
best short-window injector relation (r2 0.80) collapsed to r2 0.26 over the
longer run; the best long-window A/F relation reached r2 0.73 but was
unresolvable. These are candidates for a tagged transient, not telemetry
bindings.

### Contiguous CAN RAM sweep and same-time DID comparison

A later engine-running highway experiment swept four adjacent 63-byte windows
from `0xFFFF26A0` through `0xFFFF279B`. Each window completed 2,500/2,500
memory reads and 17,500/17,500 interleaved DID reads (`0x1201`, `0x1134`,
`0x1206`, `0x1207`, `0x1208`, `0x1225`, and `0x1226`). The daemon remained
passive while its broadcast collector stayed fresh. `tools/analyze_can_memory_log.py`
compared every U8 and big-endian U16 view with every raw DID using one-to-one
acquisition-time pairing within 15 ms; no aggregate-to-aggregate comparisons
were used.

This ECM's accepted KWP32 response-size boundary is 63 bytes. Lengths 32, 33,
40, 48, 56, 60, 62, and 63 all returned positive responses; every 64-byte
request returned NRC `0x12` while all interleaved DIDs still answered. That is
a measured constraint of this ECM/calibration, not a generic service-0x23
protocol limit.

The sweep produced two reproducible findings and one deliberate rejection:

- `0xFFFF26AE` is a big-endian engine-period counter. Transforming every
  observation as `500000 / word` and comparing it with raw coarse-RPM DID
  `0x1201` gave r2 `0.99910`, 2,499 pairs, slope `0.999007`, offset `-0.3634`,
  and 0.69% slope drift (`stable`). In engineering units the observed mapping
  is approximately `rpm = 6,250,000 / word`; its 4.2-rpm residual spread is
  consistent with the DID's 12.5-rpm quantization plus the 8-10 ms acquisition
  separation. The DID remains the calibration-independent collection route.
- Six big-endian Bank-1 injector mirrors occupy `0xFFFF2710`, `2714`, `2718`,
  `271C`, `2720`, and `2724`; six Bank-2 mirrors occupy `0xFFFF2712`, `2716`,
  `271A`, `271E`, `2722`, and `2726`. Their same-time comparisons with
  `0x1206`/`0x1207` gave r2 `0.99883-0.99965` and fitted slopes
  `0.99909-0.99971`, all stable. This independently confirms that the RAM
  block carries the injector raw counts, but Mode 22 is the cleaner source
  and already supplies the pinned `0.01 ms/count` conversion.
- `0xFFFF2742` initially tracked BFS `0x1208` at r2 `0.99944`, slope
  `10.8304`, offset `-122.55`, and 0.88% drift. A required reproduction 4-5
  minutes later disproved a fixed scale: 3,000 new pairs gave r2 `0.94005`,
  slope `8.6273`, offset `829.16`, and a `drifting` verdict. It remains an
  ordinary load-correlated candidate and is not a telemetry binding.

The quarantined evidence is on the Pi under
`/data/canary/_uprev-usb/2026-09-20/`:

- `can-memory-ffff26a0-63-fuel-dids-highway.jsonl` — SHA-256
  `24462a0d9b537348c446dd639739be3b2e8ab8de89b4e701b853f6b498c55f94`
- `can-memory-ffff26df-63-fuel-dids-highway.jsonl` — SHA-256
  `fab571979b24fe142b35ade5e67c46f8c601f989f9f65751e653b431b9d9b393`
- `can-memory-ffff271e-63-fuel-dids-highway.jsonl` — SHA-256
  `c61d1f915f3aefe55c6592ddfa998087e51c155aafc3bd90f018d4d0765b4bb8`
- `can-memory-ffff275d-63-fuel-dids-highway.jsonl` — SHA-256
  `cb08f2f73297194104ed3a6633f3226b591d3239ad82379d0a7c8aa92c523c8a`
- `can-memory-ffff2742-bfs-repro.jsonl` — the independent BFS-candidate
  rejection window, SHA-256
  `1c46efa0e15a130fbe433567598f09f98e1cd3529a031430b1ac59fb00e201f5`

These are active diagnostic-CAN observations. They do not establish passive
broadcast fields for injector pulse, BFS, A/F voltage, or final target AFR.

## Rejected or still-open readings

- A later 7,886-reply same-stream screen covered a transition from nonzero
  coarse RPM/load to 3,425 replies with both at zero. It strengthens the
  structural pairing of `0x1123/0x1124`, `0x1125/0x1126`, `0x114E/0x114F`,
  and `0x1159/0x115A`, but does not identify them. In the zero-RPM state the
  four pairs settled respectively at `100/100`, `99/99`, `250/250`, and
  `0/0`; during nonzero RPM their ranges were `88..125/89..125`,
  `95..101/94..105`, `63..250/74..250`, and `0..24/0..20`. Pairwise
  `compare_channels` results were unresolved, so these are discriminating
  observations for a future operator-labelled capture, not decoded channels.
- `0x120D/0x120E` looked like pedal voltage in a 31-second slice
  (`r2≈0.978`) but fell to `r2≈0.24` over 5,000 pairs with an unstable scale.
  They remain the existing pedal candidates; this drive does not identify or
  calibrate them.
- `0x1102` reached `r2=0.951` against GPS speed in one window, but its scale
  drifted 31.6% and older stationary rev captures disprove a speed identity.
- `0x121A` initially fit RPM at `r2=0.9952` with only 0.20% within-window
  drift. Cross-session reproduction and the raw broadcast sweep showed why
  that was still misleading: road speed and RPM moved together in one gear.
  The speed identity above survives both stopped data and independent fields.
- `0x122A` also looked like ordinary speed in short windows, but its stopped
  raw-533 floor and exact relationship to `0x121A` show that it is the clamped
  variant, not another honest road-speed reading.
- The paired fuel/air/VVT channels mostly correlate under load, but their
  scales drift. The already known `0x1206/0x1207` injector-pulse twin remains
  the only stable within-group result (`r2=0.99819`, slope 1.00022, offset
  2.04 counts, 1.50% drift over 3,000 pairs).

These rejections are part of the result: each looked convincing by r² alone,
and each would have become a wrong binding without the stability and
cross-session checks.

### Scheduled memory channel — first live daemon run

The recurring service-0x23 path had never run against the vehicle; only the
one-shot probe and `tools/can_memory_log.py` had. Validated 2026-09-20 22:36Z,
engine running at ~2,500 rpm in closed-loop highway cruise, daemon build
`23685fad`:

- `POST /control/capture` with `fuel-target-research` activated the profile
  with `memory: ['ECM_TARGET_EQUIV_RATIO']` resolved from the catalog.
- 75 s produced **83 `mem:ECM_TARGET_EQUIV_RATIO` rows** in
  `_capture-fuel-target-research-2026-09-20T223628`, unit `phi`, all 1.000000
  — stoich, which is the correct commanded target for closed-loop cruise and
  agrees with the four-state RAM observations at `0x4000`.
- Observed ~1.1 Hz against the profile's 0.5 s DID cycle, i.e. the channel is
  taking a window slot like any DID rather than running free.
- No `_SENTINEL` rows: the window held steady throttle, so no decel fuel cut
  occurred. The sentinel path is therefore still unexercised on the vehicle —
  it needs a capture containing a trailing-throttle event.

Analysis chain on the pulled session (laptop, not the Pi):
`read_channels(..., ['mem:ECM_TARGET_EQUIV_RATIO'])` returned the 83 samples
in engineering units, and `build_tuning_frame` reported
`commanded_afr: available` for the first time, converting phi 1.0 to 14.7 AFR
while retaining `commanded_afr_equiv_ratio`. The remaining roles read
`no samples`, which is correct: this profile polls six DIDs and none of the
cam or injector channels.

### Sentinel and full enrichment range, and the first semantic delivery run

A second live capture, `_capture-fuel-target-research-2026-09-20T224503`
(5 min, engine running, 2,440-3,371 rpm, throttle 0.77-4.065 V against a
4.12 V stop) exercised what the first one could not:

- **314 target samples spanning the whole commanded range**: phi 1.000000 to
  1.1640625, i.e. 14.70 down to 12.63 AFR, in eight distinct states. The rich
  end reproduces the documented `0x4A80` value exactly.
- **11 `_SENTINEL` rows** — the fuel-cut path, previously unexercised on the
  vehicle. Four of them pair within 300 ms with throttle 0.815-0.820 V at
  3,181-3,237 rpm. The capture's own closed-throttle floor (p1) is 0.78 V, so
  these are closed throttle at elevated rpm: decel fuel cut, confirmed from a
  channel that took no part in identifying the word. The other seven fall in
  the first 40 s, before this capture's throttle DID produced its first
  reading, so there is nothing to pair them against.

`delivery_error_from_semantic_frame` then ran on the all-DID roles for the
first time: 257 grid cells where rpm, measured AFR, commanded AFR and
fuelling-active were each independently observed in the same second; 234
dropped as `not_enrichment`; 23 remaining in the 3,000-3,500 rpm band at
commanded 13.26 / measured 13.53 AFR.

**That +2.0 % is not a finding and must not be quoted as one.** The measured
per-cell floor for this quantity is sd 1.49 % with a +/-2.4 % range (8-04
AM->PM null run), so a single-band result from 23 samples of one short
capture sits inside the noise. What the run demonstrates is that the chain
executes end to end on real driving data, not that fuelling is 2 % lean.

The `fueling_inactive` gate dropped zero rows, which is the designed
behaviour rather than a gate that failed to fire: fuel cut produces no row on
the target channel at all, so cut samples never entered the frame.

### Address re-verification, run live

`tools/verify_memory_channels.py` was run against the ECM on the same drive
(daemon passive with a 120 s TTL, build `7f4fcb85`):

```text
PASS  ECM_TARGET_EQUIV_RATIO   10/10 agreed   known-answer relation held on every read
```

The check is structural, not a plausibility bound: it reads the RAM word and
DID `0x1134` back to back in the held session and asserts
`0x1134 raw == max(64, word >> 8)`. A moved address keeps answering and keeps
decoding inside `plausible_min`/`plausible_max`, so bounds alone would accept
it; the relation is what a moved word stops satisfying. `--record-revision`
re-stamps `verified_tune_revision` only when every channel passes, which is
what makes the post-flash step a command rather than a hand edit someone has
to remember.

The daemon returned to `engine_running` with 0.02 s OBD freshness after the
passive window closed.

### RAM sweep of four unexplored windows — no new bindings

Four 63-byte windows adjacent to the previously covered block were swept on
the same drive (engine running, rpm 2,212-2,962, air charge spanning ~9x,
throttle and A/F voltage both spanning >55% of range), 1,000 memory reads and
4,000 interleaved DID reads each, references `0x1201`/`0x1208`/`0x120F`/`0x1225`:

```text
0xFFFF2621  0xFFFF2660  0xFFFF279C  0xFFFF27DB
```

3,998 of 4,000 memory reads and 16,000 of 16,000 DID reads answered.
**No new identified telemetry.** What the windows contain:

- `0xFFFF27E4` and `0xFFFF27E6` are the same word (bit-identical on all 1,000
  samples — another mirrored pair, like the injector registers). It fits air
  charge `0x1208` at r2=0.99664 with a `[stable]` slope verdict, but that
  verdict is misleading and the per-sample ratio is the reason: `0x1208/27E4`
  has median 28.03 with p5 23.01 and p95 31.10, a ±14 % drift that the fit
  absorbed into its -1113.9 offset. It is not a rescaled copy.
- A structural hypothesis was pre-registered and **refuted**: if `27E4` were an
  airflow *rate* while `0x1208` is a *per-event* quantity, their ratio would
  track rpm. It does not — `corr(ratio, rpm) = -0.017`, and dividing the ratio
  by rpm makes the spread worse (40.4 % vs 28.9 %).
- `0xFFFF27E2` is a distinct word, not `27E4/2` (0/1000 exact, 0/1000 within 1).
- Every candidate in the other three windows came back `[drifting]`.

`27E2` and `27E4`/`27E6` therefore stay unidentified load-correlated
candidates, the same status as the `0xFFFF2742` BFS candidate the earlier
sweep rejected. They are not catalogued.

**Methodological finding, worth more than the candidates.** The analyzer's
`scale_verdict` is computed from slope drift across sub-windows and does not
see ratio drift that a large fitted offset can absorb. `27E4` passed as
`[stable]` at r2=0.9966 while its per-sample ratio moved ±14 %. Anything
promoted on that verdict alone would repeat the class of error this project
keeps retracting; a per-sample ratio spread should be reported alongside it.

### Multi-field windows, validated on the vehicle

`ECM_ENGINE_PERIOD` (`0xFFFF26AE`) and `ECM_TARGET_EQUIV_RATIO`
(`0xFFFF26BE`) now share the `fuel_target` window: one 18-byte service-0x23
read returns both from a single ECU operation. This ECM refuses batched Mode
22, so until now nothing in CANary could sample two quantities at one instant.

An 80 s capture (2026-09-20T233720, engine running, 2,443-3,403 rpm) produced
**85 target samples and 85 period samples, 85 of which share an identical
timestamp** — every one, by construction rather than by pairing tolerance.

Known-answer check against the independent fine-rpm DID, paired at 250 ms:

```text
n=83, rpm 2,446-3,403
rpm_did x period: median 6,245,775, p5 6,231,076, p95 6,265,901  (0.56% spread)
rpm(memory) - rpm(DID): mean +1.6, sd 7.1, max |err| 38.3 rpm
```

The reciprocal relation and the ~6.25e6 constant hold across the range. Some
of that sd is the 250 ms pairing window, not decode error.

**Two scheduling defects only the vehicle found.** A windowed read is named
after its window, and two separate selection gates matched that name against
the profile's *channel* list, so the read was never issued. The first capture
looked like a success — profile active, both channels resolved, zero `mem:`
rows. The second exposed the cost: the unsent request stayed pending, the
Mode-22 window was held open its full 10 s, and every DID in the capture fell
from 2 Hz to one sample per 10.5 s. Capture quality flagged it as
`rate_shortfall` with `cadence_hz: 0.095`. The ECM was never involved — the
same 18-byte read answered `0949...4000` (2,629 rpm, phi 1.0) on the first
manual probe. Fixed by giving `selection_exclusion` the request instead of a
channel string, so the unsafe call cannot be written.

### UpRev channel harvest — the full detailed scheme decoded

`uprev-detailed_scheme.pcapng` + the simultaneous
`UpRev_Log9-20-2026_6-53-59_PM.csv` (parked, idle and revs, 29 s, 406 stream
replies against 405 CSV rows) together identify essentially the whole ROM
Editor logging scheme.

**UpRev chains dynamic local identifiers.** Three definitions were sent, each
including the previous one:

```text
02 00 ac 81  <12 memory-byte descriptors>
02 00 ac 82  01 81  <5 memory bytes> <11 DID descriptors>
02 00 ac 83  01 82  <10 DID descriptors>
```

The log then polls `02 00 21 83 04 01` — service 0x21, ReadDataByLocalIdentifier
— and gets `66 34 61 83 <50 bytes> KK` back at **13.97 Hz**. The inner checksum
validated on 406/406 replies, which is what pins the payload at exactly 50
data bytes and therefore fixes every field width.

Descriptor order is reply order, so the layout is determined. Widths for eight
previously unmeasured DIDs follow from the 50-byte total. Identities were then
confirmed by reproducing ROM Editor's own displayed values:

| CSV column | field | conversion | agreement |
|---|---|---|---|
| IGN TIMING (BTDC) | DID `0x110A` @33 | `110 - raw` | **405/405 exact** |
| LT Fuel Trim B1 (%) | DID `0x1102` @45 u16 | raw | 405/405 |
| LT Fuel Trim B2 (%) | DID `0x1125` @47 | raw | 405/405 |
| INTAKE AIR TMP | DID `0x1106` @38 | `raw - 50` degC | 405/405 |
| ACCEL PED POS 1 | DID `0x120D` @21 u16 | `raw x 0.005` V | r2 0.99996 |
| B-FUEL SCHDL (ms) | DID `0x1208` @27 u16 | `raw x 0.00049` | 405/405 |
| MAS A/F -B1 (V) | DIDs `0x1126` hi + `0x1204` lo | `raw x 0.005` V | 405/405 |
| Fuel Target X Trace | RAM `0xFFFF103E` | `x 0.250859` | r2 1.00000 |
| Fuel Target Y Trace | RAM `0xFFFF11D8` u16 | `x 12.4995` (rpm) | r2 1.00000 |
| Intake Cam VQ Y Trace | RAM `0xFFFF11CA` | `x 50` exactly | resid 0.0000 |
| Throttle X Trace | RAM `0xFFFF1C2C` | `x 0.390711` | r2 0.99973 |
| Throttle Y Trace | RAM `0xFFFF1902` u16 | `x 3.124834` | r2 1.00000 |
| TARGET AFR | RAM `0xFFFF26BE` u16 | Q14 phi (already catalogued) | constant 14.70 here |

Confirmations of already-catalogued decodes, all exact: `0x1101` coolant
`raw-50` degC, `0x1135`/`0x1163` cam `raw x 0.5 - 64`, `0x1206`/`0x1207`
injector `x 0.01` ms, `0x1209` MAF `x 0.01` g/s, `0x120F` throttle and
`0x1225`/`0x1226` A/F sensors `x 0.005` V, `0x1117` load `~100/255`.

Four findings worth stating separately:

- **Ignition timing is DID `0x110A`, `110 - raw` degrees BTDC, exact on every
  row.** It was absent from the broadcast bus (best r2 0.28) and unlocated
  among the 138 answering DIDs. It is the final commanded timing after trim
  and knock feedback, which is the mapped-vs-logged comparison the tuning work
  needs and the only knock proxy this ECM offers.
- **BFS is `0x1208 x 0.00049` ms, and 2048 counts/ms is refuted** (360/405 vs
  405/405). The earlier "2048 within error" note was an artifact of deriving
  the scale transitively through per-era ratios. The provider is promoted from
  candidate to confirmed.
- **`0x1125` was never unknown — it is bank-2 long fuel trim.** It held raw
  95-99 in the first K-line captures, which is a trim sitting near its
  neutral 100.
- **AFR WB-B1/B2 are NOT streamed.** ROM Editor derives them from the A/F
  sensor volts through a non-linear transfer (0.695 V -> 11.63, 1.21 V ->
  12.84). There is no ECM-computed AFR channel to find; our own
  `voltage_transfer` curve remains the only route, and this CSV's 207 distinct
  volt/AFR pairs are a ready-made reference for calibrating it.

The knock columns (`KNOCK EVENT` 0, `KNOCK STRENGTH` 1280, `KNOCK THRESHOLD`
1.95) were constant for the whole log, consistent with the settled finding
that this ECM does not stream knock.

### Can CANary reproduce UpRev's one-request scheme over CAN? No.

UpRev samples all 38 fields at 13.97 Hz by defining a local identifier and
polling it once per sample. Tested against the engine ECM at `0x7E0` in
session `0xC0`, KOEO, 2026-09-20:

| service | meaning | result |
|---|---|---|
| `0xAC` | DynamicallyDefineLocalIdentifier (Nissan/KWP) | **NRC 0x11**, serviceNotSupported |
| `0x2A` | ReadDataByPeriodicIdentifier | **NRC 0x11**, every transmission mode |
| `0x2C` | DynamicallyDefineDataIdentifier (UDS) | **NRC 0x12** for sub-functions 01/02/03 |

`0x2C` answering 0x12 rather than 0x11 says the service is recognised and no
sub-function is usable, which is the same outcome. **UpRev's whole-scheme
sampling is a K-line capability, not a CAN one**, and no amount of chaining
gets around it: the define itself is refused.

`0x21` does answer, but only for identifiers the ECM already defines. A sweep
of all 256 found 14, every one a static identification block rather than live
telemetry:

```text
0x00-0x05  4 bytes each   0x10  11 bytes "1ZS44A00000"   0x81  19 bytes (zeros)
0x82  42 bytes (mostly ff/00)   0x83  24 bytes "ZS44A"...
0xF0/0xFE 24 bytes "12345"+"8X61BJCN41"   0xF1/0xFF 24 bytes
```

One of those is worth keeping: **`21 10` returns the calibration id as ASCII**
(`1ZS44A00000` on ZS44A-k106). That is the ECU telling us which ROM it is
running, which is exactly what `MemoryChannel.verified_tune_revision` needs
in order to notice a reflash without being told.

**The multi-value route that works on CAN is service 0x23.** One read returns
up to 63 bytes from a single ECU operation, which is what
`MemoryChannel.window` already does. Parity therefore depends on how the
wanted quantities cluster in RAM, not on a define mechanism. Rough budget at
the measured 116.8 req/s ceiling: 21 DIDs polled serially gives 5.6 Hz per
channel, while three or four 63-byte windows at 14 Hz costs ~56 req/s and is
internally same-instant.

### Ignition timing located in RAM; the spark window

With DID `0x110A` confirmed as ignition timing, the RAM hunt became a
known-answer search rather than a correlation hunt: sweep, interleave the
DID, and look for the byte that equals it. Fourteen 63-byte windows were swept
around every RAM address UpRev's own scheme reads (engine idling, operator
blipping the throttle, 1,200 samples each, all 1,200/1,200 answered).

**`0xFFFF11C7` is the ignition-timing register.**

| capture | timing range | exact | + adjacent read | unexplained |
|---|---|---|---|---|
| `ram-0xFFFF11A0` | 12-32 degBTDC | 1,132/1,200 (94.3%) | 56 | 12 (1.0%) |
| `ram-timing-repro` | steady idle | 2,996/3,000 (99.9%) | 4 | 0 |

The mismatches cluster on the 58 steps where timing actually changed, which is
the signature of two requests 1.5 ms apart rather than of a different
quantity. It is also the only offset in the window that mirrors timing —
unlike the injector registers, this value is not duplicated.

One other candidate was rejected outright: `0xFFFF11FF` matched 1,200/1,200,
but that window was captured while timing sat constant at 17 degBTDC. A
constant matching a constant is not evidence.

`0xFFFF11D8` carries engine speed (equals DID `0x1201` on 2,825/3,000; it is
also the address UpRev reads as `Fuel Target Y Trace`, pinned at raw x 12.4995
by the 2026-09-20 CSV). It is mirrored at `0xFFFF11D4`.

The two sit 17 bytes apart, so a 19-byte `spark` window returns **ignition
timing and engine speed from one ECU operation**. Live validation, engine
running, daemon build `3292cf75`:

```text
verify_memory_channels          40/40 on all three checkable channels
capture 2026-09-21T013326       spark: 80 of 80 timing+rpm rows share a timestamp
                                fuel_target: 81 of 81 phi+period rows share a timestamp
```

`MemoryKnownAnswer` gained `equals_did` and `min_agreement` for this.
Exactness and acquisition skew are different things, and demanding 100% would
have failed a correct binding for a reason that has nothing to do with the
address.

### Correction: the ECM is not the request-rate limit

An earlier note in this file reasoned that 21 DIDs at UpRev's 14 Hz would need
294 req/s against "the measured 116.8 req/s ceiling". That comparison was
wrong. 116.8 req/s is what the *daemon* achieved on a drive while also running
Mode 01, storage and inference; it is not what the ECM can answer.

Measured directly, 2026-09-21, engine idling: back-to-back single-DID reads
ran **9,734 requests in 15.0 s = 649 req/s**, which matches the ~1.5 ms per
request already recorded in this file's `access` notes. 294 req/s is 45% of
that.

So serial Mode 22 polling at UpRev's rate is not obviously out of reach, and
the honest statement of the parity gap is about daemon throughput and
same-instant sampling, not about the ECM refusing the traffic. Note that the
63-byte memory reads are multi-frame and slower — the sweeps ran at ~176 req/s
— so windows buy simultaneity, not raw rate.
