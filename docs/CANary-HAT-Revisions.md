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
  escape. §13.4 has the M2's implementation: **Q1 `IRFTS9342` P-FET gate-driven
  through Q2 `MMBT3906W`, R2/R3 = 1.00 kΩ, R4 = 10.0 kΩ.** See §13.2.5 — this is
  the one item on the list that ships untested.
- **A PTC on the input.** §3.7.1 decided the harness fuse was the whole story;
  §3.7.4 then observed that "nothing on the HAT limits fault current on a leg
  that now feeds the whole system directly." Those two sit uneasily together and
  the decision deserves re-taking rather than inheriting. §13.4. Do it *with* the
  P-FET — same input stage, one change, and rev 3.0 redesigns that stage again
  for the always-on rail and load switch (§13.3.2, §13.3.6).
- **DNI split-termination footprints on CAN, gated by a solder bridge.** §3.2 is
  right that a third terminator on a vehicle bus is a fault — but it then has to
  concede that bench use "requires an external terminator in the harness." An
  unpopulated 2 × 60.4Ω + centre-cap footprint with a solder bridge removes that
  caveat at zero cost and zero risk. Optionally the common-mode choke footprint
  too, bypassed by 0Ω jumpers as the M2 does it. §13.4.
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
| **MAX-M10M → MAX-M10S** | Meas. 4 — GNSS TTFF and C/N₀ under the Pi's WiFi hotspot | **Open — needs a call before submission** | The one blind item that is *not* free. The M10S's integrated SAW+LNA is §8.4's "robust option" against a real desense risk at 1.575 GHz, and rev 1.0 relies entirely on the active antenna's filtering. It is not a drop-in, and rev 2.0 forces the decision early with no data. |

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
- **LSM6DSOX INT2 (pin 9) → a free Pi GPIO.** Unconnected on rev 1.0 and called
  out as free in §13.3.6 item 10. One net, no parts, and it is what motion-wake
  and parked monitoring (§13.3.3d) need to exist at all.
- **0.01Ω current-sense shunt and its monitor footprints, unpopulated.** §13.4.4
  already anticipated exactly this case: "If rev 2.0 is fabricated first, add the
  shunt and its footprints then and populate the monitor later." The M2's chain
  is `R18` 0.01Ω 1% 1/4W 0805 → `ZXCT1109` high-side monitor → `LM393`
  comparator → an `OVER_CURRENT` flag and a P-FET cutoff. Land the shunt and the
  footprints; populating the monitor stays rev 3.0.
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
12). That window holds the data `docs/roadmap/data-collection-plan.md` ranks
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
`docs/roadmap/eval-longitudinal-and-dtc.md` Phase 4 exists to detect.

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

- The M10's 10 Hz nav rate is volatile configuration (§3.6). The MCU re-applies
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
| 2 | 12V divider → MCU ADC | crank/alternator/parasitic (§13.3.3b); K-line undervoltage disambiguation. **Divider lands in rev 2.0** behind a small I2C ADC (§13.2.3) — rev 3.0 re-points it at the MCU for kHz sampling |
| 3 | MCU GPIO → high-side load switch on the J3 leg | §13.3.1 sleep/wake |
| 4 | MCU ↔ Pi UART pair | handshake; works whenever the MCU is up |
| 5 | MCU ↔ Pi on **SPI0 CE1 (GPIO7, pin 26 — free, §4.1)** | bulk buffer dump; a few hundred KB over UART is painful |
| 6 | Pi 3V3 rail sense → MCU | the tri-state invariant in §13.3.2 |
| 7 | TCAN1042 RXD → MCU (fan-out) | boot-gap capture, wake, timestamps |
| 8 | K-line RXD / TXD / EN → MCU | §13.3.4; supersedes the Pi's `uart0` connection |
| 9 | GNSS TX + TIMEPULSE → MCU (fan-out) | time base, hot fix, absolute time at wake |
| 10 | **LSM6DSOX INT2 (pin 9 — unconnected on rev 1.0)** → MCU | motion wake. **Landed in rev 2.0** as a net to a free Pi GPIO (§13.2.3); rev 3.0 re-points it |
| 11 | Private MCU I2C → LSM6DSOX + BMP384 | §13.3.4 — **must not be I2C1** |
| 12 | GNSS antenna bias sense → MCU ADC | open/short antenna detection |
| 13 | STB (U4) and the §13.2 pull-up FET gate → MCU | safe-state ownership. **Both exist in rev 2.0** as Pi GPIOs (§13.2.1, §13.2.4); rev 3.0 moves ownership to the MCU |
| 14 | **A flash path from the Pi** — SWD bit-banged from Pi GPIO, or a UART bootloader with BOOT0 on a Pi-drivable pin | see below |
| 15 | Status LEDs → MCU | §13.2, §13.3.4 |

