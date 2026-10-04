# Talking to the 2008 Xterra's ECM: CAN, K-line, and UpRev

Read this for any work that sends requests to this truck's ECM, plans a
capture's request mix, or runs alongside UpRev. Everything here was measured on
this truck. Where a data file holds the value, it is named and wins over the
prose here.

## Contents

- CAN: Mode 01
- CAN: Mode 22 and the diagnostic session
- CAN: RAM reads (service 0x23) and the calibration id
- Broadcast bus
- K-line (OBD pin 7)
- The UpRev logger on this truck

## CAN: Mode 01

- **41 supported PIDs, the same set KOEO and running.** The supported-PID
  bitmask stops at `0x4E` and never claims `0x60`, so nothing at or above
  `0x4F` is reachable. That rules out the torque PIDs (`0x61`–`0x63`), fuel rate
  (`0x5E`), and rail pressure (`0x22`/`0x23`/`0x59`). Inside the reachable
  range, MAP (`0x0B`), fuel pressure (`0x0A`) and absolute load (`0x43`) are
  unsupported too. `BAROMETRIC_PRESSURE` (`0x33`) and `EVAP_VAPOR_PRESSURE`
  (`0x32`) *are* supported. Evidence: `data/nissan-xterra-2008/pid-probe-2026-07-22.md`;
  live: `get_obd_coverage`.
- Consequence for the engine-management method: the requested-vs-actual torque
  pair does not exist on this truck, and neither boost nor fuel pressure is
  observable through the ECM. Those need an added sensor (the CANary-HAT's ADC
  inputs) or a RAM channel.
- **PID `0x24` is λ and PID `0x44` is φ on this one ECM.** The two conventions
  are reciprocal, so neither can be inferred from the other. Proof:
  `0x24` pegs at 2.000 in decel fuel cut, where φ would go toward zero, and at a
  commanded φ 1.16 the sensor read λ 0.866 (`afr_calibration.yaml` →
  `pid_0x24_is_lambda_verified_2026-08-07`). In closed loop `0x44` dithers
  0.9796 / 1.0204 rather than sitting at 1.000. Gate "enrichment" at φ > 1.05.
- **Mode 01 does report the WOT target.** On a 5,762 rpm / 144 g/s pull, `0x44`
  read φ 1.195–1.242 while `0x24` measured within ~2 %. An older conclusion
  that the WOT target was unobtainable over Mode 01 came from never reaching
  real WOT.

## CAN: Mode 22 and the diagnostic session

The `access` block of `data/dids.yaml` is the source of truth. `did_sweep
--session auto` and the scheduler read it, so never hard-code these at a call
site.

- **Mode 22 needs diagnostic session `0xC0`.** In the default session it is
  refused with `7F 22 80`. Sub-functions `0x81` and `0x85` answer positively but
  do not unlock `0x22`.
- **Session `0xC0` and Mode 01 are mutually exclusive.** Inside the session the
  ECM answers 0 of 10 Mode 01 requests, functional and physical alike. That is
  why a capture profile can have Mode 22 or Mode 01 at a given instant, never
  both, and why the scheduler alternates windows (`docs/hybrid-scheduling.md`).
- **`10 81` is the only fast way out.** After it, Mode 01 answers on the very
  next request (50 ms grace). The textbook `10 01` is answered positively but
  leaves Mode 01 dead for more than 30 s. Letting S3 lapse kills DID reads
  within 3–5 s and also leaves Mode 01 dead for more than 30 s. "Wait for the
  session to expire" is the slowest possible exit.
- **TesterPresent (`0x3E`) is rejected.** The session is held by re-sending
  `10 C0` about every 2 s. While reads are flowing continuously they keep it
  alive by themselves. Reaching for the textbook `0x3E` keep-alive is a known
  dead end.
- **One DID per request.** Raw frames, 2026-09-17, engine running, `10 C0`
  held: `03 22 12 01` → `05 62 12 01 00 A2`, but `05 22 12 01 12 03` and
  `07 22 12 01 12 03 12 04` both → `03 7F 22 12`. The earlier "batching
  verified" claim could not tell success from the silent one-at-a-time
  fallback. Every dynamic-identifier service (`0xAC`, `0x2A`, `0x2C`; all
  tested 2026-09-20) is refused on CAN as well, so two DIDs are always two
  requests about 1.5 ms apart.
- **The ECM is not the rate limit.** Requests take about 1.1–1.5 ms
  (658 reads/s on a 4-DID rotation at KOEO; ~117 req/s sustained while
  driving). Every "ceiling" found so far was CANary's own event loop or storage
  writer. Watch `/status` storage queue depth, not only the response rate.
- DID space: **138 of 512 DIDs in `0x1100`–`0x12FF` answer** at idle; **27 of
  them vary** at constant idle. The rest are static configuration or status.
  A full-range sweep takes ~1.9 s; the 138 answering DIDs alone ~0.2 s flat out.
