# CANary-HAT — Future Revisions

Planned work beyond the rev 1.0 reference design described in
[`CANary-HAT-Spec.md`](CANary-HAT-Spec.md): the **rev 2.0 change list** that
will actually be fabricated, the bring-up measurements it now has to carry, and
the rev 3.0 supervisor MCU.

> **Part of the CANary-HAT board documentation**, split across four files.
> Section numbers are **stable across the split** — a bare `§3.7.5` means the
> same thing in every file; this table says which file holds it.
>
> | Sections | File |
> |---|---|
> | §1, §2, §4, §5, §8–§11 | [`CANary-HAT-Spec.md`](CANary-HAT-Spec.md) — what the board is: overview, stackup, pin map, open items, implied software work |
> | §3 | [`CANary-HAT-Circuits.md`](CANary-HAT-Circuits.md) — why each part is wired this way, per subsystem |
> | §6, §7, §12 | [`CANary-HAT-Fabrication.md`](CANary-HAT-Fabrication.md) — KiCad implementation, manufacturing notes, assembly readiness |
> | §13 | [`CANary-HAT-Revisions.md`](CANary-HAT-Revisions.md) — bring-up measurements, rev 2.0, rev 3.0, lessons from prior art |

## 13. Future revisions

**Rev 1.0 was never fabricated.** It was submitted to PCBWay 2026-08-11, the
quote stalled, and on **2026-08-22** the submission was withdrawn in favour of
sending **rev 2.0** instead. That is a good outcome — the rev-2.0 list below was
already written and costs one board instead of two — but it invalidates the
assumption this section was built on, and the correction is the single most
important thing on this page.

**The staging assumed rev 1.0 would be built and measured first.** §13.1 opens by
saying its measurements "cannot be made from datasheets and are only answerable
with a physical board," and six rev-2.0 items were explicitly gated on them.
There is now no such board. **Rev 2.0 is the first physical board**, which flips
the character of the whole exercise:

- **A measurement-gated item cannot be resolved before fabrication.** Each one
  had to be re-decided as *"keep the option on the board"* rather than
  *"change X once we know."* §13.2 records each flip and why.
- **Rev 2.0 is now the bring-up board, so it should be more hedged, not less.**
  Every "delete this once we confirm it is unnecessary" item became "fit the
  footprint, leave it unpopulated" — the M2's discipline in §13.4.1 item 4,
  applied to our own list.
- **Anything cheap and irreversible should land now.** A missing net is a
  re-spin and an unpopulated footprint is free, so the rev-3.0 hooks that cost
  one net each were pulled forward (§13.2, bucket 3). This is the only board
  for the foreseeable future.
- **One item ships genuinely untested**, and it is named as a risk rather than
  buried: the P-FET input stage replaces a working circuit (§13.2).

The staging that survives still separates *fixing the design* (rev 2.0) from
*adding a subsystem* (rev 3.0), because those still carry very different risk.

**Naming.** Revisions are numbered, not lettered, as of 2026-08-22: rev A → **rev
1.0**, rev B → **rev 2.0**, rev C → **rev 3.0**. Older commits, KiCad files and
PCBWay correspondence still say A/B/C.

### 13.1 Measure on rev 2.0 — this feeds everything below

Several decisions cannot be made from datasheets and are only answerable with a
physical board. Capture these during bring-up, while the board is new and the
questions are still live.

**These were written as rev-1.0 measurements feeding rev-2.0 decisions.** With
rev 1.0 unbuilt they now land on rev 2.0 and feed **rev 2.1 or rev 3.0** instead,
so read the "what it decides" column as *what the next board does*, not as
something rev 2.0 can still act on. Measurements 2, 6 and 9 are the ones whose
rev-2.0 decisions had to be taken blind — see §13.2.

| # | Measurement | What it decides |
|---|---|---|
| 1 | **K-line commander mode against the ECU** — does polling work at all? | The reason this board exists (§9). Everything else is secondary. |
| 2 | **Does the 1 kΩ pull-up disturb an NT510/UpRev session?** Sniff with J5 fitted, then open. | Whether §13.2's FET circuit is needed at all, or whether the jumper was a hedge against nothing. |
| 3 | **Crystal frequency and startup** with C5/C6 = 27pF — and **confirm the fitted parts are C0G/NP0**, not X7R (§13.4). | 27pF was derived from a CL figure, never measured. Confirm 40 MHz ±0.5% and clean start; a wrong load cap shows as a frequency offset. An X7R part of the right nominal value is the failure mode that looks correct on the BOM and drifts over temperature. |
| 4 | **GNSS TTFF and C/N₀**, and whether the WiFi hotspot desenses it. | Validates the RF launch (§3.6.1) and whether the MAX-M10**S** (integrated SAW+LNA) is needed. |
| 5 | **V_BCKP hot start** — TTFF with and without BT1 fitted. | Whether the backup cell earns its place, and whether the no-charge-path design is acceptable. |
| 6 | **I2C rise times** with R7/R9 plus the Pi's own 1.8 kΩ pull-ups. | Whether to depopulate R7/R9 (§8.3). |
| 7 | **Actual 3.3V draw**, idle and with GNSS acquiring. | Confirms the budget in §3.7.5 that justified deleting the buck. |
| 8 | **Thermals on D4 and R5** with the engine running. | D4 now passes only the HAT's load, and R5 is the 0.2W-rated part — both should be cool. Confirm rather than assume. |
| 9 | **BMP384 RMS noise** at the intended ODR against a grade budget. | Whether the footprint-compatible BMP390 swap is needed (§3.5). |
| 10 | **TLIN1027 at the real CONSULT baud.** | The 20 kbps *transmit* ceiling vs what the Xterra actually needs (§3.3.1). |
| 11 | **X1202 quiescent draw** once it has cut its own output. | Feeds the rev-3.0 supervisor architecture (§13.3). |
| 12 | **Power-apply to first recorded frame** — wall-clock from 12V return to `canary.service` writing row 1. | Sizes the rev-3.0 boot-gap buffer (§13.3.3) and quantifies what every cold start currently loses. The journal in `docs/time-sync.md` shows 12 s from *kernel handoff*, which is a floor, not the number. |
| 13 | **Does the K-line RXD break reach the host as SysRq?** Boot with the console left enabled on `uart0` and the HAT fitted. | Confirms the §11 step-1 hazard is real and worth designing out in rev 3.0 (§13.3.4), rather than a theoretical reading of Table 8-1. |
| 14 | **Y1 start-up margin and drive level.** Does the 40 MHz oscillator start reliably, including cold? Probe OSC2 (≤ 1 pF) for amplitude. On paper \|R_neg\| ≈ gm/(ω²·C1·C2) is only 34–50 Ω against the ABLS2's 40 Ω ESR max at CL = 18 pF (§13.5.4, *Clock*, 2026-09-14). | Confirms or retires the paper concern for rev 2.0. Rev 2.1 sidesteps it with a single oscillator (§13.5.4, *Clock*); this measurement decides whether the two-crystal fallback there is ever viable. |

**Measurement 1 does not have to wait for rev 2.0 — and waiting is now more
expensive.** Withdrawing rev 1.0 pushed the first physical board out by however
long the rev-2.0 changes take, so the one question that justifies this board's
existence is unanswered for longer. Answer it independently. The Macchina M2 (open
hardware, `github.com/macchina/m2-hardware`) brings K-line out on OBD pin 7 and
can answer "does this ECM respond to CONSULT over K-line at all" independently —
de-risking §9, the entire justification for this board, before the PCBWay boards
land.

**But it must be re-terminated first, or a negative result means nothing.** Read
off the M2-B interface schematic (`SCH-01006 R2`) on 2026-08-11: both
ISO9141/LIN channels are a TJA1021 with a **10.0 kΩ pull-up to +12V and a 220 pF
bus cap, and no diode anywhere on the sheet**. That is neither LIN responder
termination (~30 kΩ) nor the commander termination a tester requires (§3.3.1:
1 kΩ plus a series blocking diode). Fit 1 kΩ and a 1N4148W in the harness
between +12V and pin 7 before concluding anything from a silent bus.

Worth knowing while you are in there: the M2 connects essentially all 16 OBD
pins, including **single-wire CAN on pin 1 and medium-speed CAN on pins 3/11**,
with the four remaining discretionary pins on test points. Manufacturer-
discretionary bus access is therefore *not* an unserved gap in the market —
useful context for any future form-factor work.

#### 13.1.1 Rev 2.0 bring-up — first board, 2026-09-23

One assembled rev 2.0 board on the Pi 5, checked on the bench and then on the
Xterra through the OBD port (key off, then KOEO), with no UPS stacked. Tools and
the procedure are in `hardware/canary-hat/firmware/` (`checkout_hat.sh`,
`kline_bringup.py`, `kline_edges.py`, `kline_decode.py`).

| Subsystem | Result | Evidence |
|---|---|---|
| ID EEPROM (U1) | **Pass** | Arrived blank (32 KiB of `0xFF`). Programmed with a HAT+ v2 record (`vendor "CANary"`, `product "CANary-HAT rev 2.0"`, `product_ver 0x0200`, `dt_blob "canary-hat"`), read back byte-identical. Firmware reports it in `/proc/device-tree/hat/` and loads the overlay with no `config.txt` line. JP1 open = writable, as designed. |
| CAN (U3/U4/Y1) | **Pass** | `mcp251xfd` probes `MCP2518FD … o:40.00MHz`. KOEO: 20,737 frames, 0 errors/drops/overruns, the Xterra's normal ID set at its usual rates, multi-frame ISO-TP OBD replies from `7E8`. STB (GPIO22) held low by the overlay's `gpio-hog`. |
| CAN LED (D6/R16) | **Pass** | Blinks with the Pi's own ~2 Hz key-off probes; solid on a live bus. |
| 12V input + sense (Q1/D3, U8, R11/R12) | **Pass** | ADS1015 at 0x48 reads 0.000 V with no vehicle, 12.68 V key-off, 12.43 V KOEO. Pi `throttled=0x0`. |
| K-line (U2, R6) | **Pass** | See measurements 1 and 10 below. |
| IMU (U6) | **Pass** | `WHO_AM_I` 0x6C, \|a\| = 1.004 g at rest, gyro ≈ 0. |
| GNSS (U7) + PPS | **Pass** | NMEA at 9600 on `/dev/ttyAMA2` (uart2); SBAS fix, 12 satellites, HDOP 0.79 with the antenna on J4. `pps-gpio` on GPIO18 pulses once fixed. |
| **Barometer (U5)** | **Fail** | Chip ID 0x50, NVM, error register, sensor-time counter and data-ready flags all correct — the digital core is alive — but every conversion (forced and normal, soft reset, OSR ×1…×32) returns temperature **exactly 0** and pressure pinned at ≈`0x801C00`. Netlist is right (VDD pin 10 and VDDIO pin 1 on +3V3; CSB/SDO high). VDD cannot be open (POR needs VDD > 1.58 V, p.7) and a ≤700 µA load makes a marginal joint implausible, so the analog front end/converter of this part looked dead. Hand replacement of U5 is ruled out (operator, 2026-09-23: too small to rework by hand). **Register-level follow-up (`baro_diag.py`) narrows it: the commands and the pressure channel work; only the temperature ADC is pinned at code 0.** Part or process, not the schematic — findings and the investigation list are in [`CANary-HAT-Rev2.1.md`](CANary-HAT-Rev2.1.md) §13.5.6. |
| Pull-up FET (Q2/Q3, R5, D5) | **Pass** | Echo cannot test it (the ECM already pulls the bus up), so it was measured separately: K-line wire lifted from J2 pin 6, 12 V still applied, EN low, ammeter from J2 pin 6 to GND. GPIO24 low → 0 A, high → 10–20 mA (10 A range, 10 mA resolution; expected ≈ 11.8 mA through 1 kΩ + diode), low → 0 A. |
| X1202 coexistence | Not tested | UPS not stacked. |
| **GNSS backup (BT1/D2)** | **Fail (design)** | Cell polarity at BT1 is reversed. D2 blocks the reversed cell, so nothing is damaged, but V_BCKP is unpowered. Work around it by swapping the lead's crimps; the fix is in rev 2.1 (§13.5.6). |
| **J1 header** | **Fail (BOM)** | Assembler fitted a standard-height 2×20 header; the stack needs the 13.5 mm stacking header. The BOM line had no MPN and conflicting candidates. Rev 2.1 fix is in §13.5.6. |

**Overlay defect found and fixed on the board, not in the schematic.** The first
overlay enabled `uart2` with `status = "okay"` alone: `/dev/ttyAMA2` appeared but
GPIO4/5 stayed unmuxed and no NMEA arrived. The fragments now set
`pinctrl-0 = <&uart2_pins>` (and `<&uart0_pins>` for K-line), as the stock
`uart*-pi5` overlays do.

Measurements from the §13.1 table that this board answered:

- **1 — K-line commander mode: yes.** The HAT woke the ECM, opened a KWP2000
  session, read a DID and closed it on its own (KOEO):
  `81 10 FC 81 0E → 83 FC 10 C1 5D 8F 3C`, `05 22 11 03 04 01 40 → 04 62 11 03 99 13`,
  `01 82 83 → 01 C2 C3`. Two vehicle facts made it work — tester address **0xFC**
  (0xF1 is ignored) and a **518.9 ms low / 18.1 ms high** wake-up (the standard
  25 ms fast init is ignored). Both were read off UpRev's own session with the
  HAT sniffing, not guessed; the vehicle skill carries them.
- **2 — pull-up disturbing another tester: not with it off.** 93 s of UpRev's
  session (ECU check, logging setup, ~9 Hz logging) sniffed passively, EN high
  and pull-up off: 1,462 frames, every checksum good, UpRev unaffected. With the
  pull-up *on* alongside UpRev is still unmeasured.
- **3 — crystal: effectively yes.** The driver reports 40.00 MHz and 500 kbit/s
  runs error-free against the vehicle's nodes; not measured with a counter, and
  C5/C6 dielectric not inspected.
- **10 — TLIN1027 at the real baud: yes.** The ECM's K-line runs at
  **10400 baud 8N1** (ISO 14230), inside the 20 kbps transmit ceiling. Echo is
  byte-exact at 9600 and 10400 with the pull-up on or off.
- **13 — SysRq:** not exercised by design. On the Pi 5 the console is the debug
  UART (`ttyAMA10`), not `uart0`, so the hazard needs a deliberate
  `uart0_console` to arise. Confirmed by reboot with uart0 enabled in the
  overlay: `serial0` stays `ttyAMA10`, the only consoles are `tty1` and
  `ttyAMA10`, and no getty runs on `ttyAMA0`. uart0 is now on by default, and
  `install_pi.sh` refuses a `console=` that resolves to it (H03.5).
- **14 — Y1 start-up:** started at room temperature on both boots with the overlay loaded; cold
  start and OSC2 amplitude not measured.

