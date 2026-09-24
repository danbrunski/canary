# ABS over K-line: scan-tool sniff, 2026-09-24 (KOEO)

**Status: hypotheses, not bindings.** Nothing here is decoded into a channel
until a known-answer check passes (AGENTS.md: reproduce a known answer before
trusting a novel one). This is the evidence the K-line tooling checklist and
any future ABS binding start from.

## Setup

- 2008 Xterra, key on engine off. The operator's scan tool was on the ABS
  "live data" screen.
- CANary daemon at `2971d79` in passive mode (no CAN transmit). The K-line
  collector was sniffing, listen-only, on `/dev/ttyAMA0` at 10,400 baud.
- Session `2026-09-24T162507` (Pi): K-line frames from 16:32:00Z.

## What the scan tool did

- KWP2000 StartCommunication to address **0x28**:
  - request `81 28 FC 81 26`;
  - reply `83 FC 28 C1 6D 8F 64` (positive, key bytes `6D 8F`).
- Then `1A 80` (ReadEcuIdentification), and `1A 00` → `7F 1A 12`
  (sub-function not supported). Also 3E (tester present), 28, 30, then
  `22 <DID>` reads.
- After StartCommunication the frames use the length-only header, with no
  address bytes. CANary stores them as bare `DID_xxxx`, which collides with
  the ECM's CAN DID names (tooling gap 1).
- 38 DIDs were read once while the screen loaded. The live screen then cycles
  0x1301, 0x111A, 0x1105, 0x1104, 0x1102, 0x1103, 0x1302, 0x1303, 0x1210,
  0x1118 and 0x1202, at about 2 requests/s across the cycle.
- CAN carried only 3 frames on 0x740 in the same window, so the ABS live data
  is K-line only.

## Screen items against raw replies (KOEO)

The operator read the screen in its order: FR LH, FR RH, RR LH and RR RH wheel
speed, each 0 km/h; Decel G-SEN −0.01 G; eight inlet/outlet solenoids Off;
stop lamp switch Off; motor relay Off; actuator relay Off; ABS warn lamp Off;
Off lamp On; Off switch Off; slip light On; battery 12.48 V; yaw rate sensor
−0.27 °/s; engine speed 0.00 rpm; current and requested torque "--".

| DID | bytes | raw seen | hypothesis | why | what confirms it |
|---|---|---|---|---|---|
| 0x1102–0x1105 | 1 each | `00` | the four wheel speeds, in screen order (FR LH, FR RH, RR LH, RR RH) | 4 one-byte DIDs, all 0 parked | a drive: per-sample ratio against the CAN 0x6F7 per-wheel speeds and GPS speed (`compare_channels`), including wheel order |
| 0x111A | 1 | `7F` (once `80`) | Decel G = (raw − 128) × 0.01 G | 127 → −0.01 G, exactly the displayed value; 128 → 0.00 | hard braking and acceleration against d(speed)/dt |
| 0x1210 | 2 | `0000` / `FFFF` | yaw rate, signed 16-bit, about 0.27 °/s per count | 0xFFFF = −1 count ↔ −0.27 °/s displayed; flickers between −1 and 0 | left/right turns against the GPS heading rate (sign and scale) |
| 0x1118 | 1 | `A1` (161) | battery voltage, about 0.0775 V/count | 161 × 0.0775 ≈ 12.48 V | engine running: about 14 V should read about 181–185 |
| 0x1202 | 2 | `0000` | engine speed | 0 rpm displayed | engine running: idle rpm against `obd:RPM` |
| 0x1301 | 2 | `000F` | bit flags: solenoids, relays, lamps, switches | Off/On items; the constant set bits may be the two "On" lamps | stop-lamp and Off-switch presses at KOEO flip named bits |
| 0x1303 | 2 | `B0FE` ↔ `A0FE` | bit flags; bit 0x1000 toggled 6 times out of 83 | a switch or blinking lamp | same presses, logged with times |
| 0x1302 | 2 | `008F` | unknown (a torque slot? a status word?) | constant | engine running / drive |

One-off reads during screen load (0x1100 `F8000B41`, 0x1200 `C1BF0000`,
0x1306 `00FF`, 0x1309 `3CBC`, 0x130D `0002`, 0x1312 `000F`, 0x1313 `0014`,
0x1315 `0018`, 0x1120 `00000000`) are probably identification or
configuration. Not interpreted.

## Next known answers, cheapest first

1. KOEO, scan tool on this screen: brake pedal presses (stop lamp switch) and
   ABS/VDC OFF switch presses, with times noted. Each flips a named bit.
2. Engine running: battery and engine speed (0x1118, 0x1202).
3. A short drive with left and right turns, a hard stop and the screen left
   open: wheel speeds, yaw rate and decel G against CAN and GPS.

The sniffer only sees what the scan tool polls. Logging ABS data without it
needs an active K-line reader (tooling gap 4).
