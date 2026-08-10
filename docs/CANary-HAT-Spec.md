# CANary-HAT — Board Specification

## 1. Overview

The CANary-HAT is a 4-layer Raspberry Pi 5 HAT integrating a CAN FD interface, K-line/LIN transceiver, 6-axis IMU, barometric pressure sensor, GNSS/GPS receiver, HAT ID EEPROM, and an automotive-grade buck power converter with 12V input protection. The board is designed for vehicle telematics, diagnostics, and asset-tracking applications requiring simultaneous CAN, K-line, motion, altitude, and positioning data on a Raspberry Pi 5 host.

**Role in the CANary stack.** This board **replaces the Waveshare 2-CH CAN HAT** as the in-vehicle CAN interface, and additionally folds in the GNSS receiver, IMU, and barometer that are currently unbuilt or externally attached. It does **not** replace the X1202 UPS HAT, which continues to power the Pi and own battery-backed shutdown. See §7 for the resulting power topology and §11 for the software work each subsystem implies.

## 2. Board Stackup

| Layer | Assignment | Notes |
|---|---|---|
| F.Cu | Signal | Component placement + primary routing |
| Gnd.Cu (inner 1) | Ground plane | Solid GND pour, stitched with vias to component GND pads |
| Power.Cu (inner 2) | Power plane | Carries the **+12V** net (VSUP_12V_RAW) plus the derived rails |
| B.Cu | Signal | Secondary routing |

- Board thickness: 1.6mm standard
- Copper weight: 1oz per layer
- Via configuration: through-hole, 0.7–0.8mm diameter / 0.4mm drill (≥0.15mm annular ring)
- Board outline: 65mm x 56mm (standard Raspberry Pi HAT form factor)

**Plane note.** Power.Cu is a mixed-rail plane, not a single-net pour: +12V, 3V3_BUCK, 3V3_PI, and 5V0 all live there as separate zones. Any B.Cu signal that crosses a zone boundary loses its return path — keep fast signals (SPI at up to 20 MHz, the GNSS RF feed) on F.Cu over solid Gnd.Cu, or stitch a local GND island on Power.Cu beneath them.

## 3. Major Components

### 3.1 CAN FD Controller

| Parameter | Value |
|---|---|
| Part number | MCP2518FDT-E/SL |
| Manufacturer | Microchip Technology |
| Function | External CAN FD controller with SPI interface |
| Supply voltage (VDD) | 2.7V to 5.5V |
| Interface | SPI, up to 20 MHz clock speed |
| Arbitration bit rate | Up to 1 Mbps |
| Data bit rate | Up to 8 Mbps |
| Package | SOIC-14, tape-and-reel packing |
| Standards compliance | ISO 11898-1:2015, AEC-Q100 qualified |
| Active current | Max 20 mA at 5.5V, 40 MHz CAN clock |
| Sleep current | 15 µA typical |
| Support circuitry | 40 MHz crystal with load capacitors; nRESET pull-up (see open items) |

The MCP2518FDT-E/SL handles CAN FD/2.0B protocol framing and message filtering over SPI to the Pi 5, pairing with the TCAN1042VDRQ1 physical-layer transceiver to drive the actual CAN bus. Its TXCAN/RXCAN pins cross-connect directly to the transceiver's TXD/RXD pins.

Linux support is the mainline `mcp251xfd` driver, configured by device-tree overlay — `dtoverlay=mcp251xfd,spi0-0,interrupt=<INT GPIO>,oscillator=40000000`. The driver expects a level-sensitive, active-low interrupt; the 40 MHz crystal must be declared to the overlay or the bit timing is wrong.

### 3.2 CAN FD Transceiver (Physical Layer)

| Parameter | Value |
|---|---|
| Part number | TCAN1042VDRQ1 [cite:54][cite:57] |
| Manufacturer | Texas Instruments [cite:54] |
| Function | Fault-protected CAN transceiver with I/O level shifting, CAN FD capable [cite:54] |
| Supply voltage (VDD) | 4.5V to 5.5V, connected to 5V0 rail [cite:57][cite:60] |
| I/O level-shifting voltage (VIO) | 3V to 5.5V, connected to the Pi 3V3 rail [cite:54] |
| Data rate | Classic CAN and up to 2 Mbps CAN FD [cite:54] |
| Standards compliance | ISO 11898-2:2016, ISO 11898-5:2007, AEC-Q100 (Grade 1) [cite:54] |
| Protection | ESD up to ±15kV (IEC), bus fault protection ±58V, undervoltage protection on VDD/VIO, thermal shutdown [cite:54] |
| Package | SOIC (8), VSON (8) 3.0mm x 3.0mm [cite:54] |
| Operating temperature | -55°C to 125°C (junction) [cite:54] |
| Loop delay | 110 ns typical [cite:54] |
| Support circuitry | CANH/CANL routed to terminal block; **no on-board 120Ω termination** |

The TCAN1042VDRQ1 provides the physical CAN bus driver/receiver stage. Its "V" suffix indicates a secondary I/O supply pin (VIO) that allows level-shifting between the CAN controller's 3.3V logic and the transceiver's 5V bus-side supply, with decoupling kept separate between the two supply pins [cite:54].

**Termination.** The board deliberately carries no 120Ω termination resistor. A vehicle bus is already terminated at both ends by the vehicle's own nodes; a third terminator is a fault. Bench use against an unterminated bus requires an external terminator in the harness.

**Standby pin.** The transceiver's S (STB) pin must be held low for normal operation — floating or high puts the device in silent/standby and no traffic reaches the controller. Confirm this is a hard pulldown or a driven GPIO (see open items).

### 3.3 K-Line / LIN Transceiver

