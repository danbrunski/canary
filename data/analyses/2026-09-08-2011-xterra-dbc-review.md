# Review: operator-supplied 2011 Xterra DBC

Date: 2026-09-08

Status: **useful hypothesis source; not safe to load or promote as a 2008
binding catalog**.

The operator supplied a DBC identified as being for a 2011 Xterra. Its text
matches comma.ai's community-maintained
[`nissan_xterra_2011.dbc`](https://github.com/commaai/opendbc/blob/master/opendbc/dbc/nissan_xterra_2011.dbc).
The file contains no per-signal evidence comments or capture provenance, so its
labels remain external reverse-engineering claims rather than OEM facts. This
review compares it with the 2008 manual-transmission development vehicle's validated
`data/bindings.yaml`, the committed real-drive ground-truth slice, and the
existing broadcast inventory. The supplied file was not placed in `data/dbc/`:
that directory is auto-loaded, and its overlapping frame IDs would collide
with the validated 2008 DBC.

## What it corroborates

The strongest value is independent agreement on field *locations and roles*,
not exact scales:

| Supplied definition | 2008 evidence | Verdict |
|---|---|---|
| `0x1F9` bytes 2-3 = RPM, scale 0.125 | Confirmed against Mode 01 and UpRev; exact 2008 layout is big-endian | Same ID/location/scale; supplied endianness is wrong for the 2008 data |
| `0x1F9` byte 0 bits 6-7 = fan request | NT510 fan test observed `0x20 -> 0x60 -> 0xE0` | Exact bit-level corroboration |
| `0x1F9` byte 0 bit 3 = A/C request | A/C-only capture isolated the `0x08` bit | Exact bit-level corroboration |
| `0x23D` byte 1 = accelerator pedal | UpRev pedal-voltage comparison identifies this byte | Identity corroborated; supplied percent scale is not the pinned voltage decode |
| `0x23D` byte 2 = throttle position | UpRev throttle-sensor-voltage comparison identifies this byte | Identity corroborated; supplied percent scale is not the pinned voltage decode |
| `0x23D` bytes 3-4 little-endian = RPM at about 3.15 rpm/count | Confirmed 2008 scale is 3.125 rpm/count | Strong corroboration |
| `0x23D` byte 7 = coolant | Confirmed as a coolant duplicate | Identity corroborated; supplied `0.366666` scale is wrong for the 2008 data |
| `0x002` bytes 0-1 = signed steering angle; byte 2 = steering rate | Physical steering sweep isolated the same bytes | Useful semantic corroboration; endianness, scale, and zero still need synchronous NT510 angle readings |

The fan/A/C match is especially strong because the DBC's bit numbering decodes
to the exact masks measured independently on the 2008 truck: fan request
`0x40/0x80`, A/C request `0x08`.

## Exact parameters that do not transfer

`src.analysis.broadcast_discovery.sweep_session` was run on the committed
seven-minute drive slice with one-to-one acquisition-time pairing within
250 ms. This reproduces known answers before judging the external file:

| Candidate | Reference | n | r² | Fitted engineering map | Result |
|---|---:|---:|---:|---|---|
| `0x1F9[2:4]` big-endian | `obd:RPM` | 239 | 0.998745 | `rpm = raw * 0.124681 + 1.69` | Confirms CANary's big-endian `0.125`; supplied little-endian form has r² 0.002145 |
| `0x23D[3:5]` little-endian | `obd:RPM` | 239 | 0.998727 | `rpm = raw * 3.121509 - 1.05` | Confirms CANary's 3.125 scale |
| `0x551[0]` | `obd:COOLANT_TEMP` | 78 | 0.988682 | `degC = raw * 0.997152 - 39.63` | Confirms the pinned `raw - 40`, not supplied `raw - 45` |
| `0x233[0]` | `obd:COOLANT_TEMP` | 78 | 0.991730 | `degC = raw * 0.986430 - 48.13` | Consistent with the pinned `raw - 50`, not supplied `raw * 0.366666` |
| `0x23D[7]` | `obd:COOLANT_TEMP` | 78 | 0.994409 | `degC = raw * 0.991601 - 48.83` | Consistent with the pinned `raw - 50`, not supplied `raw * 0.366666` |

The coolant sub-window stability verdicts are `drifting` on this narrow 85-92
degC slice because integer quantization dominates the small range; the
committed ground-truth tests therefore carry the stronger direct 1:1 residual
checks and exact catalog pins. The fitted maps above are supporting diagnostics,
not replacements for those pins.

Several declared ranges are also internally inconsistent: a signed 16-bit
steering value is declared with a nonnegative `[0|65535]` physical range,
8-bit `RPMlow`/`RPMhi` fields scaled by 3.15 cannot reach the declared 45,000
rpm, and one-bit flags are assigned ranges wider than 0-1. Treat the file as a
reverse-engineering notebook, not production decode metadata.

## New 2008 candidates worth testing

These definitions point at fields that are present but unbound on the 2008
bus. None is promoted without same-instant 2008 evidence:

| CAN field from supplied DBC | Why it helps | Required 2008 check |
|---|---|---|
| `0x231` byte 2, pedal position, `0.5 %/count` | Existing 2008 bindings only name engine-state bytes 0 and 3 | Compare against `0x23D` pedal voltage and Mode 22 pedal data over a stepped-pedal capture |
| `0x23E` bytes 3-4 little-endian, estimated torque, `0.1 N m/count` | `0x23E` is a high-rate, wholly unbound 2008 frame; torque is an engine-analysis gap | Pair against Mode 01 requested/actual torque PIDs if supported and test scale stability across RPM/load bands |
| `0x23E` bytes 2 and 6, inverted/direct throttle | Offers redundant electronic-throttle channels | Compare against `0x23D` throttle voltage and Mode 01 throttle with same-instant pairing |
| `0x280` bytes 4-5 little-endian, speed, `0.01 km/h/count` | Existing work knows `0x280` is motion-correlated but has not bound the field | Compare both endian interpretations against GPS and Mode 01 speed; use GPS for absolute scale |
| `0x354` bytes 0-1 big-endian, speed, `0.0066 mph/count` | Existing 2008 byte-1 motion candidate is exactly where this two-byte field would land | Compare against GPS and Mode 01 speed; supplied scale equals about `0.01062 km/h/count` |
| `0x354` byte 6 mask `0x10`, brake light | A clean physical-action signal and useful event tag | Brake released/applied capture, checking the bit at each transition |
| `0x354` byte 4 mask `0x40`, TCS-off | May fill a stability-control state gap | Toggle the physical TCS switch and pair the bit transition to the action tag |

The supplied `0x253` TCU shaft speeds and `0x580` oil-temperature/RPM message
do not appear in the 2008 corpus inventory. That is expected for at least the
TCU frame because the development Xterra is a manual. They are portable
profile leads for another vehicle, not 2008 bindings. `0x35D` and `0x5C5` are
present on the 2008 bus, but the supplied DBC gives them no signals, so it adds
no decode knowledge for those IDs.

## The four-wheel-speed claim

The supplied DBC labels two 16-bit wheel speeds in each of `0x284` and `0x285`,
which would be the exact four-corner layout CANary has been looking for.
However, it cannot be accepted as written:

1. In both messages the second Intel signal starts at bit 15, so it overlaps
   the first 16-bit signal by one bit. `cantools.database.load_string(...,
   strict=True)` rejects the DBC for overlapping signals. Bit 16 is the likely
   intended aligned start, but silently correcting external evidence would be
   inventing a fact.
2. The existing 2008 full-range and cornering analysis already tested
   byte-aligned slices on `0x284`/`0x285`. The candidates behave as aggregate
   vehicle-speed copies or non-speed data and do not show the yaw-dependent
   sign/magnitude required of left/right corners.
3. The supplied file is for a different model year and appears to include an
   automatic-transmission TCU absent from the development vehicle. An ID match
   is not proof that payload semantics survived the year/powertrain change.

So this DBC does **not** fill the 2008 per-corner wheel-speed gap. It does give
a precise hypothesis to retest if a suitable 2008 raw drive is available:
decode bytes 0-1 and 2-3 of `0x284`/`0x285` in both endiannesses, require zero at
rest, then regress wheel-minus-vehicle-speed against GPS yaw rate. Promotion
still requires the left/right coefficient to flip with turn direction and
imply a plausible half-track; correlation with vehicle speed alone is not
enough.

The Pi was not reachable from this workstation during the review, so the new
unbound candidates above remain explicitly untested rather than being inferred
from aggregate reports.
