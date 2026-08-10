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

#### 3.7.3 Consequences of having no power sense on this board

The CANary-HAT has no ignition sense, no 12V voltage sense, and no power-loss detect of its own. Two accepted consequences:

1. **The rig is unplugged when not in use, or** the daemon infers engine-off from bus activity and commands a shutdown/suspend. The second path is software work that does not exist yet — `vehicle_state` already classifies `engine_off` / `no_data` (see `AGENTS.md`), and `RecordArmingConfig.disarm_after_seconds` already detects the quiet period, but nothing currently escalates that to a system shutdown.
2. **Brownout asymmetry on the UPS battery path.** When vehicle 12V disappears but the X1202 keeps the Pi alive on battery, the Pi's 3V3/5V rails stay up while **+12V and therefore 3V3_BUCK collapse**. Anything on 3V3_BUCK dies mid-run while the host keeps driving its I/O lines into a dead rail — current flows through the parts' ESD clamps into 3V3_BUCK, which is a real partial-power-down stress case and can hang a shared I2C or SPI bus for the surviving devices. Mitigations, in order of preference:
   - Put the digital devices (MCP2518FD, LSM6DSOX, BMP384, GNSS, EEPROM) on the **Pi 3V3** rail and leave 3V3_BUCK for nothing, or for the analog/bus-side loads only. Total 3.3V draw here is on the order of 100–150 mA, well inside the Pi 5 header's HAT budget — which raises the fair question of whether the buck earns its place at all.
   - Or keep 3V3_BUCK, and accept/handle the collapse deliberately: series resistors on host-driven lines into buck-powered devices, and a documented expectation that CAN and sensors drop out the instant vehicle power does.
   - The **ID EEPROM must be on Pi 3V3 regardless** — the Pi reads it at boot, and a UPS-battery boot with the vehicle disconnected must still enumerate the HAT.

### 3.8 HAT ID EEPROM

| Parameter | Value |
|---|---|
| Part number | 24Cxx family (density to be finalized — **24C32 recommended**, per the Pi HAT reference design; 24C02 is tight) |
| Function | Raspberry Pi HAT ID EEPROM, stores HAT identification data for auto-configuration |
| Interface | I2C, address 0x50 |
| GPIO connection | GPIO0 (ID_SD), GPIO1 (ID_SC) — dedicated HAT ID EEPROM pins on Pi 40-pin header, isolated from main I2C1 bus |
| Package | SOIC-8 |
| Supply | Pi 3V3 rail (must enumerate at boot with no vehicle power present) |
| Support circuitry | Pull-up resistors on GPIO0/GPIO1 (3.9 kΩ per the HAT spec); write-protect (WP) jumper **normally open, not assembled** |

The WP jumper pads are left unsoldered in production. Confirm that WP has a **defined logic level with the jumper open** — a floating WP pin on a 24Cxx is undefined unless the part specifies an internal pulldown. A resistor to the intended default rail plus the solder jumper to the other state costs one 0402.

Only one ID EEPROM may exist across the whole stack. The X1202 does not present one; confirm this before adding any third board.

### 3.9 Terminal Blocks

| Parameter | Value |
|---|---|
| Part series | Phoenix Contact MC/MCV series |
| Pitch | **5.08mm** |
| Footprint source | KiCad built-in TerminalBlock_Phoenix library — the MC/MCV-G-5.08 `P5.08mm` variants |
| Block 1 | **5-position** — vehicle side: 12V, GND, CANH, CANL, K-line |
| Block 2 | **2-position** — +12V pass-through (12V, GND) to the X1202 via a DC barrel-jack pigtail |
| Placement | Board edge, with protection diode cluster placed nearby |

The 5-position block has no spare way for an ISO 9141 **L-line**. The dev vehicle does not need one (Nissan CONSULT inits with a byte sequence, not an L-line pulse), but some pre-CAN vehicles do — a scope decision to record rather than discover later.

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
| GPIO0 (ID_SD) | GPIO0 | 24Cxx HAT ID EEPROM (dedicated ID bus) |
| GPIO1 (ID_SC) | GPIO1 | 24Cxx HAT ID EEPROM (dedicated ID bus) |
| 3V3 (Pi) | 3.3V rail, header | TCAN1042 VIO, K-line RXD pull-up, EEPROM + ID pull-ups |
| 3V3_BUCK | AP63203Q output | HAT device supplies (see §3.7.3 before finalizing) |
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
| I2C (dedicated ID bus) | 24Cxx HAT ID EEPROM (0x50) | Standard I2C |
| UART (secondary, TBD) + PPS | MAX-M10M-00B | NMEA / UBX protocol |
| Power | Vehicle 12V → protection → +12V; AP63203Q → 3V3_BUCK; Pi header → 3V3, 5V0; 12V pass-through → X1202 | Distributed via Power.Cu |