Still open on this board: 4 (TTFF / hotspot desense, measured only as "fixed
within minutes"), 5, 6, 7, 8, 9 (blocked by U5), 11, 12.

### 13.2 Rev 2.0 — the board that gets fabricated

**This is the submission list**, re-triaged 2026-08-22 when rev 1.0 was withdrawn
from PCBWay. It was originally written as "fixes and basic GPIO control" for a
*second* board, assuming rev 1.0's bring-up had already answered §13.1. It hasn't,
so every item below is marked with how it was decided:

| Mark | Meaning |
|---|---|
| **[decided]** | No measurement dependency. Was always unconditional; unchanged by the withdrawal. |
| **[blind]** | Was gated on a §13.1 measurement that will now never precede fabrication. Re-decided toward keeping the option on the board. |
| **[pulled forward]** | A rev-3.0 hook that costs one net or one unpopulated footprint. Landed now because a missing net is a re-spin and rev 2.0 is the only board for the foreseeable future. |

No MCU, no new subsystems, no firmware — the supervisor stays in rev 3.0 (§13.3),
whose §13.3.2 power-domain analysis is unwritten and whose §13.3.6 net list is
15 nets onto a 65 × 56 mm board already carrying 48 footprints.

> **Implementation status — updated 2026-08-22.** The KiCad project for rev 2.0
> lives at `C:\Users\thebo\Documents\canary-hat - rev2`. Landed so far, all
> verified by exporting the netlist with `kicad-cli` and diffing net→node sets
> against a pre-edit baseline (67 nets, 45 components, unchanged; ERC 0 errors /
> 3 warnings, identical before and after):
>
> | Item | State |
> |---|---|
> | C2 → 47µF/50V + MPN | **done** — schematic, BOM, fab notes |
| **Input stage: Q1 + R10 + D3, D4 removed** | **done 2026-08-23** — schematic entered and verified by netlist diff: `Q1.2 (D)` on `+12V_RAW`, `Q1.3 (S)` on `+12V`, D3 cathode to source, J3 moved to `+12V`. ERC unchanged at 0/3 |
| **STB → GPIO22** (J1 pin 15) | **done 2026-08-23** — `U4.8` left GND. §13.2.1's "the one that matters most". Carries a **software prerequisite**: see §11 |
| **LSM6DSOX INT2 → GPIO23** (J1 pin 16) | **done 2026-08-23** — `U6.9` was unconnected on rev 1.0 |
| **12V sense: U8 + R11/R12 + C17/C18** | **done 2026-08-23** — divider on `+12V`, `V12_SENSE` → `U8.4 (AIN0)`, ADDR to GND = 0x48, **AIN1 left free** (spent by rev 2.1 on `MAP`, §13.5.3; the current-sense monitor reads an MCU ADC per §13.4.4) — originally noted as reserved for the rev 3.0 current-sense monitor |
| **K-line pull-up FET: Q2/Q3 + R13/R14/R15, J5 removed** | **done 2026-08-23** — `Net-(Q2-G)` is Q2.1+R13.2+R14.1 with the drain properly separate, `R5.2` moved to `KLINE_PU`, `R5.1` now direct to `D5.2`. Sniff mode is the default; commander mode needs **GPIO24 (J1 pin 18)** driven high — a new §11 item |
| **J2 pins 4–6 re-assigned** | **operator change 2026-08-23**, for routing. Pin 4 CANH, 5 CANL, 6 K-line. Caught by netlist diff — ERC cannot see it, since every net stays connected, just to different pins. §3.9 and `FAB-NOTES.md` carry the harness table |
| **CAN activity LED: D6 + R16** | **done 2026-08-23** — hardware-driven from the TCAN1042's RXD, no GPIO. Separates "the bus is dead" from "the software is not reading it" |
| **Test points** | **dropped** — LIN, +12V and GND are all probeable at J2's screw terminals, and EN is reachable at U2 pin 2 (SOIC lead) or J1 pin 37 on the stacking header. A pad would have been convenience, not access |
| **Field + BOM sweep** | **done 2026-08-23** — every component now carries `MPN` and `Note` except `J1` (operator-sourced) and `JP1` (a PCB feature, not a part). `BOM.csv` is now **generated from the schematic**, so the two cannot drift |
| **CAN common-mode choke: FL1** | **done 2026-08-23** — Coilcraft `1812CAN-513NRC`, 51 µH, on the stock KiCad footprint. **Took three attempts and every failure was silent** |
|  | *57 components, 74 nets, ERC 0 errors / 3 warnings — unchanged from rev 1.0's three. **PCB still needs* Update PCB from Schematic *plus* Update Footprints from Library *for U6.*** |
> | `KLINE_RAW` → `KLINE` | **done** — 2 hierarchical labels, 2 root sheet pins, 20 PCB net refs |
> | R7/R9 → DNP | **done** — schematic flags, BOM line split, assembler notes |
> | U6 lands → TN0018 | **done** in `lib/footprints.pretty/LGA-14L_STM.kicad_mod`; needs *Update Footprints from Library* + a DRC run in KiCad |
>
> Everything else in §13.2 requires placing symbols, drawing wires or moving
> copper, and is done in the KiCad GUI.
>
> **Two things the project files taught us.** `Inputs.kicad_sch` and
> `Power.kicad_sch` were orphans — superseded by `OBD_IO.kicad_sch` but still on
> disk, still containing a `C2` with the wrong value, and not referenced by the
> root sheet. Deleting them changed the netlist by exactly one line (the export
> timestamp), which is how that was confirmed. And `BOM.csv` plus `FAB-NOTES.md`
> both said the *footprint* was what had to change on C2, contradicting §3.7.1's
> decision to change the value — the same constraint in two places with nothing
> checking them, which is the defect that produced C2 in the first place.

#### 13.2.1 [decided] — unconditional fixes

**The one that matters most: STB on a GPIO.** The TCAN1042V's STB pin is
hardwired to GND on rev 1.0, which permanently forbids standby mode — see §13.3
for what that costs. It is a one-net change and every low-power option depends on
it. Note the pin has an internal pull-up forcing standby when floating, so a GPIO
that is high-Z at boot gives low-power-by-default and requires software to assert
normal mode; the daemon must drive it low at startup or CAN silently will not
work.

- **Rename the `KLINE_RAW` net to `KLINE`** — on this board `_RAW` means
  "upstream of protection", but this net *is* the protected, terminated bus.
- **U6 land pattern to ST TN0018** (0.350 × 0.575mm, mask +0.025mm/side). Rev 1.0
  ships ~19% under-size — a solder-joint reliability position, not a defect
  (§12.4).
- **C2 documented value → 47µF/50V**, matching the Ø6.3mm / 2.5mm footprint it
  lands in. This is the BOM defect that reached the PCBWay quote (§8.1, §3.7.1):
  100µF at 50V does not exist in a 6.3mm can. Change the *value*, not the
  footprint — 6.3mm is right for a rail carrying tens of mA, and §13.3.6 notes
  area gets tight once an MCU arrives. Populate/DNP stays a bring-up call; the
  footprint is there either way.
- **P-FET reverse protection on `+12V_RAW`**, ahead of the fan-out to D4 and J3.
  This is §3.7.4's own option 2 ("an ideal-diode controller, or a Schottky sized
  for 3A"), and it protects the X1202 leg that nothing currently protects —
  without reintroducing the diode drop that moving the tap upstream was meant to
  escape. See §13.2.5 — this is the one item on the list that ships untested.

  **Parts selected 2026-08-22.** A simple passive reverse-polarity circuit, not an
  ideal-diode controller: a true ideal diode blocks reverse *current*, and all we
  need is to block reverse *polarity*.

  | Ref | Part | Why |
  |---|---|---|
  | Q1 | **ROHM `RD3L07BAT`** — P-channel, −60V, **12.7 mΩ max at V_GS = −10V**, TO-252/DPAK 6.6 × 10.0mm | At 3A that is **38 mV and 0.11W**, against D4's ~500 mV and ~1.5W — which is the entire point, since §3.7.4 moved the tap upstream precisely to keep a diode drop out of a supply that must survive cranking sag. −60V clears D1's 38.9V clamp. *Its headline "−70A" carries a `*Tc = 25°C` footnote — silicon limit with an ideal heatsink, not a board rating. Do not quote it.* |
  | | *smaller alternative:* ROHM `RQ7G080AT`, −40V, 18.2 mΩ, TSMT8 **3.0 × 2.8mm** | A ninth the area, which matters here. −40V is defensible because the FET only sees voltage when **off**, and that is reverse polarity (−14V, ~−16V alternator fault), not a positive transient — during those it is on and conducting. Take the −60V part if the area exists. |
  | R | 100 kΩ 0603, gate → GND | Turns Q1 on in normal polarity. |
  | D | **15V Zener, SOD-123** (BZT52C15 class), gate–source | **Required, not optional.** When D1 clamps at 38.9V the unclamped V_GS would be −38.9V against a ±20V limit. Clamp current ≈ (38.9 − 15)/100k ≈ 240 µA, so 3.6 mW. |

  **Rejected on measurement, recorded so they are not re-proposed:** `ZXMP6A16K`
  (60V DPAK) at **85 mΩ** — 0.77W at 3A, which discards the whole reason for
  using a FET — and `AOD407` at 115 mΩ, worse.

  **Still to confirm before ordering (§12.5):** V_GS(max) on the specific part.
  ROHM's selection guide does not list it, and "a value and its rating recorded in
  two places with nothing checking them" is exactly what produced C2.

  **Orientation is the silent-failure detail.** The body diode must conduct in the
  *normal* direction, so the **drain faces the input (`+12V_RAW`) and the source
  faces the protected output**. Wired the other way the part looks right, powers
  the board, and passes reverse current straight through. Verify at bring-up
  before trusting it: with correct polarity the board should come up with a
  near-zero drop across Q1, and with the supply reversed on a current-limited
  bench supply it should draw nothing.
- ~~**A PTC on the input.**~~ **Re-taken 2026-08-22 against real part data — see
  §3.7.1 for the table and the reasoning. Outcome: no PTC on the Pi leg, one on
  the HAT leg.** No PPTC exists that both passes the Pi's ~2.3A worst case at
  cabin temperature and survives D1's 38.9V clamp: in the Littelfuse 2920L family
  every part rated above the clamp gives 0.70A at 85°C, and the part that passes
  2.10A at 85°C is rated 15V. That is the C2 defect shape — two sensible numbers
  that do not coexist in a purchasable part — and it is why this item was flagged
  as the likeliest place on the list to hit it.

  A ~0.5A/60V 1812L part on the **HAT leg** would have worked — tens of mA of
  load, ~10× margin, full rating against the clamp. **Operator decision
  2026-08-23: not fitted.** So **rev 2.0 carries no fuse at all**, which is rev
  1.0's position kept deliberately rather than inherited: on the Pi leg because
  no suitable part exists, on the HAT leg because the area is worth more than
  protecting a tens-of-mA branch against a fault that has never occurred. §3.7.1
  names the exposure — D4 becomes the weak link and diodes fail short — and names
  the trigger to revisit: an actual TLIN1027 or 12V-rail capacitor failure, not a
  general worry about fusing. **The input stage is therefore three parts, not
  four.**
- ~~**DNI split-termination footprints on CAN, gated by a solder bridge.**~~
  **Dropped 2026-08-23. The concession it retires is not real.** §3.2 is right
  that a third terminator on a vehicle bus is a fault, and it then concedes that
  bench use "requires an external terminator in the harness" — which sounds like
  a harness accessory someone has to build. **It is not: J2 is a Phoenix MKDS
  screw terminal block.** An external terminator is a 120Ω resistor clamped into
  two screws, ten seconds and a part from the drawer. Three footprints, a BOM
  line and a documentation entry to replace that is a bad trade.

  Two supporting reasons. The *split* arrangement's advantage over a plain 120Ω
  is better common-mode rejection — which matters on the **vehicle** bus, exactly
  where we deliberately do not terminate; on a bench it buys nothing. And the
  solder bridge §13.2 specified would have made "termination enabled" reachable
  by accident, on a board where §3.2 calls a third terminator on a live vehicle
  bus *a fault*. Pure DNI would have been safer than the gated version, and no
  footprints at all is safer still.

  **Replaced by one line in the fab notes**: for bench use, clamp 120Ω across J2
  pins 4 and 5. That documents the workaround better than footprints implement
  it.

- **Common-mode choke on CANH/CANL — fitted, not a DNI footprint.** Kept when the
  split termination was dropped, and on measurement the area objection did not
  survive: **Coilcraft `1812CAN-513NR`, 51 µH**, is 4.95 × 3.18 × 3.0mm — a
  20 mm² courtyard, about 1.5× a SOT-23 and well under Y1's 3.3mm height. Its
  KiCad footprint is `Inductor_SMD:L_CommonModeChoke_Coilcraft_1812CAN`, **named
  for this exact part**, which matters more than it sounds: every third-party
  footprint import on this board is a pin-1/rotation risk, and §12 already calls
  that "the most likely defect on this board" for U5/U6/U7. This adds no fourth.

  **Populate it rather than gating it.** An earlier sketch had 0Ω bypass jumpers
  (the M2's approach) or a cut-trace bypass. Both were over-thought: a 51 µH
  CAN-specific choke in a CAN line is the ordinary automotive configuration, not
  a hedge, and the gated versions each carry their own cost — two more series
  joints, or a PCB short that DRC would flag. If bring-up shows a problem the
  fallback is to desolder one part and bridge two pad pairs on a 1812, which is
  easy. §13.4.

  **Three silent failure modes were hit entering this one part**, all caught by
  netlist diff or datasheet check rather than by ERC/DRC. Recorded because the
  pattern generalises to any four-terminal magnetic:

  1. ~~**The KiCad generic symbol disagrees with the part.**~~ **Resolved by using
     a part-specific symbol** (`1812CAN-513NRC`), whose pins 1 and 4 sit on the
     same side — so the naive wiring is now the correct one and the drawing reads
     without crossings. The constraint lives in the symbol geometry rather than in
     a note. Kept below as the *reason*, not a live warning:
     `Device:Filter_EMI_CommonMode` puts pins 1 and 3 on one side and treats the
     windings as 1–2 / 3–4. Coilcraft's are **1–2 and 4–3 with the dots at 1 and
     4**. Wiring through the symbol as drawn puts the transceiver on pins 1 and 3,
     reversing one winding — which converts a common-mode choke into a
     **differential-mode** one: common-mode noise passes unfiltered and 51 µH
     lands in series with the CAN signal.

     **The rule stated physically, which is the version worth remembering:**
     *both bus lines must enter at the same physical end of the part.* Pins 1 and
     4 are the two pads **1.73mm** apart at one end; 2 and 3 are the pair at the
     other. The windings run the length, so the **3.68mm** pairs (1–2, 4–3) *are*
     the windings and the 1.73mm pairs are same-end neighbours. That is the same
     statement as "dots at 1 and 4", but it survives a reader who does not recall
     the dot convention — and it makes the failure obvious, since entering at
     opposite ends sends the two signals through the core in opposite directions.
  2. **The UltraLibrarian footprint is wrong in one axis.** Body outline, pad
     sizes and the 3.68mm along-length spacing are all correct; the across-width
     spacing is **3.68mm where Coilcraft specifies 1.73mm**. Terminals would
     overlap their pads by ~0.17mm and the part would float or tombstone. The
     signature is a square 3.68 × 3.68 pattern — what an IPC generator produces
     from a "4-pad array at the body corners" assumption, where the real
     terminals are inset to 0.865mm from the centreline. **Use the stock
     `Inductor_SMD:L_CommonModeChoke_Coilcraft_1812CAN`**, which matches
     Coilcraft and adds no fourth third-party import to a board where §12 already
     calls pin-1 rotation on imports "the most likely defect".
  3. **The value code reads as the wrong number.** `513` is EIA three-digit in
     **nanohenries** — 51 × 10³ nH = 51 µH, not 510. Someone "correcting" it to
     look more like 51 would order the wrong part.

  None of these is visible to ERC or DRC: every net stays connected, every
  footprint is present, and the board assembles. They surface as "CAN doesn't
  work properly" at bring-up, where the transceiver, crystal and bit timing would
  all be suspected first.
- **Status LEDs and test points** (§8.4) — power good, CAN activity, GPS fix, plus
  probe points on LIN, RXD and EN. In-vehicle debugging currently costs an SSH
  round trip, and on a first board that cost is paid far more often.
- **C4 placement** — it sits out by the connector rather than at U2's LIN pin
  (§8.3). TI's own layout example puts it at the pin. Make it a decision either
  way rather than an accident.
- ~~**C5/C6 and C4 as C0G/NP0**~~ — **withdrawn 2026-08-13, already done.** §12.1
  has specified these since 2026-08-11 (and C4 at 100V, better than the M2's
  50V). See the retraction in §13.4.1. Nothing to change; the residual action was
  documentation and is applied in §3.1.1. Likewise **C5/C6 = 27pF**, which §12.1
  records as already changed.
- **Everything still open in §8.3.** That list was "confirm before fabrication"
  for rev 1.0 and none of it was ever confirmed against a board. It transfers to
  rev 2.0 wholesale.

#### 13.2.2 [blind] — items whose gate no longer exists

Each was deferred pending a §13.1 measurement. Recorded with the flip, because
"we decided this without the data" is a different claim from "we decided this",
and the next board should know which it inherited.

| Item | Was gated on | Decided instead | Why |
|---|---|---|---|
| **Depopulate R7/R9** | Meas. 6 — I2C rise times with the Pi's own 1.8 kΩ pull-ups | **Keep the footprints, ship DNP** | The Pi's pull-ups are sufficient on their own and earlier revisions of §5 explicitly said not to add these. Reversible in both directions; DNP is the direction that matches the original intent. |
| **GPIO-controlled K-line pull-up FET** | Meas. 2 — does the 1 kΩ disturb an NT510/UpRev session | **Fit it** (below) | Five parts. It makes sniff mode the safe default when the Pi is off, booting or crashed — which matters more, not less, on a first board — and it turns measurement 2 itself from an under-dash unstack into a GPIO toggle. |
| **BMP390 swap** | Meas. 9 — BMP384 RMS noise vs the grade budget | **No board change** | Footprint-compatible (§3.5). Ship the BMP384 and swap on the bench if it falls short. The gate costs nothing to leave open. |
| **C2 populate vs DNP** | Bring-up — TLIN1027 upsets during crank or hot-plug | **No board change** | Footprint present regardless; §3.7.1 shows the capacitance is not load-bearing. |
| **MAX-M10M → MAX-M10S** | Meas. 4 — GNSS TTFF and C/N₀ under the Pi's WiFi hotspot | **Defer — keep the M10M.** *Changed 2026-08-22, see below* | Was "the one blind item that is not free", forcing a part change before fabrication with no data. The Integration Manual removes that pressure: the desense mitigation has a **configuration-only first move**, so the decision becomes measurable on rev 2.0 instead of guessed ahead of it. |

**Why the M10S decision defers — Integration Manual §4.3.1.** The M10M's internal
LNA has three gain modes, and u-blox states the thresholds plainly: "for RF
front-end designs with **15 dB or higher total external gain, low-gain mode** can
be used to improve immunity", and 25 dB or higher → **bypass mode**. A typical
active GNSS antenna is 20–28 dB, so this rig almost certainly qualifies for
low-gain and may qualify for bypass. The mode is settable at run time in BBR/RAM
(`CFG-HW-RF_LNA_MODE` plus a software reset) or permanently in OTP.

That does **not** make the M10S unnecessary — reducing internal LNA gain helps
when the internal LNA is the stage being overloaded, whereas the M10S's SAW
filter rejects out-of-band energy *before* any gain stage, which is the stronger
fix. But it means measurement 4 can be run on rev 2.0 hardware against three gain
settings before spending a part change, rather than the part change being a blind
pre-fabrication bet. **Keep the M10M; add the LNA-mode sweep to measurement 4.**

#### 13.2.3 [pulled forward] — rev-3.0 hooks that cost one net

Rev 1.0 could treat these as "later" because a rev 2.0 was coming anyway. Rev 2.0
is the board that gets lived with, so the calculus changed: **a missing net is a
re-spin, an unpopulated footprint is free.**

- **12V sense — divider plus a small I2C ADC.** §13.2 has queued a 12V divider
  since it was written, and §13.4.1 item 7 supplies ready-made values from the
  M2: a **10.0 kΩ / 2.20 kΩ divider with a 16.2V zener clamp**, ratio 0.180,
  putting 14.4V at 2.60V and 16V at 2.88V into a 3.3V input. **But the item had
  no destination** — there is no ADC on this board and the Pi 5 has none, so
  "a divider into an ADC" was not buildable as written. Rev 2.0 adds a small I2C
  ADC (MCP3021 / ADS1015 class) behind the divider, which gives the daemon the
  actual voltage rather than a threshold bit: engine-running (~14V), key-off
  (~12.4V) and cranking all fall out of one channel, including the
  key-on-engine-off case that `vehicle_state` reports as `ignition_only` and that
  a comparator would miss. Put the divider where an MCU can also reach it —
  §13.3.3(b) turns the same net into crank-profile and alternator-ripple
  diagnostics once it is sampled at kHz rather than read as a status bit.
  **Watch the bus:** §5 has the X1202 fuel gauge at 0x36 on I2C1 with the
  BMP384 at 0x77 and LSM6DSOX at 0x6B — check the chosen part's address strap
  against all three, and note §13.3.4's warning about what must not become flaky
  on that bus.

  **Parts selected 2026-08-22 — and the M2's ready-made values do not survive
  contact with this board.** Two of the three numbers §13.4.1 item 7 promoted
  had to change.

  | Ref | Part / value | Why |
  |---|---|---|
  | U8 | **TI `ADS1015IDGSR`** — 12-bit, 4-ch, I2C, **VSSOP-10 (3 × 4.9mm)** | Addresses `0x48`–`0x4B` via the ADDR pin (GND/VDD/SDA/SCL), clear of 0x36, 0x6B and 0x77. 150 µA operating, **0.5 µA powered down**. VDD 2–5.5V. Set FSR **±4.096V** — ±2.048V would clip our 2.92V full scale. LSB is 2.0 mV, i.e. **11.1 mV referred to the 12V side**. Order the **VSSOP** part: the X2QFN is 1.5 × 2mm and not hand-solderable. |
  | R, R | **100 kΩ / 22.0 kΩ**, 1% | **Not the M2's 10.0k/2.20k.** Same ratio (0.1803 — 14.4V → 2.60V, 16V → 2.89V), ten times the impedance. See below. |
  | — | **no clamp diode** | Also not the M2's 16.2V zener. See below. |

  **Why the divider is scaled 10×: the M2's values would consume rev 3.0's entire
  sleep budget on their own.** 10.0k/2.20k draws **~1.18 mA at 14.4V**, and this
  divider cannot be switched off — §13.3.1's supervisor needs it live to decide
  when to wake, so it sits ahead of the load switch permanently. That section
  budgets **"HAT asleep + µA supervisor: <1 mA"** in total. At 100 kΩ/22.0 kΩ the
  draw is **118 µA**, about a tenth of the budget, and the ratio is identical
  because both resistors scaled together. The M2 has no sleep state, so its values
  were right for it and wrong here — the same shape as its SMBJ14A TVS (§13.4.2).

  **Why no clamp diode, and why the M2's zener would have been a defect.** Their
  16.2V zener sits across the *12V input*, sized for the M2's clamp. Ours is D1 at
  **38.9V**, which puts **7.0V** on the divider node — nearly twice the ADS1015's
  `VDD + 0.3` = **3.6V** absolute maximum. Copying the value would have left the
  ADC exposed to exactly the case the zener is supposed to cover. But at 100 kΩ no
  clamp is needed at all: the ADC's own input protection holds the node at 3.6V and
  the series resistor limits fault current to **(38.9 − 3.6)/100k ≈ 353 µA**,
  against TI's **±10 mA continuous input-current** rating — a 28× margin, and
  another consequence of scaling the divider up rather than a separate decision.

  **Spare channels are worth having deliberately.** The ADS1015 is 4-channel and
  we need one. §13.2.3 also lands the current-sense shunt footprints, whose
  monitor was scoped to rev 3.0 because "continuous current data is only worth
  having when something is awake to read it" (§13.4.4). A spare ADC channel *is*
  something awake to read it — so populating the `ZXCT1109` becomes a rework on
  rev 2.0 rather than waiting for the MCU. Reserve a channel for it and route the
  net.

  **This is the board's first genuine cross-domain path, and §3.7.5 is owed an
  argument.** That section's case for a single rail was that nothing on rev 1.0 is
  alive when the Pi's 3V3 is down. The divider breaks that: it is fed from vehicle
  12V and drives an input whose VDD is the Pi's 3V3, so in HAT+ STANDBY it faces a
  dead rail. The magnitude is small — the node clamps near 0.3V and draw is
  **~141 µA**, 70× inside the ±10 mA rating and far too little to back-power
  anything — and the 100 kΩ is doing exactly the job §13.3.2 prescribes for
  crossing nets ("series resistance on every crossing net", R6's 1 kΩ precedent).
  **Stated rather than assumed**, because §13.3.2 says HAT+ conformance becomes
  something to argue rather than inherit, and this is the first place that bites.

  **Rejected: `MCP3221`** (12-bit, SOT-23-5, single channel). Cheaper and smaller,
  but it is a SAR with a sampling capacitor and Microchip's own guidance is to
  drive it from an op-amp with "tens of ohms" output impedance. Against our
  18.0 kΩ source impedance that would force either the 1 mA divider or an added
  buffer. The ADS1015 is a ΔΣ with ~6 MΩ common-mode input impedance, where
  18.0 kΩ is a **0.30% gain error** — calibrate once against a meter and it is
  gone. Its address is also fixed at manufacture rather than pin-selectable.
- **LSM6DSOX INT2 (pin 9) → a free Pi GPIO.** Unconnected on rev 1.0 and called
  out as free in §13.3.6 item 10. One net, no parts, and it is what motion-wake
  and parked monitoring (§13.3.3d) need to exist at all.
- ~~**0.01Ω current-sense shunt and its monitor footprints, unpopulated.**~~
  **Dropped entirely 2026-08-23 — shunt included.** §13.4.4 anticipated this case
  ("if rev 2.0 is fabricated first, add the shunt and its footprints then"), and
  §13.4.1 item 6 listed three uses from the M2. Examined one at a time, none of
  them survives for this board.

  1. **It does not close §13.1 measurement 7.** That measurement is the board's
     *3.3V* draw, and the 3.3V rail is fed from the Pi header — a shunt on
     `+12V_RAW` cannot see it. Closing measurement 7 needs a shunt in the `+3V3`
     feed, a different circuit, for a figure you take once on a bench supply.
  2. **Parked parasitic draw needs the Pi off, and then the Pi cannot read the
     ADC.** Rev 2.0 has no sleep state: the Pi is on whenever 12V is present, so
     "parked draw" is just its ~250 mA, which is already known. The micro-amp
     version only exists once §13.3.1's supervisor cuts the Pi — and at that
     point the **MCU** must read it. That is exactly why §13.4.4 scoped the
     monitor to rev 3.0. *The argument that "a spare ADC channel is something
     awake to read it" was wrong: the Pi is awake precisely when the measurement
     is uninteresting.*
  3. **The firmware-independent over-current trip contradicts the fuse
     decision.** §3.7.1 removed the PTC on the reasoning that the branch does not
     need fault protection. A comparator-plus-FET cutoff is the same protection
     in a more elaborate form; re-adding it needs a new argument and there is
     none.

  **And the "a missing net is a re-spin" case does not hold here either.** The
  shunt sits in series in the 12V path, so retrofitting means breaking that
  path — but **rev 3.0 redesigns that stage regardless** (§13.3.2's always-on
  rail and the load switch on the J3 leg both live there), so adding a shunt then
  costs nothing extra. The same reasoning that kept Y1 unchanged.

  **`U8` AIN1 stays free.** That costs nothing and leaves a channel if a use
  appears — but nothing in rev 2.0 needs it, and it should not be treated as
  reserved for a monitor that is no longer planned.
- **Y1 → 3.2 × 2.5mm SMD crystal.** The HC49/US can is 11.4 × 4.7 × 3.3mm, and
  §3.1.1 already flags it as physically large. §13.3.6 wants the area back once
  an MCU arrives; doing it now means the MCU respin does not also have to be a
  crystal respin. Keep the `F` in `ABLS2-40.000MHZ-D4YF-T`'s equivalent — at
  40 MHz the standard product is 3rd overtone and will not run in the
  MCP2518FD's Pierce oscillator (§3.1.1, §8.3).

**Deliberately not pulled forward: the supervisor MCU itself.** §13.3.2's HAT+
STANDBY conformance argument for a live always-on rail is owed and unwritten, and
§13.3.6 is 15 nets. That is a board revision, not an addition to this one.

#### 13.2.4 GPIO-controlled K-line pull-up (replaces the J5 jumper)

*Was gated on measurement 2 in §13.1 — see §13.2.2 for why it is now fitted
rather than deferred.*

```
+12V ──┬── Ra 100k ──┬── gate ── Q1 (P-FET, BSS84)
       │             │           source = +12V
       └─────────────┴───────────drain ──> R5 (1k) ──> D5 ──> LIN
                     │
                  Rb 10k
                     │
                  Q2 drain (N-FET, BSS138), source = GND
                  Q2 gate ── GPIOxx, 100k pulldown to GND
```

- GPIO low **or floating** → Q2 off → Q1 gate at +12V → Vgs = 0 → pull-up
  disconnected → **sniff mode is the safe default**, so a Pi that is off,
  booting or crashed cannot perturb an NT510 session.
- GPIO high → Q2 on → Q1 gate at ~1.1V → Vgs ≈ −11V → pull-up active.

Numbers: Vgs −11V is inside BSS84's ±20V limit; the divider wastes ~110 µA;
Q1's ~10Ω Rds(on) is a 1% error on a 1 kΩ pull-up; D5 still blocks bus-side
leakage when Q1 is off.

**Two approaches that do not work**, recorded so they are not re-attempted:
driving the P-FET gate directly from a GPIO never turns it *off* (3.3V against a
12V source is still Vgs = −8.7V), and a low-side N-FET cannot sit in the LIN leg
because its source would be at bus potential, needing gate drive above the rail.

**The hedge may still turn out to be unnecessary — fit it anyway.** With 1 kΩ
paralleling an NT510's ~510Ω, a transmitting tool sinks roughly 40 mA to reach
dominant, well inside what a commercial scan tool drives, so measurement 2 may
well come back "the jumper disturbed nothing" and these five parts will have been
redundant. That was a reason to defer while a second board was coming. It is not
one now: five 0603-class parts are cheaper than the wrong answer on the only
board, and the circuit is what lets measurement 2 be taken at all without
unstacking the HAT from under the dash. If the result is negative, rev 3.0 drops
the parts and keeps the GPIO — which it wants anyway (§13.3.6 item 13).

#### 13.2.5 The one item that ships untested

**The P-FET input stage is the only change on this list that replaces a working
circuit with a new one**, and rev 2.0 is now the first board — so it ships with
no bench validation, where rev 1.0's SS54 Schottky at least had a well-understood
failure mode. The M2 is proven prior art for the topology, which is why it is on
the list at all, but "proven on someone else's board" and "measured on ours" are
different claims and §13.1 exists because this project keeps conflating them.

**Mitigation, in the M2's own idiom (§13.4.1 item 4 — fit the footprint, gate the
connection): keep D4's SS54 SMB footprint on the board** as a bypass position, so
a P-FET stage that misbehaves at bring-up is one rework back to the rev-1.0
circuit rather than a re-spin. The ~0.4–0.5V drop and the unprotected X1202 leg
are exactly the trade §3.7.4 already characterised and accepted once; being able
to fall back into it costs one footprint.

Everything else on the list is either a value/name/land correction, an
unpopulated footprint, or a net that did not exist before — none of which can
make a working board stop working.

### 13.3 Rev 3.0 — the supervisor MCU

The goal that opened this section: leave the rig plugged into OBD permanently
without draining the vehicle battery. Working it through settles on a
**supervisor MCU** rather than a comparator (§13.3.1) — and once an always-on MCU
exists, a set of capabilities the Pi structurally cannot provide come nearly
free, several of which close defects and procedural hazards recorded elsewhere in
this document.

**The board revision owes the MCU only *connections*.** Firmware grows for years
after fabrication; a missing net means a re-spin. So the design question is
*which nets must exist*, not *what the firmware should do* — which is what makes
the marginal items in §13.3.3–§13.3.4 cheap enough to include on spec, and why
the net list (§13.3.6) deserves care even where the firmware behind it is still
speculative.

| | |
|---|---|
| §13.3.1 | Sleep, wake, and why the Pi must be genuinely off |
| §13.3.2 | The power-domain question — settle this before drawing anything |
| §13.3.3 | Capabilities the Pi cannot provide |
| §13.3.4 | Hardware-side benefits, by subsystem |
| §13.3.5 | The bus-access rule: receive-only fan-outs |
| §13.3.6 | Nets the board must carry |
| §13.3.7 | Deliberately out of scope |
| §13.3.8 | Rejected: PCIe as the data transport |
| §13.3.9 | Rev 2.1 deltas rev 3.0 inherits |

#### 13.3.1 Sleep, wake, and why the Pi must be genuinely off

**One net change unlocks most of the HAT side.** The TCAN1042V's STB pin is
currently hardwired to GND, which permanently forbids standby. Per its §9.4.3
and EC table:

| TCAN1042V mode | VCC (5V) | VIO (3.3V) |
|---|---|---|
| Normal, recessive | 1.5–2.5 mA | 90–300 µA |
| **Standby** | **0.5 µA** — *VCC may be removed entirely* | **12–17 µA** |

In standby the low-power receiver detects the ISO 11898-2 **Wake-Up Pattern** and
signals it by driving RXD low — this is already a wake-on-CAN transceiver.
§9.3.5 also notes STB has an internal pull-up forcing standby when floating, so
**STB on a GPIO gives low-power-by-default** and requires software to assert
normal mode. Combined with the TLIN1027 (8–12 µA sleep, wake-on-LIN, already on
GPIO26), MCP2518FD (15 µA sleep), BMP384 (2 µA) and the LSM6DSOX's wake-on-motion,
the HAT reaches roughly **50 µA**.

**The Pi is the whole problem.** Against ~20 Ah of usable battery before a
no-start becomes a risk:

| State | Draw at 12V | Time to 20 Ah |
|---|---|---|
| Pi 5 idle | ~250 mA | **~3 days** |
| Pi 5 halted | ~80 mA | ~10 days |
| HAT asleep + µA supervisor | <1 mA | months |

So the Pi must be genuinely **off**, and something else must turn it back on.

**Proposed architecture — a load switch on the J3 pass-through leg**, reusing the
shutdown path that already exists rather than replacing the X1202:

```
+12V_RAW ──> [high-side load switch, ~3A] ──> J3 ──> X1202 ──> Pi
                        ▲
                 supervisor (µA)
```

- *Wake:* supervisor closes the switch → X1202 gets 12V → Pi boots.
- *Sleep:* supervisor opens the switch → X1202 sees power loss → PLD on GPIO6 →
  `ups_shutdown_watch.py` shuts down gracefully → Pi off.

This avoids the HAT having to generate 5V for the Pi, which would make it a
Power HAT+ (§3.8).

**Wake sources, cheapest first:**

1. **Alternator voltage sense.** Engine running ~13.8–14.4V vs key-off ~12.4V; a
   comparator with hysteresis at ~13.2V is unambiguous, needs no bus activity,
   ~6 parts, ~50 µA, no firmware. Misses key-on-engine-off, which the project
   does use (`ignition_only`).
2. **CAN wake via the TCAN1042.** Catches KOEO, but its low-power receiver runs
   from **VIO = Pi 3V3**. Pi off ⇒ no VIO ⇒ no wake detector. Requires a small
   always-on 3.3V rail from vehicle 12V — reintroducing the buck deleted in §3.7,
   though now at a few mA and with a concrete justification rather than the
   current-headroom argument that did not survive scrutiny.
3. **Supervisor MCU.** All wake sources, timers, and a proper handshake letting
   the Pi request its own shutdown. Costs firmware and a programming header.

**Decided: option 3.** Not because 1 and 2 fail on their own terms — a
comparator is genuinely cheaper and needs no firmware — but because the
handshake at the end of this section already forces an MCU (a comparator can cut
power; it cannot *agree with the host* about when), and because options 1 and 2
stop being separate line items once an MCU is present: alternator sense is one
ADC channel, CAN wake is one interrupt pin. The MCU absorbs both wake circuits
rather than adding to them. Everything in §13.3.3 onward is what that decision
then makes available.

**X1202 behaviour — confirmed 2026-08-11** from `scripts/ups_shutdown_watch.py`,
which documents the full cycle:

> "The X1202 then cuts battery output, so the next time switched 12 V returns
> (key-on) it's a fresh power-apply event and the board auto-boots."

- **Auto-boot on 12V return: yes.** No button press, so the load-switch approach
  is viable.
- **Power-loss signalling: yes**, via PLD on GPIO6, HIGH = input OK, LOW = lost.
- **It cuts its own battery output after shutdown**, so post-sleep draw should be
  small. Still worth measuring, but it is no longer a blocking unknown.

The historical unreliability is understood rather than mysterious: an earlier
version had the **PLD polarity inverted**, and PLD alone cannot distinguish
vehicle key-off from bench USB-C with the barrel jack unplugged. That second
ambiguity is now resolved by fusing PLD with the fuel gauge's discharge rate
(`power_truly_lost()`), so a flat SoC on bench power never trips a countdown.

**Do not route routine sleep through the power-loss path.** `UPS_SHUTDOWN_DELAY`
defaults to **300 s**, so simply cutting 12V leaves the Pi running five more
minutes *on the UPS battery* before it powers off. Every key-off would partially
cycle a cell whose actual job is emergency shutdown, and each sleep transition
would take ~6 minutes. Use a handshake instead:

1. Daemon sees sustained `engine_off` / `no_data` (the §3.7.6 software, still
   unwritten).
2. Daemon signals the supervisor "cutting in N seconds", then `systemctl poweroff`.
3. Supervisor waits, confirms the Pi is down, opens the switch.
4. The X1202 has nothing to hold up — the battery is never touched.

`UPS_SHUTDOWN_DELAY` then remains what it should be: the emergency path for
*unexpected* power loss, not the routine one. The handshake needs a bidirectional
link (at least one GPIO each way), which pushes the supervisor toward an MCU
rather than a bare comparator — a comparator alone can only cut power, not agree
with the host about when.

**Still to measure:** the X1202's quiescent draw once it has cut output, and
whether its auto-boot is reliable enough to be the only wake mechanism.

**Related software** (§3.7.6): escalating a sustained `engine_off` / `no_data`
`vehicle_state` to a shutdown still does not exist, and is a prerequisite for any
of this regardless of which wake source is chosen.

#### 13.3.2 The power-domain question — settle this before drawing anything

An always-on 3.3V rail is what every item below depends on, and it reintroduces
exactly what §3.7.5 designed out. That section's argument was never about current
headroom — it was that the HAT sits between a vehicle feed and a UPS-backed host,
so **each supply can outlive the other**, and with split rails a device is powered
while the domain driving its I/O is not. The single-rail board satisfies the HAT+
STANDBY requirement *by construction*. Rev 3.0 breaks that construction
deliberately, so it has to satisfy the requirement by design instead.

The §3.7.5 state table with the MCU rail added:

| State | Vehicle +12V | Pi 5V | Pi 3V3 | MCU rail | Cross-domain? |
|---|---|---|---|---|---|
| Normal run | up | up | up | up | no |
| UPS battery run (key off, X1202 carrying the Pi) | **down** | up | up | **down** | yes — MCU side dead |
| STANDBY (`sudo halt`, vehicle connected) | up | up | **down** | up | yes — Pi side dead |
| **Parked, Pi powered off, vehicle connected** — the rev-3.0 steady state | up | **down** | **down** | up | **yes, and it is now the common case** |

The fourth row is new, and it is where the rig will spend most of its life. Every
MCU↔Pi net is then a powered driver facing an unpowered input: current through
the Pi's ESD clamps into a dead rail, and on a shared bus, a bus hung for whoever
is left alive.

Three mitigations. Rev 3.0 should state which one it uses **per crossing net**:

- **Series resistance on every crossing net.** R6's precedent (1 kΩ in the TXD
  line, ~10 ns RC against a 96 µs bit) — cheapest, and sufficient for slow
  handshake lines.
- **MCU I/O specified for an unpowered far side.** Check the datasheet rather
  than assuming; "5V tolerant" is a different claim.
- **The MCU must not drive a Pi-domain net high while 3V3 is down.** This is the
  rule that actually closes it, and it requires the MCU to *know*: one ADC
  channel or comparator on the Pi's 3V3 rail, tri-stating everything that crosses
  when it reads low. Note this is a firmware invariant guarding a hardware
  hazard — the shape AGENTS.md warns about — so back it with the series resistors
  regardless, and prefer a hardware default (crossing nets idle low, MCU drives
  only when it must) over a firmware promise.

**Decided 2026-10-01: take all three, on every crossing net.** This section has
stood open as "settle before drawing anything" while listing its own answer. The
three mitigations are not alternatives — series resistance is a few parts, the
I/O check is a datasheet read, and the tri-state rule is firmware that wants the
hardware default behind it anyway. Adopt the lot: series resistance on 4, 5, 6
and 14; crossing nets pulled to their safe state in hardware so an undriven net
is harmless; item 6's rail sense as the firmware belt on top. Nothing here is
worth a second round of deliberation.

**Item 14 is a crossing net too, in the other direction, and was missing from
the list.** Items 4, 5 and 6 are MCU→Pi. Item 14 is **Pi→MCU**: `BOOT0` and
reset driven from a Pi GPIO. In the fourth row above — the state the rig lives
in — that GPIO is an unpowered output facing an MCU input that is awake. If the
MCU's `BOOT0`/`nRST` float to the wrong level there, the supervisor sits in its
bootloader or in reset while parked, which disables wake, the watchdog and the
boot-gap buffer at once: every always-on capability, lost to a pull-down that
was never fitted. **Both nets get their pull in the MCU domain**, sized against
the Pi GPIO's leakage, so the safe level holds when the Pi side is dark. This is
the §13.3.2 hazard in the direction the section's framing ("the MCU must not
drive a Pi-domain net high") does not cover.

**HAT+ conformance becomes something to argue rather than inherit.** §3.7.5
quotes the spec: HAT+ boards "must be electrically compatible with the STANDBY
power state, where the 5V power rail is powered, but the 3.3V rail is not
powered," and "must not assume any particular sequencing timing between the 5V
and 3.3V rails." Rev 1.0 satisfies both by having nothing alive when 3V3 is down.
Rev 3.0 must show it satisfies them with a rail that *is* alive — a paragraph of
analysis owed to this document, not a checkbox.

**The regulator's quiescent current drives the sleep budget, not the MCU's sleep
mode.** Against ~20 Ah of usable battery, 1 mA is ~2.3 years and 200 µA is over a
decade — well past the point where the vehicle's own parasitic load and the
battery's self-discharge dominate. Specify a nA-class-Iq buck (TPS62840 class,
~60 nA) and the MCU's sleep figure stops being a selection constraint. That
matters because it reopens part choice: an RP2350's 520 KB of RAM (§13.3.3a) or
an STM32G4's three FDCAN peripherals become affordable, where a µA-first
selection would have forced a 32 KB part.

