# H05 — native K-line: transport, protocol, session ownership, collector

Part of [H00](H00-hat-integration-README.md). Goal: CANary reads over K-line
everything the UpRev cable reads, from the HAT, without the cable, and
records it with its own lineage.

## Where it stands

Proven 2026-09-23 with the bring-up scripts in `hardware/canary-hat/firmware/`
(KOEO only): wake-up (518.9 ms low / 18.1 ms high), StartCommunication
`81 10 FC 81 0E` → `83 FC 10 C1 5D 8F 3C`, a read `05 22 11 03 04 01 40` →
`04 62 11 03 99 13`, StopCommunication. UpRev's full session was sniffed:
`AC` defines local ids `81`/`82`/`83` from DID (`02 hh ll`) and RAM
(`83 addr32`) descriptors, chains ids with `01 <lid>`, then polls
`04 21 83 04 01` for 52-byte replies at ~9 Hz
(`docs/uprev-usb-kline-research.md` "On-wire view"; the vehicle skill).

What exists in `src/` is coupled to the USB cable:

- `uprev_protocol.py` mixes reusable KWP logic (descriptor encoding,
  order-preserving descriptor parsing, width-driven reply splitting that
  refuses unknown widths) with the cable's framing: the `02 00` request prefix,
  the `0x66` reply byte, local id `0x81` hard-coded, no `01 <lid>` include
  descriptor.
- `uprev_session.py` interleaves cable-local operations with ECU requests and
  is typed to `UpRevUsbTransport`.
- Nothing in the daemon: no transport, config, source, collector, status, or
  owner for the ECM's diagnostic session, which CAN and K-line share (an
  active K-line stream starved CAN Mode 01 for ~132 s on 2026-09-20).

## Rules

- **Reads and volatile operations only.** StartCommunication/Stop, session
  control, `21`, `22`, `23` and `AC` definitions (volatile RAM definitions)
  need no conditions. Anything that writes non-volatile memory is refused by
  the client itself, with no raw-request escape hatch, as the UpRev probe
  already is (AGENTS.md: explicit per-operation permission, `ignition_only`).
- **One owner of the ECM's diagnostic session at a time.** Broadcast CAN
  capture is never gated.
- Vehicle facts (addresses, wake-up timing, key bytes, known answers) are
  profile data. The next vehicle may be a 5-baud ISO 9141 ECU.
- The UpRev USB path keeps working and keeps its tests; the refactor moves
  shared logic out from under it, it does not replace it.
- The TLIN1027 start/stop order (HAT-Spec §11) is enforced by the transport
  class, so it cannot be done out of order by a caller.

---

## Decisions

- [ ] **H05.1 Confirm the proposed defaults** (H00): source `kline`; K-line
  channels not trainable until decided; collector off by default and started
  by a capture profile; CAN diagnostic requests stop while K-line owns the
  session. **Accept:** the operator's answer recorded in H00 and here.

## Desk

- [ ] **H05.2 Vehicle K-line facts as profile data.** **Do:**
  `data/nissan-xterra-2008/kline.yaml` with a pydantic model: baud, ECM and
  tester addresses, the wake-up sequence(s) in order of preference, key
  bytes, frame format after StartCommunication, P2/P3 timings, and
  known-answer requests with their expected reply shape. Required fields, no
  defaults for the vehicle-specific ones. **Accept:** loader tests; a profile
  without the file means "K-line not configured", named.
- [ ] **H05.3 Wire-level KWP2000 protocol module.** **Do:**
  `src/collection/kline/protocol.py` (pure): addressed frames (format byte,
  target, source) for StartCommunication, length-byte frames after it,
  checksums, a `local_id` parameter for `AC`/`21`, the `01 <lid>` include
  descriptor, DID and RAM byte descriptors, reply splitting by catalog widths
  from `data/dids.yaml` (`dids:` and `memory:`), negative-response decoding.
  Then make `uprev_protocol.py` build on it, keeping only the cable envelope
  (`02 00` out, `0x66` in). **Accept:** the frames captured on 2026-09-23 are
  test fixtures and round-trip byte-exact; every existing `test_uprev_*` test
  still passes unchanged.