| Parameter | Value |
|---|---|
| Part number | TLIN1027-Q1 [cite:53][cite:56] |
| Manufacturer | Texas Instruments [cite:56] |
| Function | LIN physical layer transceiver, **without dominant state timeout**; also supports ISO 9141 K-line diagnostics [cite:53][cite:56] |
| Supply voltage (VSUP) | 4V to 36V, connected to VSUP_12V_RAW rail, in series with an external reverse-blocking diode; 100 nF decoupling at the pin [cite:56] |
| Input voltage threshold (VIH) | 2V to 5.5V (supports 2.5V/3.3V/5V logic) [cite:62] |
| Data rate | LIN **transmit up to 20 kbps**; LIN receive up to 100 kbps for in-line programming [cite:53][cite:56] |
| Standards compliance | LIN 2.0/2.1/2.2/2.2A, ISO/DIS 17987-4.2, SAE J2602, AEC-Q100 (Grade 1: -40°C to 125°C) [cite:53][cite:56] |
| Bus fault protection | ±45V [cite:56] |
| Protection | Undervoltage protection on VSUP, thermal shutdown, unpowered/ground-disconnect failsafe, bus-stuck-dominant false-wake lockout [cite:53] |
| Package | SOIC (8), VSON (8) 3.0mm x 3.0mm [cite:56] |
| Support circuitry | **4.7 kΩ RXD pull-up**; bus-side commander termination (see below); protection diode network |

The TLIN1027-Q1 provides the K-line diagnostic interface, converting the Raspberry Pi's UART_TXD signal into a current-limited, wave-shaped K-line/LIN bus signal to reduce EMI, with UART_RXD feeding the response back to the host and GPIO26_EN controlling transceiver enable state [cite:56].

#### 3.3.1 Datasheet-verified design notes (TI SLLSF58B, rev. May 2022)

Verified against the TLIN1027-Q1 datasheet, since the K-line path is the board's highest-value new capability:

- **No dominant state timeout — this is the part's defining feature**, and it is the correct choice for K-line. Every other TI LIN transceiver enforces a TXD dominant timeout in the 10–20 ms range, which would abort an ISO 9141-2 **5-baud slow init** (200 ms per bit, up to ~600 ms held dominant). The TLIN1027-Q1 imposes no such limit, so both 5-baud slow init and fast init are possible. The datasheet Features list states "Supports ISO 9141 (K-Line)" outright. *Caveat from TI's applications team: the family is specified as* compatible with *ISO 9141 systems, not* compliant with *the ISO 9141 physical-layer levels and timings — it works in an otherwise-compliant system, but its own levels/timings are not guaranteed against that standard.*
- **RXD is an open-drain output** and requires an external pull-up of **1 kΩ to 10 kΩ** to the host I/O supply. The board's **4.7 kΩ is in spec** for 3.3V (check IOL: 1.5 mA at VOL = 0.4V). Pull it up to the Pi 3V3 rail, not 3V3_BUCK, so RXD stays referenced to the host that reads it.
- **Commander (master) mode needs external bus termination.** The internal termination is a **45 kΩ** pull-up (spec 20/45/60 kΩ) plus series diode to VSUP — that is *responder/slave* termination. A diagnostic tester is a commander node and requires an **external 1 kΩ pull-up plus series blocking diode from LIN to VSUP**, per the LIN spec and the datasheet's Figure 9-1. A 220 pF (responder) / 200 pF (commander) LIN-to-GND capacitor completes the network. **Confirm the "protection diode network" in the current design includes this 1 kΩ + series diode path** — the 4.7 kΩ RXD pull-up is a logic-side part and does not substitute for it.
- **TXD is internally pulled to ground**, so the bus defaults *dominant* if the host UART floats. This is why the EN gate matters: hold EN low (sleep, transmitter off) until the K-line collector is ready to talk, and pull EN low by default at boot. Per the datasheet, if EN were tied permanently active it should go to the host I/O supply through a 1–10 kΩ series resistor.
- **Mode-transition blanking.** RXD is indeterminate during mode changes while the receivers switch; software must not treat an RXD edge as data or as a wake event until `tMODE_CHANGE` has elapsed after raising EN.
- **20 kbps transmit ceiling.** This covers ISO 9141/KWP2000 at 10.4 kbps and Nissan CONSULT at 9600 baud, with margin for 15625 baud. It does **not** cover any protocol needing >20 kbps *transmit* (receive is fine to 100 kbps). Verify against the intended CONSULT baud before assuming full UpRev-equivalent coverage.
- **Single-wire echo is unavoidable.** Everything transmitted appears on RXD. The collector must strip its own echo — a software requirement, not a hardware fix.

### 3.4 Inertial Measurement Unit (IMU)

| Parameter | Value |
|---|---|
| Part number | LSM6DSOXTR |
| Manufacturer | STMicroelectronics |
| Function | 6-axis IMU (3-axis accelerometer + 3-axis gyroscope only — no onboard sensor fusion, no magnetometer) |
| Supply voltage | 1.71V to 3.6V (analog); 1.62V independent IO supply |
| Interface | I2C (shared bus with BMP384), also supports SPI / MIPI I3C |
| Accelerometer range | ±2/±4/±8/±16 g full scale |
| Gyroscope range | ±125/±250/±500/±1000/±2000 dps full scale |
| Supply current | 0.55 mA in combo high-performance mode |
| Package | LGA-14L, 2.5mm x 3.0mm x 0.83mm |
| Temperature range | -40°C to +85°C |
| Packing | Tape and reel (TR suffix) |
| Interrupt | INT1 to a host GPIO (pin reassigned — see §4) |
| Address strap | SA0 pin strapped for I2C address selection (0x6A / 0x6B) |

