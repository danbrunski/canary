# CANary form factor — exploration: one CM5 board, and a module on the OBD port

> **Exploratory. Nothing here is decided** (operator, 2026-10-31). This records
> a design alternative and the direction the operator wants to explore, so that
> later ideas have something concrete to compare against. It changes nothing in
> [`CANary-PWR-Spec.md`](CANary-PWR-Spec.md), the HAT documents or any roadmap
> task: PWR rev 1.0 and HAT rev 2.1 go ahead as specified. It is not roadmap or
> overnight work, and an agent should not treat any option below as chosen.

**Where this is heading.** The operator's long-term aim is a form factor that
**sits in a module hanging off the OBD port**, not a stack under the dash on a
harness. Option A (§2) is a step toward that: one board instead of four, built
on a Raspberry Pi Compute Module 5. §3 looks at what the OBD port itself imposes
and at three ways to put CANary there.

## 1. Today's baseline

The rig today is a Pi 5 with three boards around it, plus the cap bank on a
lead:

```
  CANary HAT        rev 2.1, on a 13.5 mm stacking header
  CANary-PWR        rev 1.0, on the Pi's header
  Pi 5 + active cooler
  NVMe base
  (cap bank)        87 × 55.5 mm board on a Micro-Fit lead
```

Part courtyards summed from the KiCad boards (2026-10-31, `GetCourtyard()` per
footprint):

| Board | Outline | Footprints | Courtyard, top + bottom | Largest items |
|---|---|---|---|---|
| HAT rev 2.1 | 87 × 56 mm | 104 | 1,572 + 314 = **1,886 mm²** | J2 13-way 3.5 mm terminal block 399, J1 stacking header 314, GNSS module 114 |
| PWR rev 1.0 | 65 × 56 mm | 132 | 1,787 + 865 = **2,652 mm²** | J1 stacking header 314, cap-bank connector 260, vehicle-in 191, HAT-out 178 |