**One rail serves two jobs.** §13.3.1's wake source 2 needs VIO powered for the
TCAN1042's low-power receiver; the MCU needs the same voltage. That is one
regulator, and it is the concrete justification §13.3.1 asked for — as against
the current-headroom argument that "did not survive scrutiny" in §3.7.5.

#### 13.3.3 Capabilities the Pi cannot provide

Each is unavailable to the Pi for a structural reason: it is asleep, it has no
sensor, or Linux cannot make the timing guarantee.

**(a) The boot-gap black box.** The strongest case, and rev 3.0's own sleep
architecture is what creates it.

Once `idle_shutdown.py` (§11) plus the load switch put the Pi genuinely off when
parked, key-on → first recorded frame is the full boot time (§13.1 measurement
12). That window holds the data `docs/roadmap/archive/data-collection-plan.md` ranks
first and calls unfakeable — capture 1, the cold-start session: "warmup curve,
cold-enrichment fueling, open→closed-loop transition… **Can't be faked; needs an
actually-cold engine.**" There is one per day, and rev 3.0 is otherwise about to
lose the first 20–40 s of every one. Crank itself has never been observed at all.

The MCU is awake at that instant — deciding to wake *is* its job — so buffering
broadcast CAN from the moment of wake costs RAM and nothing else. 500 kbit/s at
realistic load for 40 s fits in a few hundred KB uncompressed. Frames carry
MCU-timebase timestamps (c), so the recovered window is not second-class data.

