# TPMS light, 2026-11-11: the BCM decodes no wheel sensor

The operator reported the TPMS light on after the 2026-11-11 flash and drive, and
that the truck has new tires. The flash is not the cause: the broadcast pressures
were already empty on the morning of 2026-11-10, before either November flash.

## What the BCM says (read 2026-11-11T18:1xZ, engine running)

Sent from the Pi with `cansend` to 0x745, replies on 0x765, session `10 C0`
closed with `10 81`. `POST /control/obd_request` cannot send these: it always
encodes a 2-byte identifier, and the BCM's local ids are one byte.

- **Stored codes, `17 FF 00` → `57 08 …`, eight:** C1704, C1705, C1706, C1707
  (low pressure FL, FR, RR, RL; status 48, 28, 48, 48) and C1720, C1721, C1722,
  C1723 (**[CODE ERR]** FL, FR, RR, RL; status 30). Names from the FSM
  (`xterra_fsm/bcs.pdf`, pages 49–50). No [NO DATA] code (C1708–C1711). The
  status bytes are not decoded, so current and past are not told apart here.
- **Registered sensor ids, `21 1F`:** `0A640E`, `506A0E`, `C39D0F`, `8F980F`,
  fifth slot empty, the block repeated twice as before. On 2026-09-24 the same
  read gave `855D9B`, `C5B3FA`, `85ADF0`, `85B8FD`
  (`2026-09-24-scan-tool-bcm-ecm-session.md`). All four changed, so an id
  registration has been run since then.

## What the bus showed (0x385, the BCM's pressure broadcast)

Bytes 2–5 are four pressures at 0.25 psi/count (`data/bindings.yaml`, still
`hypothesized`, corner order unverified). Two more fields fall out of the history
of the local sessions since 2026-09-09:

- **Byte 6, high nibble: which sensors are being received.** `F0` with all four
  bytes populated, `D0` with byte 4 empty, `10` with only byte 5, `00` with none.
- **Byte 1, low nibble: per-tire low-pressure flags.** `0D` on 2026-09-18 with the
  three reporting tires at about 20 psi; it stepped `09` → `08` → `00` as they
  were aired back up, each bit clearing near 33 psi.

| Dates | 0x385 | Reading |
|---|---|---|
| 2026-09-10 | `84 00 9D 9F 9E 91 F0` | four sensors, 36–40 psi |
| 2026-09-17 → 10-27 | e.g. `84 00 7F 86 00 82 D0` | three sensors (29–38 psi); the byte-4 sensor mostly silent, heard briefly on a few drives |
| 2026-11-08 | `84 01 00 00 00 57 10` | one sensor, 21.75 psi, flagged low |
| 2026-11-10, 11-11 | `84 00 00 00 00 00 00` | none, on every drive |

## Reading

The BCM has four ids registered that are not the ones it had in September, it has
stored a [CODE ERR] for every wheel, and it decodes no pressure. That fits new
sensors whose registration did not take: wrong ids entered, or sensors the BCM
cannot decode. The light is the system-fault indication, so
the truck currently has no pressure monitoring at all.

Not established:

- Whether the tires are actually low. The last decoded value was 21.75 psi on one
  wheel on 2026-11-08, and low-pressure codes are stored for all four. Check with
  a gauge.
- When the sensors changed. Three of the old four still reported on 2026-10-27,
  which is after the 2026-10-04 tread-depth baseline for the new tires.

## Next

Registration writes the BCM's non-volatile memory, and the service bytes for it
have never been observed. CANary should watch the shop's tool do it (passive
mode plus candump), then confirm with `21 1F`, `17 FF 00` and four populated
bytes with `F0` in byte 6 of 0x385 on the drive home.

The two 0x385 fields above belong in `data/bindings.yaml` once a controlled
one-tire pressure change has fixed the corner order.