- **Some DIDs were misidentified for weeks.** The current names in
  `data/dids.yaml` are the corrected ones, so read the `description` and
  evidence before you trust a name:
  - `0x1208` `ECM_AIR_CHARGE` is the base fuel schedule: air charge × the
    injector constant K, raw × 0.00049 ms (verified BFS, operator 2026-10-02).
    The DID stays in raw counts because the deployed models were trained on
    them. The ms reading is `derived:BASE_FUEL_SCHEDULE`, the Target AFR load
    axis. It is **not** injector pulse width: it reads the same per gram of air
    in fuel cut as when fuelled. Never compute fuel mass or φ from it, and never
    use it as a DFCO marker. At key-on, engine off it holds a frozen value.
  - `0x1206`/`0x1207` `ECM_INJECTOR_PULSE_1/2` are the injected pulse. They are
    convex in air charge (about +7 % from idle to cruise), so a straight line
    fitted in one region does not extrapolate.
  - `0x1134` is an open-loop rich target (raw/64 as φ), not the commanded φ: it
    holds 64 through closed loop.
  - **On CAN, `0x1102` is vehicle speed** (one byte, km/h = raw × 2), not
    `ECM_LONG_FUEL_TRIM_B1` as catalogued. The catalog took that meaning from
    UpRev's K-line stream, and the firmware's K-line and CAN DID tables
    differ.
  - **Learned trim on CAN:** `0x1125` is LTFT **B1** (the catalog says B2) and
    `0x1126` is LTFT B2. They are the same RAM words as PIDs `0x07`/`0x09`.
  - **Short-term trim on CAN:** `0x1123`/`0x1124` are STFT B1/B2.
  - Firmware evidence is in `docs/ecu-firmware-analysis.md`; the
    `data/dids.yaml` fix is pending.
  - The firmware's DID → RAM pointer tables are at `0x024988`, `0x024B84` and
    `0x024D00` for CAN, and at `0x022738`, `0x022938` and `0x022AB4` for
    K-line.
  - `0x1225`/`0x1226` are the A/F sensor voltages. `afr_calibration.yaml` maps
    them to AFR with a monotone table. Fitted curves were tried and are far
    worse, so don't replace it with one.

## CAN: RAM reads (service 0x23) and the calibration id

- `ReadMemoryByAddress` returns up to **63 bytes** per read (64 → NRC `0x12`).
  A catalogued read *window* is the only same-instant multi-channel read on this
  ECM. The `memory:` catalog in `data/dids.yaml` holds the channels
  (`mem:<NAME>`), including the ECM's final commanded equivalence ratio
  (`mem:ECM_TARGET_EQUIV_RATIO`, φ, the preferred commanded source in
  `lambda_delivery.yaml`), RAM ignition timing, and map-trace channels that give
  the ECM's own table coordinates.
- Addresses belong to the ECU *program*, not the calibration, so they survive
  UpRev calibration flashes. `AGENTS.md` → "RAM (memory) channels" has the rules;
  `python -m tools.verify_memory_channels` checks each one against its known
  answer.
- Service `0x21` local identifier `0x10` returns the calibration id as ASCII
  inside session `0xC0` (`7F 21 80` in the default session). **UpRev flashes
  keep the same id** (`1ZS44A00000`), so an unrecorded flash cannot be detected
  by comparing ids. Resolve the calibration by flash time from
  `tune_revisions.yaml`.

- **The whole 1 MiB flash is readable with plain requests, with no
  SecurityAccess.** UpRev's backup on 2026-10-04 sent `10 85` once, then
  `10 FB` before each `23 [BE32 address] [BE16 0x003F]`, on 0x7E0/0x7E8, at
  about 35 ms per read. 1 MiB took 624 s. The result was byte-identical to
  the decoded tune. It was only done key on, engine off. See
  `docs/skunkworks/uprev-backup-20261004.md` and
  `data/nissan-xterra-2008/rom_backup.yaml` (the recipe as data, from W08).

## CAN: the BCM (0x745 → 0x765)

- The BCM answers on CAN in the same Nissan `10 C0` session as the ECM
  (`data/analyses/2026-09-24-scan-tool-bcm-ecm-session.md`: TPMS ids via
  `21 1F`, a data monitor via `21 32`).
- Active tests use the ECM fan test's shape: `30 <lid> 20 01` to actuate,
  `30 <lid> 11` to release. The ids (horn, high beams, wipers, buzzer, and two
  unidentified) are in `data/nissan-xterra-2008/bcm-active-tests.yaml`. They
  are volatile and reversible, and each one is a known physical action for
  binding body frames. Confirmation is open as D03.33.

## Broadcast bus

500 kbit/s, ~1640 frames/s at idle, 27 distinct IDs passively (29 when CANary
is also polling; `7DF` and `7E8` are the two extras).