Note the scope: **broadcast only** — no requests, no ISO-TP, no poll plan in
firmware (§13.3.7). That suffices, because broadcast is already the primary live
path; `docs/data-architecture.md` on it: "zero polling cost, no contention,
deterministic cadence, high refresh rate."

**(b) Electrical-system diagnostics.** §13.2 already queues a 12V divider into an
ADC as a *status* input. Behind an MCU sampling at 1–10 kHz it becomes an
instrument, measuring quantities the ECU does not publish at all:

- crank voltage minimum and recovery slope → battery health
- crank duration → starter and cold-fuelling health
- alternator ripple → rectifier-diode failure, an ordinary fault at this vehicle
  age and invisible to OBD
- parasitic draw trend while parked, over weeks

These need no reverse engineering and carry no binding risk — rare in this
project — and they degrade slowly and monotonically, the exact shape
`docs/roadmap/archive/eval-longitudinal-and-dtc.md` Phase 4 exists to detect.

**(c) One hardware time base.** Today CAN frames, OBD responses, IMU samples and
GPS fixes are each stamped in a different place with different latency, all
through Linux scheduling. An MCU counter disciplined by the M10's TIMEPULSE can
stamp CAN RX, K-line bytes, IMU FIFO entries and its own ADC in **one monotonic
domain**, with the PPS edge tying that domain to GPS absolute time.

This is worth more here than in most projects. AGENTS.md's "never compare two
signals by their aggregates" rule, and `compare_channels`' insistence on
same-instant pairing, exist because cross-signal comparison has produced six
retracted findings in three months. A single hardware time base attacks that at
the source rather than in review.

It also largely dissolves `docs/time-sync.md`'s cold-boot problem: the MCU keeps
monotonic time across Pi power cycles, so a session's *internal* timeline is
never fractured by a mid-session step. That doc's outstanding "clock-jump session
roll" item drops from a correctness issue to bookkeeping.

**(d) Parked monitoring.** Battery voltage every few minutes, CAN wake events,
and motion via the LSM6DSOX — **INT2 (pin 9) is unconnected on rev 1.0**, so it is
free. Tiny data volume, ingested on the next boot. Catches a tow, a developing
parasitic drain, or someone in the truck.

**(e) A watchdog with power-cycle authority.** The MCU already holds the load
switch; a heartbeat line plus a timeout means a hung Pi recovers without someone
under the dash — currently the failure mode with the worst manual cost.

