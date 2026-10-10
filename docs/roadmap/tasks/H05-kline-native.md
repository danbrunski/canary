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

- [x] **H05.1 Confirm the proposed defaults** (H00): source `kline`; K-line
  channels not trainable until decided; collector off by default and started
  by a capture profile; CAN diagnostic requests stop while K-line owns the
  session. **Accept:** the operator's answer recorded in H00 and here.
  **Evidence (2026-09-23):** the operator chose all three recommended options:
  a new `kline` source; not trainable yet; the collector takes the ECM
  session only when a capture profile asks, otherwise the HAT sniffs.

## Desk

- [x] **H05.2 Vehicle K-line facts as profile data.** **Do:**
  `data/nissan-xterra-2008/kline.yaml` with a pydantic model: baud, ECM and
  tester addresses, the wake-up sequence(s) in order of preference, key
  bytes, frame format after StartCommunication, P2/P3 timings, and
  known-answer requests with their expected reply shape. Required fields, no
  defaults for the vehicle-specific ones. **Accept:** loader tests; a profile
  without the file means "K-line not configured", named.
  **Evidence (2026-09-23):** `src/collection/kline/profile.py` (strict model:
  no default for any vehicle fact; wake-ups, known answers and the profile
  itself each require `evidence`), `data/nissan-xterra-2008/kline.yaml`, and
  `daemon.kline_profile`, scoped per vehicle by `scope_config` (the exhaustive
  scoping test covers it). A missing file raises `KlineNotConfiguredError`.
  `tests/test_kline_profile.py` checks the file rebuilds the captured
  StartCommunication and known-answer frames byte for byte.
- [x] **H05.3 Wire-level KWP2000 protocol module.** **Do:**
  `src/collection/kline/protocol.py` (pure): addressed frames (format byte,
  target, source) for StartCommunication, length-byte frames after it,
  checksums, a `local_id` parameter for `AC`/`21`, the `01 <lid>` include
  descriptor, DID and RAM byte descriptors, reply splitting by catalog widths
  from `data/dids.yaml` (`dids:` and `memory:`), negative-response decoding.
  Then make `uprev_protocol.py` build on it, keeping only the cable envelope
  (`02 00` out, `0x66` in). **Accept:** the frames captured on 2026-09-23 are
  test fixtures and round-trip byte-exact; every existing `test_uprev_*` test
  still passes unchanged.
  **Evidence (2026-09-23):** `src/collection/kline/protocol.py`; 32 tests in
  `tests/test_kline_protocol.py` over every frame shape captured on the truck
  (`tests/fixtures/kline/uprev_session_frames.json`), all byte-exact,
  including UpRev's three chained `AC` definitions and the 50-byte `61 83`
  reply. The wire parser keeps duplicate descriptors (UpRev defines RAM
  `0xFFFF13FE`/`13FF` twice); only our own definitions refuse them.
  `uprev_protocol.py` now encodes descriptors and checksums through it and
  keeps only the cable's envelope and its tested parsing contract; all 44
  UpRev tests pass unchanged. **Found by it:** five DIDs UpRev logs have no
  width in `data/dids.yaml` (`0x1117`, `0x1126`, `0x1204`, `0x1209`,
  `0x122B`), so the real reply is refused, correctly; the reply pins their
  total at 7 bytes. A native `22` read of each gives the widths (H05.9).
- [x] **H05.4 Serial + GPIO transport.** **Do:**
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
  **Evidence (2026-09-23):** `src/collection/kline/transport.py`
  (`KlineTransport`, `command`/`sniff` modes, the six ordering rules in its
  docstring) over `src/collection/gpio_lines.py` (GPIO v2 uAPI through
  `ctypes`, no dependency). 16 tests (`tests/test_kline_transport.py`) with a
  scripted ECU answering in the frames captured on the truck, virtual time,
  and an event log that pins the order: tty, lines, EN high, discard; EN and
  pull-up low, release, tty closed, also when the body raises or raising EN
  fails. **On the HAT:** wake-ups measured 519.01 ms (profile 518.9) and
  25.08 ms (25.0); after an exception mid-session GPIO24 and GPIO26 read low.
  With the OBD plug out the transceiver has no VSUP and nothing echoes, now a
  named `NoEchoError` rather than a bare timeout. **Measured on the Pi 5:** a
  released GPIO line keeps its last level, so EN is always driven low before
  release; a SIGKILL mid-session can still leave it high until reboot.
  Still owed: an ECM exchange through this class (KOEO, H05.9).