The LSM6DSOXTR was selected over a fusion-capable part such as the BNO085 to cut sensor cost roughly 5-7x, deliberately dropping onboard sensor fusion and the magnetometer entirely; orientation-fusion is instead handled in software running on the Raspberry Pi 5 host.

**This is compatible with the existing CANary code path.** `src/preprocessing/imu_frame.py` already implements the raw-chip route — a gravity low-pass over quiescent windows, yaw-offset resolution from straight segments, and longitudinal sign resolution against the OBD speed derivative — with on-chip fusion (BNO055) handled as a `gravity_removed=True` parameter rather than a separate code path. The absent magnetometer costs nothing: heading already comes from GPS course, not from the IMU.

### 3.5 Barometric Pressure Sensor

| Parameter | Value |
|---|---|
| Part number | BMP384 [cite:52][cite:55] |
| Manufacturer | Bosch Sensortec [cite:55] |
| Function | Digital absolute pressure sensor for altitude tracking [cite:52] |
| Supply voltage | 1.65V to 3.6V, VDD and VDDIO both on the same 3.3V rail [cite:61][cite:64] |
| Interface | I2C (up to 3.4 MHz, shared bus with LSM6DSOXTR) / SPI (up to 10 MHz) [cite:61] |
| Pressure range | 300 hPa to 1250 hPa [cite:61] |
| Resolution | Up to 21-24 bit [cite:61][cite:64] |
| Relative accuracy | ±9 Pa (900-1100 hPa, 25-40°C) [cite:61] |
| Current draw | ~2 µA sleep, ~700 µA peak during measurement, 3.4 µA @ 1Hz low-power mode [cite:61][cite:64] |
| Package | 10-pin metal-lid LGA, 2.0mm x 2.0mm x 0.95mm [cite:55] |
| Operating temperature | -40°C to +85°C [cite:61] |
| Interrupt | INT to a host GPIO (pin reassigned — see §4) |

The BMP384 provides altitude/barometric data suited to drone and vehicle applications, with best-in-class temperature coefficient offset in the -20°C to 65°C range [cite:52].

**Range check:** the vehicle operates at ~80 kPa (800 hPa, ~2072 m), comfortably inside the 300–1250 hPa range.

**Noise vs the roadmap's BMP390.** `docs/roadmap/imu-gps-integration.md` specifies a BMP390 for the `derived:GRADE` channel. Grade is a *derivative* of altitude, so what matters is short-term RMS noise and noise density, not the ±9 Pa relative-accuracy figure quoted above (BMP390: ±3 Pa). Confirm the BMP384's RMS noise at the intended ODR/oversampling against a grade budget. Both parts are 2.0 × 2.0 mm 10-pin LGA — keep the footprint swap-compatible so a BMP390 can drop in if the 384 proves too coarse.

### 3.6 GNSS / GPS Module

| Parameter | Value |
|---|---|
| Part number | MAX-M10M-00B [cite:35][cite:41] |
| Manufacturer | u-blox AG [cite:35] |
| Function | Standard precision GNSS/GPS receiver module [cite:35] |
| Supply voltage (VCC) | 1.8V to 5V main supply range [cite:35] |
| Interface | **UART** (to a secondary Pi UART — see §4), I2C (DDC) unused [cite:34][cite:41] |
| GNSS support | Concurrent GPS, GLONASS, Galileo, BeiDou (default: GPS + Galileo with QZSS/SBAS) [cite:39] |
| Position accuracy | 1.5 m CEP (high performance mode) [cite:39] |
| Power consumption | Less than 25 mW continuous tracking; less than 10 mW in 1 Hz cyclic power-save mode [cite:35][cite:39] |
| Package | LCC, 9.7mm x 10.1mm x 2.5mm [cite:41] |
| Operating temperature | -40°C to +85°C [cite:41] |
| Notes | Cost/power-optimized variant without integrated LNA/SAW filter; requires external active antenna with integrated filtering [cite:39] |

**UART, not I2C — this is a hard requirement, not a preference.** CANary's GPS path is `src/collection/gps_collector.py` (serial NMEA) or the gpsd path in `docs/time-sync.md`. gpsd requires a tty; it has no u-blox DDC/I2C transport. A GNSS module reachable only over I2C would leave both the `gps:*` telemetry channels and the chrony clock discipline with nothing to read.

**Nav rate.** The roadmap requires 10 Hz (1 Hz modules are "too slow to help transients or grade"). The M10 meets this; the receiver must be configured off its default rate at bring-up.

### 3.7 Power Regulation, Protection, and System Power Topology

| Parameter | Value |
|---|---|
| Part number | AP63203Q |
| Manufacturer | Diodes Incorporated |
| Function | Synchronous buck converter, fixed 3.3V output (**3V3_BUCK** net) |
| Input voltage range | 3.8V to 32V (fed from +12V on Power.Cu) |
| Output voltage | Fixed 3.3V |
| Continuous output current | 2A |
| Switching frequency | 1100 kHz |
| Quiescent current | 22 µA (Pulse Frequency Modulation mode) |
| Package | TSOT26 |
| Standards compliance | AEC-Q100 qualified, Temperature Grade 1 (-40°C to +125°C) |
| Protection | UVLO, output overvoltage protection, cycle-by-cycle peak current limit, thermal shutdown |

**12V Input Protection Chain** (upstream of buck converter and bus transceivers): reverse-blocking diode, TVS diodes, PTC fuse, bulk capacitor feeding the VSUP_12V_RAW net on Power.Cu.

#### 3.7.1 Rails