**The load switch must fail safe toward Pi-on** (added 2026-10-01). One MCU
carries two jobs whose failure requirements are opposite: the supervisor side
(this item, the load switch, safe-state ownership in item 13, wake) wants to be
boring and bulletproof, while the coprocessor side (§13.3.3a's ring buffer,
item 5's SPI bulk dump, kHz ADC sequencing, timestamp discipline) is the most
complex firmware on the board. Sharing one image makes the recovery path for the
worst-manual-cost failure depend on the most failure-prone code here.

A second MCU is the wrong fix — §13.3.6 already says area is tight. Fix the
*direction* instead: pull the load switch's enable so an MCU in reset, in its
bootloader, or hung leaves the **Pi powered**. A dead supervisor then degrades
the rig to rev 2.1 behaviour — always-on Pi, the ~250 mA/3-day drain of §13.3.1's
table, fully functional — rather than a dark board under the dash. Item 4's UART
handshake is what keeps that from being silent: the Pi sees no supervisor and
says so. Note the standing cost of this choice, and accept it — a crashed MCU
silently reintroduces the battery drain rev 3.0 exists to remove, which is
recoverable, where a rig that will not boot is a trip under the dash.

#### 13.3.4 Hardware-side benefits, by subsystem

**K-line (U2, TLIN1027) — the largest hardware win on the board.** §11 lists a
seven-step startup order the collector must execute correctly. Most of those
steps exist because a Linux tty cannot do what the part needs, and an MCU owning
the transceiver's host side removes them as a class rather than making them
easier:

| §11 / §3.3 requirement | Why Linux struggles | With the MCU |
|---|---|---|
| Blank RXD ~50 µs after raising EN (`tMODE_CHANGE` + `tNOMINT`); 1.5 ms after power-up | userspace cannot gate a UART RX at µs scale — the garbage lands in the tty buffer and is filtered after the fact | receiver gated for exactly that window; the garbage never exists |
| Power-up lands in **standby**, RXD driven low = a continuous serial **break**; with a console on `uart0`, magic SysRq can kill or reboot the host (§11 step 1) | mitigated only by *remembering* to disable the console | the break physically cannot reach the Pi |
| Open the tty before raising EN; drop EN before releasing it, including on crash paths | depends on a Python process not dying at the wrong moment — R8 exists to cover part of it | EN raised only while the MCU is itself driving TXD: guarantees the *sequence*, not just the static level |
| Strip the local echo (single-wire) | heuristic matching on content and timing through a buffered tty | byte-exact — the MCU knows what it sent and when |
| RXD low means three different things: dominant bit, bus wake, or VSUP undervoltage (§3.3.2) | "the line alone cannot distinguish them" | the 12V ADC resolves the third case directly |
| 5-baud slow init — 200 ms per bit, up to ~600 ms dominant | termios cannot do 5 baud: drop the UART, re-mux TXD as GPIO mid-protocol, bit-bang, hand the pin back | ordinary firmware |

The last row is an *optimization, not an enabler* — the TLIN1027 was chosen
precisely so slow init is possible without an MCU (§3.3.1, no dominant state
timeout). It is listed because the Linux mechanics are worse than they look, not
because rev 1.0 cannot do it.

One addition rather than a fix: with the MCU driving §13.2's commander-pull-up
FET, the 1 kΩ can be enabled **only for the duration of a transaction** instead
of for a whole session. That shrinks the open NT510 question (§13.1 measurement
2) from "does the pull-up disturb a tool session" to "does it disturb one during
our own ~20 ms of talking."

**CAN (U3/U4) — modest, plus one option to record and defer.** The MCU should not
touch the working path: MCP2518FD → mainline `mcp251xfd` → SocketCAN `can0` →
`can_collector.py` is clean and has no problem an MCU solves. The hardware story
is the receive-only RXD fan-out (§13.3.5) plus the standby-wake fix — the
TCAN1042's low-power receiver runs from VIO, so the always-on rail is what makes
wake-on-CAN work at all.

The option worth recording and *not* doing yet: the CANable2 already in the rig
is an **STM32G431 running candleLight/gs_usb** (`docs/canable-firmware.md`), the
build and flash tooling is committed (`scripts/build_canable_fw.sh`,
`firmware/canable2_fw-ba6b1dd.bin`), and the G431 carries three FDCAN
peripherals. An STM32G4-class supervisor could therefore present extra CAN
channels to the Pi as **native SocketCAN over USB on a mainline driver** — a
route back to the multi-bus capability rev 1.0 gave up (§10: 2 channels → 1), which
matters for the "generalized to any vehicle" intent in AGENTS.md, since plenty of
vehicles put the interesting traffic on a second bus.

**Deferred for two concrete reasons.** candleLight is a CAN adapter and nothing
else, so combining roles means implementing gs_usb *alongside* the supervisor
firmware rather than reusing the committed binary; and it needs a USB connector
on the HAT plus a cable to a Pi port, where every other interface on this board
arrives through the header. Pick one role for rev 3.0 — **supervisor, timebase and
sensors** — and leave CAN alone.

**IMU and barometer (U6/U5) — real gains, and one trap not to walk into.**

> **Do not put the MCU on I2C1.** §5 records the X1202 fuel gauge at **0x36 on
> bus 1**, and `scripts/ups_shutdown_watch.py` / `src/dashboard/ups.py` depend on
> it. Multi-master I2C between Linux and an MCU is a reliability hazard even
> where it is legal, and what it would put at risk here is the UPS shutdown
> path — the one subsystem on the rig that must not become flaky.

The clean split is **a private MCU I2C bus carrying the LSM6DSOX and BMP384
entirely**, leaving Pi I2C1 with the fuel gauge and nothing else. That buys:

- **FIFO offload.** The LSM6DSOX's on-chip FIFO with batching and timestamps,
  drained by the MCU, gives clean 416 Hz–1.6 kHz IMU data without Linux interrupt
  jitter and without spending Pi interrupts — which is what `imu_frame.py`'s
  gravity low-pass and yaw-offset resolution actually want.
- **A possible rescue for the BMP384.** §13.1 measurement 9 asks whether its RMS
  noise supports `derived:GRADE`, holding the footprint-compatible BMP390 swap in
  reserve (§3.5). Oversampling at high ODR on a hardware clock cuts noise as √N,
  which may close the gap and save the swap.
- **Sensors that work while the Pi is off** — motion wake, parked altitude and
  temperature, orientation continuity across a power cycle.
- **R7/R9 stop being a question.** §5's doubled pull-ups (~1.3 kΩ effective,
  ~2.5 mA sink per line) that §8.3 wants depopulated do not exist on a new bus.

**The timing for this is unusually favourable and will not recur.**
`imu_collector.py` does not exist, and §11 records that the barometer has "no
config, collector, or channel." Both collectors would be written against the MCU
link *from the start* rather than migrated onto it later.

**GNSS (U7) — the MCU's rail closes a recorded defect for free.** §3.6:
"**Backup supply has no fallback.** V_BCKP is fed *only* from BT1 through D2. If
the JST connector is empty the pin floats… the receiver cold-starts every boot —
the exact cost PPS exists to avoid," and u-blox expects V_BCKP diode-OR'd with
the main rail. The always-on 3.3V rail **is** the missing OR leg: one diode
closes it, and whether BT1 is fitted stops mattering.

Beyond that:

- ~~The M10's 10 Hz nav rate is volatile configuration (§3.6).~~ **Weaker than
  stated — corrected 2026-08-22 from the Integration Manual.** Configuration has
  three layers, not one: **RAM** (lost at power-down), **BBR** — battery-backed
  RAM, "retained as long as the backup power supply remains", i.e. for as long as
  BT1 holds V_BCKP — and **OTP**, a 74-byte one-time-programmable store that the
  device applies automatically at every startup and that "cannot be reverted". So
  the nav rate can be made to survive power cycles without any host or MCU
  involvement, and this benefit largely evaporates. What survives is narrower: OTP
  is irreversible and space-limited, and BBR dies with BT1 — which §8.3 now costs
  at roughly ten months on a CR2032 at the measured 32 µA. **Losing V_BCKP
  therefore costs more than §3.6 recorded: not just hot-start ephemeris, but any
  configuration held in BBR.** That strengthens the V_BCKP diode-OR below rather
  than this bullet. The MCU re-applies
  it deterministically at every power-up, before the Pi is alive.
- Keeping the receiver tracking while the Pi is off gives a **hot fix at key-on**
  and — more usefully — gives the MCU valid *absolute* time at the instant of
  wake, which is what makes §13.3.3(a)'s buffer timestamps real rather than
  merely relative.
- §3.6 notes the antenna bias feed has "no bias fuse or short-circuit
  protection." A sense resistor plus one ADC channel gives open/short antenna
  detection, currently neither protected nor observable.

**Debug.** §13.2 wants status LEDs because "in-vehicle debugging currently costs
an SSH round trip." Driven by the MCU they mean something in the case that
matters most — when the Pi is the broken part: no 12V, CAN silent, Pi not
answering the heartbeat, GNSS no fix.

#### 13.3.5 The bus-access rule: receive-only fan-outs

**Every MCU bus connection is a receive-only fan-out. Transmit stays with the
Pi's existing path.**

The TCAN1042's RXD is a push-pull CMOS output, so it drives the MCP2518FD's RXCAN
and an MCU input with no contention. The same holds for K-line RXD (already
pulled up by R3) and the GNSS TX line. The MCU gains full receive-side
visibility — boot-gap buffer, timestamps, wake detection — and is *physically
incapable* of disturbing a vehicle bus or contending with SocketCAN.

This is what preserves the software stack: `mcp251xfd` stays mainline, `can0`
stays SocketCAN, `can_collector.py` is untouched. **The rejected alternative** is
an MCU with an integrated CAN FD controller replacing the MCP2518FD —
electrically tidier (one controller per transceiver, direct frame access) but it
discards the mainline driver and everything downstream of it. Recorded so it is
not re-proposed on the tidiness argument alone.

K-line is the one deliberate exception: §13.3.4 has the MCU owning that UART in
both directions, because the value there *is* the transmit-side sequencing. If
that is judged too large a step, the fallback is an RX-only K-line tap with EN
and TXD left on the Pi — which keeps the echo-stripping and break hazards and
earns none of the §13.3.4 table's guarantees.

#### 13.3.6 Nets the board must carry

Fix this list before layout: a missing net is a re-spin, unused firmware is free.

| # | Net | For |
|---|---|---|
| 1 | **Always-on 3.3V** from `+12V_RAW`, nA-class-Iq regulator | MCU + TCAN1042 VIO (wake) + GNSS V_BCKP OR leg (§13.3.2) |
| 2 | 12V divider → MCU ADC | crank/alternator/parasitic (§13.3.3b); K-line undervoltage disambiguation. **Divider lands in rev 2.0** behind a small I2C ADC (§13.2.3) — rev 3.0 re-points it at the MCU for kHz sampling. **Rev 2.1 makes this four channels** (`V12_SENSE`, `MAP`, `IAT`, `5V_SENS`; §13.5.3), and MAP/IAT are ratios against `5V_SENS`, so all three must land on the *same* ADC — §13.3.9 |
| 3 | MCU GPIO → high-side load switch on the J3 leg | §13.3.1 sleep/wake |
| 4 | MCU ↔ Pi UART pair | handshake; works whenever the MCU is up |
| 5 | MCU ↔ Pi on ~~SPI0 CE1 (GPIO7, pin 26)~~ **SPI0 with a GPIO chip select on GPIO20** | bulk buffer dump; a few hundred KB over UART is painful. **CE1 is spent by rev 2.1 on CAN1** (§13.5.4); re-decided in §13.3.9 |
| 6 | Pi 3V3 rail sense → MCU | the tri-state invariant in §13.3.2 |
| 7 | TCAN1042 RXD → MCU (fan-out) | boot-gap capture, wake, timestamps. **Rev 2.1 adds U11 (CAN1/aux)**: its RXD fan-out is one more net and captures fuel pressure across the boot gap; wake from it is pointless (the gauge is on switched 12 V) — §13.3.9 |
| 8 | K-line RXD / TXD / EN → MCU | §13.3.4; supersedes the Pi's `uart0` connection |
| 9 | GNSS TX + TIMEPULSE → MCU (fan-out) | time base, hot fix, absolute time at wake |
| 10 | **LSM6DSOX INT2 (pin 9 — unconnected on rev 1.0)** → MCU | motion wake. **Landed in rev 2.0** as a net to a free Pi GPIO (§13.2.3); rev 3.0 re-points it |
| 11 | Private MCU I2C → LSM6DSOX + BMP384 | §13.3.4 — **must not be I2C1** |
| 12 | ~~GNSS antenna bias sense → MCU ADC~~ **→ u-blox two-pin antenna supervisor** | Open/short antenna detection, **but not via the MCU** — the M10M does it itself. Deferred here from rev 2.0 on 2026-08-22. See below. |
| 13 | STB (U4), **STB (U11, rev 2.1)**, **U9 sensor-rail EN (rev 2.1)** and the §13.2 pull-up FET gate → MCU | safe-state ownership. **U4 STB and the FET gate exist in rev 2.0** as Pi GPIOs (§13.2.1, §13.2.4); rev 2.1 adds U11 STB on GPIO19 and ties U9 EN to `+3V3` (§13.5.3, §13.5.4); rev 3.0 moves all of them to the MCU |
| 14 | **A flash path from the Pi** — SWD bit-banged from Pi GPIO, or a UART bootloader with BOOT0 on a Pi-drivable pin | see below |
| 15 | Status LEDs → MCU | §13.2, §13.3.4 |

**Item 12 changed shape entirely.** It was written as an MCU ADC channel watching
the antenna bias, because §3.6 recorded that feed as unprotected and nothing else
could see it. The Integration Manual (read 2026-08-22) shows the receiver already
has the function — a **two-pin antenna supervisor**, and both its signals stay on
the module, so it costs **zero MCU pins and zero Pi GPIOs**. Corrected part count
for our board: **four** — `T1` P-FET, `T2` N-FET, `R7` 100 kΩ pull-up, `R8` 10 Ω
0.25 W current limiter. u-blox's `L3` is already our L1 at the same 27 nH value,
and their open-drain buffer `U7` is unnecessary because V_ANT (VCC_RF = VCC −
0.1 V = 3.2 V) sits in the same domain as V_IO. Wire `ANT_OFF_N` to **LNA_EN
(pin 13)** and `ANT_SHORT_N` to a free PIO — EXTINT, SDA or SCL, none of which
this board uses. *Confirm the PIO-index-to-package-pin mapping in the interface
description: u-blox's reference says `ANT_SUP_SHORT_PIN 5`, but their three-pin
design calls the I2C pins "2" and "3" where the package numbers them 16 and 17,
so the two numbering spaces are not the same.*

**Why it is worth having at all**, since rev 2.0 ships without it: `ICC_RF` = 100 mA
is an **absolute maximum**, not an operating limit, and the module has no internal
protection — so a shorted coax kills U7 outright. `R8` alone does not fix that
(3.2 V across 10 Ω is ~320 mA, still over the limit); it only survives the
detection window, and `T1` is what actually disconnects the supply. That pairing
is also what makes the circuit **antenna-agnostic**: nothing is tuned to a
particular antenna's operating current, so any active antenna works, and
`ANT_OFF_N` doubles as software bias control so a *passive* antenna needs no
hardware change either. The standing limit is unrelated to the supervisor —
VCC_RF is 3.2 V, so 5 V-only active antennas need an external bias leg regardless.

