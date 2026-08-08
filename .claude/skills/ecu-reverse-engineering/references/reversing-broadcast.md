# Reversing the broadcast CAN bus

The proprietary, non-diagnostic packets the car uses to actually operate. Unique
per make/model/year — you reverse them by correlation, not lookup. From *The Car
Hacker's Handbook* Ch. 5.

## Why generic packet analysis fails

Every controller sees every broadcast frame, the bus is noisy, and payloads are
vehicle-specific. Scrolling raw traffic in time order is hopeless. Two moves make
it tractable: **group by arbitration ID** (collapse the flood) and **highlight
what changed** (find the field that responds to you).

## The universal binding method (record → act → playback → bisect)

Bind a byte to a function through a **controlled physical action**, never by
inspection:

1. **Record** the bus (`candump -l can0`, or a GUI like Kayak).
2. **Perform one physical action** — for engine work: blip the throttle, let it
   idle-up, apply brake. (Immobilize the vehicle first — in park, e-brake on, and
   for any drivetrain testing up on rollers/blocks so it can't lurch.)
3. **Stop** recording.
4. **Play it back** (`canplayer`) and confirm the action reproduces (the tach
   moves). If nothing happens: you're on the wrong bus, the signal is hardwired
   and not on this bus, or playback collided with live traffic — retry, or find
   the correct bus.
5. **Bisect**: keep halving the logfile and replaying each half until a single
   arbitration ID reproduces the effect. Kayak's playback slider sets in/out
   points for fast halving; with can-utils, cut the logfile in a text editor.

Then isolate that ID in a sniffer and repeat the action live, watching which
**byte/bit changes**.

## Grouping and filtering with cansniffer

`cansniffer` groups by arbitration ID and highlights bytes that changed since that
ID was last seen — repeating, unchanging traffic can be suppressed so only motion
remains.

- `cansniffer -c slcan0` — colorize changing bytes.
- Interactive filters (typed while running; input isn't echoed):
  - `-000000` — clear all (bitmask of all-0 matches every ID; `-` removes matches).
  - `+301` then `+308` — add just those IDs back.
- Range via ID+bitmask: `+500700` adds `0x500`–`0x5FF` (bitmask `700` = top nibble
  must match, low bits wildcard). `+3017FF` == `+301` (7FF = exact match).

## Decoding the field once you've found it

- **Bitfields.** A "lock"-style control decodes bit-by-bit: `0x01`=first item,
  `0x02`=second, `0x03`=both, `0x0F`=all four low bits set. Toggle unknown bits to
  1 and watch for behavior; no response may mean reserved/unused.
- **Shifted / compressed scalars.** Gauge values are usually *not* the plain hex of
  the reading. RPM might show as `0xFA0` (~1000) because firmware bit-shifts to pack
  it. Expect **multi-byte, big-endian** (`256·A + B`) with a scale and offset you
  must solve for.
- **The diagnostic PID is a different encoding** of the same quantity — you can't
  reuse the PID formula on the raw packet. But the PID gives you a **ground-truth
  value stream** to solve the broadcast scale/offset against, and to validate the
  final binding 1:1 (see the validation discipline in SKILL.md — this is the check
  that catches wrong offset/endianness/scale).

To override a constantly-broadcast value during testing you must transmit **faster
than** the real sender (send just after each real frame so yours is the last seen),
not merely flood — fewer collisions, cleaner result. `while` loop over `cansend`,
or `cangen`, or Kayak's Send-Frame with the Interval box checked.

## Record / playback formats

can-utils and Kayak use near-identical ASCII logs:

```
(1442245115.027238) slcan0 166#D0320018
```
`(timestamp) bus ARBID#DATAHEX`. Portable between `candump -l` and `canplayer`.

## Fuzzing (use sparingly)

Sending random-ish data to find undocumented services/functions. Easy to build
(`cangen` random traffic) but rarely productive — useful behavior is often a
*sequence* of packets (e.g. gated behind a SecurityAccess token), so a single
fuzzed frame seldom does anything. For discovering diagnostic surface, a
brute-forcer like CaringCaribou's `dcm` is cleaner than blind fuzzing. Never fuzz
a moving vehicle.

## ICSim — practice without a car

The Instrument Cluster Simulator (`github.com/zombieCraig/ICSim`, Linux/vcan) emits
realistic CAN noise plus a controllable cluster (speedo, turn signals, door locks).
`./icsim vcan0` + `./controls vcan0`; difficulty 0–3 (`-l`) and randomized IDs/byte
positions (`-r`, share the seed with `-s`) let you rehearse the bisection method
before touching a real bus. Skills transfer directly.

## OpenXC (Ford, and DIY)

An open translation layer that maps proprietary CAN to named signals
(`engine_speed`, `vehicle_speed`, `steering_wheel_angle`, `accelerator_pedal_position`,
…) as JSON. Where supported it removes the need to reverse those signals; with custom
firmware (a JSON message-set defining `bit_position`/`bit_size` compiled into the VI)
you can expose signals you've reversed yourself. Read-only by default; raw read/write
is opt-in per bus/signal.
