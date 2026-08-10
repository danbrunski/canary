# Skunkworks — bench ECU read/write research (`canary-rw`)

> **This is NOT roadmap work. NOT queue work. NOT overnight-eligible.**
> It lives under `docs/skunkworks/`, deliberately outside
> `docs/roadmap/`, so the `/overnight` orchestrator and
> `OVERNIGHT-QUEUE.md` never discover or action it. No task here is
> ever to be checked off by an unattended run. It is a for-fun,
> operator-driven research project pursued by hand, at the bench, on
> the operator's own schedule. "Challenging" or "not worth it" is not
> a reason to descope anything — the point is the doing.

Status: **exploratory / parked** (2026-07-20). Hand-run only.

## What this is

A learning/research effort to reproduce, from scratch, what a
commercial Nissan tuner (UpRev, EcuTek) does: read and write the
calibration flash of a Hitachi SH705x Nissan ECU. Scoped **entirely to
a bench ECU the operator buys** — never the vehicle, never the truck's
bus.

Motivation is curiosity and craft, not tuning throughput. For actually
tuning the truck, the UpRev Tuner license is the sane path and the
`engine-analysis-and-mapping.md` / `engine-tuning-support.md` roadmaps
are the real plan. This doc is the "but could we build the flasher
ourselves" rabbit hole, done properly and safely on isolated hardware.

## Hard boundaries (non-negotiable, this is what keeps it safe & legal)

- **Bench hardware only.** A separately purchased ECU on a bench
  harness. The write path never touches the vehicle or the vehicle's
  CAN bus. If it's plugged into the truck, this project is not running.
- **`canary-rw` is a SEPARATE tool from `canary`.** `canary` transmits
  freely — telemetry polling, session control, security access, memory
  reads — but NFR-06 still forbids it writing ECU non-volatile memory,
  because that is the one failure mode that bricks a module in the
  operator's own truck. Reflashing therefore lives in a distinct
  module/package with its own entry point and its own hardware target.
  No flash-write capability is ever linked into the `canary` daemon. The
  only shared surface is *offline binary analysis* (see Phase 3),
  which touches files, not buses.
- **Own hardware, own vehicle.** Legitimate as personal
  reverse-engineering on hardware the operator owns (US DMCA
  §1201 vehicle exemption covers the operator's own vehicle; bench
  RE for interoperability is the *Sega/Sony* tradition). **Distributing**
  any of it — recovered keys, extracted factory code, a flasher built
  from them — is a different and riskier legal question (anti-trafficking
  §1201(a)(2), copyright, Clean Air Act). This project stays
  personal/non-distributed unless/until an IP attorney is consulted.
  Keep emissions-related experimentation on the bench, not on a
  street-driven tune.

## The prior art (most of the hard part is already open source)

The brick-prone 80% exists as a mature open-source stack for exactly
the SH7055/SH7058 microcontrollers in the VQ40DE-era ECU:

- **nisprog + npkern** (`github.com/fenugrec/npkern`) — open-source
  reflashing kernel for SH7055/SH7058 Nissan/Infiniti ECUs; read/write
  flash over K-line; boot-mode recovery of a bricked ECU. This is the
  dangerous part, already solved.
- **RomRaider** + Nissan **definition files** — ROM map interpretation
  (which offset is which table/axis). Community defs bootstrap the map;
  author-your-own for an unlisted ROM ID.
- **FastECU** (`github.com/miikasyvanen/FastECU`) — newer OSS
  logging/flashing for OEM ECUs incl. Nissan.
- **Nissan ECU RE wiki** (`nissanecu.miraheze.org`) — boot mode + RE
  specifics.
- **Ghidra** — SH-2 disassembly for the firmware/security-access RE.

**Empirical confirmation that K-line is live on this ECU (2026-07-26).**
These tools all assume a working K-line on the VQ40DE-era ECM; that is
no longer an assumption. UpRev's datalogger was observed pulling 16.2 Hz
of live parameters while putting **zero frames on CAN** — a 76 s
`candump` with the daemon in passive mode caught 124 086 frames and not
one tester frame on `7E0`/`7DF`/`0x743`. So the K-line is present,
active, and carries manufacturer parameters that CAN does not expose
(MAF sensor voltage, throttle map traces, `A/F ADJ`). Full evidence in
`docs/data-architecture.md` §5.