| Rail | Source | Feeds |
|---|---|---|
| VSUP_12V_RAW (+12V) | Vehicle OBD 12V via terminal block + protection chain | AP63203Q input, TLIN1027-Q1 VSUP, K-line commander pull-up, 12V pass-through block |
| 3V3_BUCK | AP63203Q from +12V | The HAT's own device supplies |
| 3V3 (Pi) | Raspberry Pi 40-pin header | Anything that must reference the host rail: TCAN1042 VIO, the 4.7 kΩ K-line RXD pull-up, ID EEPROM + its pull-ups |
| 5V0 | Raspberry Pi 40-pin header | TCAN1042VDRQ1 VDD (~70 mA) |

The HAT does **not** generate 5V and does **not** power the Pi.

#### 3.7.2 System topology

```
Vehicle OBD 12V ──> CANary-HAT terminal block ──> protection chain ──> +12V (Power.Cu)
                                                       │
                                                       ├──> AP63203Q ──> 3V3_BUCK (HAT devices)
                                                       ├──> TLIN1027-Q1 VSUP + K-line pull-up
                                                       └──> 2-pin Phoenix pass-through
                                                                  │
                                                       barrel-jack pigtail
                                                                  │
                                                                  v
                                                          X1202 UPS HAT ──> Raspberry Pi 5 (5V)
                                                                  │
                                                          (battery backup + GPIO6 PLD +
                                                           I2C fuel gauge at 0x36)
```

The X1202 remains the sole power source for the Pi and the sole owner of power-loss detection and battery-backed shutdown (`scripts/ups_shutdown_watch.py`, `systemd/ups-shutdown.service`).

#### 3.7.3 The pass-through carries the Pi's entire supply

The 12V pass-through leg branches off VSUP_12V_RAW **downstream of the protection chain**, so the PTC fuse, reverse-blocking diode, TVS, and +12V copper are in series with everything the X1202 draws — the Pi 5 itself plus UPS battery charging — not just the HAT's own ~150 mA. A Pi 5 at full tilt is ~25W, so budget on the order of **3A at 12V**, worst case.

Two consequences:

- **A series reverse-blocking diode dissipates ~2W at 3A.** In a TSOT/SMA package in a hot cabin that is a thermal problem, and the forward drop also eats into the buck's and the X1202's input headroom during cranking sag. Prefer an ideal-diode controller with a P-FET, or at minimum a low-Vf Schottky sized for the full load with the pad area to sink it.
- **Size the PTC hold current above the Pi's peak, not the HAT's.** A fuse chosen for the HAT alone will trip on a Pi 5 workload spike, and the failure looks like a random power-down rather than a fuse.

If neither is attractive, the alternative is to branch the pass-through *upstream* of the protection chain and let the X1202 own its own input protection — at the cost of the vehicle-side protection no longer covering the pigtail.

#### 3.7.4 3.3V current budget: does the buck earn its place?

The AP63203Q was added on the assumption that the HAT's 3.3V draw was near the limit of what the Pi can source on its header 3V3 pins. Itemising it against datasheet figures does not support that assumption.

| Device | 3.3V draw | Source |
|---|---|---|
| MCP2518FD (active, 40 MHz CAN clock) | 20 mA max | §3.1, worst case quoted at 5.5V — less at 3.3V |
| TCAN1042VDRQ1 **VIO only** | a few mA | VDD's ~70 mA sits on **5V**, not this rail |
| MAX-M10M (continuous tracking) | ~8 mA avg (<25 mW) | §3.6 |
| MAX-M10M (acquisition peak) | several tens of mA, transient | confirm against the u-blox datasheet |
| **Active antenna LNA bias** | 5–20 mA | easily the largest single item, and the one most often forgotten |
| LSM6DSOX (combo high-performance) | 0.55 mA | §3.4 |
| BMP384 (peak during measurement) | 0.7 mA | §3.5 |
| 4.7 kΩ K-line RXD pull-up (when low) | 0.7 mA | §3.3.1 |
| ID EEPROM + pull-ups | µA | — |
| **Total** | **~35 mA typical, ~90 mA peak** | ~150 mA with generous margin |

Against that, **there is no official Pi 3V3 limit to be near.** The Raspberry Pi HAT+ specification (RP-008281-DS) sets no maximum current for a HAT+ on the 3.3V rail, and Raspberry Pi has consistently declined to publish one for any model — the widely-cited 500 mA is an unofficial rule of thumb dating to the B+, not a spec. Even taken at face value, this board's peak is under a fifth of it.

So the buck is not buying current headroom. What it does buy is a second, independently-powered 3.3V domain — and in this stack that is a **liability, not a feature**, because it creates two asymmetric power states (§3.7.5). It also puts a 1.1 MHz switcher on a board whose GNSS receiver has no SAW filter.

**Recommendation:** lay out the buck footprint but **ship it depopulated**, with a 0Ω link tying the HAT's 3.3V devices to the Pi 3V3 rail. Measure the assembled board's actual draw; populate the buck only if the measurement contradicts the budget above. This keeps the option without paying for it, and the depopulated case is HAT+ compliant by construction. Nothing on the board requires a 12V-derived 3.3V rail — the TLIN1027-Q1 runs its VSUP and its commander pull-up directly off +12V.

#### 3.7.5 Two asymmetric power states, and why one rail avoids both

The HAT sits between a vehicle 12V feed and a UPS-backed host, so the two supplies can each outlive the other. With split rails, both directions are a partial-power-down case: a device is powered while the domain driving its I/O is not, and current flows through the parts' ESD clamps into the dead rail — which can also hang a shared I2C or SPI bus for whatever survives.