- [ ] **H05.4 Serial + GPIO transport.** **Do:**
  `src/collection/kline/transport.py`: one class that owns the tty, EN
  (GPIO26) and the commander pull-up (GPIO24), and enforces the order: tty
  open before EN rises, RX discarded across the mode change, break-based
  wake-up with host timing checked (reject a wake-up whose measured low
  differs from the profile by more than a stated tolerance), echo verified
  and stripped, P2/P3 respected, EN and pull-up dropped before the tty closes
  on every exit path including exceptions and cancellation. A fake serial +
  fake GPIO for tests. **Accept:** tests for each ordering rule (the fake
  records the sequence), echo mismatch (bus contention) as a named error,
  timeout, and cleanup on cancellation.
- [ ] **H05.5 Service allowlist in the client.** **Do:** the K-line client
  accepts only the read/session/define services, and refuses every
  non-volatile write service by id (e.g. `0x2E`, `0x3B`, `0x3D`,
  `0x34`/`0x36`/`0x37`, routine controls that erase) with an error naming
  the AGENTS.md policy. **Accept:** a test per refused id.
- [ ] **H05.6 ECM session ownership.** **Do:** generalise `tx_gate` from a
  CAN passive flag into an owner of the ECM diagnostic session:
  `can_diagnostics` (default), `kline`, `foreign` (H06: another tester seen
  on K-line), `none`. CAN diagnostic requests go out only when the owner is
  `can_diagnostics`; manual passive stays as today. Transitions are logged,
  shown in `/status`, and accounted in `capture_lifetime` like today's passive
  pauses. **Accept:** tests for each transition, and that broadcast CAN is
  never affected; the MCP `set_passive_mode` behaviour is unchanged.
- [ ] **H05.7 The `kline` source.** **Do (after H05.1):** add it to
  `TelemetrySource`, `CAPTURED_SOURCES`, H01.1's trainability classification
  (not trainable), `channels.SpecSource`, `spec_producibility`, the live
  router and `tests/test_telemetry.py` — the sweep `f73333f` made for
  `sensor`. Channels: `kline:DID_<hex>` and `kline:MEM_<NAME>` (matching the
  catalog names), raw frames as `kline:FRAME` rows with payload bytes so any
  session can be re-decoded later. **Accept:** `grep` for every place
  `TelemetrySource` members are listed shows `kline` or a stated reason.
- [ ] **H05.8 The collector.** **Do:** `src/collection/kline_collector.py`:
  acquire ownership (H05.6), wake up, StartCommunication, define the requested
  set with `AC` (DIDs and RAM addresses from a capture profile's new `kline:`
  list), poll `21`, and emit records stamped at each exchange's midpoint with
  `acquired_at` (the `_Emitter` policy from `tools/uprev_kline_probe.py`);
  reconnect on session loss with backoff; StopCommunication and release
  ownership on stop. `CaptureProfile` gains `kline`; capture quality learns its
  achieved rate. **Accept:** fake-transport tests for define/poll/decode,
  session loss mid-poll, and release on cancellation; a `kline` capture
  profile validates against the catalog.

## Truck

- [ ] **H05.9 The session recipe, engine running.** **Do (truck):** find the
  minimal sequence for `AC`/`21` streaming (does it need `10 C0`, `10 85` or
  `10 FB` first, as UpRev's cable sends?), what the `04 01` trailer on `21`
  and `22` means (try without it), how long a session survives idle and
  under polling, and the achievable rate for 1, 12 and 52-byte sets.
  **Accept:** the recipe recorded in `kline.yaml` and the vehicle skill, with
  the captures in `data/analyses/`.
- [ ] **H05.10 Known answers before novel ones.** **Do (truck):** K-line
  `0x1201` RPM against broadcast `decoded:ECM_RPM` (the project's strongest
  control) and `0x1101` coolant against `obd:COOLANT_TEMP`, through
  `compare_channels` on the acquisition clock; per-sample ratio read, not only
  the verdict. **Accept:** both 1:1 within the decode's resolution; an
  expectation added to the ground-truth suite with a committed slice.
- [ ] **H05.11 A ten-minute engine-running session.** **Do (truck):** the
  collector polling the tuning set (commanded AFR, ignition timing, BFS, air
  charge) through a drive segment with no manual intervention. **Accept:**
  achieved rate, session losses and recoveries, and CAN broadcast frame
  counts unchanged from a K-line-off segment, all recorded here.
- [ ] **H05.12 Tuning providers and noise floor.** Add the K-line instrument
  to `data/nissan-xterra-2008/tuning_providers.yaml` for the roles it serves
  (commanded AFR, ignition timing, BFS), with its own null-run noise floor
  (all-did-tuning-hardening 3.8, 7.5; `instrument: kline` kept separate from
  `uprev`). **Accept:** a same-ROM null run recorded with `conditions`.
