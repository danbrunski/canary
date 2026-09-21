# W07 — flash interlocks

Roadmap task W11. **Depends on nothing, and must land before anything that can
program a module exists.** It is last in this set only because nothing else
here builds that capability.

AGENTS.md (operator decision 2026-09-21) sets two conditions on any write to
the ECU's non-volatile memory: explicit operator permission per operation, and
key on / engine off / vehicle stationary. This file turns both into code, so
the capability can never exist without them. A rule every caller must remember
is at the wrong layer.

**Scope:** the guard and its tests. **Not** in scope: any flash, erase,
download or transfer routine, any seed/key work, any ROM programming. This file
builds a gate with nothing behind it yet, on purpose.

## The guard

- [ ] **W07.1 — One choke point.** A single function,
  `authorize_nonvolatile_write(request) -> Authorization`, in its own module.
  Every future code path that writes non-volatile memory takes an
  `Authorization` as a required argument, so an unguarded write fails to
  type-check rather than failing review.
- [ ] **W07.2 — Vehicle-state condition, checked at the moment of the write.**
  Authorize only when `classify_vehicle_state` returns `ignition_only` from
  *fresh* signals. Refuse on `engine_running`, on `unknown`, on `no_data`, on
  `replay`, and on stale inputs — an unclassifiable state is a refusal, not a
  pass. The state that matters is the one when the first block is sent, so the
  check runs inside the write path, not only when a page loads.
- [ ] **W07.3 — Re-check during a multi-step operation.** Between blocks,
  re-read the signals that can change: RPM rising from zero, or vehicle speed
  non-zero, aborts before the next block. Record what it saw.
- [ ] **W07.4 — Permission is per operation and cannot be inferred.** An
  `Authorization` binds to one image by SHA-256 and one ECU, expires after a
  short TTL, and is single-use. It is minted only by an explicit operator
  action that names that hash. There is no "remember this", no bulk grant, and
  no path by which an agent's instruction to "tune", "fix" or "deploy" produces
  one.
- [ ] **W07.5 — Not reachable by agents on their own initiative.** The MCP
  server exposes no tool that mints an `Authorization`. Minting requires the
  dashboard confirmation flow (or an equivalent operator-present path). State
  this in `docs/mcp-server.md`.
- [ ] **W07.6 — Supply floors.** Refuse under a configured battery/supply
  voltage, and refuse when the UPS could not carry the Pi through the whole
  operation. 28 % SOC on 2026-09-21 refuses. Floors live in config with
  conservative defaults, not in code.
- [ ] **W07.7 — Every refusal names its cause**, and distinguishes "the engine
  is running" from "I cannot tell whether the engine is running". Both refuse;
  they call for different responses from the operator.
- [ ] **W07.8 — Audit.** Every authorization request — granted or refused —
  goes to the control audit log with the image hash, the vehicle state and
  signals it saw, and the reason. Refusals are not noise here; they are the
  record that the guard worked.

## Tests (these are the deliverable)

- [ ] **W07.9 — The state table, exhaustively.** Every `vehicle_state` value ×
  fresh / stale → exactly one expected outcome, written as data. Adding a new
  vehicle state without deciding its row fails the test.
- [ ] **W07.10 — Permission cannot be reused, replayed, or moved.** Second use
  of an `Authorization` refuses; expired refuses; a different image hash
  refuses; a different ECU refuses.
- [ ] **W07.11 — A state change mid-operation aborts.** Simulated RPM rising
  between blocks stops before the next block and is audited.
- [ ] **W07.12 — No tool mints one.** A test enumerates the MCP tool registry
  and the HTTP routes and fails if anything other than the operator
  confirmation flow can produce an `Authorization`.

## Existing guards these do not replace

`RAW_QUERY_SERVICES` in `src/collection/can_obd_client.py` and the ELM327
path's `assert_read_only_command` still restrict which services the read paths
may send. They stay. The guard in this file governs a write path that does not
exist yet; those govern the read paths that do.

## Done when

The guard and W07.9–12 are merged with nothing behind them; an attempt to add a
non-volatile write path without an `Authorization` does not type-check; and
`docs/skunkworks/ecu-rw-bench-research.md` — still written around the old
blanket prohibition — has been revised to describe a capability that lives
behind this gate.