| State | Vehicle +12V / 3V3_BUCK | Pi 5V | Pi 3V3 | Split-rail consequence |
|---|---|---|---|---|
| Normal run | up | up | up | fine |
| **UPS battery run** (key off, X1202 carrying the Pi) | **down** | up | up | HAT devices die mid-run; host drives I/O into a dead 3V3_BUCK |
| **STANDBY** (`sudo halt`, vehicle still connected) | **up** | up | **down** | HAT devices stay alive and drive I/O into an unpowered host domain |

The STANDBY row is not hypothetical — it is a HAT+ conformance requirement: *"HAT+ boards must be electrically compatible with the STANDBY power state, where the 5V power rail is powered, but the 3.3V rail is not powered. Raspberry Pi 4 and later models support the STANDBY state,"* and *"HAT+ boards must not assume any particular sequencing timing between the 5V and 3.3V rails."* A HAT whose devices are powered from vehicle 12V violates this the moment the Pi is halted with the truck still plugged in.

Putting every 3.3V device on the Pi 3V3 rail collapses both rows: the HAT's logic domain lives and dies exactly with the host's, which is the state the HAT+ rules are written around. The one device that stays on 5V is the TCAN1042's VDD — and that is precisely what its VIO undervoltage protection exists for, so confirm that behaviour in the datasheet rather than assuming it.

If the buck is populated anyway, handle both rows deliberately: series resistors on every line crossing between domains, and a documented expectation that CAN and sensors drop out the instant vehicle power does.

#### 3.7.6 Consequences of having no power sense on this board

The CANary-HAT has no ignition sense, no 12V voltage sense, and no power-loss detect of its own. Accepted consequence: **the rig is unplugged when not in use, or** the daemon infers engine-off from bus activity and commands a shutdown/suspend. The second path is software work that does not exist yet — `vehicle_state` already classifies `engine_off` / `no_data` (see `AGENTS.md`), and `RecordArmingConfig.disarm_after_seconds` already detects the quiet period, but nothing currently escalates that to a system shutdown.

Note the interaction with §3.7.5: a software-commanded `halt` lands the system in STANDBY with the vehicle still connected, which is exactly the row that punishes a split rail.

### 3.8 HAT ID EEPROM

| Parameter | Value |
|---|---|
| Part number | **24C32** — see the conformance rules below; 24C02 is disqualified |
| Function | Raspberry Pi HAT ID EEPROM, stores HAT identification data for auto-configuration |
| Interface | I2C (100 kHz is sufficient), address 0x50 |
| GPIO connection | GPIO0 (ID_SD), GPIO1 (ID_SC) — dedicated HAT ID EEPROM pins on Pi 40-pin header, isolated from main I2C1 bus |
| Package | SOIC-8 |
| Supply | **Pi 3V3 rail** — 3.3V part, not a 5V-only EEPROM |
| Support circuitry | 3.9 kΩ pull-ups to 3.3V on GPIO0/GPIO1; WP pulled up to 3.3V via 1 kΩ to a test point |

#### 3.8.1 HAT+ conformance rules (RP-008281-DS)

The specification is prescriptive here, and two of its rules change the current design:

- **16-bit addressing is mandatory** — *"Use 16-bit addressable EEPROM. Do not use EEPROMs with 8-bit addressing."* This **disqualifies the 24C02** outright; it is an addressing rule, not a capacity judgement. The open item resolves to 24C32. The spec notes these restrictions "exclude many smaller I2C EEPROMs," so check the datasheet of whatever part is actually sourced.
- **WP should default asserted, not floating.** The spec's guidance is *"connect the EEPROM write protect pin to a test point on the board pulled-up to 3.3V with a 1KΩ resistor. During board test and probe, you can write to the EEPROM by driving the write protect pin low."* The current plan — an unassembled, normally-open jumper — leaves WP floating and the part unprotected-but-undefined. Adopting the spec's 1 kΩ pull-up plus test point costs two passives, gives WP a defined level, keeps the part programmable on the bench, and is what `eepflash.sh` expects.
- Also required: a 3.3V part (not 5V-only), a WP pin covering the **entire** memory, **no I2C clock stretching**, and no paged EEPROM where the low address bits select the page.
- Only an EEPROM and its 3.9 kΩ pull-ups may connect to ID_SD/ID_SC — nothing else, ever.

**Stacking.** Contrary to the older HAT rules, HAT+ explicitly supports stacking: permitted EEPROM addresses take the form `101_00XY`, where the `XY` suffix encodes the HAT class. Confirm the X1202 presents no ID EEPROM (it predates HAT+ and almost certainly does not), and keep 0x50 for this board.

**The EEPROM can carry the board's device-tree overlay name**, which firmware loads at boot. That is the cleanest way to bring up `mcp251xfd`, the GNSS UART, and `pps-gpio` — an overlay named in the EEPROM instead of hand-edited `config.txt` entries that have to be re-applied on every fresh Pi image. See §11.

**Not a Power HAT+.** This board sources no 5V to the Pi, so the Power HAT+ rules (`hat_current_supply`, the MODE1 address that makes firmware set `usb_max_current_enable=1`) do not apply. They would apply if the X1202's job ever migrated onto this board.

### 3.9 Terminal Blocks

| Parameter | Value |
|---|---|
| Part series | Phoenix Contact MC/MCV series |
| Pitch | **5.08mm** |
| Footprint source | KiCad built-in TerminalBlock_Phoenix library — the MC/MCV-G-5.08 `P5.08mm` variants |
| Block 1 | **6-position** — vehicle side: 12V, signal ground, chassis ground, K-line, CANH, CANL |
| Block 2 | **2-position** — +12V pass-through (12V, GND) to the X1202 via a DC barrel-jack pigtail |
| Placement | Board edge, with protection diode cluster placed nearby |

