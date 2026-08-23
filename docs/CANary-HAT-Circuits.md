# CANary-HAT — Circuit Reference

Per-subsystem circuit reasoning for the board described in
[`CANary-HAT-Spec.md`](CANary-HAT-Spec.md): each part choice, its support
network, and the datasheet arguments behind them.

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

## 3. Major Components

As-built reference designators are given for each block. Full designator list:
U1–U7, Y1, J1–J4, JP1, BT1, L1, D1/D2/D5, R1–R9, C1–C16, MH1–MH4 — 48 as
built for rev 1.0; rev 2.0 removes D4 and adds Q1/R10/D3
footprints, with schematic and PCB in sync. There is no D3.

### 3.1 CAN FD Controller — U3

| Parameter | Value |
|---|---|
| Part number | MCP2518FDT-E/SL (MPN pinned on the symbol) |
| Manufacturer | Microchip Technology |
| Function | External CAN FD controller with SPI interface |
| Supply voltage (VDD) | 2.7V to 5.5V — **wired to +3V3 (Pi rail)**, decoupled by C7/C9 (100n) |
| Interface | SPI, up to 20 MHz clock speed |
| Arbitration bit rate | Up to 1 Mbps |
| Data bit rate | Up to 8 Mbps |
| Package | SOIC-14, tape-and-reel packing |
| Standards compliance | ISO 11898-1:2015, AEC-Q100 qualified |
| Active current | Max 20 mA at 5.5V, 40 MHz CAN clock |
| Sleep current | 15 µA typical |
| Support circuitry | **Y1 40 MHz crystal (ABLS2-40.000MHZ-D4YF-T, Abracon) with C5/C6 = 22pF** on OSC1/OSC2 |

TXCAN (pin 1) and RXCAN (pin 2) cross-connect directly to the TCAN1042's TXD and
RXD. CLKO/SOF (pin 3), nINT1/GPIO1 (pin 8), and nINT0/GPIO0/XSTBY (pin 9) are
left unconnected. nINT (pin 4) goes to GPIO25.

**The old "nRESET must not float" open item is void.** The 14-pin SOIC
MCP2518FD has no nRESET pin — the package exposes TXCAN, RXCAN, CLKO/SOF, nINT,
OSC2, OSC1, VSS, nINT1/GPIO1, nINT0/GPIO0/XSTBY, SCK, SDI, SDO, nCS, VDD and
nothing else. There is nothing to pull up.

#### 3.1.1 Crystal: resolved against both datasheets

Worked through the Abracon ABLS2 datasheet (rev. 11.15.2016) and MCP2518FD
DS20006027B on 2026-08-11.

**C5/C6 should be 27pF, not 22pF.** The part number `ABLS2-40.000MHZ-D4YF-T`
carries no load-capacitance field, and the ABLS2 ordering scheme treats an
omitted field as standard — **CL = 18pF**. The load caps follow from
C = 2·(CL − C_stray); with ~4pF of stray that is **28pF, so 27pF standard
value**. At 22pF the circuit presents CL ≈ 15pF to a crystal cut for 18pF.

**But the frequency consequence is negligible, and that is worth knowing before
anyone panics.** The pulling from a 3pF CL error is roughly **30ppm**, against
the MCP2518FD's `FOSC1,40M` requirement of **40 MHz ±0.5% — that is ±5000ppm**.
Even stacked with the crystal's own ±30ppm tolerance and ±30ppm stability, the
total is under 0.01% of a 0.5% budget. Fit 27pF because it is correct and free;
the secondary reason is drive level, since undersized load caps raise it toward
the ABLS2's 1000µW maximum.

**C5/C6 must be C0G/NP0, and that constraint outweighs the value above.** X7R
moves ±15% over −40 to +85°C and loses further capacitance to DC bias, which on a
27pF part is ±3–4pF — **the entire error the arithmetic above just corrected**.
A C0G/NP0 part holds ±30ppm/°C with no bias sensitivity, so 27pF is actually
27pF. At this capacitance it costs nothing.

**This was already specified, and the fact that it had to be re-derived is the
actual lesson.** §12.1 has carried `C5, C6 | C0G/NP0, 50V | Crystal load caps.
X7R shifts with temperature and pulls the 40 MHz oscillator` since 2026-08-11,
along with dielectrics, tolerances and voltage ratings for every other capacitor
on the board. On 2026-08-13 the constraint was "discovered" a second time from
the Macchina M2's BOM (§13.4) by someone reading §3.1.1 — which does all the
capacitance arithmetic and never mentions a dielectric — and grepping only this
file.

**The constraint lived one document away from the arithmetic it protects**, and
after the four-way split those are literally different files. So: state a
part's governing constraint where its value is derived, not only where the BOM is
assembled. §12.1 remains the authority on ratings; the per-part sections should
carry the constraint that makes their own reasoning true.

**The part of this BOM line that actually matters is the `F`.** The ABLS2
frequency table makes 24.01–70.00 MHz **3rd overtone the *standard* product**, and
24.01–50.00 MHz fundamental only as an *option*. The `F` in `D4YF` is that option
("Fund. AT >24MHz"). The MCP2518FD's oscillator is a plain Pierce circuit with no
tank, so a 3rd-overtone crystal in this socket either fails to start or runs at
its ~13.3 MHz fundamental — and `mcp251xfd` would be told 40 MHz regardless, so
every bit time on the bus would be wrong by 3×. **One letter in the part number
prevents that.** Do not let it be dropped in BOM transcription.

Two other options in that part number are load-bearing:

- **`D` = −40°C to +85°C.** The ABLS2 *standard* temperature range is 0 to +70°C,
  which is not a vehicle-cabin part.
- **`4Y` = ±30ppm tolerance, ±30ppm stability.** Ample against ±0.5%.

Supporting numbers: ESR 40Ω max for 16–50 MHz fundamental, C0 ≤ 7pF, drive level
100µW typ / 1000µW max, against the MCP2518FD's specified transconductance of
2040–3060 µA/V for a 40 MHz crystal — comfortable margin. Oscillator
stabilisation is 3ms from power-on.

Two notes rather than problems: the ABLS2 is an HC49/US can, **11.4 × 4.7 ×
3.3mm**, which is physically large for a 65 × 56mm board where a 3.2 × 2.5mm SMD
crystal would do; and Abracon markets it as COTS commercial/industrial, not
AEC-Q200, unlike the AEC-qualified parts elsewhere on this board. Neither
matters for a single research rig.

Linux support is the mainline `mcp251xfd` driver, configured by device-tree
overlay — `dtoverlay=mcp251xfd,spi0-0,interrupt=25,oscillator=40000000`. The
driver expects a level-sensitive, active-low interrupt.

### 3.2 CAN FD Transceiver (Physical Layer) — U4

| Parameter | Value |
|---|---|
| Part number | TCAN1042VDRQ1 [cite:54][cite:57] |
| Manufacturer | Texas Instruments [cite:54] |
| Function | Fault-protected CAN transceiver with I/O level shifting, CAN FD capable [cite:54] |
| Supply voltage (VDD) | 4.5V to 5.5V — **wired to +5V (Pi header pins 2/4)**, decoupled by C8 (100n) [cite:57][cite:60] |
| I/O level-shifting voltage (VIO) | 3V to 5.5V — **wired to +3V3 (Pi rail)** [cite:54] |
| Data rate | Classic CAN and up to 2 Mbps CAN FD [cite:54] |
| Standards compliance | ISO 11898-2:2016, ISO 11898-5:2007, AEC-Q100 (Grade 1) [cite:54] |
| Protection | ESD up to ±15kV (IEC), bus fault protection ±58V, undervoltage protection on VDD/VIO, thermal shutdown [cite:54] |
| Package | SOIC-8 (footprint `D0008A_L`) [cite:54] |
| Loop delay | 110 ns typical [cite:54] |
| Support circuitry | CANH/CANL straight to J2 pins 5/6; **no on-board termination, no ESD array, no common-mode choke** |

