# CAN reverse-engineering workflow

How we go from "a CAN ID and 8 mystery bytes" to a typed entry in
`data/bindings.yaml` with confidence and an evidence trail. Task 4.1. The
methodology was developed across `data/analyses/*.md`; this is the consolidated
version now that a full hypothesis loop has closed (`ECM_RPM`).

## The rig

Three devices on the OBD-II port (via a splitter):

- **Canable** (knockoff USB-CAN-FD, CANable2 slcan firmware) — the passive
  sniffer. `record.py` opens it via python-can's `slcan` backend at 500 kbps
  classic CAN and only ever `recv()`s — it never injects a data frame. (It
  *does* emit per-frame ACK bits in normal-open mode; the firmware ignores the
  slcan `L` listen-only command, so true bus-monitoring isn't available — but
  ACK-only on a multi-node vehicle bus is benign and is exactly what every scan
  tool on the port already does. See `can_collector.bus_factory`.)
- **ELM327** (CH340 clone) — the *automatable* tester. python-obd polls Mode 01
  PIDs (and, later, Mode 22 DIDs) and `record.py --obd-port` logs them with
  `time.time()` timestamps to `pids.jsonl` — the ground truth the byte-vs-PID
  regression fits against. (CH340 over WSL-USB is flaky; run `usbipd attach`
  with `--auto-attach`. The OBD side of `record.py` is supervised and survives
  drops — see 1.E.1.)
- **NT510** (handheld Nissan scan tool) — the *human-driven* stimulus + extra
  reference. Live data for the many Nissan-Consult parameters generic OBD
  doesn't expose, and bidirectional actuator tests (cooling fan, IACV, EVAP,
  power balance, …) that force a sensor/actuator to a known state — a big clean
  signal in the sniff. **Never poll the same ECU with the ELM327 and the NT510
  at once** (ISO-TP collisions on 7E0/7E8) — stagger them, or keep the ELM327
  off the ECU while the NT510 is on it.

Capture CAN-only (`record.py` with no `--obd-port`) when the NT510 is the active
player, so there's zero contention.

## Capture

`record.py` writes a session directory: `frames.log` (candump format,
append-safe), `manifest.json` (vehicle, bus, counts), and — with `--obd-port` —
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
`docs/data-collection-plan.md` for the capture menu and "how much is enough."

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
     (calibration). Read-only — service 0x22 only.
4. **FSM cross-reference** — `tools/decode_isotp.py` uses `data/fsm/signals.json`
   (parsed from the Factory Service Manual CONSULT-III tables) to score
   captured DID raw ranges against named FSM parameters' expected ranges.
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