Block 1 maps 1:1 onto the OBD-II connector: pin 16 (battery +12V), pin 5 (signal ground), pin 4 (chassis ground), pin 7 (K-line), pin 6 (CAN High), pin 14 (CAN Low).

**Signal ground and chassis ground are tied together on the board.** That makes the HAT the bonding point between the two returns, which is fine on a vehicle where they are already bonded at the battery — the offset is millivolts. The layout consequence is worth being deliberate about: bring the 12V return in on chassis ground and reference the bus receivers to signal ground, joining the two at a **single star point** near the connector rather than letting them merge across the whole Gnd.Cu pour. Otherwise the HAT's power-return current (including the pass-through leg feeding the X1202 and therefore the entire Pi) shares copper with the CAN and K-line voltage reference.

The block has no spare way for an ISO 9141 **L-line**. The dev vehicle does not need one (Nissan CONSULT inits with a byte sequence, not an L-line pulse), but some pre-CAN vehicles do — a scope decision to record rather than discover later.

## 4. Raspberry Pi 5 GPIO Header Mapping

> **Status:** several assignments below were deconflicted after the first draft and are not yet reflected here. Rows marked **TBD** must be updated from the schematic before this table is trusted.

| Net Label | Pi 5 GPIO / Pin Function | Connected To |
|---|---|---|
| UART_TXD | GPIO14 (UART0 TXD) | TLIN1027-Q1 TXD (K-line) |
| UART_RXD | GPIO15 (UART0 RXD) | TLIN1027-Q1 RXD (via 4.7 kΩ pull-up to Pi 3V3) |
| GPIO26_EN | GPIO26 | TLIN1027-Q1 EN pin (default low = sleep) |
| SPI0_SCK | GPIO11 (SPI0 SCLK) | MCP2518FDT-E/SL SCK |
| SPI0_SDO (MCP → Pi) | GPIO9 (SPI0 MISO) | MCP2518FDT-E/SL SDO |
| SPI0_SDI (Pi → MCP) | GPIO10 (SPI0 MOSI) | MCP2518FDT-E/SL SDI |
| SPI0_nCS | GPIO8 (SPI0 CE0) | MCP2518FDT-E/SL nCS |
| CAN_INT | GPIO25 | MCP2518FDT-E/SL nINT |
| I2C1_SDA | GPIO2 (I2C1 SDA) | BMP384, LSM6DSOXTR (shared bus) |
| I2C1_SCL | GPIO3 (I2C1 SCL) | BMP384, LSM6DSOXTR (shared bus) |
| BARO_INT | **TBD** — *was GPIO6, moved* | BMP384 INT pin |
| IMU_INT | **TBD (verify)** — drafted as GPIO16 | LSM6DSOXTR INT1 pin |
| GNSS_TXD / GNSS_RXD | **TBD** — a secondary Pi 5 UART | MAX-M10M-00B UART |
| GNSS_PPS | **TBD / unassigned** | MAX-M10M-00B TIMEPULSE |
| GPIO0 (ID_SD) | GPIO0 | 24C32 HAT ID EEPROM (dedicated ID bus) |
| GPIO1 (ID_SC) | GPIO1 | 24C32 HAT ID EEPROM (dedicated ID bus) |
| 3V3 (Pi) | 3.3V rail, header | TCAN1042 VIO, K-line RXD pull-up, EEPROM + ID pull-ups |
| 3V3_BUCK | AP63203Q output (**depopulate — see §3.7.4**) | Nothing, if the recommendation is taken |
| 5V0 | 5V rail, header | TCAN1042VDRQ1 VDD |
| VSUP_12V_RAW | 12V input (via terminal block) | TLIN1027-Q1 VSUP, protection chain, buck input, 12V pass-through |
| GND | Ground plane (Gnd.Cu) | All subsystems |

### 4.1 Pins that are not available

- **GPIO6 is claimed by the X1202 UPS.** `scripts/ups_shutdown_watch.py` (`UPS_PLD_GPIO=6`) holds it as the power-loss-detect input, and `docs/roadmap/mcp-and-power-hardening.md` records that two processes claiming that line via `lgpio` collide with EBUSY. It was the draft's BMP384 INT pin; the reassignment above must be captured.
- **GPIO0/GPIO1 are the ID bus** and may carry nothing else — this also rules out Pi 5 `uart1`, which shares those pins.
- **GPIO14/15 (uart0) are consumed by K-line**, so GNSS must use another UART. Candidates on the Pi 5: `uart2` on GPIO4/5, or `uart4` on GPIO12/13. (`uart3` on GPIO8/9 collides with SPI0.) The serial console must be disabled on whichever UART is used.
- **PPS** wants a plain GPIO; GPIO18 is the `pps-gpio` overlay's default and is currently free.
- Free after the above: GPIO5, 7, 13, 17, 19, 20, 21, 22, 23, 24, 27 (subject to the UART choice). GPIO7 is SPI0 CE1 — worth reserving for a possible second CAN controller (see §10).

## 5. Interfaces Summary

| Signal Bus | Connected To | Protocol |
|---|---|---|
| SPI0 | MCP2518FDT-E/SL | Up to 20 MHz |
| CAN | MCP2518FDT-E/SL → TCAN1042VDRQ1 → bus (no on-board termination) | ISO 11898-1:2015 / ISO 11898-2:2016, CAN 2.0B / CAN FD up to 2 Mbps |
| K-Line / LIN (uart0) | TLIN1027-Q1 | ISO 9141 (K-line), LIN 2.x — 20 kbps TX / 100 kbps RX, no dominant timeout |
| I2C (I2C1) | BMP384 (0x76/0x77), LSM6DSOXTR (0x6A/0x6B) | Up to 3.4 MHz |
| I2C (dedicated ID bus) | 24C32 HAT ID EEPROM (0x50) | Standard I2C, 100 kHz |
| UART (secondary, TBD) + PPS | MAX-M10M-00B | NMEA / UBX protocol |
| Power | Vehicle 12V → protection → +12V; AP63203Q → 3V3_BUCK; Pi header → 3V3, 5V0; 12V pass-through → X1202 | Distributed via Power.Cu |

