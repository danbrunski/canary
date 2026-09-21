# CAN reverse-engineering workflow

How we go from "a CAN ID and 8 mystery bytes" to a typed entry in
`data/bindings.yaml` with confidence and an evidence trail. Task 4.1. The
methodology was developed across `data/analyses/*.md`; this is the consolidated
version now that a full hypothesis loop has closed (`ECM_RPM`).

## The rig

The current baseline rig is the Canable on the OBD-II port. It handles
broadcast CAN capture plus active OBD Mode 01 and selected Mode 22 DID polling.
Add the NT510 when the session needs human-driven diagnostic stimulus.
The ELM327 path remains only as legacy fallback/emulator coverage.

- **Canable** (knockoff USB-CAN-FD, CANable2 **candleLight/gs_usb** firmware) —
  the primary interface. The Pi opens it via python-can's `socketcan` backend
  on the native `can0` interface at 500 kbps classic CAN for broadcast capture.
  `tools/can_obd_probe.py` also uses it as an ISO-TP OBD requester for
  Mode 01 / 03 / 09. The streaming Canable-native OBD collector
  (`record.py --obd-source canable`) interleaves Mode 01 and selected Mode 22
  DID polling while writing the same `pids.jsonl` sidecar the legacy ELM path
  used. (The adapter was originally flashed with slcan firmware; that is
  superseded — slcan exposes only `/dev/ttyACM0` and breaks the headless
  `can0.service` automation. See `docs/canable-firmware.md`.)
  In the default active-diagnostics mode the recorder transmits diagnostic
  requests and the adapter emits per-frame ACK bits, so this is not silent bus
  monitoring. That traffic is normal scan-tool operation. For true passive
  capture, gs_usb supports kernel `listen-only` (see `docs/can-setup.md`). See
  `can_collector.bus_factory`.
- **ELM327** (CH340 clone) — legacy automatable tester. It previously polled
  Mode 01 PIDs and Mode 22 DIDs via `record.py --obd-port`, logging ground truth
  to `pids.jsonl` for byte-vs-PID regression. The current adapter is dead; keep
  this path documented only for emulator/fallback context and do not plan new
  required captures around it.
- **Z1 Nissan scan tool / NT510** — the *human-driven* stimulus + extra
  reference. Live data for the many Nissan-Consult parameters generic OBD
  doesn't expose, and bidirectional actuator tests (cooling fan, IACV, EVAP,
  power balance, …) that force a sensor/actuator to a known state — a big clean
  signal in the sniff. **Never poll the same ECU with the ELM327 and the NT510
  at once** (ISO-TP collisions on 7E0/7E8) — stagger them, or keep the ELM327
  off the ECU while the NT510 is on it.

Capture CAN-only (`record.py --obd-source off`) when the NT510 is the active
player, so there's zero diagnostic-request contention. For non-NT510 sessions,
use `record.py --obd-source canable` so the session has both broadcast frames
and OBD ground-truth readings.

### Knock channel hunt — CLOSED 2026-08-01. Do not reopen.

**The ECM does not stream knock, by design.** This is settled from the FSM
(`xterra_fsm/ec.pdf`), not inferred from unsuccessful searches, so no further
sweep, sniff, or scan-tool work is justified:

- **EC-69**, the CONSULT-III application table, marks the knock sensor under
  *self-diagnostic results* **only — not DATA MONITOR**. Sensors that are
  streamable (MAF, ECT, A/F sensor) carry marks in both columns.
- `data/fsm/signals.json` agrees independently: **no knock item among the 333
  extracted DATA MONITOR parameters.**

The ECM exposes knock through exactly one interface: a **DTC** (P0327/P0328
bank 1, P0332/P0333 bank 2). Earlier negative results — UpRev's four constant
knock-labelled fields, the empty Z1 menus — were not sampling gaps; they were
correctly observing something that isn't there. **Do not budget DID sweeps,
broadcast RE, or capture sessions against finding a knock channel.**

Knock *protection* works regardless (EC-34: retard "is designed only for
emergencies… does not operate under normal driving conditions", with base timing
inside the anti-knock zone on 87 octane). Health is monitored by **polling DTCs**
— which set no MIL (EC-211), so a sensor failure here is otherwise silent.

