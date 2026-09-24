# H00 — CANary-HAT integration (index)

Opened 2026-09-23, the day the first CANary-HAT rev 2.0 passed checkout
(`docs/CANary-HAT-Revisions.md` §13.1.1). The board gives the Pi, on one
header: CAN FD, its own K-line transceiver, a 6-axis IMU, GNSS with PPS and a
12 V sense ADC. The hardware works; almost none of it reaches the daemon yet.
These checklists take each capability from "proven in a bring-up script" to
"recorded, checked and used".

Same conventions as D03: `[x]` only with the evidence linked, `[~]` partial
with the rest named. **(truck)** marks items that need the vehicle, **(Pi)**
the live Pi, **(hardware)** a part that is not fitted yet. (The archived
PENDING-HARDWARE used bare `H0`–`H9`; these are `H01.1` onward.)

| File | Scope |
|---|---|
| [`H01-source-policy.md`](H01-source-policy.md) | Which sources may train or score detectors; source allowlist sweeps; runtime dependencies; stale docs |
| [`H02-imu.md`](H02-imu.md) | LSM6DSOX config and collector, record load, frame resolution, known-answer validation |
| [`H03-hat-platform.md`](H03-hat-platform.md) | 12 V sense, PPS → chrony, GNSS rate, uart0 default, barometer (deferred) |
| [`H04-committees.md`](H04-committees.md) | Sensor cross-checks that flag: OBD ↔ GPS speed, OBD ↔ HAT baro, longitudinal accel |
| [`H05-kline-native.md`](H05-kline-native.md) | Native K-line transport, protocol, session ownership and collector |
| [`H06-kline-sniffer.md`](H06-kline-sniffer.md) | Passive decoding of UpRev and scan-tool sessions, live |
| [`H07-driving-dyno.md`](H07-driving-dyno.md) | Road-load and power from on-road pulls, IMU-compensated |

## Operator decisions (2026-09-23 — do not re-open)

1. **External sensors never train or score the anomaly detectors.** GPS,
   IMU, the HAT's 12 V and barometer channels and anything under `sensor:`
   are excluded from the Isolation Forest, the sequence detector and the
   behavior models, at training and at serve. Enforced by an allowlist of
   ECU-origin sources, not a blocklist of sensor names (H01.1).
2. **External sensors take part in detection only through committees** —
   deterministic cross-checks of one quantity measured two ways. Wanted now:
   OBD speed that greatly disagrees with GPS speed, and (once a working part
   is fitted) the HAT barometer disagreeing with OBD barometric pressure
   (H04). A known, measured offset between two instruments is data, not a
   fault: ECU speed reads ~8 % under GPS on this truck.
3. **The barometer is parked.** The BMP384 on the first board is dead
   (§13.1.1); everything that needs it is marked **(hardware)**.

## Proposed defaults (confirm or override before the item that needs them)

- **K-line gets its own source, `kline`.** Not `uprev` (documented as
  import-only, and a different instrument) and not `obd` rows (instrument
  lineage matters: all-did-tuning-hardening 2.7, 8.5). H05.1.
- **K-line channels are not trainable until decided.** They are ECU data, so
  decision 1 does not exclude them, but they exist only while a K-line
  session runs, and a feature that comes and goes is the D02.5 failure shape.
- **The native K-line collector is off by default** and runs when a capture
  profile asks for it. While it owns the ECM session, CAN *diagnostic*
  requests stop; broadcast CAN capture never does (H05.6).
- **Passive sniffing is the default K-line mode** when nothing asks for a
  session: transceiver listening, pull-up off, never transmitting (H06).

## Order

```
H01 (policy + plumbing) ──┬─> H02 IMU ──────┬─> H04 committees
                          ├─> H03 platform ─┘        │
                          └─> H05 K-line ──> H06     │
H02 + H03.5 (10 Hz GNSS) + H04.3 ─────────────────> H07 dyno
```

H01.1 edits `src/preprocessing/feature_pipeline.py`, which another session had
uncommitted work in on 2026-09-23: land it after that work is committed, not
over it.

## What this unlocks (why the order is worth it)

- **Scan tools and UpRev become visible.** Anything another tester reads over
  K-line can be decoded live and time-aligned with CAN, without the UpRev USB
  cable (H06).
- **Everything UpRev reads, CANary can read itself**, including one-request
  synchronous sets (`AC` define + `21` poll) and the multi-DID read the ECM
  refuses on CAN (H05).
- **A driving dyno**: wheel power from acceleration and road load, with the
  IMU supplying grade and the both-directions protocol cancelling wind (H07).
  It is the measurement the supercharger build is judged by
  (`supercharger-build.md` NA5).