- [x] **H05.5 Service allowlist in the client.** **Do:** the K-line client
  accepts only the read/session/define services, and refuses every
  non-volatile write service by id (e.g. `0x2E`, `0x3B`, `0x3D`,
  `0x34`/`0x36`/`0x37`, routine controls that erase) with an error naming
  the AGENTS.md policy. **Accept:** a test per refused id.
  **Evidence (2026-09-23):** `protocol.check_request`: an allowlist (read,
  session, define, StartCommunication/Stop, TesterPresent), named refusals for
  every write service, and `10 85` (programming session) refused as a
  sub-function. Tests: one per refused id, plus every request the module
  builds passing its own allowlist. The transport (H05.4) calls it on every
  send, so no caller can skip it.
- [x] **H05.6 ECM session ownership.** **Do:** generalise `tx_gate` from a
  CAN passive flag into an owner of the ECM diagnostic session:
  `can_diagnostics` (default), `kline`, `foreign` (H06: another tester seen
  on K-line), `none`. CAN diagnostic requests go out only when the owner is
  `can_diagnostics`; manual passive stays as today. Transitions are logged,
  shown in `/status`, and accounted in `capture_lifetime` like today's passive
  pauses. **Accept:** tests for each transition, and that broadcast CAN is
  never affected; the MCP `set_passive_mode` behaviour is unchanged.
  **Evidence (2026-09-23):** in `src/collection/tx_gate.py`, not a new
  module: holders `kline` and `foreign` claim the session (optionally with a
  TTL, renewable), a foreign claim outranks ours, and `is_passive()` — the
  question every existing caller asks — is true while any claim stands, so
  the OBD loop, the `send` backstop, record arming and idle shutdown needed
  no change and no daemon file was touched. The snapshot (`/status`,
  `/control/passive`) gains `ecm_session_owner`, `ecm_session_claims` and
  `tx_suppressed`, additively; `enabled` stays the operator's switch. Owner
  changes bump the capture generation and open/close `ecm-session-<holder>`
  pause windows; `check_expiry` (already polled) releases expired claims; the
  validation harness resets claims. Six new tests in `tests/test_tx_gate.py`;
  the passive-control, record-arming, idle-shutdown and did_log suites pass
  unchanged (118 tests).
- [x] **H05.7 The `kline` source.** **Do (after H05.1):** add it to
  `TelemetrySource`, `CAPTURED_SOURCES`, H01.1's trainability classification
  (not trainable), `channels.SpecSource`, `spec_producibility`, the live
  router and `tests/test_telemetry.py` — the sweep `f73333f` made for
  `sensor`. Channels: `kline:<module>:<hex>` and `kline:<module>:MEM_<NAME>`
  (e.g. `kline:ECM:1206`, `kline:ECM:MEM_ECM_TARGET_EQUIV_RATIO`; K01.1
  module-attributed names, `src/collection/kline/names.py` — rows recorded
  before 2026-09-24 are the legacy `DID_<hex>`/`MEM_<NAME>` and are never
  rewritten), raw frames as `kline:FRAME` rows with payload bytes so any
  session can be re-decoded later. **Accept:** `grep` for every place
  `TelemetrySource` members are listed shows `kline` or a stated reason.
  **Partial (2026-09-23):** `KLINE_SOURCE` in `src/telemetry.py` (Literal,
  sync assert, `CAPTURED_SOURCES`, a docstring stating the lineage and the
  not-trainable decision), `channels.SpecSource` with a reader, the exact-set
  test, and `spec_producibility`'s comment giving the reason it is absent
  there. The live router's catch-all already handles it. **Owed:** the
  trainability classification is H01.1's, still waiting on the other
  session's `feature_pipeline.py` work; until then the exposure is small,
  because a K-line session runs only under a capture profile and captures
  (`_capture-*`) are excluded from training discovery.
  **Done (verified 2026-09-26):** the owed classification landed with H01.1
  in `3f73755`. `src/telemetry.py` lists `KLINE_SOURCE` in
  `NON_TRAINABLE_SOURCES`, with the reason "not trainable until the operator
  decides (H00 K-line defaults)". The module then calls
  `require_trainability_classified` at import time. That call raises
  `TypeError` for a source that is unclassified, classified twice, or not a
  source at all, so a missing entry fails the first import. `kline` is also
  in `channels.SpecSource` and the `_kline_series` reader, so the acceptance
  grep holds. `pytest tests/test_telemetry.py tests/test_trainability.py`:
  35 passed. `test_trainability.py` covers `kline:ECM:1206` and
  `kline:ECM:MEM_ECM_TARGET_EQUIV_RATIO`, plus the refusal of an unclassified
  source.
