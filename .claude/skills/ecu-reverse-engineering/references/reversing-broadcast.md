# Reversing the broadcast CAN bus

The proprietary, non-diagnostic packets the car uses to operate. Unique per
make/model/year — reversed by correlation, not lookup. From *The Car Hacker's
Handbook* Ch. 5. **In CANary this is `can_activity` + `compare_channels`;** the
external tools below are named only as reference behavior / candidate features.

## Why inspection fails

Every controller sees every broadcast frame, the bus is noisy, and payloads are
vehicle-specific. Reading raw traffic in time order is hopeless. Two moves make it
tractable: **group by arbitration ID** and **highlight what changed**. `can_activity`
does both for a bracketed window (`changed_byte_indices` per ID); `cansniffer` is the
live-CLI reference for the same idea.

## The method — bind a byte through a controlled action

Never bind by inspection. Make the thing happen on purpose and watch the byte move.

**In CANary:**
1. Start `can_activity` for N seconds (≤30).
2. Do **one clean physical thing** — blip the throttle, press the brake. Immobilize
   the vehicle first (park, e-brake; on rollers for any drivetrain test).
3. **Tag what you physically did** — `tag_current_capture` / `record_vehicle_event`.
   The physical action is the ground truth, not a graph selection (AGENTS.md).
4. Read `changed_byte_indices` per arbitration ID → the ID(s) and byte position(s)
   that moved with your action. Re-run with `ids=[…]` to confirm on the candidate.

**The underlying method** (offline logs, or when explaining the concept): record →
one action → play back to confirm the effect is bus-borne → bisect the log (halve,
replay each half) until one ID reproduces it → watch the changed byte. This is what
`can_activity` compresses into a single bracketed capture.

## Decoding the field once found

- **Multi-byte big-endian scalar** (`value = 256·A + B`) is the common case, with a
  **scale and offset you must solve for**. Watch for an adjacent byte *pair* that
  rises and falls together with your action.
- **Shifted / compressed** values — raw hex is not the reading (RPM may appear as
  `0xFA0` for ~1000). The diagnostic PID for the same quantity is a *different*
  encoding; don't reuse its formula on the raw packet — but do use its value stream
  as ground truth to solve the broadcast scale, and to validate 1:1 (see the
  validation discipline in SKILL.md).
- **Bitfields** decode bit-by-bit (`0x01`, `0x02`, `0x03=both`). Toggle-and-observe
  for unknown bits; no response may mean reserved.
- **Counters and checksums masquerade as data.** A byte that ticks `…0E 0F 00 01…`
  every frame regardless of what you do is a rolling counter; one that changes
  unpredictably every frame is likely a CRC. Neither is a physical signal even if it
  correlates.

## Static vs data

Bytes that never move during your action are unrelated, reserved, or state you
didn't change — ignore them for that action. A byte that only takes a few discrete
values is a status/enum, not a scalar.

## Candidate features (external references, not in CANary)

These are capabilities CANary doesn't have; the external tool is the reference if
the user wants to explore or you're proposing a feature:
- **Live grouped changed-byte view** (`cansniffer -c`, interactive `+/-` ID/bitmask
  filters): CANary's `can_activity` covers the bracketed-window case; a continuous
  live view is a possible addition.
- **ICSim** (`github.com/zombieCraig/ICSim`, Linux/vcan): a practice rig emitting
  realistic CAN noise + a controllable cluster, difficulty 0–3. Useful for rehearsing
  the method with no vehicle.
- **OpenXC**: an open translation layer mapping proprietary CAN to named JSON signals
  where the OEM supports it (Ford), or via custom firmware for signals you've reversed.