PWR was placed at ~49 % courtyard fill on top and ~36 % across both sides, and
routing it took several rounds, the last by hand (PWR spec, "Hand routing,
2026-10-19"). That density is the yardstick for every size estimate below.

## 2. Option A — one CM5 board

### 2.1 What the CM5 brings

From the CM5 datasheet (RP-008180-DS-7):

- **A real off state.** PMIC_EN low puts the module at **1.3 mA** at 5 V; shut
  down with PMIC_EN high it draws 3 mA. Idle is ~400 mA and typical operation
  ~900 mA. The halted Pi 5 draws ~80 mA at 12 V, which is why PWR has to cut
  the buck (PWR §P1).
- **The power button is on the module connector.** PWR_BUT: a short press
  starts an OS shutdown, holding it over 5 s forces power-off. The watchdog gets
  a ladder: clean shutdown, then forced, then power cut. The Pi 5 does not
  bring its button out on the 40-pin header, so PWR cannot do this today.
- **RTC battery input.** VBAT takes 2.5–3.5 V and draws ~6 µA with the module
  off. It can run from the always-on rail. So can the GNSS backup supply, which
  PWR stopped exporting on 2026-10-18; on one board it is a trace. The HAT's
  coin cell (BT1) goes.
- **Rated −20 to +85 °C**, non-condensing; wireless is best below +75 °C.
- **The same GPIO bank, switchable to 3.3 V**, with the HAT EEPROM bus
  (ID_SD/ID_SC). SPI0 to the MCP2518FD, uart0 for K-line and uart2 for GNSS
  carry over, and an ID EEPROM lets `hat_rev.py` identify the board as a new
  revision beside 2.0 and 2.1.
- **eMMC on the module** (16, 32 or 64 GB; none on CM5Lite), and a Wi-Fi
  antenna switch: PCB antenna or U.FL (`dtparam=ant2`).
- **A clean power-up order:** 5 V, then PMIC_EN, then 3.3 V, then 1.8 V. The
  3.3 V output follows PMIC_EN, not the bootloader. That appears to answer
  HAT measurement 15 (§13.3.0) for a CM5.

### 2.2 What a carrier owes the CM5

- Two 100-pin Amphenol board-to-board connectors. **10164227-1004A1RLF** gives
  a 4.0 mm stack with 2.5 mm under the module for low parts; 10164227-1001A1RLF
  gives 1.5 mm and nothing underneath.
- A 90 Ω PCIe Gen 2 ×1 pair to an M.2 slot. Gen 3 sometimes works but is
  unsupported. An M.2 drive carries its own RX coupling caps.
- Ethernet magnetics and an RJ45: the gigabit PHY is on the module, and deploys
  go over Ethernet today (AGENTS.md, `192.168.137.2`).
- A USB host port (Canable, scan tools) and, with eMMC, a USB path for rpiboot
  (`nRPI_BOOT`).
- **No pin powered before the 5 V rail**, in the datasheet's own words. That is
  the HAT's §13.3.2 rule, now with the vendor's backing.
- 5 V at up to 5 A, which PWR's LM61460 already provides.

That points to six layers with controlled impedance; PWR and the HAT are
already ordered with impedance control.

### 2.3 How big

**Content.** Merging removes about 980 mm² of courtyard: the two stacking
headers (628), PWR's HAT-out connector (178), the HAT's duplicate 12 V
protection Q1/D1/C2 (146) and BT1 (32). The two vehicle connectors become one.
It adds the rev 3.0 MCU block (~300, a guess, since rev 3.0 is undrawn) and
~600 for the carrier's RJ45, USB and USB-C. The M.2 2230 drive goes on the
bottom in the module's shadow. Total: **~4,200 mm² of parts plus the
2,200 mm² module.**

**Area.** At PWR's density, balancing the two sides: **~6,000 mm², about
90 × 67 mm.** A Pi 5 is 85 × 56.

**Height.** The module sits 7.44 mm above the board on the 4.0 mm stack. The
official CM5 passive cooler is 56 × 41 × 12.7 mm, so ~20 mm above the board;
the M.2 drive adds 4–5 mm below. **About 27 mm in all**, or ~15 mm if a thermal
pad couples the SoC to an aluminium lid instead. Heat sets the height more
than the parts do.

**What could shrink it further.** Tag-Connect pads instead of SWD headers; one
compact vehicle connector instead of the 13-way terminal block (399 mm² by
itself); ceramics for the three 47 µF/50 V electrolytics. Perhaps 80 × 65 mm.
Below that, the minimum is set by the power stage (LTC3350, the 14 A inductors,
the FETs), not the CM5.

**Keep the cap bank off the board.** Supercap life roughly halves for every
10 °C (PWR §P4), and under a CM5 is the hottest place in the box.

**Layout zoning.** The power stage goes at the vehicle-connector end, the CM5
in the middle, and GNSS, the IMU and the ADC at the far end. PWR's §P8 already
asks for the IMU, ADC and crystal to stay off the buck.

### 2.4 One STM32 or two

**One would work.** The STM32U575 is low-power enough to stay on while parked,
and an always-on coprocessor gets back what the 2026-10-04 split gave up
(HAT §13.3.0's disposition table):

- CAN capture from the moment the bus wakes, before the CM5 is even powered,
  instead of tens of ms later (wake → buck → PMIC → MCU boot);
- motion-while-parked from the IMU;
- one firmware, one toolchain, one reflash path.

**Current leaning: keep two** (the G031 supervisor and the U575 coprocessor) on
the one board. The split was about failure domains; board count was only the
objection to it (§13.3.3(e) rejected a second MCU because "area is tight").
On one PCB the G031 costs ~40–90 mm² and a dollar or two. With one MCU:

- **The power logic shares an image with the firmware reflashed most often.**
  Crank-vs-unplug, the low-battery refusal, the watchdog and `~PWR_FAIL` would
  live beside K-line timing, ADC sequencing and the bulk dump. A bad build or a
  gdb breakpoint in coprocessor code leaves nothing watching for an unplug.
  The default-on rule keeps the CM5 powered, but nothing asserts `~PWR_FAIL`,
  so it dies uncleanly when the bank runs out.
- **Every MCU-to-CM5 line crosses power domains.** The SPI link, DATA_RDY and
  the SWD lines the CM5 drives are inside one domain today. With an always-on
  MCU each needs §13.3.2's three mitigations.
- **The MCU's private buses cross too.** The IMU, ADC and K-line transceiver
  either stay powered while parked, or each bus gets the same treatment.

With two, the split carries over unchanged: the G031 on the always-on
regulator, the U575 and every HAT-side part on the CM5's own 3.3 V output
(600 mA available, several times what those parts draw).

**What would change the leaning:** if the boot-gap capture (the first frames
after wake) turns out to matter more than expected, or the coprocessor
firmware settles down enough that "reflashed most often" stops being true.

## 3. Toward a module on the OBD port

### 3.1 What the port imposes

- **Weight and lever arm.** A J1962 socket is built for a plug held for
  minutes, not a box riding on it over years of washboard roads. AutoPi sells a
  CM4-based unit that plugs into the port, and its install guide offers an
  extension cable or a velcro strip. The port's mounting and the clearance
  around it (knees, pedals) have to be measured, and they differ by vehicle.
  The design aims at any vehicle, so a rigid module must fit the worst port it
  targets, or ship with a pigtail.
- **It takes the port the scan tool needs.** Watching a scan tool talk to a
  module is routine CANary work (AGENTS.md). A module in the port therefore
  needs a **female J1962 pass-through** on its back, wired straight through on
  all 16 pins, with CANary listening on the pins it uses.
- **Heat in a small box.** A rough check, not a measurement: a
  90 × 60 × 30 mm box has ~0.02 m² of surface. Still air under a dash carries
  roughly 5–10 W/m²·K off it, so 5 W raises the inside 25–50 K above the
  footwell. On a 40 °C day that is 65–90 °C inside, where the SoC throttles
  and supercap life is shortest. A finned aluminium shell coupled to the SoC
  changes those numbers; a plastic dongle does not.
- **Hold-up volume.** Energy is load × time. PWR budgets ≥ 15 s at ≤ 10 W
  (§P4), which is why the bank is four 25 F cells (~200 J delivered). Most of
  the 15 s is for an NVMe drive to flush its write cache. Without NVMe (eMMC
  only, on a filesystem built for power loss; the CM5 datasheet itself suggests
  f2fs, btrfs or overlayfs), a shutdown might fit in ~8 s at ~5 W. That is
  ~40 J, about one cell of today's bank. Unmeasured.
- **Antennas.** GNSS wants sky and gets little under a dash. The HAT already
  uses an SMA with active-antenna bias, so a module keeps a coax pigtail to an
  antenna on the dash. Wi-Fi from the footwell is probably fine for a phone in
  the cab.
- **Wired inputs.** The supercharger instrumentation (Bosch TMAP and AEM fuel
  pressure into the ADS8688A, `CANary-HAT-Rev2.1.md`) runs wires from the
  engine bay. A module either grows a small sensor connector, or the analog
  front end becomes a remote node with its own link. A new broadcasting node
  on the vehicle's own CAN bus would be a bigger step than anything CANary does
  today.
- **Service ports.** An RJ45 does not fit a dongle. The candidates are USB-C
  (rpiboot for eMMC; USB device mode, which networking over USB would need, is
  "not officially documented" on CM5) or Wi-Fi only.

### 3.2 Three shapes

**O1 — everything in the plug.** Option A's content in a rigid dongle, with
the RJ45, USB-A, M.2 and terminal blocks gone (the OBD plug is now the vehicle
connector), Tag-Connect for SWD, and hold-up cut to one cell by dropping NVMe.
About 3,200 mm² of parts with the cell. At PWR's density: **~75 × 60 mm,
~30 mm thick, plus the plug body.** For scale, an earlier AutoPi dongle (Pi
Zero W based) was 90 × 45 × 25 mm. This is the shape the heat, weight and
hold-up constraints above all bite hardest.

**O2 — a box on a pigtail.** Option A in an enclosure strapped under the dash,
on a 20–30 cm J1962 extension with the pass-through on the box. It solves
weight and port wear, relaxes heat, keeps the hold-up and the CAN path as they
are, and needs no new architecture. It is a module, but not one that hangs off
the port.

**O3 — split at the port.** The dongle holds the vehicle-facing front end:
protection, transceivers, the supervisor, the coprocessor MCU, GNSS and the
IMU. A cable runs to a compute box (CM5 carrier, hold-up, storage) wherever
there is room to cool it, such as under a seat. The cable carries USB and the
compute box's switched 12 V, so the supervisor still owns its power and
`~PWR_FAIL`.

- **The dongle is small.** The HAT's content minus its headers, terminal block,
  EEPROM and coin cell, plus the MCU block and the supervisor, is roughly
  1,500–1,600 mm². That makes a **~45 × 40 mm board**, about the size of a
  common OBD dongle, at well under 1 W.
- **CAN travels over USB as gs_usb**, the path the Canable already uses
  (`src/collection/` runs it through SocketCAN today), with the coprocessor
  protocol (`coprocessor-link.md`, SPI in v1) needing a USB transport.
- **It inverts a rev 3.0 rule.** The operator's 2026-10-14 failure-containment
  decision says the coprocessor must not take the Pi's CAN path down with it.
  If the dongle's MCU carries CAN, an MCU fault does exactly that. Keeping the
  rule would mean a second, simple USB-CAN MCU in the dongle (candleLight
  class, rarely reflashed) beside the coprocessor, behind a USB hub. The
  alternative is to change the rule knowingly.
- **Cheap to prototype.** HAT §13.4.3a already noted that a dev board plus a
  transceiver breakout answers the module-architecture questions (does gs_usb
  leave `src/collection/` untouched, is the timing as good as MCP2518FD over
  SPI) for less than a board spin.

| | O1 everything in the plug | O2 box on a pigtail | O3 split at the port |
|---|---|---|---|
| At the port | ~75 × 60 × 30 mm + plug | plug and cable | ~45 × 40 mm dongle |
| Heat at the port | ~5 W and up | none | < 1 W |
| Hold-up | in the dongle, ~1 cell without NVMe | in the box, as today | in the compute box |
| CAN path | MCP2518FD on SPI, as today | as today | gs_usb over USB through the dongle |
| New risk | heat, weight, port wear | a box to mount | a cable and a second box; CAN depends on MCU firmware |

**Leaning, not a decision:** O2 is Option A in a box and carries no new risk.
O3 is the shape that actually yields a dongle-sized module, and the one to
prototype first. O1 only if measurements say the heat and the weight work.

**Further out:** a dongle that is the whole system, logging on its own and
talking to a phone, with no Pi in the vehicle. That is outside the product
boundary in [`companion-app-plan.md`](companion-app-plan.md), where the Pi is
the only live collector and runs inference in the vehicle. It would only make
sense if that boundary moves.

## 4. What would decide it

1. **The OBD port's envelope on the dev vehicle:** clearance in every
   direction, how the socket is mounted, what load its latch tolerates. Then
   the same for any other vehicle in scope.
2. **Sustained power under CANary's real load** (daemon, detectors, a capture).
   PWR's INA226 measures the Pi's 5 V current, so the Pi 5 can stand in for a
   CM5 here.
3. **Clean-shutdown time and energy without NVMe.** This sizes the hold-up for
   O1 and O3. PWR §P10 already tracks the hold-up measurement.
4. **Temperature at the port** in summer with the engine running. Log it before
   designing an enclosure.
5. **Storage need:** a month of drives against 64 GB of eMMC with the retention
   sweep, which decides whether M.2 stays.
6. **GNSS under the dash** with the antenna on a pigtail, against the antenna
   on the dash.
7. **For O3:** whether candleLight-class gs_usb firmware runs on the chosen
   MCU's FDCAN, and how its timestamps and jitter compare with the
   MCP2518FD on SPI.

## 5. Software touch points

- **Board identity:** an ID EEPROM on ID_SD/ID_SC and a new entry in
  `hardware/canary-hat-firmware/hat_revisions.json`. Rev 2.0 and 2.1 stay
  supported.
- **Device tree:** the CM5 base tree plus a carrier overlay, in place of the
  overlay the HAT EEPROM names.
- **Provisioning:** eMMC needs an rpiboot path. An eMMC-only build is the
  no-drive case `install_pi.sh` already allows for `/data`.
- **Supervisor:** the I²C target protocol is unchanged; PWR_BUT and PMIC_EN
  are new outputs for it.
- **O3 only:** a USB transport for the coprocessor link, and CAN over gs_usb.

## 6. Sources

- Raspberry Pi, *Compute Module 5 datasheet*, RP-008180-DS-7:
  <https://pip-assets.raspberrypi.com/categories/944-raspberry-pi-compute-module-5/documents/RP-008180-DS-7-cm5-datasheet.pdf>
- Raspberry Pi CM5 passive cooler, 56 × 41 × 12.7 mm (The Pi Hut listing):
  <https://thepihut.com/collections/raspberry-pi/products/raspberry-pi-compute-module-5-passive-cooler>
- AutoPi TMU CM4 specifications (CM4 based, 2 × CAN, −20 to +70 °C) and
  install guidance: <https://docs.autopi.io/hardware/autopi_tmu_cm4/device_specifications/>,
  <https://docs.autopi.io/hardware/autopi_tmu_cm4/device_dimensions/>
- AutoPi's earlier Pi Zero W dongle, 90 × 45 × 25 mm (CNX Software):
  <https://cnx-software.com/?p=48876>