- [x] **H05.8 The collector.** **Do:** `src/collection/kline_collector.py`:
  acquire ownership (H05.6), wake up, StartCommunication, define the requested
  set with `AC` (DIDs and RAM addresses from a capture profile's new `kline:`
  list), poll `21`, and emit records stamped at each exchange's midpoint with
  `acquired_at` (the `_Emitter` policy from `tools/uprev_kline_probe.py`);
  reconnect on session loss with backoff; StopCommunication and release
  ownership on stop. `CaptureProfile` gains `kline`; capture quality learns its
  achieved rate. **Accept:** fake-transport tests for define/poll/decode,
  session loss mid-poll, and release on cancellation; a `kline` capture
  profile validates against the catalog.
  **Partial (2026-09-23):** `src/collection/kline_collector.py`
  (`KlineCollector`), `src/collection/kline/plan.py` (a profile's `kline:`
  list → one definition; unknown names and DIDs without a catalog width
  refused by name, with how to learn the width), `CaptureProfile.kline`, and
  `hardware.kline` (`KlineConfig`: off by default; enabling means the
  hardware may be used, not that it transmits). Idle until the active capture
  profile lists `kline:` channels; then claims the ECM session, wakes the
  ECM, defines local id `0x81`, polls, and emits `kline:ECM:<hex>` /
  `kline:ECM:MEM_*` rows (named `kline:DID_*`/`kline:MEM_*` until K01.1) with raw bytes and `acquired_at` at the exchange midpoint; ends with
  StopCommunication and releases when the list empties or the daemon stops.
  7 tests (`tests/test_kline_collector.py`): idle-then-own, retry after a
  failed start with ownership released between, a new session after loss
  mid-poll, release on task cancellation, and refusals that never transmit.
  The plan resolved `ECM_TARGET_EQUIV_RATIO` to RAM `0xFFFF26BE`/`26BF`,
  the same two bytes UpRev's Target AFR definition reads. **Owed:** saving a
  capture profile does not yet validate its `kline:` list against the catalog
  (the collector refuses at run time instead, logged once); capture quality
  does not yet measure the K-line rate; raw `kline:FRAME` rows are left to
  the sniffer (H06), since each collector row already carries its raw bytes.

  **Done (2026-09-26):** *Save validates.* `KlineCatalog`
  (`src/collection/kline/plan.py`: K-line profile path, DID catalog, poll
  interval) owns the one resolution — load both, `resolve_module_plan`, the
  P3max rule — and both the collector's `_prepare` and
  `CaptureProfileStore.save` call `KlineCatalog.resolve`, so save refuses
  exactly what the collector would (unknown name, DID with no width, two
  modules, unknown module, P3max, no `kline.yaml` for the vehicle) with the
  same message; the route answers 409. A store built without a catalog
  refuses any profile listing `kline:` rather than skipping the check; the
  daemon's store (`runtime.py`) and the app's fallback store (`api.py`) get
  `KlineCatalog.from_config(config)`. Tests: 8 in `tests/test_capture_profiles.py`
  (readable ECM and ABS lists save; 4 unreadable lists refused by name and not
  written; P3max; no K-line profile; no catalog; 409 through an injected store
  and through `create_app`'s own). *Capture quality learns the rate.*
  `ModulePlan.entry_channels` maps each profile entry to its stored channel;
  the collector observes the capture ledger once per delivered entry, keyed
  `kline:<entry>` (`names.capture_key`); `evaluate_capture_quality` adds a
  `kind: "kline"` row per entry for every caller, whose cadence is the
  achieved K-line rate. No rate is requested (the profile names none), so
  missing and gaps are judged, a shortfall never; CANary's own
  `ecm-session-kline` window is acquisition time for these rows, not a pause.
  A verdict rebuilt from persisted rows (no ledger) says it did not score
  K-line instead of calling it unanswered. Tests: 7 in
  `tests/test_capture_quality.py`, 3 in `tests/test_kline_collector.py`
  (a fake-transport session fills the ledger for both entries and scores
  `ok`; two ABS flags of one word both observed; no capture armed, no rows),
  2 in `tests/test_kline_plan.py`. **Limits:** (1) *Passive armed during a
  K-line session fails the capture.* The ledger merges overlapping pause
  reasons into one window, so passive joins the open `ecm-session-kline`
  window and keeps it open until passive disarms; K-line rows cannot tell its
  parts apart and count the whole window, passive tail included, as eligible.
  The silence after the session is then an `acquisition_gap`, a hard clause:
  the verdict turns `fail`, or `degraded` if coverage stays at least 90 %
  (`test_a_passive_window_merged_into_the_kline_session_reports_a_gap_never_hides_one`:
  session 0-100 s, passive to 300 s, one window 0-300 s, 100-300 s scored as
  a gap). The fix is per-reason window bounds in `capture_lifetime`. (2) The
  row-rebuilt recompute does not reconstruct K-line cadence (it reads Mode
  01/22 rows only); the live and stop-time verdicts carry it.

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
- [x] **H05.13 Measure the scan tool's airbag wake-up.** CANary's own wake-up
  is ignored by the SRS (`0x58`). On 2026-10-08 five variants were tried on the
  truck with the same `81 58 FC 81 56` StartCommunication the scan tool sends,
  and none got a reply: long-low 518.9/18.1 ms, fast 25/25, 25/20, 25/15 and
  25/10 ms (low/high). The scan tool is answered on the first try every time,
  and CANary's bytes echo correctly, so the difference is the pulse. **Do:**
  record wake-ups as `kline:BREAK` rows (sniffed: PARMRK break markers;
  CANary's: host-measured low/high), show them before each StartCommunication
  in the exchange report, then measure the scan tool's SRS wake-up on the next
  scan-tool session. No new wake-up goes into `kline.yaml` until that
  measurement. **Accept:** the scan tool's low + high (and its bounds) beside
  CANary's in one session's report, in `data/analyses/`.
  **Evidence (2026-10-09, recording):** `transport.mark_breaks` sets
  `PARMRK | INPCK` and clears `IGNBRK`, `BRKINT`, `IGNPAR`, `ISTRIP` inside
  `KlineTransport.open` for every sniff port, reads the flags back and refuses
  to sniff without them (`test_the_real_sniff_port_reads_marked_bytes_and_says_so`
  on a pty; `test_pyserial_alone_does_not_mark_breaks_so_the_transport_must`).
  `FrameDecoder` un-escapes `FF FF` / `FF 00 00` / `FF 00 xx` across reads; UpRev's
  whole fixture session re-encoded as a marked port would deliver it decodes
  to the same frames byte for byte
  (`test_uprevs_session_marked_decodes_byte_for_byte_as_unmarked`). Known
  answer: `FF 00 00` at t0 then the SRS StartCommunication at t0 + 50 ms gives
  one wake-up, span 50.0 ms, target SRS, answered
  (`test_one_break_before_the_srs_start_spans_fifty_ms`). CANary's pulses are
  stored with `origin: canary`
  (`test_canarys_own_wake_up_is_stored_with_its_measured_low_and_high`).
  **Remaining (truck):** the scan-tool session itself. Not measurable here:
  how a sniffed low splits into low and high (the UART never reports the
  release), and how late the pin follows CANary's break ioctls.
  **Evidence (2026-10-10, the scan-tool session `2026-10-10T153117`): the
  difference is not the pulse but the byte spacing.** The tool's fast init
  measured 49.2-50.4 ms low + high (CANary's: 50.145 ms). A BREAK's
  `next_byte_ns` is the frame's *first* byte; a FRAME row is stamped at its
  *last* (`SniffedFrame.finished`), and its `timing_uncertainty_s` bounds that
  byte. The tool's `81 58 FC 81 56` ran 63.7-64.5 ms first byte to last, i.e.
  14.97-15.17 ms idle between bytes (ISO P4); CANary sent the same five bytes
  back to back in one write. Control: across 10-04, 10-08 and 10-10, 177 of 177
  tool frames to the SRS had their last byte alone in a read (uncertainty > 0)
  and 199 of the module's 209 back-to-back replies did not. Fix: per-module
  `request_timing.p4_ms` (15.0 for `0x58`, schema-checked against recorded
  `sniffed_starts`, required when a sniffed start was spaced); the transport
  writes byte by byte at that gap, echo checked per byte; the ECM keeps
  `timing.p4_ms` 0 (one write). Wake-up records carry `p4_ms` and the measured
  gaps; sniffed FRAME rows carry `byte_gap_ms`
  (`test_a_frame_row_is_stamped_at_its_last_byte_and_a_break_at_the_next_byte`,
  `test_the_airbag_module_gets_its_bytes_one_by_one_at_the_declared_gap`,
  `test_a_spaced_sniffed_start_without_request_timing_is_refused`).
  **Remaining (truck):** a KOEO read_codes on the SRS with the fix deployed.

  **Closed 2026-10-10, live:** with 2f7effd deployed (per-module p4 15 ms),
  CANary's own read_codes on the SRS succeeded KOEO (session
  2026-10-10T165941): `a2 00 -> e2 00`, `a2 01 -> e2 00`, "no codes stored"
  (the scan tool's earlier B1134 clear had gone through). Canary BREAK rows,
  two attempts: long-low (520/18 ms, gaps 15.1-15.6 ms) unanswered both times;
  iso14230-fast (25.4/29.2 and 25.2/25.1 ms, gaps 15.1-15.5 ms) **answered**
  both times. The cause was byte spacing, not the pulse. Follow-up: an
  SRS-only fast-init wake list saves the ~0.9 s spent on long-low.