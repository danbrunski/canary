# CANary-PWR — Power Board Spec (proposed)

> **Status: proposal, nothing in KiCad.** Opened 2026-10-04, when the operator
> retired the X1202 and moved every power function off the CANary HAT.
> [`CANary-HAT-Revisions.md`](CANary-HAT-Revisions.md) §13.3.0 records the
> decision and what it does to HAT rev 3.0. This file is the power board's own
> spec. Its sections are numbered §P1–§P10 so they never collide with the HAT's
> §1–§13.
>
> Part numbers below are **candidates to verify against their datasheets**, not
> selections. Energy and current figures are estimates until §P10's
> measurements replace them.

## P1. What this board is

A board that sits **on top of the CANary HAT** and owns power for the whole rig:

- vehicle 12V input from always-hot OBD pin 16, and its protection;
- **supercapacitor hold-up** in place of the X1202's four Li-ion 18650s;
- the 5V supply to the Pi;
- an **always-on supervisor MCU**: wake, the Pi's power switch, watchdog,
  power-fail signalling, parked battery monitoring.

**Why supercaps instead of a battery.** In this rig the battery has two real
jobs: riding through the crank dip, and shutting down cleanly if the rig is
unplugged. OBD pin 16 is always hot (operator, 2026-10-04), so the truck never
cuts power on its own, and key-off shutdown is a software decision driven by
`vehicle_state`. Both jobs need **seconds to a minute** of hold-up, not hours.
Li-ion is also the wrong chemistry for a truck cab: it sits at 60 °C+ parked in
summer and gets charged below 0 °C in winter. Automotive-grade supercaps are
rated about −40 to +85 °C and charging cold does not wear them.

**Why a separate board.** It splits the rev 3.0 MCU's two jobs along their
power domains (§13.3.0): the boring always-on supervisor lives here, and the
complex data coprocessor stays on the HAT, powered by the Pi. It also frees the
bottom of the stack for an NVMe base, which is what started this (S01.7).

## P2. Stack and mechanics

```
  CANary-PWR        ← this board
  CANary HAT        (rev 2.x now, rev 3.0 later)
  Pi 5 + active cooler
  NVMe base         ← in the slot the X1202 + 18650s used
```

- **Height.** The X1202 with its four 18650s is thicker than an NVMe base, so
  removing it frees height. The gap between the HAT and this board is set by the
  HAT's tallest top-side parts: the terminal blocks (rev 2.1's two-level
  MKKDS adds height, §13.5.5) and the GNSS connector. **Measure the case
  interior with the X1202 removed before drawing an outline** (§P10).
- **Access.** The HAT's terminal-block screws and the GNSS antenna connector
  must stay reachable with this board fitted. Either leave them uncovered at
  the outline, or accept that the power board comes off to rewire the HAT.
- **Fan.** This board sits above the HAT, not the fan. It does not touch the
  fan keep-out (§13.3.13).
- **Heat.** The 5V buck dissipates ~2 W at full load (25 W at ~92 %). Place it
  away from the supercaps: cap life halves for roughly every 10 °C.

## P3. 12V input and protection

**The HAT's input chain moves here**: Q1 (P-FET reverse block), D3/R10 (its gate
clamp), D1 (SMBJ24A TVS) and C2 (bulk). §3.7.1 is the design record, so carry
it across rather than re-deriving it, including the 50V capacitor ratings that
D1's clamp voltage sets.

- **Harness.** OBD 12V, ground, CAN_H and CAN_L land on this board's own input
  terminal (CAN only for wake, §P7). On a rev 2.x HAT the signals and the HAT's
  own 12V still land on J2 as today (§P8).
- **Ground enters once.** OBD ground lands here, the board that carries the 12V
  return current, and reaches the HAT through the connector and the header.
- **Reverse blocking** also stops the supercaps back-feeding the vehicle during
  a dip. Q1 does that already. If the hold-up controller has its own input
  ideal diode (the LTC3350 does), decide which one owns the job rather than
  stacking two.

## P4. Hold-up

**What it must cover:**

1. **The crank dip.** OBD pin 16 sags during cranking. Size from measurement 16
   (§13.1), not from folklore.
2. **Unplugged with the Pi running.** A clean shutdown, including the NVMe
   drive flushing its write cache. Most consumer NVMe drives have no power-loss
   protection of their own, so this board is it. Budget **≥ 15 s at ≤ 10 W**
   until the shutdown time is measured on NVMe (§P10).