**Rev 2.0 already lands items 2, 10 and 13** (§13.2.3), so this list is three
nets shorter than it was — the point of pulling them forward. The remaining
twelve are what still make rev 3.0 a board revision rather than an addition.

**Item 14 is a hard requirement, not a convenience.** If updating firmware means
pulling the board out from under the dash, firmware stops being updated, and
every capability above freezes at whatever shipped.

**Items 4, 5 and 6 are the crossing nets** that §13.3.2 governs — size their
series resistance there, not here.

Header budget: §4.1 lists GPIO27, 22, 23, 24, 7, 12, 13, 19, 20 and 21
unconnected, so items 3–6 fit without disturbing anything, and `uart4`
(GPIO12/13) is the natural home for item 4. Note that item 8 *frees* `uart0`
(GPIO14/15) on the Pi side — a net simplification of §11's K-line software work
rather than an addition to it.

Physical: 65 × 56mm already carries 48 footprints. An MCU, an always-on buck,
crossing-net components and a programming header are not free there. Swapping
Y1's HC49/US can (**11.4 × 4.7 × 3.3mm**, already flagged in §3.1.1 as
"physically large" against a 3.2 × 2.5mm SMD equivalent) is worth doing on its
own merits and helps here — **done in rev 2.0** (§13.2.3), so rev 3.0 inherits
the area rather than having to spend a re-spin on it.

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
- **1% as the default resistor tolerance.** The M2 uses 1% throughout and reaches
  for E96 values wherever the value matters (60.4, 240, 374, 866, 2.00K, 6.49K,
  8.06K, 8.87K, 22.1K, 124K), reserving 5% for 0Ω jumpers. Rev 1.0's pull-ups and
  dividers do not need 1%, but there is no reason to specify looser — and the
  12V sense divider (§13.2) genuinely does want it.
- **Test points and solder bridges everywhere.** Already wanted by §8.4 and
  queued in §13.2; the M2 simply confirms the habit is worth the area.

#### 13.4.4 Scope: rev 2.0 or rev 3.0

Taken as decisions rather than left open, since none of them blocks on
measurement:

| Item | Revision | Why |
|---|---|---|
| C0G/NP0 on C5/C6 and C4 | **rev 2.0** | BOM line only; no layout impact |
| P-FET reverse protection | **rev 2.0** | Independent of the MCU, and it closes §3.7.4's accepted exposure |
| PTC on the input | **rev 2.0** | Same input stage as the P-FET — do them together, once |
| DNI split termination + solder bridge | **rev 2.0** | Footprints only |
| 12V sense divider values | **rev 2.0** | Already queued there — but the divider had no destination until §13.2.3 added a small I2C ADC behind it. The M2's values transfer; the readback did not exist on rev 1.0 |
| Current sense + over-current trip | **split — shunt in rev 2.0, monitor in rev 3.0** | Resolved by this table's own escape clause. The shunt is nearly free, but continuous current data is only worth having when something is awake to read it — the supervisor MCU (§13.3.3b). Since rev 2.0 *is* now the board being fabricated, the conditional fired: land `R18` and the `ZXCT1109`/`LM393` footprints unpopulated (§13.2.3), populate in rev 3.0 |

The input-stage items are grouped deliberately: rev 3.0 redesigns that stage anyway
for the always-on rail and the load switch (§13.3.2, §13.3.6), so doing the P-FET
and the PTC as one change avoids touching it twice.

**Re-read 2026-08-22 against the withdrawal of rev 1.0.** Every row above still
lands where it did — this table was written as decisions that "none of them blocks
on measurement", which is exactly the property that made it survive rev 1.0 never
being built. The one row that moved, current sense, moved because it had already
written down what to do in this case.