I2C1 address check: the X1202 fuel gauge sits at 0x36 on bus 1 and does not collide with the BMP384 or LSM6DSOX. The Pi already fits 1.8 kΩ pull-ups on GPIO2/3 — do not add more on the HAT.

## 6. KiCad Implementation Notes

- Schematic organized hierarchically: root sheet plus six sub-sheets — Power, GPIO_Header, KLine_Transceiver, CANFD_Interface, Sensors, ID_EEPROM.
- Board outline: 65mm x 56mm standard HAT form factor with four mounting holes at Raspberry Pi standard hole coordinates.
- Mechanical: screw terminals mount on the **top** side, facing away from the stack. The Pi 5 active cooler sits below the HAT and does not conflict with them; stack height is therefore set by the cooler plus the X1202 below, so size the standoffs and the header extension to clear both.
- Symbols/footprints for TLIN1027-Q1, TCAN1042VDRQ1, BMP384, and LSM6DSOXTR sourced from SnapEDA/UltraLibrarian in KiCad format; MCP2518FDT-E/SL uses a generic SOIC-14 footprint from the built-in Package_SO library with a custom symbol built from datasheet pinout if no verified listing is found.
- Layout guidance: terminal blocks at board edge with protection diode cluster nearby; MCP2518FDT-E/SL + TCAN1042VDRQ1 + 40 MHz crystal grouped tightly near the terminal block; BMP384/LSM6DSOXTR placed away from the crystal and any regulator, over solid Gnd.Cu with stitching vias underneath.
- TLIN1027-Q1 layout per datasheet §11: two vias minimum on GND, 100 nF at VSUP as close to the pin as possible, protection/filtering placed at the connector so ESD transients never propagate inboard.
- GNSS placement: keep the module and its antenna feed away from the AP63203Q switching node and the 40 MHz crystal; 50Ω controlled-impedance RF trace on F.Cu referenced to Gnd.Cu, kept short, with a ground keepout under the antenna feed as the module datasheet requires.
- Routing: wider traces on 12V, K-line, and CAN nets; standard widths elsewhere; 5V0 and 3V3 decoupling kept separate at the TCAN1042VDRQ1.

## 7. Manufacturing Notes

- Gerbers generated for all 4 copper layers (F.Cu, Gnd.Cu, Power.Cu, B.Cu), soldermask, and silkscreen layers on both sides, plus Edge.Cuts board outline.
- Drill files exported in Excellon format.
- Minimum trace/space and drill sizes verified against standard fabrication capabilities (typically 0.127mm/5mil trace-space, 0.2mm minimum drill).
- Gnd.Cu and Power.Cu filled as zones — Gnd.Cu solid, Power.Cu split per §2 — stitched to component pads via through-hole vias sized to meet a minimum 0.15mm annular ring requirement (0.7–0.8mm via diameter / 0.4mm drill).
- MAX-M10M-00B requires an external active antenna with integrated SAW filter and LNA, since this variant omits onboard RF front-end filtering to reduce cost and power [cite:39].
- Pick-and-place/centroid file to be exported alongside Gerbers if outsourcing partial assembly (e.g. PCBWay Combo sourcing) for BMP384 and LSM6DSOXTR; these two line items should be flagged in the BOM/order notes as assembly targets while other SOIC parts are self-supplied and hand-soldered.
- EEPROM WP: 1 kΩ pull-up to 3.3V plus a test point, per HAT+ §3.3 — drive WP low to program with `eepflash.sh`, leave it pulled up otherwise.
- AP63203Q and its passives: footprints laid out, **depopulated by default**, with the 0Ω link to Pi 3V3 fitted instead (§3.7.4).

## 8. Open Items

Confirm before finalizing the board for fabrication.

**Must resolve (blocking)**

- **§4 pin map is stale.** Capture the deconflicted BARO_INT pin from the schematic; confirm IMU_INT.
- **GNSS UART assignment** — which secondary Pi 5 UART (uart2 GPIO4/5 or uart4 GPIO12/13), and disable the console on it.
- **GNSS PPS** — still unaddressed. Roadmap task 2b.3 and `docs/time-sync.md` both depend on it; without a pulse chrony stays at `precision 5e-1` and sits unselected, and the truck has no NTP at boot. Route TIMEPULSE to a free GPIO (GPIO18 = `pps-gpio` default).
- **3V3_BUCK vs Pi 3V3 device assignment**, per §3.7.4/§3.7.5 — the recommendation is one rail (Pi 3V3) with the buck laid out but depopulated. Either way this must be an explicit decision, because a split rail has to survive both the UPS-battery row and the HAT+-mandated STANDBY row.
- **TCAN1042 S/STB pin** tied low (or driven).
- **MCP2518FD nRESET** pull-up / RC — must not float.
- **K-line commander termination**: 1 kΩ + series blocking diode from LIN to VSUP, plus the 200–220 pF bus capacitor.

**Confirm**