**Confirmed absent from the broadcast bus** (well-powered no-load sweep,
2026-08-08; `data/bindings.yaml` → `absent_signals` has the range each search
covered): ignition timing, wideband AFR, intake cam timing, injector pulse
width, base fuel schedule, calculated load, and any fine-grained MAF. Those come
only from Mode 22, RAM, or the UpRev K-line log. **Caveat: that sweep was
no-load only**, so a signal that appears only under road load could still have
been missed.

`ECM_RPM` (`0x1F9` bytes 2–3 big-endian, ×0.125) is the strongest control for
validating any time alignment or decode chain. It fits slope 1.0000 against
UpRev RPM, with scale stability 0.9985–1.0011 across 750–3700 rpm.

## K-line (OBD pin 7)

`data/nissan-xterra-2008/kline.yaml` is the source of truth (the model is
`src/collection/kline/profile.py`); the wire-level story is in
`docs/uprev-usb-kline-research.md` → "On-wire view".

- **10400 baud 8N1, KWP2000 (ISO 14230).** ECM address **`0x10`**. The **ABS
  module (`0x28`)** is on the same K-line and answers too, with its own reader
  and DIDs in `kline.yaml`. The **airbag (SRS) module (`0x58`)** is K-line
  only: it ignored `10 C0` on CAN 0x752. The scan tool clears its codes with
  a bare `14`, which gets about 350 ms of `7F 14 78` and then `54`
  (`data/analyses/2026-10-04-srs-kline-scan-tool.md`).
- **Tester address `0xFC`, not the ISO 14230-4 default `0xF1`.** A
  StartCommunication from `0xF1` (or to `0x33`/`0x11`/`0x01`/`0x12`) gets
  silence, which reads exactly like dead hardware.
- **The standard 25 ms fast init is ignored.** The wake-up this ECM answers is
  **518.9 ms low, then 18.1 ms high**, then `81 10 FC 81 0E` sent back-to-back
  → `83 FC 10 C1 5D 8F 3C` (key bytes `5D 8F`).
- After StartCommunication, frames are `<len> <data…> <sum & 0xff>` with no
  address bytes. StopCommunication is `01 82 83` → `01 C2 C3`.
- **Known answer:** `05 22 11 03 04 01 40` → `04 62 11 03 <value> <cs>` (value
  `0x99`–`0x9A` at KOEO). UpRev appends `04 01` to its `0x21`/`0x22` requests.
  **What that trailer means is unresolved**: it may not be a second DID. The
  same bytes are refused on CAN.
- **One diagnostic session, two transports.** The ECM's session state is shared
  between CAN and K-line, so put CANary in passive mode (`POST
  /control/passive` with a TTL) while anything else owns the K-line.
- **A local identifier's reply is capped at ~61 data bytes.** A definition that
  would exceed it is refused with `7F AC 12`. UpRev then drops the session and
  restarts, which looks like a flaky start. The fix is fewer measurands
  (2026-09-24; layout in `docs/uprev-usb-kline-research.md`).

## The UpRev logger on this truck

- **Its logging is K-line (pins 7/15), not CAN.** With CANary passive it emits
  *zero* CAN frames, so broadcast logging alongside it is completely safe.
  UpRev's streaming protocol is not discoverable by sniffing CAN.
- If CANary transmits, UpRev reacts on CAN: it flow-controls CANary's
  multi-frame Mode 06 responses on `7E8`, and may fire its own
  `22 11 03 04 01` reads, which this ECM refuses on CAN. That's harmless to
  UpRev's logging, but both tools share the `7E0`/`7E8` endpoint, so gate
  CANary with `POST /control/passive` for simultaneous windows.
- **UpRev drops out every few minutes on its own.** That is confirmed
  independent of CANary: the operator reproduced it with no Y-cable and no
  CANary present. Don't diagnose it as bus interference. Keep windows to about
  3 minutes or less.
- **It is rotation-polled, so its channels do NOT share a timestamp.** Aligning
  a CSV to CAN on RPM leaves the other channels skewed: MAF sits **+0.10 s**
  later than RPM. Always scan a per-channel lag before concluding a channel has
  no broadcast home. Skipping that once nearly killed a valid airflow binding.
- CSV `Time` is milliseconds from log start and is UpRev's receipt time of each
  reply (±10 ms against the wire, 2026-09-24). `Time = 0` fell **1.72 s after
  the filename timestamp** on the Pi clock (setup plus clock offset; one
  measurement). It logs to memory and the operator saves the CSV by hand, so
  mtime is the save time, not the stop time.
- Aligning by RPM cross-correlation **fails at idle** (CANary polls RPM every
  5–7 s and idle blips alias). Use a rev sweep, or decode RPM off `0x1F9` for a
  ~100 Hz reference. A peak r ≥ 0.999 proves the alignment and that both saw
  the same events; below 0.99 the windows weren't really simultaneous.
- Its AFR channels come from the factory A/F sensors, which **floor at 10.0
  AFR**. Samples at the floor are censored, not measured, and the sensors are
  good to roughly ±5 % at WOT. Stop chasing a WOT AFR error smaller than that
  with these sensors.
