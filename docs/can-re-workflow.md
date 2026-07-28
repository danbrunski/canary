# CAN reverse-engineering workflow

How we go from "a CAN ID and 8 mystery bytes" to a typed entry in
`data/bindings.yaml` with confidence and an evidence trail. Task 4.1. The
methodology was developed across `data/analyses/*.md`; this is the consolidated
version now that a full hypothesis loop has closed (`ECM_RPM`).

## The rig

The current baseline rig is the Canable on the OBD-II port. It handles
broadcast CAN capture plus read-only OBD Mode 01 and selected Mode 22 DID
polling. Add the NT510 when the session needs human-driven diagnostic stimulus.
The ELM327 path remains only as legacy fallback/emulator coverage.

- **Canable** (knockoff USB-CAN-FD, CANable2 **candleLight/gs_usb** firmware) —
  the primary interface. The Pi opens it via python-can's `socketcan` backend
  on the native `can0` interface at 500 kbps classic CAN for broadcast capture.
  `tools/can_obd_probe.py` also uses it as a read-only ISO-TP OBD requester for
  Mode 01 / 03 / 09. The streaming Canable-native OBD collector
  (`record.py --obd-source canable`) interleaves Mode 01 and selected Mode 22
  DID polling while writing the same `pids.jsonl` sidecar the legacy ELM path
  used. (The adapter was originally flashed with slcan firmware; that is
  superseded — slcan exposes only `/dev/ttyACM0` and breaks the headless
  `can0.service` automation. See `docs/canable-firmware.md`.)
  In the default active read-only OBD mode the recorder transmits diagnostic
  read requests and the adapter emits per-frame ACK bits, so this is not silent
  bus-monitoring — but ACK + read-only OBD on a multi-node vehicle bus is benign
  and is exactly what every scan tool on the port already does. For true passive
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

### Z1 knock-channel inventory — completed, negative

The current result is **no confirmed observable knock channel**, not proof that
the ECM exposes none. UpRev's four knock-labelled fields are constant on this
ROM, and Nissan's published CONSULT-III ECM Data Monitor list has no named
knock/retard item. Our successful session-`0xC0` Mode 22 inventory covered only
`0x1100`-`0x12FF`, however, and its stimuli stopped at a no-load 2,500-rpm hold.

On 2026-07-27 the operator inspected the Z1 Nissan scan tool's available data
streams and tests. It exposes no knock, detonation, knock-retard, octane-state,
or related correction item. Cross the Z1 menu/sniff experiment off as a knock
discovery path; without a displayed parameter it supplies no knock-specific
request to identify or replay.

The negative result does not make the broader DID space or unbound broadcast
bytes disappear. Investigate those independently if desired, but require a
repeatable response distinct from final ignition timing, RPM/load, IAT,
counters, and checksums before assigning a knock-related label. Correlation
with timing alone is insufficient. External knock instrumentation remains the
timing-development ground truth unless such a channel is validated.

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
`docs/roadmap/data-collection-plan.md` for the capture menu and "how much is enough."

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
     (calibration). Read-only — service 0x22 only. This currently uses the
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
(it tracks airflow, r² ≈ 0.8) — now `ECM_AIRFLOW_PROXY_23D`.