Note the split, because it matters for npkern: **reflashing on this
vehicle runs over CAN**, not K-line — captured the same day, with the
firmware streaming as raw 8-byte frames on `0x743` and *no* standard UDS
download services. npkern's K-line path is therefore an alternative
route to the ECU, not a mirror of what the stock tooling does.

So the realistic shape is *assemble + extend existing tools*, and the
genuinely novel contribution is the **CANary bridge**: translating
CANary's behavior-analysis output (binned AFR-error / correction
surfaces from `engine-tuning-support.md` Phase 2) into concrete ROM
table edits, then flashing them — one closed loop from "what the engine
is doing" to "what the ROM says." Nobody has built that.

## Milestones (hand-run; no acceptance-gate ceremony, no auto-checkoff)

Each milestone is a "prove the next scary thing works" step. Do them in
order; each de-risks the next. These are for the operator to tick by
hand if desired — never by automation.

- [ ] **M0 — Acquire + power a bench ECU.** Correct part for the
  VQ40DE-era Hitachi unit (or any SH705x unit to learn on). Bench
  harness: 12V, ground, switched ignition, K-line (and CAN) breakout.
  Confirm it powers up and responds to a basic Consult/diagnostic
  hello. *No reading/writing yet — just a live bench target.*
- [ ] **M1 — Read the ROM (boot mode, no security needed).** Put the
  MCU in boot mode; dump the calibration flash with nisprog/npkern.
  Deliverable: a byte-exact ROM image on disk, re-dumpable
  identically. This is the whole foundation and touches nothing that
  can brick irrecoverably (boot mode is the recovery path).
- [ ] **M2 — Recover to a bricked state and back.** Deliberately write
  garbage / interrupt a write, confirm boot-mode recovery restores a
  known-good dump. Proves the brick-and-recover loop *before* trusting
  any real write. Do this early — it's the safety net for everything
  after.
- [ ] **M3 — Map the ROM.** Load the dump in RomRaider; find or author
  a definition for this exact ROM ID (Ghidra for the SH-2 disassembly
  where defs are missing). Identify at minimum: fuel/AFR-target table,
  MAF transfer curve, timing table, and their axes. Deliverable: a
  definition file that reads sensible values out of the dump.
- [ ] **M4 — Round-trip a no-op write.** Modify one benign cell, flash
  it, re-dump, diff — confirm the change landed exactly and only where
  intended, ECU still boots/responds. First real write; still bench,
  still recoverable.
- [ ] **M5 — Recover the security-access algorithm (optional).** For
  normal-mode (non-boot) diagnostic read/write, extract the seed→key
  routine from the firmware via static analysis; validate against the
  bench ECU dynamically. Skippable if boot-mode flashing suffices for
  the research goal.
- [ ] **M6 — `canary-rw` skeleton.** A standalone tool/package
  (separate entry point, separate hardware target) that wraps the
  read/dump/write/verify flow against the bench ECU. Explicitly NOT
  linked into the `canary` daemon. Read-back-verify after every write;
  refuse to run against anything but the configured bench interface.
- [ ] **M7 — The CANary bridge (the actual novel bit).** Offline:
  take a correction surface from `engine-tuning-support.md` Phase 2,
  map its RPM×load / MAF-voltage cells onto the ROM tables from M3,
  emit a proposed modified ROM. Human reviews the diff; `canary-rw`
  flashes the bench ECU; re-dump confirms. Closed loop, entirely on
  the bench.

## Explicitly out of scope

- Anything on the vehicle's bus or a driven tune (that's UpRev + the
  read-only roadmaps).
- Distribution of tools, keys, or extracted factory code.
- Linking write capability into the `canary` monitoring daemon.
- Any overnight/automated execution of the above.

## Pointers

- `engine-tuning-support.md` — the read-side companion (behavior
  analysis → correction surfaces) that M7 consumes. That doc IS
  roadmap/queue-eligible; this one is not. Keep the entanglement
  one-directional: skunkworks may read roadmap outputs; roadmap never
  depends on skunkworks.
- `AGENTS.md` / NFR-06 — no ECU non-volatile writes from `canary`. The
  constraint `canary-rw` must never violate by leaking into `canary`.
  (NFR-06 formerly said "read-only bus", which was over-broad: ordinary
  diagnostics transmit. Only the no-reflash half survives.)