- ~~EEPROM density~~ — **resolved: 24C32.** HAT+ mandates 16-bit addressing, which disqualifies the 24C02 (§3.8.1). Verify the sourced part also has no clock stretching and a whole-device WP pin.
- TCAN1042 VIO undervoltage behaviour with VDD present and VIO absent — the STANDBY row depends on it (§3.7.5).
- MAX-M10M acquisition peak current and the active-antenna LNA bias draw, the two least-certain lines in the §3.7.4 budget.
- SPI net naming: SDI/SDO labels vs the chip's own pin names, so MISO/MOSI don't get crossed at layout.
- Edge budget for the two 5.08mm blocks: 6-position + 2-position is 8 ways ≈ 41mm of pitch plus end walls, against a 65mm board edge. Confirm it clears the mounting holes.
- **Current rating of the protection chain and the pass-through leg** (see §3.7.3) — the PTC fuse, reverse-blocking diode, and +12V copper carry the Pi's entire supply, not just the HAT's.
- Star-point placement for the signal/chassis ground bond (§3.9).
- Specific protection component part numbers in the 12V input chain (TVS diode rated for automotive load dump, reverse-blocking diode, PTC fuse ratings).
- Quiescent draw on +12V with the vehicle parked, if the harness ever lands on always-hot OBD pin 16.
- BMP384 RMS noise against the grade budget; keep the BMP390 footprint option (§3.5).
- TLIN1027-Q1 transmit ceiling (20 kbps) against the intended CONSULT baud rate.

**Recommended additions**

- **GNSS front end.** The MAX-M10M has no SAW/LNA and will sit directly above a Pi 5 running a WiFi hotspot (`docs/pi-hotspot.md`) — a real desense risk at 1.575 GHz. Either move to the MAX-M10S (integrated SAW + LNA) or add an external SAW + LNA. Specify the active-antenna bias network (feed inductor, DC block, short-circuit protection) and the antenna connector; none is currently in the spec.
- **GNSS V_BCKP** to a supercap, so the receiver hot-starts instead of cold-starting every boot — this directly shortens the pre-NTP window that PPS exists to close.
- **Status LEDs** (power good, CAN TX/RX, GPS fix) and a few test points. In-vehicle debugging currently costs an SSH round trip.
- **Second CAN channel**, or at least reserve CE1 (GPIO7) + an INT pin and leave the footprint. This board is a one-channel replacement for a two-channel Waveshare HAT; vehicles with HS-CAN and MS-CAN on separate OBD pins need both.
- **Common-mode choke footprint** on CANH/CANL, DNP by default.
- **Spread-spectrum buck variant**, if available in the AP63xxx family, to reduce broadband coupling into the GNSS feed.

## 9. What This Board Unlocks for CANary

The K-line interface is the reason to build this board. Two long-standing blockers close with it:

- **Commanded AFR and ignition timing are not on the CAN bus** (`docs/roadmap/engine-tuning-support.md`, `data/bindings.yaml`), and Mode 01 / Mode 22 exclusivity means a single CAN tuning capture cannot produce them. On-board K-line puts spark and commanded AFR on the *same host clock* as the CAN capture.
- It retires the laptop-side UpRev import path (`tools/import_uprev_log.py`) and the cross-log clock alignment it requires (`src/analysis/clock_align.py`) for anything the Pi can poll itself.

The IMU, barometer, and GNSS consolidate hardware the roadmap already committed to (`docs/roadmap/imu-gps-integration.md` Phase 2b, `docs/sensor-committees.md`) and unblock `derived:GRADE` plus four of the six declared-but-dormant sensor committees.

## 10. Capability Delta vs the Current Rig

| | Current (Waveshare 2-CH + X1202 + USB GPS) | CANary-HAT |
|---|---|---|
| CAN channels | 2 | 1 (see open items) |
| CAN FD | No (MCP2515) | Yes |
| K-line | Laptop only (UpRev) | On-board |
| GNSS | USB dongle, 1 Hz class | On-board M10, 10 Hz capable, PPS pending |
| IMU / baro | None | LSM6DSOX + BMP384 |
| Power sense | X1202 PLD + fuel gauge | Unchanged (X1202 retained) |
| Wiring | Multiple pigtails | Single 5-way terminal block + 12V pass-through |

## 11. Software Work Implied

None of this is blocking for fabrication, but none of it exists yet either.

- **Overlay delivery:** write a single board overlay covering `mcp251xfd`, the GNSS UART, and `pps-gpio`, and name it in the HAT+ ID EEPROM so firmware loads it at boot (§3.8.1). That beats hand-edited `config.txt` entries, which `scripts/install_pi.sh` would otherwise have to re-apply on every fresh image.
- **CAN:** swap the `mcp2515` overlay for `mcp251xfd` with `oscillator=40000000` and the final interrupt GPIO; `hardware.can` config (`interface: socketcan`, `channel: can0`) is unchanged.
- **IMU:** add `lsm6dsox` to the `ImuConfig.source` Literal in `src/config.py` (currently `bno055 | mpu6050 | icm20948 | disabled`) and write `imu_collector.py` (roadmap task 2b.2). The frame-resolution math in `imu_frame.py` already covers the raw-chip case.
- **Barometer:** no config, collector, or channel exists — only the `trainable=False` committee declarations from task 2a.8, waiting on members.
- **GNSS:** works as-is over a tty (`gps_collector.py` / gpsd), but the receiver needs its nav rate configured and the chosen UART's console disabled. PPS needs `pps-gpio` plus the chrony refclock change in `docs/time-sync.md`.
- **K-line:** entirely new — a source in the telemetry Literal, a `hardware.kline` config block (note `HardwareConfig` still carries a required `elm327_port`, which this is not), a CONSULT/KWP collector with EN-gating, `tMODE_CHANGE` blanking, and local-echo suppression.
- **Shutdown on engine-off:** the software half of §3.7.6 — escalate a sustained `engine_off` / `no_data` `vehicle_state` to a system shutdown or suspend. Nothing does this today.