**Store energy on the 12V side.** Energy goes as V², so a stack charged near 10V
holds far more per farad than one at 5V. The 5V buck then runs from the
backed-up bus.

| Stack | C_stack | Usable 10V → 5V | At 8 W |
|---|---|---|---|
| 4S × 10 F | 2.5 F | ~94 J ideal, ~80 J delivered | ~10 s, **too thin** |
| **4S × 25 F** | **6.25 F** | **~234 J ideal, ~200 J delivered** | **~25 s** |
| 4S × 50 F | 12.5 F | ~470 J ideal | ~50 s, larger than needed |

- **Run cells at 2.5 V, not their 2.7 V rating.** Supercap life roughly halves
  per 10 °C and per 0.1 V of overvoltage, and a parked cab passes 70 °C. Pick
  85 °C-rated parts. A 25 F/2.7 V cell is about 16 × 26 mm, so four fit on a
  HAT-sized board.
- **Candidate controller: ADI LTC3350.** It is a 1–4 cell supercap
  charger/backup controller with a 4.5–35 V input, cell balancing,
  current-limited charging and an input ideal diode. It boosts the stack back up
  to the bus when the input fails, and reports input/stack voltages, currents
  and **measured capacitance and ESR** over I2C. That last feature means the
  board can report its own caps wearing out. *Verify:* its quiescent current
  (it decides whether the charger is switched off while parked, §P7), its
  maximum charge current, and the default I2C address (0x09, but it sits on the
  MCU's private bus anyway, §P6).
- **Charging inrush** is limited by the controller's charge current, not by
  the vehicle wiring. At 2 A, charging 6.25 F to 10 V takes ~30 s.

**Design tension to resolve: gating vs boot latency.** The supervisor must not
power the Pi until the stack holds one shutdown's worth of energy. Otherwise an
unplug during that window is an unclean shutdown. But if the charger is off
while parked and the caps have self-discharged, every key-on waits for a
recharge, and that delay lands in the boot gap rev 3.0 exists to capture. The
options:

- **(a)** Keep the stack topped up while parked. Costs the charger's quiescent
  current plus cap leakage.
- **(b)** Gate only on a *minimum* energy, well below full. Overnight
  self-discharge is usually small, so the gate is normally already met at wake.
- **(c)** Do not gate at all, and accept the small risk.

Decide from the measured parked self-discharge (§P10). **(b) is the expected
answer.**

## P5. 5V to the Pi

**Buck.** A 36 V-class automotive synchronous buck, 6 A, 5.1 V out. Candidate:
TI LM61460-Q1. Its enable pin **is** the Pi's power switch, so no separate load
switch is needed. Size the current for Pi + NVMe + USB devices + the HAT's 5V:
the Pi 5 alone peaks ~3–4 A under load, and an NVMe drive adds 2–3 W.

**Delivery: a USB-C cable to the Pi's power input, no USB-PD.**
- The Pi 5's 5.1 V/5 A mode is not a standard PD profile. A PD source offering
  it also needs a 5 A e-marked cable, and the Pi would still need the same
  firmware setting. So PD buys nothing here.
- The source presents Rp on CC like any plain USB-C supply. The Pi then assumes
  3 A unless told otherwise: set `usb_max_current_enable=1` in `config.txt`,
  or `PSU_MAX_CURRENT=5000` in the bootloader config.
- A cable keeps 4–5 A out of two stacking headers in series and out of HAT
  copper not sized for it. It also uses the Pi's normal input path, and on the
  bench you can unplug it and fit a stock Pi PSU.
- Cost: a short right-angle cable down the side of the stack, and possibly a
  case notch.

**Fallback: 5V through the header.** Use it if the cable cannot be routed. Then
this board is a **Power HAT+** (§3.8: the class whose ID EEPROM declares
`hat_current_supply`, so firmware sets the current limit itself, at a stacking
address `101_00XY` distinct from the HAT's 0x50). Every 5V and ground pin
through the pass-through must be used. This route is second choice because the
current crosses two header interfaces.

## P6. Connections

**To the Pi, via the header pass-through**:

| Net | Pin | Direction | Purpose |
|---|---|---|---|
| `PWR_FAIL` | **GPIO6** (pin 31) | PWR → Pi | Input lost, running on hold-up. Freed by the X1202's removal, so existing software keeps its pin. |
| I2C1 SDA/SCL | GPIO2/3 | Pi master, PWR **target** | Telemetry, commands, watchdog heartbeat. The address must avoid 0x6B and 0x77 (and 0x36/0x48 to keep old docs unambiguous). |
| Pi 3V3 sense | pin 1/17 | Pi → PWR | Tells the supervisor the Pi has halted: on a Pi 5, halt is HAT+ STANDBY, 3V3 down. This is the shutdown handshake's completion signal. |
| `PI_POWEROFF` (optional) | **GPIO27** (pin 13) | Pi → PWR | `dtoverlay=gpio-poweroff` confirmation, if 3V3 sense alone proves ambiguous. GPIO27 is spare on rev 2.1 and in rev 3.0's budget (§13.3.6). *Confirm the overlay behaves on a Pi 5.* |

**Every one of these is a crossing**: this board is always-on and the Pi
side is not. §13.3.2's decision applies to each one:
- **series resistance**;
- **hardware pulls to the safe state**, so an undriven net is harmless;
- **firmware tri-states every crossing net while Pi 3V3 is down.**

The I2C target sits behind a buffer powered from the Pi side (a
TCA4307-class hot-swap buffer, or a PCA9306 with Pi-side VREF). An always-on
board must never pull up, drive or hang the Pi's I2C1 while the Pi is dark. The
LTC3350 and any other monitoring parts sit on the **MCU's private bus**, so the
Pi sees one I2C target.

**To the HAT, rev 3.0 only** (a small keyed connector; rev 2.x has none):

| Net | Purpose |
|---|---|
| `+12V` (protected) | TLIN1027 VSUP, the K-line pull-up, U9's sensor 5V. This replaces the HAT's own input chain. |
| GND × several | Return. Ground enters at this board (§P3). |
| `V_BCKP_AON` | Always-on supply for the GNSS module's V_BCKP: the missing diode-OR leg from §3.6, which retires BT1. Not a hazardous crossing, because V_BCKP is meant to be live while the module is off. |

**Rule: no 3.3V or 5V logic rail goes to the HAT.** The HAT's logic stays on
the Pi's 3V3 so it powers up and down with the Pi (§3.7.5, §13.3.0). This board
supplies the Pi; the Pi supplies the HAT's logic.

## P7. Supervisor

**One MCU, boring by design.** A small Cortex-M0+ class part (STM32G0/C0
class) is plenty. The rev 3.0 lessons carry over:
- SWD on a bench header, with `BOOT0` strapped (§13.3.12);
- assembled by the fab (§13.3.11);
- **the Pi's 5V defaults on**: the buck's enable pull puts the Pi *powered*
  when the MCU is in reset, in its bootloader or hung (§13.3.3e). A dead
  supervisor then degrades to an always-on Pi, which is recoverable, never to a
  dark rig under the dash.

**States:**

```
 PARKED ──wake──► GATE ──caps ≥ min──► RUN ──Pi 3V3 drops──► OFF_HOLDOFF ──quiet──► PARKED
                                         │                                            ▲
                                         └──input lost──► HOLDUP ──Pi halted / caps exhausted──┘
```

- **PARKED.** Pi off (buck disabled). Only the MCU, the wake transceiver and
  battery sampling are alive. Charger per §P4.
- **Wake sources:**
  - a **receive-only wake-capable CAN transceiver** (TCAN1043-Q1 class) on the
    CAN tap, which catches key-on with the engine off;
  - VIN rising past ~13.2 V (alternator);
  - a bench pushbutton.

  The transceiver's TXD is strapped recessive, so the board is *physically
  incapable* of transmitting: §13.3.5's receive-only rule, enforced by wiring.
- **GATE.** Wait for the minimum hold-up energy (§P4), then enable the buck.
- **RUN.** The Pi decides when to shut down: sustained `engine_off` in
  `vehicle_state` (§3.7.6, still unwritten). When Pi 3V3 drops, the supervisor
  cuts the buck. A halted Pi 5 still draws ~80 mA at 12V (§13.3.1), so this cut
  is required, not a nicety.
- **HOLDUP.** Input lost: assert `PWR_FAIL`. The Pi shuts down at once, with no
  `UPS_SHUTDOWN_DELAY`-style wait: a hold-up stack is not a battery to ride on.
- **OFF_HOLDOFF.** After key-off the bus stays busy while modules wind down, so
  a naive wake-on-CAN would wake the Pi straight back up. Re-arm wake only
  after the bus has been quiet for a set time. The window is firmware, set from
  observation.
- **Watchdog.** Heartbeat over I2C. On timeout, power-cycle the Pi.
- **Low-battery refusal.** Do not power the Pi from a resting VIN below
  ~11.8 V. A no-start is the one failure this rig could inflict on the truck.

**Parked current budget: < 1 mA total.** The arithmetic is §13.3.1's: ~20 Ah
usable, so 1 mA is years. The regulator for the always-on rail sets this, not
the MCU (§13.3.2: a TPS62840-class nA-Iq buck). Everything else is switched off,
including the charger unless §P4(a) is chosen.

**Telemetry the board should expose.** VIN, stack voltage, measured
capacitance and ESR, 5V current, state, the last wake source, the last shutdown
cause, and parked battery-voltage history (§13.3.3d's parasitic-draw trend).
The Pi ingests the parked history at each boot.

## P8. Running with a rev 2.x HAT

CANary-PWR rev 1.0 does not wait for HAT rev 3.0:

- **12V.** The harness feeds 12V to this board *and* to the HAT's J2 pin 1. The
  HAT keeps its own Q1/D1 for its few-mA loads. J3 goes unused.
- **Pi.** USB-C cable (§P5), as designed.
- **Status.** Through the header pass-through: GPIO6, I2C1, Pi 3V3 sense,
  optionally GPIO27. All are free on rev 2.0 and 2.1 once the X1202 is gone.
  *Open:* whether the rework-fitted 13.5 mm stacking header's pins protrude far
  enough above the rev 2.0 HAT to take a socket. If not, use a short ribbon or
  flying leads to those few nets.
- **GNSS backup.** Unchanged (BT1). `V_BCKP_AON` needs rev 3.0's connector.

## P9. Software implied

- **Replace `scripts/ups_shutdown_watch.py`'s logic.** It fuses GPIO6 with the
  fuel gauge's discharge rate to tell the bench apart from key-off. Here
  `PWR_FAIL` is unambiguous, so the watcher becomes: on `PWR_FAIL`, shut down
  now. `src/daemon/ups.py`'s `input_lost` heartbeat field maps onto it directly.
- **Key-off shutdown**: escalate sustained `engine_off` in `vehicle_state` to a
  poweroff (§3.7.6, §11). Prerequisite for the whole sleep design, whatever
  board does the switching.
- **Ingest** the supervisor's telemetry and parked history: a small I2C reader
  feeding a status field and a channel.
- **Pi config**: `usb_max_current_enable=1` (or `PSU_MAX_CURRENT=5000`);
  `BOOT_ORDER=0xf416` once the NVMe base is fitted (NVMe, then SD as recovery,
  then USB); `PCIE_PROBE=1` and `dtparam=pciex1` if the NVMe base has no HAT+
  EEPROM.

## P10. Open items and measurements

| # | What | Decides |
|---|---|---|
| 1 | Case interior height with the X1202 removed, against NVMe base + this board + the HAT's tallest top-side part | Whether the lid fits; this board's height budget |
| 2 | Crank dip profile at OBD pin 16 (§13.1 measurement 16) | Stack size; the buck's minimum input |
| 3 | Shutdown time and peak power on NVMe, from `PWR_FAIL` to Pi 3V3 down | The ≥ 15 s / ≤ 10 W budget in §P4 |
| 4 | Pi 5 3V3 rise after 5V is applied (§13.1 measurement 15) | The rev 3.0 split (§13.3.0); also confirms 3V3 sense as the halt signal |
| 5 | Supercap self-discharge over a parked night and over a week, against the charger's quiescent current | §P4's gate-vs-latency choice |
| 6 | Bus-quiet time after key-off on the Xterra (from existing captures) | OFF_HOLDOFF window |
| 7 | Whether the 13.5 mm stacking header's pins protrude above the rev 2.0 HAT | §P8 header access |
| 8 | Datasheet checks: LTC3350 quiescent current, charge current and address; LM61460-Q1 dropout at 6 V in; TCAN1043-Q1 wake behaviour with TXD strapped | Part selection |