**One consequence of deferring, worth taking deliberately.** `R8` and `T1` sit
*in series* in the antenna supply path, which on rev 2.0 runs L1 → J4 with nothing
between. Retrofitting them in rev 3.0 therefore means breaking and re-routing that
path inside the RF region — the most constrained area on the board. If rev 2.0
lands the four footprints unpopulated with solder-bridge links across the `R8` and
`T1` positions, plus the two module-side signal traces, rev 3.0 becomes a populate
rather than an RF re-layout. That is §13.4.1 item 4's own discipline — fit the
footprint, gate the connection — applied here. **Not adopted; recorded as the
cheaper option if the RF area turns out to have room during layout.**

**Rev 2.0 already lands items 2, 10 and 13** (§13.2.3), so this list is three
nets shorter than it was — the point of pulling them forward. The remaining
twelve are what still make rev 3.0 a board revision rather than an addition.

**Item 14 is a hard requirement, not a convenience.** If updating firmware means
pulling the board out from under the dash, firmware stops being updated, and
every capability above freezes at whatever shipped.

**Items 4, 5 and 6 are the crossing nets** that §13.3.2 governs — size their
series resistance there, not here.

Header budget — **re-counted 2026-09-14 against rev 2.1.** §4.1's list of ten
unconnected GPIOs is stale: rev 2.0 spent 22, 23 and 24 (STB, INT2, K-line
FET gate) and rev 2.1 spends 7, 27 and 19 (CAN1 CS, nINT, STB). **What remains
for rev 3.0 is GPIO12, 13, 20 and 21 — four pins.** `uart4` (GPIO12/13) is the
home for item 4; GPIO20 is item 5's chip select; GPIO21 is the one pin left
for item 14's `BOOT0`/reset. That closes with **zero spare**, which is why
§13.3.9 rules out the bit-banged SWD variant of item 14. Items 3 and 6 are
MCU-side pins and cost no header GPIO. Note that item 8 *frees* `uart0`
(GPIO14/15) on the Pi side — a net simplification of §11's K-line software work
rather than an addition to it.

**Recount, 2026-10-01 — that budget double-counts, and item 14 is not actually
blocked.** The count above debits GPIO19, 22, 23 and 24 as "spent by rev 2.0/2.1"
for U4 STB, U11 STB, INT2 and the K-line FET gate — but items 10 and 13 move
exactly those four functions to the MCU, and item 8 moves K-line off `uart0`.
On a rev 3.0 *re-layout* those traces are simply not drawn to the header, so the
pins come back: **19, 22, 23, 24 plus `uart0`'s 14/15 return, against 12, 13, 20
and 21 spent.** Rev 3.0 is pin-*positive*, by about six, not at zero spare —
which removes the stated reason §13.3.9 ruled out the bit-banged SWD form of
item 14.

Two caveats before spending the headroom. **(a)** A pin only returns if rev 3.0
declines to route it, so this is a layout commitment, not free inventory; decide
per pin at schematic entry. **(b)** There is a real argument for keeping one or
two as a **Pi-side fallback** — a Pi that can still drive U4's STB directly is a
Pi that can put the vehicle transceiver in a safe state with the supervisor dead,
which pairs with the fail-safe-to-Pi-on rule in §13.3.3(e). If a pin is retained
for that, it is retained *deliberately* and the net carries two drivers, so it
needs the arbitration spelled out. What is not defensible is treating those pins
as unavailable by accident.

**And SWD does not need header GPIO at all.** Item 14 is called a hard
requirement, then satisfied in its weakest form — a UART bootloader, no debugger
— because of a budget that turns out not to bind. The cheaper reading: put SWD on
its own 4-pin 1.27 mm header, programmed from a laptop at the bench, costing zero
Pi GPIO and the area §8.4 already wants for test points, and keep the Pi's UART
bootloader for unattended in-vehicle updates. Both, not either. For a part whose
firmware "grows for years after fabrication", shipping without a debugger is the
wrong economy.

Physical: 65 × 56mm carries **57 footprints as built in rev 2.0** and about
**82 after rev 2.1** (§13.5.7), which may already push passives onto B.Cu. An
MCU, an always-on buck, crossing-net components and a programming header are
not free there. Swapping Y1's HC49/US can (**11.4 × 4.7 × 3.3mm**, already
flagged in §3.1.1 as "physically large" against a 3.2 × 2.5mm SMD equivalent)
is worth doing on its own merits and helps here — **it was listed in §13.2.3
but did not land: the rev 2.0 PCB still carries `XTAL_ABLS2-40.000MHZ-D4YF-T`**
(corrected 2026-09-14). **Rev 2.1 deletes Y1 and C5/C6 outright**: one
2.5 × 2.0 mm 40 MHz oscillator clocks both MCP2518FDs on their OSC1 pins
(§13.5.4, *Clock*), so rev 3.0 inherits the area after all — and if the MCU
wants 40 MHz too, the same X1 can feed a third stub.

#### 13.3.7 Deliberately out of scope for the MCU

Recorded with reasons, because each is individually tempting:

- **The OBD scheduler and ISO-TP.** Hard-real-time sweep scheduling would deliver
  `docs/roadmap/capture-profiles.md` Phase 5.c cleanly, and 5.c is deferred there
  precisely because it means "a second scheduling mode inside `run_can_obd_loop`,
  which already carries backoff, DID windows, budget pacing, freeze frames and
  DTC polling." **But the measurements say Linux is not the constraint:**
  `docs/data-architecture.md` records 85 single-PID requests/s and 33.9 Hz per
  channel batched, with the ECM answering in ~1.5 ms, against a live rate of ~8
  req/s — and names the cause outright, `BUS_BUDGET_MULTIPLIER` × `poll_rate_hz`,
  "a conservative heuristic, not a measured limit." Fix the constant in Python
  before building hardware to route around it. Moving ISO-TP, the poll plan, DID
  session gating and per-vehicle profile logic into C would duplicate tested code
  and put vehicle-specific behaviour where `pytest` cannot reach it.
- **Decode and bindings.** Byte → quantity stays in Python against
  `data/bindings.yaml`, gated by `compare_channels` and
  `tests/test_analysis_ground_truth.py`. A second decode implementation in
  firmware would be a second source of truth for exactly the class of claim this
  project has retracted six times.
- **Any inference or detection.** Obvious, but stated so it stays stated.
- **The gs_usb CAN-adapter role** — §13.3.4, deferred with reasons.
- **Anything that writes ECU non-volatile memory.** AGENTS.md's one hard line
  applies to the MCU identically. An MCU with its own transmit path onto the
  vehicle bus is precisely the component that could quietly acquire that
  capability, and §13.3.5's receive-only rule is also what prevents it.

#### 13.3.8 Rejected: PCIe as the data transport

Considered and rejected 2026-08-11. Recorded because the Pi 5's exposed PCIe is a
visible unused resource and the idea will otherwise be re-derived.

**The bandwidth case fails by three orders of magnitude.** The bus is 500 kbit/s
classic CAN (`docs/can-setup.md`). At 100% load that is ~4000 frames/s, each
costing ~30 bytes of SPI traffic out of the MCP2518FD's RX FIFO — ~120 kB/s ≈
1 Mbit/s against a 20 Mbit/s SPI link, about 5% utilisation. GNSS at 10 Hz is a
few kB/s and the IMU single-digit kB/s. Total ingest is under ~200 kB/s against
PCIe Gen2 x1's ~500 MB/s.

**The header data already crosses a PCIe link.** Every GPIO, SPI, UART and I2C
line on a Pi 5 hangs off the RP1 southbridge, which reaches the BCM2712 over an
internal PCIe link. The only non-PCIe segment is the 20 MHz SPI hop — the
5%-utilised one. There is no congested link to relieve.

**Latency would not improve either.** A frame read over SPI is ~15–20 µs;
IRQ-to-handler latency on a stock Pi 5 kernel is tens to hundreds of µs, so the
transport is not the dominant term. Timestamp quality — the thing actually worth
wanting — comes from the MCP2518FD's own time base counter (`mcp251xfd` drives
hardware RX timestamps from it; confirm at bring-up) and from §13.3.3(c), not
from PCIe.

**Costs, all real:** no drop-in PCIe CAN part exists, so it means an FPGA or a
PCIe-endpoint MCU plus a custom kernel driver replacing a mainline one; 5 GT/s
differential routing on a board whose single 1.575 GHz launch already cost two
rounds and a custom DRU rule (§3.6.1); the Pi 5's PCIe is a 16-pin FFC rather
than header pins, so the board stops being a HAT; and it consumes the only x1
lane, which is worth far more as **NVMe** — storage for the Parquet ring buffer,
training corpus and session directories, against the microSD/USB-SSD plan in
`docs/CANary-overview.md`.

**What would change the answer:** a front end of a different character — raw
analogue sampling of CANH/CANL at MS/s for transceiver fingerprinting or
physical-layer intrusion detection, or sub-bit timing analysis. Two channels at
10 MSa/s × 12-bit is ~30 MB/s, where a fast link starts to matter. That is
outside anything rev 1.0/2.0/3.0 contemplates, and even then USB 3.0 carries it — the
FPGA front end would be the hard part, not the link.

#### 13.3.9 Rev 2.1 deltas rev 3.0 inherits

Rev 2.1 (`CANary-HAT-Rev2.1.md`, §13.5) was written after most of §13.3 and
changes its starting point. Recorded here so rev 3.0 is drawn against the board
that will actually exist, not against rev 2.0.

**1. SPI0 CE1 is gone — item 5 re-decided.** Rev 2.1 puts the aux CAN
controller (U10, CAN1) on CE1, the pin §13.3.6 item 5 had reserved for the MCU
bulk link. Three ways out were weighed on 2026-09-14:

| Option | Cost | Verdict |
|---|---|---|
| **(a) MCU on SPI0 with a GPIO chip select (GPIO20)** — the Pi's SPI0 overlays already drive CS through `cs-gpios`, so a third select is a small custom overlay, not a driver change | GPIO20; SPI0 shared three ways (bandwidth is a few percent at 20 MHz) | **Adopted.** Keeps every interface on the header, as §13.3.4 wanted |
| (b) MCU as a USB device — bulk dump, DFU flashing, optional `gs_usb` channels on one cable | A USB connector on the HAT and a cable to a Pi port; frees GPIO20/21 | Fallback if (a) fails at bring-up. §13.3.4's objection (every other interface arrives through the header) is weaker now that the header is full, but it still stands |
| (c) Fold CAN1 into the MCU and give CE1 back | Makes the aux bus depend on supervisor firmware; the §13.3.4 rejected-alternative argument applies | Rejected |

Consequence of (a): the header closes with **no spare GPIO** (budget above), so
**item 14 must be the UART-bootloader form** (`uart4` + `BOOT0` on GPIO21), not
bit-banged SWD, which needs two pins the Pi no longer has.

**2. The ADC is four channels, and three of them are a set.** Item 2 was
written for one divider. Rev 2.1 adds `MAP`, `IAT` and `5V_SENS` on U8 and
computes MAP and IAT as *ratios* against `5V_SENS` (§13.5.3 item 4). Re-pointing
at the MCU means **all four dividers go to MCU ADC pins on one converter with
one reference**, or the ratio no longer cancels what it was built to cancel. U8
is then deleted, which also takes the ADS1015's traffic off I2C1 (§13.5.3 item
5's bus-loading concern goes with it). The crank-voltage instrument in
§13.3.3(b) gains MAP-at-crank for free.

**3. Two transceivers, one always-on rail.** Item 1's always-on VIO is for
wake-on-CAN on the *vehicle* bus (U4). **U11 does not need it**: the AEM gauge
is on switched 12 V and cannot wake anything. Item 7's RXD fan-out from U11 is
still worth one net, for fuel pressure across the boot gap (§13.3.3a).

**4. Item 13 grows by two.** U11's STB (rev 2.1: GPIO19) and U9's EN (rev 2.1:
tied to `+3V3` so the sensor rail follows the Pi) both move to the MCU. With
the Pi off, the MCU decides whether the MAP/IAT sensor is powered; a parked
MAP-equals-baro check is cheap, continuous sensor power is not.

**5. The terminal block starts from rev 2.1's.** J2 is the two-level MKKDS
1,5/6-5,08 with all 12 positions used and the rev 2.0 vehicle pinout frozen on
the lower level (§13.5.5). §3.9's "rev 3.0 should weigh deleting J3" now has a
concrete payoff: J3's ~10 mm is the only slack left on that edge.

**6. Termination is decided per channel, not per revision.** CAN0 (vehicle)
carries no termination footprint by design; CAN1 (aux) carries JP2-selectable
120 Ω split termination (§13.5.4). §13.4.4's row assigning "DNI split
termination + solder bridge" to rev 2.0 is superseded — it was dropped there
(§13.2.1) and lands only on CAN1 in rev 2.1. Do not re-add it on CAN0.

**7. More buses are USB, not board.** Rev 2.1 decided that additional CAN buses
are `gs_usb` adapters under the aux-bus config role (§13.5.4). That leaves the
§13.3.4 candleLight option exactly where it was — deferred — but the "route
back to multi-bus" it describes now has a shipped alternative, so rev 3.0 does
not need the MCU to carry vehicle CAN to satisfy the multi-vehicle intent.

**8. Clock.** Y1 is still HC49 on rev 2.0 (corrected above). Rev 2.1 replaces
it with **one oscillator, X1, driving both controllers' OSC1** (§13.5.4,
*Clock*); the start-up-margin arithmetic recorded there also shows rev 2.0's
18 pF crystal circuit thin on paper (§13.1 measurement 14). Rev 3.0 owes
nothing here. If the MCU's own clock needs are compatible, X1 can supply it as
well — one clock domain for CAN and the MCU timebase is exactly what
§13.3.3(c) wants.

#### 13.3.10 MCU part selection

The §13.3.2 note that "the regulator's quiescent current drives the sleep budget,
not the MCU's sleep mode" is what makes this a free choice rather than a
µA-first one. Collecting the requirements this section has accumulated:

- **≥2 CAN decode paths** — item 7's two RXD fan-outs (U4 vehicle, U11 aux),
  with headroom for the channels §13.5.4 would put here.
- **Several hundred KB SRAM** — §13.3.3(a)'s boot-gap buffer. *Unmeasured; see
  the gating item below.*
- **Four ADC channels on one converter with one reference** — §13.3.9 item 2's
  ratiometric MAP/IAT against `5V_SENS`. Split across converters and the ratio
  stops cancelling what it exists to cancel.
- **µs-deterministic UART gating** — §13.3.4's K-line table.
- **SPI slave** for item 5's bulk dump; **LQFP**; able to take 40 MHz from rev
  2.1's X1 (§13.3.9 item 8).

| Part | FDCAN | SRAM | ADC | Standby |
|---|---|---|---|---|
| STM32G4 (G474) | **3×** | **128 KB** | 5× 12-bit, 4 Msps, HW oversampling | — |
| STM32H563/573 | 2× | 640 KB | good | 3.5 µA |
| STM32U575/585 | **1×** | **786 KB** | 2× 14-bit @ 2.5 Msps, HW oversampling | 210 nA |

Three things that table settles, all verified against datasheets 2026-10-01:

- **The STM32U5 has one FDCAN.** It is the part "ultra-low-power plus big RAM"
  leads to, and it cannot decode both fan-outs in hardware. The aux line carries
  one node (the AEM gauge) at low rate, so software decode there is defensible
  — but it is a second CAN implementation in firmware, which §13.3.7 is
  rightly allergic to.
- **The standby column is a red herring.** §13.3.2's own arithmetic puts 200 µA
  at over a decade on 20 Ah, so 3.5 µA and 210 nA are indistinguishable here.
  Do not let low-power marketing pick this part; **RAM and FDCAN count decide it.**