## Capture

`record.py` writes a session directory: `frames.log` (candump format,
append-safe), `manifest.json` (vehicle, bus, counts), and — with the legacy
`--obd-port` path or the Canable-native streaming collector —
`pids.jsonl`. Tag events into `tags.jsonl` with `tag.py` or, hands-busy in the
vehicle, walk a protocol with `guided_session.py` (one Enter per event, `s` to
skip): `rev` (idle / throttle blips / sustain), `nt510` (module scan, ABS live
data + steering/brake actions, ECU/ABS actuator tests), `drive` (cold-start →
idle → city → highway → coast/DFCO → WOT → 4-Lo → A/C → engine-off).

**Tag physical actions on the vehicle, not NT510 screen selections.** The first
NT510 session (2026-05-11T034516) used screen-selection tags and produced noisy,
later-corrected bindings; the rev session repaired this. **Always know the
engine state** — a wrong engine-state assumption cascades into wrong bindings;
set `--vehicle-state` and tag the start/stop of each phase. (The Xterra is a
manual, so there's no TCM on the bus — skip TCM steps; and its power steering is
engine-driven, so steering tests need the engine running.)

For Phase 2/3 ML and Phase 4.E behavior surfaces, you need the *engine under
load* — on-road drives, not just no-load revs in neutral. See
`docs/roadmap/archive/data-collection-plan.md` for the capture menu and "how much is enough."

## Analysis pipeline

1. **Inventory** — `tools/inventory_broadcast.py <session>`: per-CAN-ID frame
   counts, periods, DLC, per-byte unique-value counts. Surfaces which IDs are
   signal-bearing vs. counter/padding, and which IDs are diagnostic chatter
   (7Ex pairs — e.g. an NT510 module scan parks all of them in one window).
2. **Byte-vs-tag differential** — `tools/broadcast_signals.py <session>`: for
   every `(can_id, byte)`, the per-tag-window mean/range/uniques, ranked by
   cross-window mean spread (Δ). High Δ + a sensible ordering with the physical
   action (RPM rises through the blips, brake byte spikes only during
   `brake_press`, etc.) = a real signal. Watch for "latch" bytes that flip once
   and don't revert (usually engine/ignition state, not the action you tagged).
3. **Cross-reference to pin scale/offset** —
   - `tools/cross_reference_broadcast.py <session>`: regress a broadcast byte's
     raw value against each Mode 01 PID; high r² names the PID and the
     slope/offset give the scaling. Probes default to `bindings.yaml`'s unpinned
     signals — "for each candidate, which PID, at what scale?" (How `ECM_RPM`
     went confirmed: 0x1F9:2-3 vs PID 0x0C, r²=1.000, RPM = raw÷8.)
   - `tools/cross_reference.py <session>`: the Mode 22 ↔ Mode 01 version — pair
     a captured DID's reads against PID readings by timestamp, linear-regress.
     Used to identify Nissan DIDs (e.g. injector PW = the DID tracking
     `RPM × (1 + STFT/100)`).
   - `tools/scan_constant_dids.py --port …`: sweep Mode 22 DID space over ≥2
     engine states; splits varying (candidate live signals) from constant
     (calibration). This scanner issues service 0x22 requests only. It currently uses the
     legacy ELM327 path; selected live DID polling is already available through
     `record.py --obd-source canable --obd-dids`.