I2C1 address check: the X1202 fuel gauge sits at 0x36 on bus 1 and does not collide with the BMP384 or LSM6DSOX. The Pi already fits 1.8 kΩ pull-ups on GPIO2/3 — do not add more on the HAT.

## 6. KiCad Implementation Notes

- Schematic organized hierarchically: root sheet plus six sub-sheets — Power, GPIO_Header, KLine_Transceiver, CANFD_Interface, Sensors, ID_EEPROM.
- Board outline: 65mm x 56mm standard HAT form factor with four mounting holes at Raspberry Pi standard hole coordinates. Verify clearance for the Pi 5 active cooler and for the X1202 stacking below.
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
- EEPROM WP jumper: pads left open, not soldered closed.

## 8. Open Items

Confirm before finalizing the board for fabrication.

**Must resolve (blocking)**

- **§4 pin map is stale.** Capture the deconflicted BARO_INT pin from the schematic; confirm IMU_INT.
- **GNSS UART assignment** — which secondary Pi 5 UART (uart2 GPIO4/5 or uart4 GPIO12/13), and disable the console on it.
- **GNSS PPS** — still unaddressed. Roadmap task 2b.3 and `docs/time-sync.md` both depend on it; without a pulse chrony stays at `precision 5e-1` and sits unselected, and the truck has no NTP at boot. Route TIMEPULSE to a free GPIO (GPIO18 = `pps-gpio` default).
- **3V3_BUCK vs Pi 3V3 device assignment**, per §3.7.3 — decide the brownout behavior deliberately rather than discovering it on the first UPS-battery run.
- **TCAN1042 S/STB pin** tied low (or driven).
- **MCP2518FD nRESET** pull-up / RC — must not float.
- **K-line commander termination**: 1 kΩ + series blocking diode from LIN to VSUP, plus the 200–220 pF bus capacitor.

**Confirm**

- Exact density of the 24Cxx HAT ID EEPROM — 24C32 recommended over 24C02.
- EEPROM WP level when the jumper is open (§3.8).
- SPI net naming: SDI/SDO labels vs the chip's own pin names, so MISO/MOSI don't get crossed at layout.
- Edge budget for the two 5.08mm blocks: 5-position + 2-position is ~36mm of connector plus end walls, against a 65mm board edge. Confirm it clears the mounting holes and the Pi 5 active cooler.
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

- **CAN:** swap the `mcp2515` overlay for `mcp251xfd` with `oscillator=40000000` and the final interrupt GPIO; `hardware.can` config (`interface: socketcan`, `channel: can0`) is unchanged.
- **IMU:** add `lsm6dsox` to the `ImuConfig.source` Literal in `src/config.py` (currently `bno055 | mpu6050 | icm20948 | disabled`) and write `imu_collector.py` (roadmap task 2b.2). The frame-resolution math in `imu_frame.py` already covers the raw-chip case.
- **Barometer:** no config, collector, or channel exists — only the `trainable=False` committee declarations from task 2a.8, waiting on members.
- **GNSS:** works as-is over a tty (`gps_collector.py` / gpsd), but the receiver needs its nav rate configured and the chosen UART's console disabled. PPS needs `pps-gpio` plus the chrony refclock change in `docs/time-sync.md`.
- **K-line:** entirely new — a source in the telemetry Literal, a `hardware.kline` config block (note `HardwareConfig` still carries a required `elm327_port`, which this is not), a CONSULT/KWP collector with EN-gating, `tMODE_CHANGE` blanking, and local-echo suppression.
- **Shutdown on engine-off:** the software half of §3.7.3 — escalate a sustained `engine_off` / `no_data` `vehicle_state` to a system shutdown or suspend. Nothing does this today.
