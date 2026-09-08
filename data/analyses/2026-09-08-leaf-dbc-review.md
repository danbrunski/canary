# Review: operator-supplied Nissan Leaf CAR-CAN DBC

Date: 2026-09-08

Status: **useful cross-platform Nissan evidence; no 2008 Xterra binding is
promoted by this review**.

The supplied text matches the community-maintained
[`CAR-can_AZE0.dbc`](https://github.com/dalathegreat/leaf_can_bus_messages/blob/main/CAR-can_AZE0.dbc)
from the
[`leaf_can_bus_messages`](https://github.com/dalathegreat/leaf_can_bus_messages)
project. That project explicitly warns that ZE0, AZE0, and ZE1 Leafs differ
substantially. The DBC also labels many fields as unknown or tentative, so it
is reverse-engineering evidence rather than an OEM contract.

Seventeen of the 29 broadcast IDs in CANary's original Xterra inventory also
appear in the Leaf file. An ID match alone is weak: the Xterra's `0x625`
carries A/C compressor and power-state information, while the Leaf file calls
the same ID a lighting-status message. Agreement on ID, field location,
behavior, and scale is much more useful than ID agreement by itself.

## Strongest contribution: `0x385` TPMS

The Leaf file maps bytes 2-5 of `0x385` to four tire pressures at exactly
`0.25 psi/count`. This independently corroborates the existing QM5/Koleos
hypothesis already recorded in `data/bindings.yaml`:

| byte | Existing Xterra candidate | Leaf DBC | Xterra observed mean at supplied scale |
|---:|---|---|---:|
| 2 | FR | `TirePressure1` (`FL?` in a comment) | 36.9 psi |
| 3 | FL | `TirePressure2` (`FR?`) | 38.8 psi |
| 4 | RR | `TirePressure3` (`RL?`) | 38.4 psi |
| 5 | RL | `TirePressure4` (`RR?`) | 37.5 psi |

The signal names deliberately do not claim corners, and the comments mark the
Leaf order with question marks. The Leaf and QM5/Koleos tentative orders swap
left/right on each axle. Therefore this is strong support for the **four-byte
pressure group and scale**, but no support for CANary's current corner names.

Do not promote the bindings until a controlled test changes one known Xterra
tire by a measured amount and then rolls far enough to wake the sensors. A
4 psi change should move exactly one of bytes 2-5 by 16 counts. That test pins
the scale and corner simultaneously.

## New candidates that fill real gaps

| Leaf definition | Existing 2008 evidence | What would establish it on the Xterra |
|---|---|---|
| `0x5C5` bytes 1-3, big-endian 24-bit odometer | The 2011 Xterra DBC names `0x5C5` `MILEAGE`; the 2008 truck broadcasts the same eight-byte ID at about 10 Hz | Record the raw field before and after a GPS-measured drive. Require a monotonic delta and derive units from the delta, not the absolute reading. |
| `0x5C5` byte 0 mask `0x04`, parking brake | The frame is present, and prior Xterra captures saw byte 0 change with broader engine-state transitions | Apply/release only the parking brake and pair each tagged action to the bit. |
| `0x60D` door, lock, lamp, turn-signal, and vehicle-state bits | The Xterra broadcasts an unbound eight-byte `0x60D` at about 10 Hz | Use an engine-off action sequence with one physical change per tag: each door, locks, parking lamps, high beam, left/right indicators, then hazards. |
| `0x358` byte 1 mask `0x80`, headlights | Xterra tests already pin blower-active to `0x40` in this byte and separately show a larger headlight-dependent component | In a blower-off capture, require bit `0x80` to follow headlights off/on/off exactly. Keep `0x40` and `0x80` as independent flags. |
| `0x358` byte 2 values 0/4/8/12, turn signals | The frame and byte are present but unbound | Tag left, right, hazards, and off and require the four declared values. |
| `0x002` byte 4, steering-sensor heartbeat/counter | The Xterra and Leaf frames are both five bytes; CANary currently binds only bytes 0-2 | Check whether byte 4 increments or cycles independently of angle while the frame cadence stays constant. |

The odometer candidate is the best wholly new lead. The prior 2011 Xterra
file supplied the message identity but no layout; the Leaf file supplies the
missing field location. Its DBC unit is ambiguous (`km / m`), so neither a
factor of one nor market units should be copied without a measured-distance
delta.

## Corroborated speed and steering layouts

- `0x002`: the Leaf file agrees that bytes 0-1 are steering angle and byte 2
  is steering-rate-like. Its Motorola start bit 7 is a big-endian 16-bit
  layout, matching CANary's working 2008 interpretation, and its `ddeg` unit
  implies 0.1 degree/count. The DBC marks the field unsigned while its own
  comment says left is negative, so signedness and exact scale still require
  synchronous scanner readings. The operator-supplied 2011 Xterra DBC also
  claims 0.1 degree/count but disagrees on endianness.
- `0x280`: the Leaf file calls bytes 4-5 a big-endian cluster speed at
  `0.01 km/h/count`. The 2011 Xterra file names the same bytes and scale but
  declares them little-endian. The 2008 byte-5 motion result is consistent
  with either until the two-byte value is paired against GPS.
- `0x354`: the Leaf file calls bytes 0-1 a big-endian ABS speed in pulses. The
  2011 Xterra file uses the same field and gives `0.0066 mph/count`, about
  `0.01062 km/h/count`. Both also place an ESP/TCS-off flag at byte 4 mask
  `0x40`. This triangulates location and role, not the Xterra's exact scale.

## `0x284` / `0x285`: useful structure, not a reversal

Unlike the malformed 2011 Xterra DBC, the Leaf CAR-CAN file gives a valid,
non-overlapping layout:

- `0x284[0:2]` big-endian = front right, `0x284[2:4]` = front left;
- `0x285[0:2]` big-endian = rear right, `0x285[2:4]` = rear left;
- scale `0.005 km/h/count` for all four.

This is meaningful structural evidence because both external files associate
the same message pair with wheel speeds, and CANary independently observed the
same fast cadence and byte-1/byte-3 variability pattern. It still does not
overturn the Xterra result:

- CANary already tested the aligned byte fields over the full speed range;
- the Xterra fields behave as aggregate speed copies under the yaw test rather
  than separating into left and right corners;
- the Xterra fit was about 153 counts/(km/h), not the Leaf's 200;
- the source project warns that payloads differ even among Leaf generations.

If the stored full-drive frames become available, reproduce only this exact
four-field layout and record a compact table of scale and
`right - left ~ yaw_rate` coefficients. Do not run another unconstrained
correlation sweep and do not promote a corner unless the coefficient changes
sign with turn direction and implies the Xterra's plausible half-track.

## Fields that do not transfer

The Leaf file is also a useful negative control. `0x625` has demonstrably
different semantics on the Xterra, while `0x551` has a confirmed Xterra
coolant byte but a different Leaf message role. EV-specific motor, inverter,
battery, regenerative-braking, charge, and VSP fields have no Xterra meaning.
These differences are why this file was not added to `data/dbc/` and why no
binding confidence changed.

The Pi was not reachable during this review, so the new `0x5C5`, `0x60D`, and
bit-level `0x358` hypotheses remain untested against raw stored Xterra frames.
