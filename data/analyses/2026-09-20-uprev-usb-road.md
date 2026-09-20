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