- **Decode the fan-outs with FDCAN peripherals in silent/listen-only mode**, not
  by bit-banging. Silent mode never drives TX, so §13.3.5's receive-only rule is
  enforced in *hardware* rather than by firmware discipline, and hardware RX
  timestamps and ID filtering come free — which is §13.3.3(a) and (c) together.

**The gating open item: compute the boot-gap buffer size before choosing.**
§13.3.3(a)'s "a few hundred KB" is an estimate, and §13.1 measurement 12 (boot
time) is still open — yet that number is exactly what separates the 128 KB part
from the 786 KB part. It does not need measurement 12 to start: the broadcast
frame rate is already in captures on disk, so *rate × window × bytes-per-stored-frame*
is an hour's arithmetic. At ~1500 frames/s and 20 B/frame, 40 s is over 1 MB; at
~500 frames/s it is ~400 KB; with per-ID delta encoding — most broadcast traffic
is the same IDs repeating — it could land under 100 KB. **Do this first.** If it
fits in 128 KB, the STM32G4 wins on every other axis (3× FDCAN, the best ADCs of
the three, cheapest, and the part this section already named). If it needs
hundreds of KB, the **STM32H563 in LQFP64** matches the two fan-outs exactly.

**Rejected: the Teensy / i.MX RT1062 class.** Raised 2026-10-01 from the
coprocessor idea, and it belongs to a different architecture — an external box on
a cable to an unmodified Pi, not a part on this board. The i.MX RT1062 is a
196-ball BGA with no internal flash, draws ~100 mA active with weak low-power
modes on a board whose premise is µA standby, and the Teensy module itself is
61 × 18 mm against a 65 × 56 mm board already near ~82 footprints. Its 600 MHz M7
answers a question §13.3.7 declines to ask.

**One cross-reference to tidy.** §13.3.5 rejects "an MCU with an integrated CAN FD
controller replacing the MCP2518FD"; §13.5.4 calls the MCU "the principled home
for three or more on-board channels". These are compatible — CAN0 stays on the
MCP2518FD behind the mainline driver; the MCU's FDCAN peripherals are for
listen-only decode and for future channels — but they read as contradictory, so
say so where either is quoted.

#### 13.3.11 Assembly: ICs placed by the fab

**Operator decision, 2026-10-01: PCBWay assembles the board, at least every IC.
Headers and resistors may be reworked by hand afterwards.** Three constraints
recorded elsewhere were premised on hand assembly and change here.

1. **§13.4.3's "keep 0603" loses its premise.** That bullet reasons from "a
   hand-assembled single research board where hand-rework is routine", and even
   then conceded "§13.3.6 notes area gets tight once an MCU arrives" — which it
   now has, alongside an always-on buck, crossing-net parts and a programming
   header, on a board §13.5.7 already thinks may push passives to B.Cu.
   **Shop-placed passives go 0402; keep 0603 only where hand-rework is actually
   expected.** This is the cheapest area the revision will find.
2. **BGA stops being ruled out by assembly — and is still declined.** Every part
   in §13.3.10 ships in LQFP, BGA needs X-ray to inspect and cannot be reworked
   in place, and nothing here needs the pin density. No upside; the answer is
   unchanged, but the *reason* is now inspectability rather than hand-soldering.
3. **§13.1.1's U5 ruling no longer binds.** "Hand replacement of U5 is ruled out
   (operator, 2026-09-23: too small to rework by hand)" was a hand-assembly
   constraint. A shop can replace the BMP384, so the rev 2.0 barometer failure
   becomes a rework rather than a dead subsystem, and §13.1 measurement 9 —
   currently blocked by it — becomes reachable without waiting for rev 2.1.
   Carry this into [`CANary-HAT-Rev2.1.md`](CANary-HAT-Rev2.1.md) §13.5.6's
   investigation list.

#### 13.3.12 USB: an option on the module path; the header does data and programming