4. **FSM cross-reference** — `tools/decode_isotp.py` uses `data/fsm/signals.json`
   (parsed from the Factory Service Manual CONSULT-III tables) to score
   captured DID raw ranges against named FSM parameters' expected ranges.
   **Caveat (2026-05-16): treat range-match output as weak hints only, not
   bindings.** Only ~9 % of FSM params (24 ECM) have parsed ranges, the
   scale/offset grid is permissive, and small-range bytes coincidentally
   fit narrow bands — a cross-ref of the 0x1100/0x1200 DID sweep mis-named
   *every* already-known DID at score ≥0.96 (RPM 0x1219 → "TRVL AFTER MIL",
   TPS 0x1210 → "INJ PULSE-B2"). Name DIDs from ground truth (NT510 /
   Mode 01 cross-reference), never from FSM range-fit alone. **Why the
   coverage is low (scoping a future fix, 2026-05-16):** it is *not* a
   `parse_ranges` regex gap — the 41 unranged ECM params have
   unparseable source text: cross-page references (`"See EC-79."` — the
   range is on another page the extractor doesn't follow), garbled
   pdfplumber column splits (`"| K | SULT-III value"`), or categorical
   specs (`"LEAN ←→ RICH"`, no numeric range). A useful fix is a
   dedicated rework — follow `See EC-NN` cross-refs + per-PDF table
   geometry tuning across the 8 FSM PDFs — not a regex tweak.
5. **Validate** — `tools/decode_bindings.py <session>`: decode every binding
   against every matching frame; report raw + decoded ranges + plausibility. It
   catches binding bugs (the COOLANT engine-off −40 °C sentinel was found this
   way). New decoded signals also flow into the ML pipeline as numeric features
   via `src/preprocessing/decode.py` (Task 4.D.1).

## Recording findings

Each finding goes in `data/bindings.yaml` as a `Signal` (strict Pydantic schema —
typos fail at load): `can_id`, `byte_start`, `byte_length`, `byte_order`,
`scale`/`offset` (`null` until pinned), `unit`, `confidence`, optional
`plausible_min`/`plausible_max`, and an `evidence` list (one entry per session ×
method × finding). Promote/demote/correct as data accumulates — keep superseded
readings in the evidence trail (see `ECM_THROTTLE_TRANSIENT`'s "earlier
screen-tagged session is treated as misattributed"). Confidence ladder:

| level | meaning |
|---|---|
| `confirmed` | scale/offset validated against ground truth (a Mode 01 PID, an FSM table) |
| `high` | directional behaviour matches across multiple observations |
| `medium` | direction matches, exact scaling unknown |
| `low` | suggestive but ambiguous |
| `hypothesized` | best-guess; could be wrong |

Then write up the session in `data/analyses/<timestamp>.md` and add a row to
`data/sessions.md`. `bindings.yaml` is hand-edited; tools consume it (decode,
DBC export later, the daemon decoder) but don't auto-rewrite it.

## Worked example: `ECM_RPM`

Rev protocol session 2026-05-13T005522 (engine running, ELM327 polling Mode 01,
Canable sniffing): `broadcast_signals.py` flagged 0x1F9 byte 2 rising
monotonically through the 2k/3k/4k blips and ~0 with the engine off →
`cross_reference_broadcast.py` regressed bytes 2-3 against PID 0x0C: r² = 1.000,
RPM = raw × 0.1251 − 4.8 → the clean form is `raw ÷ 8` (byte 2 alone ≈ RPM/32,
also r² ≈ 1.0) → `bindings.yaml`: `ECM_RPM`, 0x1F9:2-3 big-endian, scale 0.125,
offset 0, `confirmed`, plausible 0–7000 rpm → `decode_bindings.py` confirms it
decodes to 0–4597 rpm on the session. The earlier rev-session analysis had
mislabeled 0x23D:1-2 as the RPM candidate; the cross-reference ruled that out
(it tracks airflow, r² ≈ 0.8) — renamed `ECM_AIRFLOW_PROXY_23D`.

**That second label was also wrong, and the way it was wrong is the lesson**
(settled 2026-08-04 against UpRev K-line ground truth — see "Ground truth from
a second logger" below). 0x23D:1-2 is not one 16-bit field at all: byte 1 is
accelerator pedal position and byte 2 is throttle position, two independent
8-bit signals. Both earlier labels were fitted against quantities that sit
*downstream* of pedal position — first RPM, then airflow — so each produced a
believable r² without being the signal. **Correlation picks the wrong byte
whenever the true signal drives the one you regressed against.** The test that
settles it is scale stability across independent windows, not r².

## Ground truth from a second logger (UpRev K-line)

The OBD-request path is a poor cross-reference for broadcast RE: the scheduler
round-robins ~86 PIDs against an 8 req/s budget, so each PID lands roughly every
11 s and never at the same instant as the frame you are trying to explain. An
UpRev K-line datalog is the opposite — 17 channels, all sampled synchronously on
every row at ~24-41 ms. When both loggers ran at once, that is a labelled
dataset for the broadcast bus.

**Finding the overlaps.** Session directories are UTC-named; UpRev encodes local
time in its filename and stores `Time` as **milliseconds** since log start. Do
not trust session bounds — check for actual frames, because plugging in the
UpRev cable can take the bus away from the Pi. On 2026-07-29 the CAN side went
silent 11:12→11:42, exactly covering that log's window, leaving 0 s of usable
overlap out of a nominal 314 s. Verified simultaneous coverage across the whole
corpus is **171 s**, both windows on 2026-07-27.

**Aligning the clocks — free, and self-validating.** The two loggers have
independent clocks (offsets measured at -1.3 s and -1.6 s, so this is not
optional). Cross-correlate a signal already confirmed on both sides — RPM — over
a lag grid on a common 100 ms grid. Peak correlation gives the offset; the
r = 0.9998/0.9999 at that peak simultaneously proves the alignment, the RPM
binding, and that both loggers saw the same events. If this step does not reach
r > 0.99, stop: the windows are not really simultaneous.

**Scanning.** Enumerate every contiguous 1-byte and 2-byte (big- and
little-endian) field of every frame, drop zero-variance ones, and correlate each
against each UpRev channel. ~300 candidates × 16 targets is seconds of compute.

**Acceptance — three gates, in order.** r² alone will hand you the wrong byte:

1. **Out-of-sample sign and strength.** Two independent windows, |r| ≥ 0.85 in
   both, same sign. Kills window-specific coincidences.
2. **Partial correlation given RPM.** Almost everything on an engine bus is
   RPM-monotonic. `0x23D:W3le` correlates 0.9998 with RPM but has partial
   r ≈ 0.02 — an RPM duplicate, correctly rejected.
3. **Scale stability — the decisive one.** Fit scale/offset on window A, predict
   window B in engineering units, and independently refit on B. A real binding
   keeps its scale: ECM_RPM drifts **0.10 %**, the two 0x23D bytes 0.24 % and
   0.16 %. A merely-correlated byte does not: the best airflow candidate
   (`0x233:B2`, r = 0.98) drifts **-6.5 %**. Correlation says "these move
   together"; scale stability says "this byte *is* that quantity".

Disambiguate neighbouring bytes the same way. Bytes 1 and 2 of 0x23D correlate
with *each other* at r ≈ 0.95 (pedal drives throttle), but each predicts its own
target far better than its neighbour's — 0.998 vs 0.905, and 0.993 vs 0.919 —
so they are distinct signals, not duplicates.

**Known blind spots.** Slow or constant channels cannot be recovered from a
short window: coolant temp varies 1.3 °F over 84 s, so the *correct* known
binding `0x551:B0` scores only r = 0.72/0.81 and fails gate 1. That is the
method behaving properly, not a missing signal — validate those against a
cold-start session instead.

### Spark and AFR are not on the broadcast bus

Scanned against `IGN TIMING (BTDC)`, `AFR WB-B1/B2`, `A/F WB-B1 (V)` and
`TARGET AFR`, the best candidate anywhere reached |r| ≈ 0.33-0.47 — nothing,
against a background where real signals score 0.97+. This is not a
low-variance artifact: timing swings -1° to 40° (sd 8.7°) inside the windows.

Consistent with the FSM: these are DATA MONITOR items, served on request, not
broadcast. **So the tuning campaign cannot be moved onto the CAN bus** — spark
and mixture stay on UpRev K-line logs. What the broadcast bus can contribute is
the *axes* (pedal, throttle, RPM at ~100 Hz) to give those logs dense context.

Bounded claim, not proof of absence: the scan covers contiguous 1-2 byte fields
on frames with ≥500 samples in one session, over 171 s from a single day. A
bit-packed, non-adjacent, multiplexed, or non-linearly scaled encoding would
evade it.