**STB is resolved.** Pin 8 (STB) is tied directly to GND in the schematic, so the
transceiver is hard-wired into normal mode. The old open item is closed.

**Termination.** The board deliberately carries no 120Ω termination resistor. A
vehicle bus is already terminated at both ends by the vehicle's own nodes; a
third terminator is a fault. Bench use against an unterminated bus requires an
external terminator in the harness.

That last sentence is a concession rev 2.0 can retire. The reasoning above is
right and the Macchina M2 shows the better expression of it: **unpopulated**
split-termination footprints (2 × 60.4Ω plus a centre cap to GND) gated by a
solder bridge, so the vehicle case stays correct by default and the bench case
is a rework rather than a harness accessory. Same for the common-mode choke,
whose footprint the M2 carries bypassed by 0Ω jumpers. See §13.4 — and note
§13.4.2 for the part of the M2's bus network *not* worth copying.

**Nothing else sits on the bus pins.** CANH and CANL run from the terminal block
to the transceiver with no series resistors, no bus TVS array, and no choke —
the transceiver's own ±58V fault rating and ±15kV IEC ESD rating is the entire
protection story on the CAN side. That is defensible for a device that lives
permanently inside a vehicle harness, but it is a deliberate position, not an
oversight to discover at bring-up (§8.2).

### 3.3 K-Line / LIN Transceiver — U2

| Parameter | Value |
|---|---|
| Part number | TLIN1027DRQ1 [cite:53][cite:56] |
| Manufacturer | Texas Instruments [cite:56] |
| Function | LIN physical layer transceiver, **without dominant state timeout**; also supports ISO 9141 K-line diagnostics [cite:53][cite:56] |
| Supply voltage (VSUP) | 4V to 36V — wired to **+12V** (downstream of **Q1**/D1 as of rev 2.0; was D4/D1), 100nF (C3) at the rail. *Abs max is −0.3 to +45V, which is what D1's 38.9V clamp is sized against* [cite:56] |
| Input voltage threshold (VIH) | 2V to 5.5V (supports 2.5V/3.3V/5V logic) [cite:62] |
| Data rate | LIN **transmit up to 20 kbps**; LIN receive up to 100 kbps [cite:53][cite:56] |
| Standards compliance | LIN 2.0/2.1/2.2/2.2A, ISO/DIS 17987-4.2, SAE J2602, AEC-Q100 (Grade 1) [cite:53][cite:56] |
| Bus fault protection | ±45V [cite:56] |
| Package | SOIC-8 HV (footprint `D0008B_HV`) [cite:56] |

**As-built support network:**

| Ref | Value | Connection | Assessment |
|---|---|---|---|
| R3 | 4.7 kΩ | RXD (pin 1) → **+3V3** | ✅ correct — see §3.3.2 for why 4.7 kΩ specifically |
| R6 | 1 kΩ | GPIO14/TXD0 → TXD (pin 4), in series | ✅ series protection on the host drive line, per §11.1 Pin 4 |
| **R8** | 4.7 kΩ | GPIO14/TXD0 → **+3V3**, on the **Pi side** of R6 | ✅ added 2026-08-10 — holds TXD recessive when the host stops driving (§3.3.2) |
| R4 | 4.7 kΩ | **EN (pin 2) → GND** | ✅ corrected 2026-08-10 — external pull-down backing the internal one |
| C4 | 220 pF, **100V C0G** (§12.1) | LIN (pin 6) → GND | ✅ correct — Figure 9-1 shows 220pF on the commander node too. **100V, not 50V**: the bus carries a ±45V fault rating and ISO 7637-2 pulses reach +100V/−150V, so 50V is marginal. C0G because an X7R part on a 12V bus loses capacitance to DC bias, changing the edge shaping this cap exists to set. For contrast the Macchina M2 uses `220PF 50V 5% NP0` here (§13.4) — right dielectric, **under-rated for the fault case** |
| R5 + D5 | 1 kΩ + **1N4148W** | **+12V → R5 → D5 (anode→cathode) → LIN**, commander pull-up | ✅ corrected 2026-08-10 — see below |
| C3 | 100 nF | +12V → GND at the VSUP pin | ✅ required by §10, and now drawn on this sheet |
| — | — | NC pins 3 and 8 unconnected; GND pin 5 to GND | ✅ |

**Bus topology as built** — J2 pin 4 lands directly on the LIN pin, with the
commander termination across it, which is Figure 8-1:

```
+12V ──> R5 (1 kΩ) ──> D5 anode
                       D5 cathode ──┬── U2.6 (LIN)
                                    ├── C4 (220 pF) ── GND
                                    └── J2 pin 4  (vehicle K-line)
```

The pull-up is taken from `+12V` rather than `+12V_RAW` so it shares the
receiver's reference — the LIN thresholds are ratiometric (`VBUS_CNT` = 0.5 ×
VSUP). **Rev 2.0 improves this slightly, as a side effect of removing D4.** Rev
1.0 had two drops in the pull-up path (D4 + D5), putting recessive near
Vbat − 1.1V against VSUP ≈ Vbat − 0.4V. With D4 gone there is one diode (D5) and
Q1's 38 mV, so **recessive lands near Vbat − 0.74V against VSUP ≈ Vbat − 0.04V**.
Both sit far above the 0.6 × VSUP recessive threshold, so this changes nothing
functionally — recorded because Figure 8-1 annotates the two-diode case and the
arithmetic here would otherwise read as stale.

D5 is a **1N4148W, not a Schottky**. Its job is blocking reverse current from a
live bus into an unpowered VSUP (§8.3.4), and Schottky reverse leakage — tens to
hundreds of µA at 12V, worse hot — undermines exactly that. The silicon part
leaks <100 nA while carrying 12 mA forward.

#### 3.3.1 Datasheet-verified design notes (TI SLLSF58B, rev. May 2022)

Verified against the TLIN1027-Q1 datasheet, since the K-line path is the board's
highest-value new capability:

- **No dominant state timeout — this is the part's defining feature**, and it is
  the correct choice for K-line. Every other TI LIN transceiver enforces a TXD
  dominant timeout in the 10–20 ms range, which would abort an ISO 9141-2
  **5-baud slow init** (200 ms per bit, up to ~600 ms held dominant). The
  TLIN1027-Q1 imposes no such limit, so both 5-baud slow init and fast init are
  possible. The datasheet Features list states "Supports ISO 9141 (K-Line)"
  outright. *Caveat from TI's applications team: the family is specified as*
  compatible with *ISO 9141 systems, not* compliant with *the ISO 9141
  physical-layer levels and timings.*

  **Confirmed by counterexample, 2026-08-11.** The Macchina M2 — open hardware,
  sold for exactly this job — uses the **NXP TJA1021** on both its ISO9141/LIN
  channels, and NXP documents a TXD dominant timeout on that part: it "prevents
  the bus line from being driven to a permanent dominant state… if pin TXD is
  forced permanently LOW," plus a startup check that leaves the transmit path
  disabled if TXD is LOW on entry to Normal mode. So a well-regarded automotive
  interface board most likely *cannot* do 5-baud slow init, which is the
  clearest evidence available that this bullet is selecting on something real.
  The exact `t_dom` value was not obtained (NXP's datasheet URL 404s), so the
  margin is unconfirmed — only the feature's existence. Note the scope: at
  CONSULT's 9600 baud a byte is ~1 ms, far inside any plausible timeout, so this
  is a **cross-vehicle** capability the TLIN1027 preserves, not an Xterra
  requirement.
- **RXD is an open-drain output** and requires an external pull-up to the host
  I/O supply. The datasheet's "1 kΩ to 10 kΩ" range is qualified by a footnote to
  check `IOL` for the actual VIO — at 3.3V the real floor is ~2 kΩ, not 1 kΩ.
  R3's 4.7 kΩ is correct; §3.3.2 works the numbers.
- **Commander (master) mode needs external bus termination.** The internal
  termination is a **45 kΩ** pull-up (spec 20/45/60 kΩ) plus series diode to VSUP
  — that is *responder/slave* termination. A diagnostic tester is a commander
  node and requires an **external 1 kΩ pull-up plus series blocking diode from
  LIN to VSUP**, per the LIN spec and the datasheet's Figure 9-1. R5 + D5 provide
  it. §11.1 Pin 6 is explicit that the resistor and blocking diode go "between the
  LIN pin and the VSUP pin" — across the bus, not in series with it, which is how
  they were originally drawn.

  *Value note:* 1 kΩ is the LIN commander figure and what the datasheet calls
  for. ISO 9141-2 specifies ~510Ω on the **tester** side, and this board is a
  tester rather than a LIN master. The Xterra's ECU supplies its own pull-up so
  1 kΩ should be sufficient; if a real ECU shows sluggish rise, 510Ω is the
  ISO-correct value and doubles pull-up current to ~24 mA. That is a value
  change, not a layout change.
- **TXD is internally pulled to ground** (Table 5-1; `RTXD` = 125/350/800 kΩ), so
  the transceiver requests *dominant* whenever the host stops driving TXD high.
  Two independent guards now cover that, because neither covers every state on
  its own — see §3.3.2.
- **EN has an internal pull-down** (`REN` = 125/205/800 kΩ), and §8.3.6 states its
  purpose outright: to keep the device in low-power mode if EN floats. R4 backs
  it externally so the guarantee does not rest on the 800 kΩ corner.
- **Mode-transition blanking, with numbers** (§6.8). `tMODE_CHANGE` 2–15 µs;
  sleep/standby → normal additionally needs `tNOMINT` 35 µs, so allow **~50 µs
  after raising EN** before trusting RXD. From power-up, `tPWR` is 1.5 ms. The
  bus wake filter `tLINBUS` is 25–150 µs.
- **20 kbps transmit ceiling.** This covers ISO 9141/KWP2000 at 10.4 kbps and
  Nissan CONSULT at 9600 baud, with margin for 15625 baud. It does **not** cover
  any protocol needing >20 kbps *transmit* (receive is fine to 100 kbps).
- **Single-wire echo is unavoidable.** Everything transmitted appears on RXD. The
  collector must strip its own echo — a software requirement, not a hardware fix.

#### 3.3.2 The host interface: why EN, TXD and RXD are wired the way they are

Both logic pins default to the *unsafe* state on their own, so the resistor
choices here are load-bearing rather than cosmetic. Recorded because the values
are not obvious and the failure modes are silent.

**EN — R4 as a pull-down to GND (4.7 kΩ).** EN is the only thing standing between
"host not driving TXD" and "HAT holds the vehicle's K-line dominant," so it must
be low by default and it must be *reliably* low. The internal pull-down alone
would do the job at its 205 kΩ typical, but at the specified 800 kΩ corner just
2.5 µA of node leakage reaches `VIH` = 2V — not a margin worth trusting on an
uncoated board in a vehicle cabin. R4 makes it ~425 µA. It costs GPIO26 0.7 mA to
drive EN high, against a pin good for several mA.

*Rejected alternatives:* a pull-up to 3V3 is what §11.1 Pin 2 suggests, but only
*"if this feature is not used"* — the EN gate is used here. A series resistor for
fault-current limiting (also §11.1 Pin 2) buys nothing once R4 goes to GND, since
GPIO26 → EN is entirely internal to the HAT with no connector on the net.

**TXD — R6 1 kΩ series plus R8 4.7 kΩ pull-up on the Pi side.** R6 is the
datasheet's suggested input-current limiter and is otherwise invisible: at the
125 kΩ `RTXD` corner it costs 26 mV of high level, and its RC with the pin
capacitance is ~10 ns against a 96 µs bit.

R8 closes a gap EN cannot. EN low covers the Pi being off or still booting, but
**not** the case where the collector has already raised EN and then dies,
releases the tty, or lets the GPIO revert to an input — TXD then falls to its
internal pull-down and the bus is held dominant with the transmitter still
enabled. R8 makes the released state *recessive* instead.

The two placement/value decisions:

- **Pi side of R6, not the transceiver side.** On the Pi side, the host driver
  sits directly on the pull-up node and TXD simply follows it through the 1 kΩ /
  125 kΩ divider — driving low costs nothing (~0.1V at the pin against `VIL` =
  0.8V). On the transceiver side the pull-up fights R6 and lands around 0.4V,
  spending half the low-level budget for no gain.
- **4.7 kΩ, not 10–22 kΩ.** The sizing case is GPIO14 high-Z, where the pull-up
  must beat the *Pi's own* internal pull-down (tens of kΩ) in parallel with
  `RTXD`. At a pessimistic 30 kΩ host pull-down and the 125 kΩ `RTXD` corner,
  4.7 kΩ holds 2.76V — clear of `VIH` = 2V. 22 kΩ would land near 1.8V and fail.

R8 only works while the Pi's 3V3 is up; EN covers every state where it is not.
Neither guard is sufficient alone, which is why both are fitted.

**RXD — R3 4.7 kΩ to the Pi 3V3 rail.** Squeezed from both directions:

- *Lower bound:* `IOL` is 1.5 mA at VOL = 0.4V, and `VOL` ≤ 0.6V is quoted at
  `RPU` = 2.4 kΩ. 4.7 kΩ sinks 0.62 mA — 40% of the spec point — so VOL comes in
  below 0.6V against a host input threshold near 0.8V. Counter-intuitively, a
  *smaller* pull-up makes the low level worse, not better.
- *Upper bound:* GPIO15's default internal pull-down fights R3. At a pessimistic
  30 kΩ, 4.7 kΩ still holds 2.85V; 10 kΩ would give 2.48V.
- *Not a constraint:* rise time is 4.7 kΩ × ~20 pF ≈ 94 ns, 0.1% of a 96 µs bit.

Set GPIO15's bias explicitly in the overlay rather than relying on the default
pull-down being at the weak end.

**RXD idle level differs by mode** (Table 8-1), and this is the bring-up trap:

| Mode | RXD | What the host UART sees |
|---|---|---|
| Sleep | floating → R3 pulls high | clean idle |
| **Standby** | **driven low** | **continuous break** |
| Normal | LIN bus data | traffic |

Per §8.4.1 the device powers up with EN low into **standby**, not sleep — §8.4.2
makes sleep reachable only *from* normal mode. So the first state after plugging
into the vehicle is the break-y one, and it stays that way until the collector
raises EN once. Reaching the clean idle state takes a deliberate EN high-then-low
cycle.

**RXD low means three different things** — a dominant bit (normal), a bus wake-up
request (standby, §8.4.4.1), and a VSUP undervoltage indication (§9.2.2.2, which
drops the device to standby). The line alone cannot distinguish them; the
collector must track the mode it commanded and apply the ~50 µs blanking. A
sudden break during operation may mean the truck's voltage sagged, not that the
bus went quiet.

**No series resistor on RXD** (unlike TXD): its high level is bounded by the
pull-up rail, so there is no overvoltage path into the host. For the same reason
the STANDBY power case is clean — when the Pi's 3V3 is down, both ends of R3 sit
at 0 with no back-feed path, which is exactly what a board-local 3.3V rail would
have broken.

### 3.4 Inertial Measurement Unit (IMU) — U6

| Parameter | Value |
|---|---|
| Part number | LSM6DSOXTR |
| Manufacturer | STMicroelectronics |
| Function | 6-axis IMU (3-axis accelerometer + 3-axis gyroscope only — no onboard fusion, no magnetometer) |
| Supply | VDD (pin 8) and VDDIO (pin 5) both on **+3V3** |
| Interface | **I2C** — CS (pin 12) strapped to +3V3 selects I2C mode; SCL on pin 13, SDA on pin 14 |
| I2C address | **0x6B** — SDO/SA0 (pin 1) strapped to +3V3 |
| Aux bus | SDX (pin 2) and SCX (pin 3) tied to GND (sensor hub unused) |
| Interrupts | **INT1 (pin 4) → GPIO16**; INT2 (pin 9) unconnected |
| Accelerometer range | ±2/±4/±8/±16 g full scale |
| Gyroscope range | ±125/±250/±500/±1000/±2000 dps full scale |
| Supply current | 0.55 mA in combo high-performance mode |
| Package | LGA-14L, 2.5mm × 3.0mm × 0.83mm |

The LSM6DSOXTR was selected over a fusion-capable part such as the BNO085 to cut
sensor cost roughly 5-7×, deliberately dropping onboard sensor fusion and the
magnetometer entirely; orientation-fusion is instead handled in software running
on the Raspberry Pi 5 host.

**This is compatible with the existing CANary code path.**
`src/preprocessing/imu_frame.py` already implements the raw-chip route — a
gravity low-pass over quiescent windows, yaw-offset resolution from straight
segments, and longitudinal sign resolution against the OBD speed derivative —
with on-chip fusion (BNO055) handled as a `gravity_removed=True` parameter
rather than a separate code path. The absent magnetometer costs nothing: heading
already comes from GPS course, not from the IMU.

**Every outstanding DRC violation is under this part.** The imported LGA-14L
footprint has 0.1952mm pad-to-pad gaps against a 0.2mm default clearance; the
current run flags pads 1–2, 3–4, 9–10 and 12–13. This is a footprint-geometry
fact, not a routing error — fix it with a local clearance override on U6 (or a
footprint-level rule), not by moving copper.

### 3.5 Barometric Pressure Sensor — U5

| Parameter | Value |
|---|---|
| Part number | BMP384 [cite:52][cite:55] |
| Manufacturer | Bosch Sensortec [cite:55] |
| Function | Digital absolute pressure sensor for altitude tracking [cite:52] |
| Supply | VDD (pin 10) and VDDIO (pin 1) both on **+3V3** [cite:61][cite:64] |
| Interface | **I2C** — CSB (pin 6) strapped to +3V3 selects I2C; SCK (pin 2) = SCL, SDI (pin 4) = SDA [cite:61] |
| I2C address | **0x77** — SDO (pin 5) strapped to +3V3 |
| Interrupt | **INT (pin 7) → GPIO17** (header pin 11) — moved off GPIO6, see §4.1 |
| Pressure range | 300 hPa to 1250 hPa [cite:61] |
| Resolution | Up to 21-24 bit [cite:61][cite:64] |
| Relative accuracy | ±9 Pa (900-1100 hPa, 25-40°C) [cite:61] |
| Current draw | ~2 µA sleep, ~700 µA peak during measurement [cite:61][cite:64] |
| Package | 10-pin metal-lid LGA, 2.0mm × 2.0mm × 0.95mm [cite:55] |

**Range check:** the vehicle operates at ~80 kPa (800 hPa, ~2072 m), comfortably
inside the 300–1250 hPa range.

**Noise vs the roadmap's BMP390.** `docs/roadmap/imu-gps-integration.md`
specifies a BMP390 for the `derived:GRADE` channel. Grade is a *derivative* of
altitude, so what matters is short-term RMS noise and noise density, not the ±9
Pa relative-accuracy figure quoted above (BMP390: ±3 Pa). Confirm the BMP384's
RMS noise at the intended ODR/oversampling against a grade budget. Both parts are
2.0 × 2.0 mm 10-pin LGA — the footprint on the board is swap-compatible, so a
BMP390 can drop in if the 384 proves too coarse.

### 3.6 GNSS / GPS Module — U7

| Parameter | Value |
|---|---|
| Part number | **MAX-M10M-00B** [cite:35][cite:41] |
| Manufacturer | u-blox AG [cite:35] |
| Function | Standard precision GNSS/GPS receiver module [cite:35] |
| Supply | VCC (pin 8) and V_IO (pin 7) both on **+3V3**; VIO_SEL (pin 15) unconnected |
| Interface | **UART** — TXD (pin 2) → GPIO5, RXD (pin 3) → GPIO4 (Pi 5 `uart2`). DDC/I2C pins 16/17 unconnected [cite:34][cite:41] |
| Time pulse | TIMEPULSE (pin 4) → **header pin 12 = GPIO18**, the `pps-gpio` overlay default |
| Backup supply | V_BCKP (pin 6) ← D2 (BAT54) ← BT1 (2-pin JST-SH connector, external cell) |
| RF front end | RF_IN (pin 11) ← C16 (10nF DC block) ← J4 (SMA edge-mount, Molex 73251-1153); VCC_RF (pin 14) → L1 (27nH, LQG18HN27NJ00D) → antenna feed = **active-antenna bias** |
| Unused | EXTINT (5), RESET_N (9), LNA_EN (13), SAFEBOOT_N (18) all unconnected |
| GNSS support | Concurrent GPS, GLONASS, Galileo, BeiDou (default: GPS + Galileo with QZSS/SBAS) [cite:39] |
| Position accuracy | 1.5 m CEP (high performance mode) [cite:39] |
| Power | <25 mW continuous tracking; <10 mW in 1 Hz cyclic power-save [cite:35][cite:39] |
| Package | LCC, 9.7mm × 10.1mm × 2.5mm [cite:41] |
| Notes | Cost/power-optimised variant **without integrated LNA/SAW filter**; requires an external active antenna with integrated filtering [cite:39] |

**UART, not I2C — this is a hard requirement, not a preference.** CANary's GPS
path is `src/collection/gps_collector.py` (serial NMEA) or the gpsd path in
`docs/time-sync.md`. gpsd requires a tty; it has no u-blox DDC/I2C transport. The
as-built board honours this: DDC is unconnected and the module talks UART only.

**Nav rate.** The roadmap requires 10 Hz (1 Hz modules are "too slow to help
transients or grade"). The M10 meets this; the receiver must be configured off
its default rate at bring-up.

**Antenna bias network is now specified in hardware** (it was a gap in earlier
revisions): the module's own VCC_RF output feeds the antenna centre conductor
through L1 (27nH feed inductor), and C16 (10nF) DC-blocks the path into RF_IN.
There is **no bias fuse or short-circuit protection** on that feed — a shorted
antenna cable pulls VCC_RF down, which is the module's own supply. Consider a
small series resistor or resettable element if the antenna is field-swappable.

**The topology is right and one value is confirmed exactly** (Integration Manual
UBX-22038241 §3.3, Appendix C, read 2026-08-22). u-blox's reference bias-T is the
same shape as ours — connector node with a series cap into RF_IN and a feed
inductor to the antenna supply — and their recommended feed inductor `L3` is
**27 nH**, which is exactly L1. The requirement behind that number is worth
recording because it is not a value anyone should round: the inductor is chosen
to resonate at L1 frequency, giving **>500 Ω** series impedance so the feed point
does not load the 50 Ω line, with a **current rating above 300 mA**. The manual
also confirms VCC_RF is a legitimate antenna supply ("the antenna can be supplied
by VCC_RF or an external supply"), provided it stays clean.

**Do not read the M10M's "antenna supervisor" as filling the protection gap** —
it is a host-side circuit needing switch MOSFETs, a comparator and a sense shunt,
none of which this board has. See the retraction in §8.3.

> ⚠️ **Unresolved: C16 is 10nF here and 15pF in §12.1/§12.5** — a 666× difference
> on the RF path. §12.5 logs `C16 → 15pF C0G 0402, SRF-tuned` as a *completed*
> change (2026-08-11), and §12.1 explains the sizing: self-resonance placed at
> 1.575 GHz, where series impedance nearly vanishes. That reasoning is sound and
> a 10nF 0402 has its SRF far below L-band, so **15pF is very likely the as-built
> value and this section is stale** — but the header claims §2–§7 was read back
> out of the netlist, so the two cannot both be current. Flagged rather than
> silently corrected: read the schematic before trusting either number, and note
> §13.1 measurement 4 (GNSS TTFF and C/N₀) is the test that would expose a wrong
> DC block. Found 2026-08-13.
>
> **Source of the 10nF figure identified 2026-08-22 — but it does not settle the
> as-built value.** u-blox's Appendix C gives `C14`, the RF bias-T capacitor in
> exactly this position, as **10 nF, 10%, 16 V, X7R**. So §3.6's number is the
> reference-design value and §12's is a deliberate divergence, not a typo on
> either side. **The as-built value is 15pF** — confirmed directly from the
> schematic and `BOM.csv` (`C16, 15p, C_0402_1005Metric`), which resolves the
> "which is current" question this note raised.
>
> The remaining question is whether the divergence was right, and it is defensible
> rather than obviously wrong: at 1.575 GHz a 15pF is roughly 6.7 Ω of series
> reactance — about 23 dB return loss and ~0.02 dB insertion loss in a 50 Ω line —
> while a 10nF 0402 sits far above its own SRF and behaves inductively at a
> comparable magnitude. Neither is a defect. Note also that **RF_IN is DC-blocked
> internally** (Integration Manual pin table: "the RF signal line is DC blocked
> internally"), so this cap is belt-and-braces in the first place. **Record it as
> a decision with its reasoning, not as an unexplained departure from the
> reference design** — that is the §3.1.1 lesson about stating a constraint where
> the value is derived.

#### 3.6.1 The RF launch (reworked 2026-08-11)

The feed is 0.20mm on F.Cu, which computes to **~45Ω** over the 0.1mm
F.Cu→Gnd.Cu prepreg once 1oz copper thickness is included. Across the plausible
envelope — εr 4.0–4.6 and ±10% prepreg tolerance — it stays between **42 and
48Ω**, worst case 21dB return loss. The width is not worth retuning; 0.35mm
would give ~32Ω and 0.18mm ~48Ω, but prepreg tolerance moves it further than
either.

**The problem was the SMA pad, not the trace.** J4's signal pad is
**5.08 × 2.29mm** (X 159.64–164.72, Y 67.11–69.39 — note the footprint is rotated
180°). Over a 0.1mm dielectric that is a **7Ω** transmission line 5mm long, and
length is what makes a discontinuity bite: at 0.05λ it reflects hard.

| Reference under the pad | Z₀ | Return loss |
|---|---|---|
| In1.Cu intact (0.100mm) | 7Ω | **2.9 dB** |
| **In1.Cu voided → In2.Cu (1.375mm)** | **54Ω** | **25.3 dB** |
| Both inner layers voided → B.Cu | *nominally 57Ω* | *fiction — see below* |

**The fix, as built:** a notch in the In1.Cu zone outline spanning **X 159.65 →
165.50, Y 66.90 → 69.60** — the full pad with ~0.21mm margin top and bottom,
opening out to the board edge. It is a concave notch in the single zone outline,
not a separate keepout, which is the natural shape for an edge-mount connector.

**Void In1.Cu only.** Voiding both inner layers looks similar on paper but is
wrong: B.Cu carries no copper beneath that pad, so the pad would reference
nothing and land somewhere uncontrolled around 70–90Ω. And the obvious objection
to using In2.Cu — that it is a *power* plane, not ground — does not survive the
arithmetic: 65 × 56mm across a 1.24mm core is about **112pF of interplane
capacitance, 0.9Ω at 1.575 GHz**. In2.Cu is an RF ground whatever DC sits on it.

**Bias the void inward from the pad, never outward.** The trace needs its 0.1mm
ground reference right up to where it widens into the pad — void under the trace
and it jumps to ~150Ω. The pad has 5mm of length to spare; the trace has none.

**Stitching:** five GND vias now flank the launch at (156,72), (159,75), (159,61),
(164,75) and (164,61), bracketing the void band so the return current runs down
each side rather than around the hole. Before this there were none within 10mm,
and the only layer transitions were the connector's own two plated posts.

**Fab dependency:** all of the above assumes the **0.1mm outer prepreg**. If the
fabricator substitutes 0.2mm — an ordinary thing to do when unspecified — the
0.2mm trace becomes **~66Ω**. State the outer dielectric thickness in the fab
notes; that matters far more than εr, which the envelope above already absorbs.

**Backup supply has no fallback.** V_BCKP is fed *only* from BT1 through D2. If
the JST connector is empty the pin floats, and there is no path from +3V3, so the
receiver cold-starts every boot — the exact cost PPS exists to avoid. Note also
that a series Schottky from BT1 means **no charge path**: BT1 must be a primary
cell, not a supercap. u-blox expects V_BCKP to be supplied (commonly diode-OR'd
with the main rail) rather than left open.

### 3.7 Power Input, Protection, and System Power Topology

**There is no on-board regulator.** The AP63203Q buck of earlier revisions is not
in the schematic, not in the BOM, and has no footprint on the PCB. The board
takes 3.3V and 5V from the Pi header and 12V from the vehicle, and generates
nothing.

#### 3.7.1 12V input protection chain (sheet `OBD_IO`)

| Ref | Part | Role |
|---|---|---|
| J2 pin 1 | terminal block | Vehicle 12V in → net `+12V_RAW` |
| **Q1** | **SUD50P06-15** (Vishay, P-channel, −60V, 15 mΩ, TO-252) | **Series reverse-blocking, `+12V_RAW` → `+12V`.** Added rev 2.0 (§13.2.1); replaces D4 |
| **R10 / D3** | 100 kΩ 0603 · BZT52C15 (15V Zener, SOD-123) | Q1 gate pull-down and V_GS clamp. Zero quiescent draw |
| ~~D4~~ | ~~SS54 (SMB, 5A/40V Schottky)~~ | **Removed rev 2.0** — Q1 does the one job it had. See below |
| **J3** | 2-pos terminal block | Pass-through to the X1202, now tapped off **`+12V`, downstream of Q1 and D1** |
| D1 | SMBJ24A (SMB, 600W TVS, 24V standoff) | `+12V` → GND transient clamp — **now ahead of the J3 fan-out**, so it clamps the Pi's leg too |
| C2 | **DNP on rev 1.0** — footprint Ø6.3mm / 2.5mm pitch; if populated, **47µF 50V 105°C** (Panasonic `EEU-FR1H470`) | Bulk on `+12V`. **Was documented as 100µF, which does not exist in this footprint** — see below |
| C3 | 100nF 0603 X7R, **50V minimum** | HF decoupling on `+12V`, at the TLIN1027 VSUP pin. Rating set by D1's clamp, not by the rail — see below |

**C2's value and its footprint were never compatible — found 2026-08-13, when
PCBWay asked for a part number.** 100µF at 50V does not exist in a 6.3mm can; the
smallest standard case for that combination is 8 × 11.5mm on a 3.5mm lead pitch.
At Ø6.3mm / 2.5mm pitch the largest standard 50V value is **47µF** — Panasonic
`EEU-FR1H470` (6.3 × 11.2mm, 105°C, 10,000 h) or Nichicon `UPW1H470MED`. Note the
trap this sets for anyone sourcing without the context: dropping to **25V** makes
100µF fit the footprint, and it is the obvious move, and it fails on the first
load dump.

**Rev 1.0 would have shipped with C2 unpopulated** (operator decision, 2026-08-13: 47µF/50V in
a 6.3mm can was not readily sourceable through the assembler). That is
acceptable, and the reasoning below is why — but note what it removes. C3 (100nF
at the VSUP pin) is the part the TLIN1027 datasheet §10 actually requires, and it
stays. D1 does the transient clamping either way. What C2 would have added is
bulk in front of D1, which shaves the leading edge of a transient before the TVS
conducts. **If bring-up ever shows TLIN1027 upsets or resets during cranking or
hot-plug, populating C2 is the first thing to try** — the footprint is there, and
33µF or 22µF at 50V in a 6.3mm can is a commodity part where 47µF is not.

**Nothing much is lost at 47µF or at zero, which is why this is a bookkeeping fix
and not a board change.** C2's capacitance is not load-bearing here. The 1 kΩ
commander pull-up
draws ~11 mA, so one 96 µs dominant bit at 10.4 kbps moves 1.06 µC and sags the
rail **22 mV on 47µF against 11 mV on 100µF** — both irrelevant against VSUP's
4–36V range. Crank sag lasts hundreds of milliseconds and is not bridgeable at
any value that fits this board. Transient energy is D1's job, not C2's. So **rev
2.0 should change the documented value to match the footprint, not the footprint
to match the value**: 6.3mm is the right size for a rail carrying tens of mA, and
§13.3.6 notes area gets tight once an MCU arrives.

**Both capacitors on this net take their voltage rating from D1's clamp, not from
the rail — record it as a rule.** `+12V` sits at 12–14.4V in normal operation,
which invites a 16V or 25V part and is the number printed on the schematic. The
figure that actually governs is the SMBJ24A's **38.9V maximum clamping voltage**
(confirmed 2026-08-22 against `datasheets/TVS-Diode-SMBJ-Datasheet.pdf`: V_WM
24.0V, V_BR 26.70–29.50V at 1 mA, **V_C 38.9V at I_PP 15.5A**),
so **50V is the floor for anything on `+12V`** — C2, C3, and whatever rev 2.0 adds
to this net. Stated as a constraint on the net rather than as two part choices,
because the misleading number is the one a reader sees first.

**D4 removed 2026-08-23, once Q1 landed.** Its only stated job above was series
reverse-blocking, and Q1 does that for the whole board rather than the HAT leg
alone — so D4 was redundant, not merely improved upon. It was briefly kept as the
§13.2.5 fallback (bridge Q1, D4 restores rev 1.0), but that argument does not
survive contact with the failure it would cover. **The realistic Q1 defect is a
reversed FET**, which runs normally in both directions until reverse polarity
arrives — and there D4 would protect the HAT leg while the X1202 and Pi take the
hit anyway. It covers the cheap half of the failure and not the expensive half.
The real mitigation is the bench-supply orientation check recorded on Q1's `Note`
field, which costs nothing. Removing it also returns ~25mm² of SMB footprint on a
board where §13.3.6 calls area the binding constraint, drops a BOM line, and
merges what would have been a `+12V_PROT` net back into `+12V`.

*Kept in mind and deliberately not treated as a reason to retain it: D4 was a 40V
part on a rail D1 clamps at 38.9V. It is never reverse-stressed in the new
topology, so this was never a defect — but 1.1V of margin was always tighter than
it looked.*

**D1 stays, and matters more than it did.** Q1 and D1 solve orthogonal problems:
Q1 blocks reverse polarity, D1 clamps overvoltage, and a P-FET in reverse
protection is fully *on* during a positive transient — it conducts a load dump
straight through at 15 mΩ. Confirmed against the TLIN1027 datasheet 2026-08-23:
**VSUP absolute maximum is −0.3 to +45V**, against D1's 38.9V clamp — about 6V of
margin, a correctly matched pair. The transceiver alone might survive without it
(it is rated for ISO 7637-2 Pulse 1 at −100V and Pulse 2 at +75V on VSUP), but C2
and C3 are 50V parts *because* D1 clamps at 38.9V, and the X1202 now sits on the
same net.

~~**The protection chain now guards only the HAT.**~~ **True of rev 1.0, and
reversed by rev 2.0.** As of 2026-08-10 the pass-through tapped the raw connector
node, so `+12V_RAW` fanned out to J3 and to D4's anode in parallel, and
everything downstream of D4 saw only the HAT's tens-of-mA load. **Rev 2.0 puts
J3 downstream of Q1 and D1 instead, so the protection chain guards the whole
board** — which is the point of the change, and what §3.7.4 could not achieve
with a Schottky without paying a 0.5V drop and ~1.5W on the Pi's supply.

**One consequence to carry forward: Q1 now carries the Pi's current, and D4 did
not.** The ~150 mA whole-board figure quoted elsewhere is almost entirely 3.3V
draw and still does not appear on this rail, but the J3 pass-through does — up to
the **3A** budget §3.7.4 sizes the copper for. That is why Q1's R_DS(on) is
selection-critical where D4's forward drop was not: at 15 mΩ it dissipates
0.11W at 3A, where the SS54 in the same position would have dissipated ~1.5W.
See §3.7.4 for the history.

**There is no PTC fuse.** Earlier revisions listed one in the protection chain;
the as-built board has none. §8.2 decided the vehicle-side harness fuse was the
whole story; §3.7.4 then observed that nothing limits fault current on the leg
feeding the Pi. Those two sat uneasily together and §13.2 queued the decision for
re-taking.

**Re-taken 2026-08-22 against real part data, and it splits by leg.** The
governing constraint is the one already stated above for C2/C3: voltage rating on
`+12V` is set by D1's 38.9V clamp, not by the 12–14.4V rail. Applied to a PPTC,
that turns out to be disqualifying. From the Littelfuse 2920L table — the
highest-current surface-mount PPTC family — hold current and voltage rating sit
at opposite ends of the same series:

| Part | I_hold @25°C | V_max | @60°C | @85°C |
|---|---|---|---|---|
| 2920L110/60 | 1.10A | **60V** | 0.73A | 0.50A |
| 2920L150/60 | 1.50A | **60V** | 1.02A | 0.70A |
| 2920L260/33 | 2.60A | 33V | 1.81A | 1.27A |
| 2920L300/15 | 3.00A | 15V | 2.31A | 1.83A |
| 2920L400/15 | 4.00A | 15V | 2.80A | 2.10A |

A PTC on the **Pi leg** would have to pass the ~2.3A worst-case draw at cabin
temperature *and* survive 38.9V. Every part rated above the clamp delivers
**0.70A at 85°C**, five times short; the part that passes 2.10A at 85°C is rated
**15V**, under half the clamp and below even a 16V alternator fault. **No part in
the family satisfies both**, which is the same defect shape as C2 — two
individually sensible numbers that do not coexist in a purchasable part. The
radial 30R series reaches 4A but only at 30V, so it fails the same test and is
physically large besides.

**So §8.2 was right about the Pi leg**, and now for a reason rather than by
default: the harness fuse is the whole story there because a *resettable* device
that can do the job does not exist at this voltage. If on-board limiting is ever
wanted on that leg, the right part is an eFuse with a programmable limit or a
plain blade fuse — not a PPTC.

**On the HAT leg a PTC would have worked** — the HAT's own 12V load is tens of
mA, so a ~0.5A/60V 1812L part would carry it with ~10× margin, no nuisance-trip
risk, and full rating against D1's clamp. **Operator decision 2026-08-23: not
fitted.** Rev 2.0 carries no fuse anywhere, which is rev 1.0's position retained
deliberately rather than by omission.

**What that accepts, stated so it is a position and not an oversight.** A short
on the HAT's 12V node — a cracked C3, a failed C2, a TLIN1027 damaged by an
ISO 7637 pulse on the K-line — is limited only by the vehicle's 10–20A harness
fuse. The path is 0.6mm copper (roughly 2A) through D4, a 5A Schottky. D4 is the
weak link and would fail first; diodes commonly fail **short**, which then passes
the fault to the trace, leaving the board as the current-limiting element. The
outcome is a destroyed board rather than a hazard — FR4 chars rather than burns,
and the harness fuse still bounds the energy — but it is worth knowing that
nothing on the board bounds it first.

**Why it was still the right call.** The branch draws tens of mA and the fault
modes above have never occurred; the part costs area on a board where §13.3.6
already notes area is the binding constraint; and the failure is recoverable on a
single-operator research rig. If a TLIN1027 or a 12V-rail capacitor ever does
fail short, refit is one 1812 footprint away — and that is the trigger to revisit
this, not a general worry about fusing.

#### 3.7.2 Rails

| Rail | Source | Feeds |
|---|---|---|
| `+12V_RAW` | J2 pin 1, straight off the vehicle | **Q1's drain, and nothing else.** As of rev 2.0 the pass-through no longer taps here — J3 moved downstream of Q1 and D1 |
| `+12V` | `+12V_RAW` via **Q1**, clamped/bulked by D1/C2 | TLIN1027 VSUP (decoupled by C3), the K-line commander pull-up R5+D5, **and J3's pass-through to the X1202** |
| `+3V3` | **Raspberry Pi header pins 1 and 17** | Everything else: MCP2518FD VDD, TCAN1042 VIO, BMP384, LSM6DSOX, MAX-M10M VCC + V_IO, EEPROM, all pull-ups and straps |
| `+5V` | Raspberry Pi header pins 2 and 4 | TCAN1042VDRQ1 VDD only (~70 mA) |

The HAT does **not** generate 5V and does **not** power the Pi. One 3.3V domain,
sourced from the host — which is what §3.7.5 argued for.

#### 3.7.3 System topology

```
                              R10 100k ──> GND ;  D3 15V Zener ── Q1 gate   (rev 2.0 Vgs clamp)
                                              │
Vehicle OBD 12V ──> J2 (6-way block) ──> +12V_RAW ──> Q1 (D→S) ──> +12V ──┬──> D1 TVS / C2 / C3
                                                                          ├──> TLIN1027-Q1 VSUP
                                                                          ├──> R5+D5 → K-line pull-up
                                                                          │
                                                                          └──> J3 (2-way block)
                                                                                   │
                                                                         barrel-jack pigtail
                                                                                   │
                                                                                   v
                                                                          X1202 UPS HAT ──> Pi 5 (5V)
                                                                                   │
                                                                           (battery backup + GPIO6 PLD +
                                                                            I2C fuel gauge at 0x36)

Raspberry Pi 5 header ──> +3V3 (pins 1,17) ──> every logic device on this board
                      └─> +5V  (pins 2,4)  ──> TCAN1042 VDD
```

**Two things changed here in rev 2.0**, and together they close §3.7.4's accepted
exposure. D4 (SS54) is gone, with Q1 doing the reverse blocking for the whole
board; and **J3 moved from `+12V_RAW` to `+12V`**, so the pass-through now sits
behind both the reverse protection and the TVS. The Pi's supply was previously
guarded by nothing on this board — both its reverse-polarity and its transient
case are now covered. And it costs no headroom: Q1 drops 38 mV at the 3A budget
where the SS54 dropped ~500 mV, so the cranking-sag argument that put the tap
upstream in the first place is satisfied rather than traded away.

The X1202 remains the sole power source for the Pi and the sole owner of
power-loss detection and battery-backed shutdown (`scripts/ups_shutdown_watch.py`,
`systemd/ups-shutdown.service`).

#### 3.7.4 The pass-through is an unprotected tap (changed 2026-08-10)

The 12V pass-through leg (J3) branches off `+12V_RAW` **upstream of D4** — it is
a parallel tap on the connector node, not a load on the protected rail. This is
the alternative earlier revisions named, and taking it resolves the thermal and
conductor problems it was weighed against:

- **D4 no longer carries the Pi.** It passes only the HAT's 12V load — TLIN1027
  VSUP plus the commander pull-up, tens of mA — so the SS54 dissipates milliwatts
  instead of ~1.5W, and the earlier "confirm the pad can sink 1.5W in a hot
  cabin" worry is void. The part is now generously oversized, which is fine.
- **No diode drop in the Pi's supply.** The X1202 sees the connector voltage
  directly, so cranking sag is not made worse by a series Schottky.
- **The copper was widened to match, and the widening is complete.** A Pi 5 at
  full tilt is ~25W, so budget on the order of **3A at 12V** worst case. The
  whole J2 → J3 path is **1.3mm on B.Cu, 37.3mm end to end, with no via in it**
  (both terminal blocks are through-hole, so their pads span all layers). 1.3mm
  of 1oz external copper is roughly a 2.9A-at-10°C-rise / 3.9A-at-20°C-rise
  conductor, which covers the budget with margin.

  The narrower copper on these nets is **not** in that path: the branch from the
  `+12V_RAW` node up to D4's anode (0.6mm, plus a ~1mm 0.4mm stub and one
  0.9/0.5mm via) and everything on `+12V` past D4 (0.6mm) carry only the HAT's
  own load — TLIN1027 VSUP and the K-line pull-ups, tens of mA. 0.4mm is already
  ~1.6A of headroom there.

**What it costs: nothing on this board protects the X1202 leg.** Reverse polarity
at the terminal block is the clearest case — D4 protects the HAT, and J3 passes
the reversed voltage straight through to the UPS. Transients are subtler: a
positive surge on `+12V_RAW` is clamped only indirectly, because D1 sits behind
D4 and any clamping current has to flow through the Schottky. Two things to
settle before this is fabricated — both since decided, see §8.2:

1. **Confirm the X1202 tolerates reverse polarity and automotive transients on
   its barrel jack**, or accept that a miswired terminal block kills the UPS and
   possibly the Pi.
2. **If it does not, move the protection rather than the tap** — a TVS and a
   series element (ideal-diode controller, or a Schottky sized for 3A) on
   `+12V_RAW` itself, ahead of the fan-out to both D4 and J3.

**There is still no fuse anywhere on the board**, so nothing on the HAT limits
fault current on a leg that now feeds the whole system directly. **Resolved
2026-08-22/23 (§3.7.1): it stays that way, on both legs.** On the Pi leg by
necessity — no PPTC exists that both passes the load and survives D1's clamp. On
the HAT leg by choice: a ~0.5A/60V part would have worked, and the operator
decided the area was worth more than protection for a branch drawing tens of mA.

#### 3.7.5 Why there is one 3.3V rail (retained rationale)

The reasoning that killed the buck, kept because it governs any future attempt to
re-add a local rail.

**Current was never the problem.** Itemised against datasheet figures, the whole
board is ~35 mA typical and ~90 mA peak on 3.3V (MCP2518FD ≤20 mA; TCAN1042 VIO a
few mA — its ~70 mA VDD sits on 5V; MAX-M10M ~8 mA tracking plus the active-
antenna LNA bias at 5–20 mA, easily the largest single item; LSM6DSOX 0.55 mA;
BMP384 0.7 mA peak; the 4.7 kΩ K-line RXD pull-up 0.7 mA when low; EEPROM µA).
Against that, **there is no official Pi 3V3 limit to be near** — the Raspberry Pi
HAT+ specification (RP-008281-DS) sets no maximum current for a HAT+ on the 3.3V
rail, and the widely-cited 500 mA is an unofficial rule of thumb dating to the
B+, not a spec. Even taken at face value, this board's peak is under a fifth of
it.

**A second rail was an active liability.** The HAT sits between a vehicle 12V
feed and a UPS-backed host, so the two supplies can each outlive the other. With
split rails, both directions are a partial-power-down case: a device is powered
while the domain driving its I/O is not, and current flows through the parts' ESD
clamps into the dead rail — which can also hang a shared I2C or SPI bus.

| State | Vehicle +12V | Pi 5V | Pi 3V3 | With one rail (as built) |
|---|---|---|---|---|
| Normal run | up | up | up | fine |
| **UPS battery run** (key off, X1202 carrying the Pi) | **down** | up | up | fine — only the K-line transceiver's VSUP drops, and its logic pins stay referenced to a live host |
| **STANDBY** (`sudo halt`, vehicle still connected) | **up** | up | **down** | HAT logic dies with the host, as HAT+ requires |

The STANDBY row is a HAT+ conformance requirement: *"HAT+ boards must be
electrically compatible with the STANDBY power state, where the 5V power rail is
powered, but the 3.3V rail is not powered,"* and *"HAT+ boards must not assume
any particular sequencing timing between the 5V and 3.3V rails."* The as-built
single-rail arrangement satisfies this by construction.

**Two residual cross-domain paths survive and must be checked:**

1. The TCAN1042's VDD stays on 5V while its VIO drops in STANDBY — that is
   exactly what its VIO undervoltage protection exists for, so confirm the
   behaviour in the datasheet rather than assuming it.
2. The TLIN1027's VSUP stays on vehicle 12V while the host 3V3 domain is down. Its
   EN and TXD then face an unpowered host — and R4 as drawn actively pulls EN up
   toward 12V in that state — which is why R4 was changed to a pull-down (§3.3.2).

#### 3.7.6 Consequences of having no power sense on this board

The CANary-HAT has no ignition sense, no 12V voltage sense, and no power-loss
detect of its own. Accepted consequence: **the rig is unplugged when not in use,
or** the daemon infers engine-off from bus activity and commands a
shutdown/suspend. The second path is software work that does not exist yet —
`vehicle_state` already classifies `engine_off` / `no_data` (see `AGENTS.md`), and
`RecordArmingConfig.disarm_after_seconds` already detects the quiet period, but
nothing currently escalates that to a system shutdown.

### 3.8 HAT ID EEPROM — U1

| Parameter | Value |
|---|---|
| Part number | **CAT24C256WI-GT3 (onsemi)** — 256 kbit, MPN pinned on the symbol |
| Function | Raspberry Pi HAT ID EEPROM |
| Interface | I2C, address **0x50** (A0/A1/A2 all left unconnected → internal pull-downs → 000) |
| GPIO connection | ID_SD (header pin 27) / ID_SC (header pin 28), isolated from I2C1 |
| Package | SOIC-8 |
| Supply | **+3V3 (Pi rail)**, decoupled by C1 (100n) |
| Pull-ups | R1, R2 = **3.9 kΩ** to +3V3 on ID_SDA / ID_SCL ✅ per HAT+ |
| Write protect | WP (pin 7) → **JP1, a normally-open solder jumper to +3V3** |

**Density changed: 24C32 → 24C256.** Both are 16-bit addressable, so the HAT+
rule that disqualified the 24C02 is satisfied either way; the larger part simply
leaves room for a device-tree overlay blob alongside the ID record.

#### 3.8.1 HAT+ conformance rules (RP-008281-DS)

- **16-bit addressing is mandatory** — *"Use 16-bit addressable EEPROM. Do not use
  EEPROMs with 8-bit addressing."* The CAT24C256 complies. The spec notes these
  restrictions "exclude many smaller I2C EEPROMs," so check the datasheet of
  whatever part is actually sourced.
- **WP guidance is a recommendation, not a requirement** — *"connect the EEPROM
  write protect pin to a test point on the board pulled-up to 3.3V with a 1KΩ
  resistor."* The board instead fits **JP1, an open solder jumper to +3V3**: the
  default state is *writable* (WP low via the part's internal pull-down), and
  bridging JP1 hard-ties WP high to protect the device. That is a defensible
  swap — it costs less and is unambiguous — but note the difference: a soldered
  jumper is not a probe-drivable test point, so a production programmer cannot
  temporarily unprotect a protected board.
- Also required: a 3.3V part (✅), a WP pin covering the **entire** memory
  (verify), **no I2C clock stretching** (verify), and no paged EEPROM where the
  low address bits select the page.
- Only an EEPROM and its 3.9 kΩ pull-ups may connect to ID_SD/ID_SC — nothing
  else, ever. ✅ as built.

#### 3.8.2 Floating-low WP is correct — but only for onsemi CAT24Cxx

With JP1 open, WP relies on the part's internal pull-down. From the onsemi
datasheet:

> **WP:** When pulled HIGH, the Write Protect input pin inhibits all write
> operations. **When left floating, this pin is pulled LOW internally.**

> *Note 5:* When not driven, the WP, A0, A1 and A2 pins are pulled down to GND
> internally. For improved noise immunity, the internal pull-down is relatively
> strong; therefore the external driver must be able to supply the pull-down
> current when attempting to drive the input HIGH.

The same note is why JP1 is a **hard short** rather than a resistor: `IWP` is
**120 µA at VCC = 3.3V**, so a weak pull-up would lose to the internal pull-down.
The same logic applies to A0/A1/A2, which are also left floating here to select
address 0x50 — defined behaviour on this part, undefined on many others.

**This is a BOM constraint, not a schematic one**, and it now covers *four* pins.
Microchip's 24AA256/24LC256, the natural second source, says the opposite:

> **2.4 Write-Protect (WP).** This pin must be connected to either VSS or VCC.

A drop-in substitution to that part turns four floating inputs from "defined low"
into out-of-spec floating CMOS inputs — a corruption and mis-addressing risk that
no schematic review would catch, because the schematic never changed. The MPN is
pinned on the symbol for exactly this reason; keep it pinned on the BOM line,
with the reason attached.

**Stacking.** HAT+ explicitly supports stacking: permitted EEPROM addresses take
the form `101_00XY`, where the `XY` suffix encodes the HAT class. Confirm the
X1202 presents no ID EEPROM (it predates HAT+ and almost certainly does not), and
keep 0x50 for this board.

**The EEPROM can carry the board's device-tree overlay name**, which firmware
loads at boot — the cleanest way to bring up `mcp251xfd`, the GNSS UART, and
`pps-gpio`. See §11.

**Not a Power HAT+.** This board sources no 5V to the Pi, so the Power HAT+ rules
(`hat_current_supply`, the MODE1 address that makes firmware set
`usb_max_current_enable=1`) do not apply.

### 3.9 Terminal Blocks and Connectors

| Ref | Part | Footprint | Function |
|---|---|---|---|
| J2 | 6-position screw terminal, 5.08mm | `TerminalBlock_Phoenix_MKDS-1,5-6-5.08_1x06_P5.08mm_Horizontal` | Vehicle side |
| J3 | 2-position screw terminal, 5.08mm | `TerminalBlock_Phoenix_MKDS-1,5-2-5.08_1x02_P5.08mm_Horizontal` | +12V pass-through to the X1202 |
| J1 | 2×20 2.54mm socket, **on B.Cu** | `PinSocket_2x20_P2.54mm_Vertical` | Pi 5 header |
| J4 | SMA edge-mount | `SMA_Molex_73251-1153_EdgeMount_Horizontal` | GNSS active antenna |
| BT1 | JST-SH 2-pin, 1.0mm | `JST_SH_SM02B-SRSS-TB_1x02-1MP_P1.00mm_Horizontal` | GNSS backup cell |
| JP1 | Solder jumper, normally open | `SolderJumper-2_P1.3mm_Open_Pad1.0x1.5mm` | EEPROM write protect |
| MH1–MH4 | Mounting holes | — | 58 × 49mm HAT pattern |

The footprint family is **Phoenix MKDS**, not the MC/MCV series earlier revisions
named. Both are 5.08mm pitch; the BOM must say which.

**J2 pin map, as wired:**

| J2 pin | Net | OBD-II pin |
|---|---|---|
| 1 | `+12V_RAW` | 16 (battery +12V) |
| 2 | GND | 5 (signal ground) |
| 3 | GND | 4 (chassis ground) |
| 4 | `KLINE_RAW` | 7 (K-line) |
| 5 | `CANH_RAW` | 6 (CAN High) |
| 6 | `CANL_RAW` | 14 (CAN Low) |

Note J2's *value* field still reads `Screw_Terminal_01x05` while the symbol and
footprint are 6-position — cosmetic, but it will propagate into a BOM.

**Signal ground and chassis ground are tied together on the board** — pins 2 and
3 are the same GND net, so the HAT is the bonding point between the two returns.
That is fine on a vehicle where they are already bonded at the battery. The
layout consequence: bring the 12V return in on one pin and reference the bus
receivers to the other, joining them at a **single star point** near the
connector rather than letting them merge across the whole Gnd.Cu pour. Otherwise
the HAT's power-return current — including the pass-through leg feeding the X1202
and therefore the entire Pi — shares copper with the CAN and K-line voltage
reference. **With a board-wide solid GND pour this is a placement/stitching
question, and it has not been explicitly resolved** (§8.3).

The block has no spare way for an ISO 9141 **L-line**. The dev vehicle does not
need one (Nissan CONSULT inits with a byte sequence, not an L-line pulse), but
some pre-CAN vehicles do.