**Operator decision, 2026-10-01.** Rev 3.0 stays a HAT: **every host
communication path is the 40-pin header**, and **programming is over the header
too** (§13.3.12's item-14 rewrite below). The board still carries a **USB
port**, but for one reason only — it makes a future USB *data* path pure
firmware. The alternative considered and declined was a dual-transport board
where USB carried data from the start: §13.3.5's `gs_usb` route, the MCU's own
channels, and eventually CAN.

*Corrected the same day.* This section first justified the port as the
programming path, on the argument that USB DFU is in ROM and so can flash a
virgin part with no probe. True, but **SWD flashes a virgin part too** — the
debug port is in silicon and live from reset, and the Pi is the probe. Bring-up
from the assembler is fully covered by the header, so that argument supports no
part of the port. What survives is the deferred-option argument alone.

**Why this is the simpler board, and not just the smaller one.** Four
complications disappear together:

1. **No application USB stack.** DFU lives in the MCU's ROM, so a firmware-only
   USB port means *zero* USB code: no descriptors, no endpoint handling, no
   `gs_usb` protocol, no enumeration, suspend/resume or re-attach edge cases.
   That matters most on the part that also holds the load switch and the
   watchdog — §13.3.3(e) wants the supervisor boring.
2. **No transport arbitration.** The "which link is authoritative" question does
   not arise, so there is no host election, no VBUS-presence logic and no
   both-attached case to define.
3. **No TXD merge, so no bus-jam failure mode.** Two CAN controllers behind one
   transceiver need their TXD lines combined (an AND gate works — dominant-low,
   recessive-high), and a TXD stuck dominant **jams the whole vehicle bus**, not
   just this rig. That would have been the first failure mode on this board
   reaching outside it, and it is now simply absent.
4. **§13.3.5 stays a wiring guarantee.** With no USB data path there is no
   reason for the MCU to transmit, so receive-only remains enforced by what is
   connected rather than by a firmware invariant — the distinction AGENTS.md
   cares about.

**What this defers costs almost nothing.** The open questions about a module
architecture are firmware and protocol — does a `gs_usb` endpoint leave
`src/collection/` untouched, does a module-as-system-of-record handover work, is
the jitter measurably better than MCP2518FD-over-SPI. Those belong on a Teensy
4.1 plus a transceiver breakout, which answers them sooner and cheaper than any
board spin. Rev 3.0 was never the right vehicle for that experiment.

**The port, and the one hard rule.** USB-C receptacle, device-only: `Rd` 5.1 kΩ
to GND on both CC lines, an ESD array on D+/D−. The board is powered from
`+12V_RAW` and is therefore a **self-powered device — never draw from VBUS and
never back-feed it.** Note this is also why USB adds no §13.3.2 crossing: a
self-powered device facing an unpowered host is USB's ordinary case, designed
for, not a hazard to mitigate. **Full Speed is sufficient and preferred** —
§13.3.8 puts total ingest at ~120 kB/s ≈ 1 Mbit/s against FS's 12 Mbit/s, and FS
needs no HS PHY and no high-speed differential routing on a board whose single
1.575 GHz launch cost two rounds (§3.6.1). For a firmware-only port even that
headroom is irrelevant.

**Placement, and the ranking that goes with it.** §13.3.9 item 5 leaves J3's
~10 mm as the only slack on J2's edge, so the receptacle goes on a different
edge and must stay reachable with the HAT mounted. One for §13.5.7 — and §13.3.13.

**USB is the designated cut if the edge does not close.** Every other new part
on this revision is required: the MCU, the always-on buck, the crossing-net
components, the four ADC dividers, the SWD header. USB's benefit is optional and
deferred, and it wants scarce *edge* area on a board §13.5.7 already thinks may
push passives to B.Cu. So rank it last, and if layout runs out of perimeter,
drop it — nothing else depends on it, and the module data experiment then waits
for a module board.

**The de-risking argument does not add anything**, and should not be used to
defend the port. A USB-C receptacle, two 5.1 kΩ `Rd` resistors and an ESD array
is not hard to get right first time, Full Speed routing is forgiving, and these
parts need no crystal for USB at all — the Clock Recovery System trims the
internal 48 MHz oscillator off SOF packets. Practising it here teaches little.
The port earns its place on the deferred-firmware-option argument or not at all.

##### Item 14 rewritten: SWD is the whole programming story

Item 14 was "a hard requirement, not a convenience" satisfied in its weakest
form — a UART bootloader, no debugger — under a pin budget §13.3.6 now shows
does not bind. With the budget corrected, SWD covers **flash and debug** from
both the Pi and the bench, which makes **both bootloaders redundant**: SWD
recovers a bricked part unconditionally, so nothing needs the ROM bootloader and
`BOOT0` needs no pin, jumper or button.

| Need | Path | Cost |
|---|---|---|
| In-vehicle flash + debug, over the network, board still under the dash | **SWD on Pi GPIO**, OpenOCD + gdb | 2–3 crossing nets |
| Bench flash + debug, Pi-independent | 4-pin 1.27 mm SWD header | ~30 mm², no Pi GPIO |
| `BOOT0` | **strapped low**, resistor to GND | none |

Two paths instead of four, and strictly *more* capable than the version this
replaces: a bootloader only flashes, where SWD also sets breakpoints, inspects
registers and reads a hung MCU post mortem — which is the case that matters,
since the MCU holds the load switch and the watchdog and §13.3.3(e) exists to
recover hangs.

- **SWD, not JTAG** — two pins against four or five, and full JTAG only buys
  boundary scan and chaining, neither of which applies to a single Cortex-M.
- **On a Pi 5, OpenOCD's driver is `linuxgpiod`, not `bcm2835gpio`**: the latter
  pokes BCM registers directly and Pi 5 GPIO sits behind RP1. Bit-banged SWD is
  slow, which is fine for debugging and tolerable for flashing at this image
  size; the bench probe is the fast path.
- **`BOOT0` and the UART bootloader both leave the header**, freeing **GPIO21**
  and leaving `uart4` to item 4's handshake alone.
- **SWDIO/SWCLK/nRST are one net set with two consumers** (Pi GPIO and the bench
  header). Series resistance per §13.3.2, and the procedural rule that a probe
  is not attached while OpenOCD runs on the Pi. These are Pi→MCU crossings, so
  they take §13.3.2's pulls: an undriven SWD line with the Pi dark must not hold
  the MCU in reset.
- **This closes a loop with §13.3.3(e).** Because a crashed MCU leaves the *Pi*
  powered, the Pi is alive and able to halt, inspect and reflash the supervisor
  that crashed. The fail-safe direction and Pi-as-probe reinforce each other.
- One consequence for the part search in §13.3.10: since no ROM bootloader is
  relied on, **USB-plus-DFU stops being a selection criterion**. Judge candidates
  on FDCAN count, SRAM and ADC alone.

##### Not adopted: AND-gate TXD merge, fitted DNP

Weighed and declined. §13.3.6's "a missing net is a re-spin, unused firmware is
free" argues for landing the footprints, and §13.4.1 item 4's "fit the
footprint, gate the connection" is the matching habit — but both have a
boundary, and this is past it. The gate is **a part in the vehicle-bus transmit
path**, so fitting it demands getting the hazard right now (always-on rail, so
the Pi being off cannot leave it unpowered; TXD pulled recessive when undriven,
confirmed against the TCAN1042's internal pull-up) for a capability just
deprioritised.

**The decisive argument is that the gate serves a waypoint, not a destination.**
It exists only for a transitional state where the Pi and the MCU *both* own CAN
transmit. If a module board is ever built, CAN is MCU-owned from the start and
there is no MCP2518FD to merge with — so the merge is never needed there either.
Omit it.

**What stays open at zero cost, and what closes.** Because the connector and the
MCU's USB pins exist, adding the MCU's *own* data over USB later — boot-gap
dumps, K-line, GNSS, IMU, ADC telemetry, received CAN with MCU timestamps — is
**pure firmware, no board change.** The only thing this revision forecloses is
**CAN transmit over USB**, which is what the declined gate would have bought.
This also confirms §13.3.10's framing: the MCU's FDCAN peripherals stay
**listen-only**, so two silent instances remain the natural requirement and the
STM32U5's single FDCAN remains the caveat recorded there.

#### 13.3.13 Placement: rev 3.0 goes double-sided

**Operator expectation, 2026-10-01: rev 3.0 will have to put components on
B.Cu.** §13.5.7 already reached that conclusion for rev 2.1's ~82 footprints,
and rev 3.0 adds an MCU, an always-on buck, crossing-net components, four ADC
dividers and an SWD header on top of it. Two things come back the other way —
rev 3.0 **deletes the ADS1015** (§13.3.9 item 2 moves the ADC to the MCU) and
rev 2.1 already **deletes Y1 and C5/C6** for a single oscillator (§13.3.9 item
8) — but not enough to avoid the second side. Now that the fab places the parts
(§13.3.11), double-sided assembly is a cost line rather than a skill question.

##### The gating measurement: what is actually under the board

**Settle this before any placement.** Two facts in §13.1.1 make the bottom-side
envelope an open question rather than a known quantity: the stack needs the
**13.5 mm stacking header** (the assembler fitted a standard-height part, which
is the J1 BOM defect), and **X1202 coexistence is "not tested — UPS not
stacked."** So:

1. **What is the stack order** — Pi, X1202, HAT, or Pi, HAT, X1202? If the HAT
   sits above the UPS, its bottom face looks at the UPS's components and cells,
   not at the Pi, and the envelope is a different shape entirely. An X1202
   carries 18650 cells, which are tall.
2. **Is a Pi 5 active cooler fitted?** The compute roadmap's thermal section
   assumes active cooling and progressive throttling from 80 °C, so presumably
   yes — and a cooler occupies the Pi's centre, which makes the usable
   bottom-side area a **ring around the middle**, not the whole face.
3. **Measure the clearance with the real stack.** 13.5 mm is the gap between
   boards; what is left for components is that minus the tallest thing
   underneath, in each zone.

If the answer is "cells or a cooler under most of the board", the plan changes
from *move subsystems down* to *move only passives down, in named zones*. Ten
minutes with calipers decides which document this section becomes.

##### Divide by transient path, not by subsystem

The operator's first cut was "MCU on top, all K-line and CAN ICs to the bottom
with their passives". Directionally right, but the transceivers and the
controllers want opposite things, so split those ICs in two:

- **Transceivers stay on T.Cu.** They land on J2, a through-hole terminal block
  mounted from the top, and they sit in the path that sees ISO 7637-2 pulses —
  the +100 V/−150 V that §3.1.1 specifies C4's 100 V C0G against. Routing that
  from J2 on top, through vias, to a transceiver on the bottom adds via
  inductance exactly where it is least wanted, and splits the protection
  topology (TVS, PTC, series R) across layers. Keep the protected-to-unprotected
  boundary short and on one side.
- **Controllers go to B.Cu, and take the oscillator with them.** Both
  MCP2518FDs are purely digital — SPI to the header, TXD/RXD to the
  transceivers — with no vehicle connection and nothing to see or probe. Rev
  2.1's single X1 drives both OSC1 pins (§13.3.9 item 8), so keeping X1 and both
  controllers on one side keeps that clock routing short. The
  controller↔transceiver digital nets cross on vias happily at these rates.

##### Must stay on T.Cu

- **The GNSS RF path** — J4, L1 and the antenna-supervisor parts. This is the
  one to insist on. §13.3.6 already calls the RF region the most constrained
  area on the board, and §13.1 **measurement 4 is specifically GNSS desense
  under the Pi's own WiFi hotspot**. Putting the antenna path on the bottom aims
  it at the Pi as an EMI source and makes that measurement worse. RF stays top,
  as far from the Pi as the outline allows.
- **J2 and J3** — through-hole, and the transient entry point.
- **The 12 V input stage** — the SMB TVS is physically chunky and this is the
  transient path; keep it near J2/J3.
- **Status LEDs** (§13.3.6 item 15) must be visible; the **SWD header** needs
  probe access; the **USB receptacle** needs a reachable edge (§13.3.12).
- **The MCU** — as the operator placed it. It is the part most likely to want
  rework, and its SWD and programming access is on top.

##### Good B.Cu candidates

The MCP2518FDs and X1 per above; **most passives**, which is §13.5.7's existing
plan and what §13.3.11's move to 0402 makes cheaper; the **IMU**, since a 6-axis
part on the bottom only flips a sign in the axis transform — a config constant,
not a problem; and the **barometer**, which needs open air rather than a sealed
cavity and still has it under a 13.5 mm stack.

##### One sequencing rule for the fab

Double-sided assembly is two passes, so **heavy parts go on the side reflowed
last, and light SMD rides the first pass held by solder surface tension.** 0402,
SOIC-8 and QFN are fine inverted; connectors, the terminal blocks, the GNSS
module and the buck inductor want T.Cu anyway. Tell PCBWay which side is which
so they sequence it correctly rather than inferring it — and note J1's
through-hole pin field must stay clear of bottom-side parts regardless.

### 13.4 Lessons from the Macchina M2

Read out of the M2-B interface schematic (`SCH-01006 R2` / `PCB-01006 R2`, dated
2017-01-04) and the interface-board BOM in
[`macchina/m2-hardware`](https://github.com/macchina/m2-hardware) on 2026-08-13.
The M2 is the closest thing to prior art for this board — an open-hardware
OBD-port interface carrying 2× CAN, single-wire CAN, 2× LIN/K-line and J1850 —
so it is worth reading as a second opinion on choices rev 1.0 made alone.

**Two caveats on the source, both load-bearing.** The BOM in that repo is
`BOM-PCB-01016 R1`, for the *original* M2 interface board; the schematic is
`PCB-01006 R2`, the M2-B. They are different boards and they disagree (the BOM
lists a TJA1027 where the M2-B schematic shows a **TJA1021**). So: topology and
values below come from the M2-B schematic and are solid; dielectric, tolerance
and package data comes from the older BOM and is evidence of *house style*, not
of this board's exact BOM. Second, neither PDF renders as text with the usual
tools — both were read by decompressing the PDF content streams and pairing text
against its coordinates, which is how pin-to-net assignments were recovered at
all, since Macchina's published docs do not list them.

#### 13.4.1 Where rev 1.0 is wrong, or thinner than it should be

**1. Capacitor dielectrics — retracted 2026-08-13, same day it was written.**
This item originally claimed rev 1.0 specified no dielectric for any capacitor,
having compared the M2's `22PF 50V C0G/NP0` and `220PF 50V 5% NP0` against a
grep of `CANary-HAT-Circuits.md` alone. **§12.1 has specified dielectric,
tolerance and voltage for every capacitor on the board since 2026-08-11**, and
specifies C4 at **100V** C0G against the M2's 50V — rev 1.0 is *ahead* here, not
behind, because ISO 7637-2 pulses on the K-line reach +100V/−150V.

Nothing about the M2 was wrong; the comparison was, and it was wrong in the
direction that manufactures a finding. §3.1.1 now carries the surviving point,
which is smaller and real: the constraint lived in the fabrication document
while the arithmetic that depends on it lived in the circuit reference, so a
reader of §3.1.1 sees a careful 22pF → 27pF derivation with no hint that the
dielectric dominates its error. **State a part's governing constraint where its
value is derived, not only where the BOM is assembled.**

Logged rather than deleted because it is the exact failure AGENTS.md warns
about — a confident conclusion from a partial search, reported as "across all
four docs" when one file had been searched.

**2. Reverse protection by P-FET rather than a series Schottky.** The M2's input
stage is an ideal-diode arrangement: **Q1 `IRFTS9342` P-FET** gate-driven through
**Q2 `MMBT3906W`** with R2/R3 = 1.00 kΩ and R4 = 10.0 kΩ. Milliohms of drop, no
dissipation, no thermal question.

Rev 1.0 uses D4, an SS54 Schottky, and §3.7.4 had to move the Pi's supply
*upstream* of it to escape the ~0.4–0.5V drop and ~1.5W — accepting in exchange
that "nothing on this board protects the X1202 leg," so reverse polarity at the
terminal block reaches the UPS unimpeded. That section already named the fix as
option 2: "a TVS and a series element (**ideal-diode controller**, or a Schottky
sized for 3A) on `+12V_RAW` itself." The M2 is a working instance of exactly
that. It does not work around the trade in §3.7.4 — it dissolves it, protecting
both legs with no drop.

**3. A PTC on the input.** The M2 fuses at **F1, 2.6A hold / 5.2A trip**, plus
0.75A per 12V IO channel and 36 mA hold / 91 mA trip on small branches. Fusing
granularity is clearly house style there. §3.7.1 decided rev 1.0 needs no fuse
because the vehicle-side harness fuse is the whole story — but §3.7.4 then notes
"there is still no fuse anywhere on the board, so nothing on the HAT limits fault
current on a leg that now feeds the whole system directly." Those two statements
sit uneasily together, and the second was written after the first. Re-take the
decision rather than inherit it.

**4. Optionality discipline — fit the footprint, gate the connection.** The M2's
CAN bus network is the clearest example on the board:

```
TJA1051 → CANH0/CANL0 → [J6/J7 solder bridges] → R128/R129 60.4Ω 1% split term
                                                  └── C36 4.7nF to GND
        → R126/R127 0.0Ω jumpers ∥ L2 (90Ω @ 100MHz choke position)
        → C31/C32 100pF (DNI) → D11 SP1002-02 → HSC_BUS+/−
```

Every option is *on the board* and off by default: the common-mode choke has a
footprint bypassed by 0Ω jumpers, the split termination is gated by solder
bridges, the bus caps are DNI. One rework, not one respin.

Rev 1.0 is right to refuse termination (§3.2 — "a third terminator is a fault"),
but it has no footprint either, which forces the concession that bench use
"requires an external terminator in the harness." An unpopulated 2 × 60.4Ω plus
centre cap behind a solder bridge costs nothing, risks nothing, and removes that
caveat.

**5. Switched rails, and the same FET topology twice.** The M2 puts `HSC_S` and
`MSC_S` (the transceiver standby pins) on GPIOs — the change §13.2 calls "the one
that matters most" — and then goes further: the transceivers run from **`+5V_SW`**,
a switched rail behind **Q3/Q4 `IRLML5203`** P-FETs, gate-driven through **Q5
`2N7002W`** with 100 kΩ pull-ups and a 470Ω series gate resistor, enabled by a
signal named `J1850 9141 ON`.

That is the *same* level-shifted high-side switch topology §13.2 sketches for the
K-line commander pull-up (BSS84 + BSS138). Two uses, one block — worth drawing
once and instantiating, rather than deriving twice.

**6. On-board current sense with a firmware-independent trip.** `R18` = **0.01Ω
1% 1/4W 0805** shunt → **U2 `ZXCT1109`** high-side monitor → **U3A/U3B `LM393`**
comparator → an `OVER_CURRENT` flag *and* a **Q6 `IRFTS9342`** P-FET cutoff.

Two things rev 1.0 wants this for. §13.1 measurement 7 ("actual 3.3V draw, idle and
with GNSS acquiring") stops being a bench-supply exercise and becomes a telemetry
channel; and §13.3.3(b)'s parasitic-draw-while-parked monitoring gets real
hardware instead of inference. The part most worth copying is the comparator trip
that cuts power **without firmware in the loop** — "make it impossible, not
documented", implemented in silicon.

**7. The 12V sense divider values, ready-made.** The M2's IO sense channel is
**R32 = 10.0 kΩ / R33 = 2.20 kΩ with a V1 = 16.2V zener clamp** into an analog
input. Ratio 0.180 puts 14.4V at 2.60V and 16V at 2.88V against a 3.3V ADC, with
the zener as load-dump backstop. Promoted into §13.2 rather than derived again.

#### 13.4.2 Where rev 1.0 is right — do not copy these

**The TVS.** The M2 uses an **SMBJ14A**, 14V standoff. An automotive charging
system runs 13.8–14.4V, so that part operates *at or above its standoff voltage
during ordinary driving* — leakage and self-heating as a normal condition. Rev
A's **SMBJ24A** at 24V standoff is the conventional automotive choice and is
correct. This is the clearest case in the comparison of the M2 being wrong.

**The bus ESD array.** `D11`/`D12` are **`SP1002-02`**, a 6V-standoff / 16V-clamp
array, sitting on the vehicle CAN lines at the connector. ISO 11898-2 requires a
bus node to survive −27V to +40V; a 6V array will conduct hard on a
short-to-battery. Rev 1.0's position — no bus TVS, relying on the TCAN1042's own
±58V fault and ±15kV IEC ESD ratings (§3.2) — is defensible, and is arguably
better than a 6V array. **If** bus ESD is ever added, the right class of part is
an automotive CAN protector at ~24V standoff (NUP2105L / PESD2CAN family), never
a low-voltage USB-style array.

**Termination on a vehicle bus.** The split termination above is present and
solder-bridge-gated; whether the M2 *ships* with J6/J7 bridged could not be
determined from the schematic alone, and the repo's BOM is for a different board
so it cannot settle it either. Flagged rather than asserted — but if bridged, it
is the third terminator §3.2 refuses, and §3.2's reasoning stands regardless.

#### 13.4.3 Deliberate divergences, recorded so they are not "fixed"

- **0603 vs the M2's 0402 passives.** Rev 1.0 is a hand-assembled single research
  board where hand-rework is routine; 0402 buys area this board does not need
  (though §13.3.6 notes area gets tight once an MCU arrives). Keep 0603.
  **Superseded for rev 3.0 (§13.3.11): the fab places the parts, so shop-placed
  passives go 0402 and 0603 survives only where hand-rework is expected.**
- **1% as the default resistor tolerance.** The M2 uses 1% throughout and reaches
  for E96 values wherever the value matters (60.4, 240, 374, 866, 2.00K, 6.49K,
  8.06K, 8.87K, 22.1K, 124K), reserving 5% for 0Ω jumpers. Rev 1.0's pull-ups and
  dividers do not need 1%, but there is no reason to specify looser — and the
  12V sense divider (§13.2) genuinely does want it.
- **Test points and solder bridges everywhere.** Already wanted by §8.4 and
  queued in §13.2; the M2 simply confirms the habit is worth the area.

#### 13.4.3a Fabricating an M2 is possible — what that does and does not buy

Checked 2026-10-01, because "buy an M2 to prototype on" runs into stock (out of
stock on Tindie; a Macchina forum thread asks for one) and the question becomes
whether to build one.

**The repo carries fabrication outputs, not just the PDFs §13.4 was read from.**
`M2/Gerbers/` holds RS-274X sets plus drill files for **`PCB-01005 R3`**
(processor board) and **`PCB-01006 R3`** (interface board) — GTL/GBL/G1/G2, so
4-layer — and `M2/Interface Board/BOM-PCB-01016 R1.csv` and
`M2/Processor Board/BOM-PCB-01015 R1.csv` are full BOMs. That is a turnkey
submission as-is: gerbers, drills and a BOM CSV are exactly PCBWay's inputs.
Licence is CC BY-SA 4.0. Note it is **two boards that stack**, so two runs.
`M2-B/` has PDFs only — no gerbers — so what is buildable is the *original* M2,
not the M2-B whose schematic §13.4 analysed.

**Verify the revision pairing before paying.** The gerbers are R3 and the BOMs
are R1. §13.4's caveat about BOM-vs-schematic disagreement was across two
*different boards* (an original-M2 BOM against the M2-B schematic), which does
not apply here — gerbers and BOMs are both original-M2 — but an R1 BOM against
R3 artwork is still unconfirmed, and a designator that moved between revisions
is a mis-stuffed board.

**Two of §13.4.2's three defects are BOM substitutions, not re-layouts** — which
matters, because without editable CAD a layout change is not available:

| §13.4.2 finding | Fix available? |
|---|---|
| `SMBJ14A` TVS, 14V standoff under a 13.8–14.4V charging system | **Yes** — `SMBJ24A`, same SMB footprint, drop-in |
| `SP1002-02` 6V bus ESD array against ISO 11898-2's −27/+40V | **Probably** — NUP2105L / PESD2CAN family in SOT-23; confirm footprint |
| `C4` at 50V against ISO 7637-2's +100/−150V K-line pulses | **Check** — 100V C0G at that value may not fit the fitted footprint |

**What fabrication cannot fix** is the reason the M2 is prior art rather than the
answer: 96 KB SRAM against §13.3.3(a)'s buffer, no GNSS so no PPS for
§13.3.3(c), no IMU for §13.3.3(d)'s motion wake, no barometer, no µA-class sleep
design, classic CAN only, a single XBee socket contended between BLE and GNSS,
and SAM3X8E sourcing for a 2017 part. Four of the five §13.3.3 capabilities are
out of reach on it.

**So the honest use is a prototype, and it is an expensive one.** The questions a
module architecture actually leaves open are firmware and protocol — does
`gs_usb` leave `src/collection/` untouched, does a module-as-system-of-record
handover work, is the jitter measurably better than MCP2518FD-over-SPI — and a
Teensy 4.1 plus a transceiver breakout answers all of them for the price of
neither fab run. **Build an M2 only for what nothing else offers: J1850 VPW/PWM,
single-wire CAN and all 16 OBD pins**, i.e. the multi-vehicle bus breadth
AGENTS.md's generalisation intent would eventually want and §13.1's M2 note
already flagged as "not an unserved gap in the market".

#### 13.4.4 Scope: rev 2.0 or rev 3.0

Taken as decisions rather than left open, since none of them blocks on
measurement:

| Item | Revision | Why |
|---|---|---|
| C0G/NP0 on C5/C6 and C4 | **rev 2.0** | BOM line only; no layout impact |
| P-FET reverse protection | **rev 2.0** | Independent of the MCU, and it closes §3.7.4's accepted exposure |
| PTC on the input | **rev 2.0** | Same input stage as the P-FET — do them together, once |
| DNI split termination + solder bridge | ~~rev 2.0~~ → **dropped in rev 2.0 (§13.2.1); CAN1-only with a jumper in rev 2.1 (§13.5.4)** | The vehicle channel never gets a termination footprint; the aux channel's is a real, fitted, jumpered terminator — §13.3.9 item 6 |
| 12V sense divider values | **rev 2.0** | Already queued there — but the divider had no destination until §13.2.3 added a small I2C ADC behind it. The M2's values transfer; the readback did not exist on rev 1.0 |
| Current sense + over-current trip | ~~split — shunt in rev 2.0~~ → **all of it rev 3.0** | The escape clause fired and was then re-examined 2026-08-23. Its own premise — "continuous current data is only worth having when something is awake to read it" — is what kills the split: the thing that must be awake is the **MCU**, because the measurement that matters (parked parasitic draw) happens with the Pi off. Landing the shunt early buys nothing, and rev 3.0 re-lays-out that stage anyway. §13.2.3 |

The input-stage items are grouped deliberately: rev 3.0 redesigns that stage anyway
for the always-on rail and the load switch (§13.3.2, §13.3.6), so doing the P-FET
and the PTC as one change avoids touching it twice.

**Re-read 2026-08-22 against the withdrawal of rev 1.0.** Every row above still
lands where it did — this table was written as decisions that "none of them blocks
on measurement", which is exactly the property that made it survive rev 1.0 never
being built. The one row that moved, current sense, moved because it had already
written down what to do in this case.
