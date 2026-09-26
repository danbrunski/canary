# H06 — passive K-line: watching UpRev and scan tools live

Part of [H00](H00-hat-integration-README.md). Goal: whatever another tester
does on K-line — UpRev logging, an NT510 or other scan tool — CANary decodes
it live, time-aligned with CAN, and gets out of its way.

## Where it stands

On 2026-09-23 the HAT sniffed 93 s of UpRev's session with the transceiver in
normal mode and the pull-up off: 37,403 bytes, 1,462 frames, every checksum
good, and UpRev unaffected. That used GPIO edge capture (`kline_edges.py`)
and a batch decoder (`kline_decode.py`), which split messages on gaps. None
of it is in the daemon, there is no request/reply pairing, and nothing lays
out a `21` reply using the `AC` definition that preceded it (a capture that
starts mid-stream loses the definition, as UpRev's ignition capture did on
2026-09-20).

## Rules

- **Never transmit and never enable the pull-up while a foreign tester is
  active.** The pull-up's effect on another tool is only measured with it off
  (HAT measurement 2).
- A foreign tester owns the ECM session (H05.6 `foreign`): CAN diagnostic
  requests stop automatically while it is active, instead of relying on the
  operator to arm passive mode.
- Raw frames are kept, so a session can be re-decoded when a definition or a
  width is learned later.
- Two decoders exist in the end, one per mechanism: the UART receiver for
  10400-baud KWP (the normal case), edge capture for unknown protocols (5-baud
  init, other baud rates). They are mutually exclusive on GPIO15.

---

## Desk

- [x] **H06.1 A streaming frame decoder.** **Do:** turn the batch logic into
  a streaming decoder in `src/collection/kline/sniff.py` fed by UART bytes
  with timestamps: frames by length byte (and by format byte during init),
  checksum per frame, direction by protocol (StartCommunication and requests
  from the tester address; replies are the request's service + 0x40 or
  `7F`), request/reply pairing, and the wake-up pulse recognised as a UART
  break. **Accept:** the 2026-09-23 capture, replayed byte by byte through
  it, gives the same 1,462 frames with every checksum good, paired.
  **Evidence (2026-09-23):** `src/collection/kline/sniff.py` (`FrameDecoder`,
  `exchanges`). Replayed byte by byte over UpRev's session
  (`tests/fixtures/kline/uprev_session_bytes.json.gz`, 27 KB: every UART byte
  with timing, each wake-up as the `0x00` break a Linux UART delivers): 1,462
  frames, 12 wake-ups, 0 resyncs, 723 requests paired with 723 replies, and 12
  StartCommunication attempts of which the ECM answered 4. **A bug the
  corruption test found:** a resync that left a data `0x00` at the front of
  the buffer waited on it forever, so one flipped bit cost 547 frames; fixed,
  200 random single-bit flips now lose at most 7. 9 tests in
  `tests/test_kline_sniff.py`.
- [x] **H06.2 Definition tracking.** **Do:** remember each `AC` definition by
  local id (including `01 <lid>` chains), and lay out later `21 <lid>`
  replies into `kline:DID_*`/`kline:MEM_*` values using `data/dids.yaml`
  widths and scales; replies whose definition was not seen are kept raw with
  the reason. **Accept:** the captured logging stream decodes into named
  channels; a capture cut to start after the definition yields raw frames
  and a named reason, no guessed values.
  **Evidence (2026-09-23):** `DefinitionTracker` records UpRev's three
  acknowledged definitions (`81`, `82` including `81`, `83` including `82`)
  and lays out its 590 logging replies: all 590 refused today, naming DID
  `0x1117`, because five of UpRev's DIDs have no catalog width (see H05.3);
  given widths that fit the reply, every one lays out into 38 values. A
  capture cut after the definitions keeps its raw replies with "never
  defined". Raw bytes are kept on every result for a later re-decode.
- [x] **H06.3 Sniff mode in the daemon.** **Do:** the K-line collector
  (H05.8) runs in `sniff` mode whenever it is not commanding: EN high,
  pull-up off, transmit impossible (the transport refuses writes in this
  mode), emitting decoded channels and `kline:FRAME` rows with
  `metadata.tester` set to the source address seen (`0xFC` for UpRev).
  **Accept:** fake-serial tests that sniff mode never writes, and that a
  switch to command mode is refused while a foreign tester is active.
  **Evidence (2026-09-23):** `KlineCollector` sniffs whenever no capture
  profile asks for a session (`hardware.kline.sniff`, default on): transport
  in `Mode.SNIFF` (pull-up off, `send_frame`/`wake` refuse), bytes read in
  50 ms bursts through `read_burst`, every frame recorded as `kline:FRAME`
  (raw bytes, direction, addresses when addressed), values from a tracked
  `AC` set or a `22` read as `kline:DID_*`/`kline:RAM_*` with
  `instrument: sniff`. Replaying UpRev's session through it: 1,462
  `kline:FRAME` rows and its `22 11 03` reads decoded (`0x99`/`0x9A`, a live
  value); its logging set stays raw until the five missing widths are known.
  `tests/test_kline_collector.py`.
- [x] **H06.4 Foreign tester detection.** **Do:** traffic from a tester that
  is not CANary moves session ownership to `foreign`, which holds until the
  line has been quiet for a configured time after that tester's
  StopCommunication or last frame. **Accept:** tests for enter, hold and
  release; `/status` shows the foreign tester's address and when it was last
  heard.

  **Partial (2026-09-23):** every tester frame heard in sniff mode renews a
  `foreign` claim on the ECM session with a TTL of
  `hardware.kline.foreign_hold_seconds` (default 10 s), which `check_expiry`
  (already polled by the daemon) releases; while it stands CAN diagnostics
  pause and a capture profile's K-line request waits in sniff mode, logged
  once. Tests: ownership held while UpRev talks and released after the hold;
  a K-line request never switches to command mode while foreign. **Owed:**
  `/status` shows the owner and the claim's remaining hold, but not yet the
  foreign tester's address or when it was last heard.

  **Done (2026-09-26):** `/status.passive` (and `/control/passive`) now carry
  `foreign_tester`: `{tester_address, module_address, last_heard_at,
  seconds_since_heard}` (`"FC"`, `"10"`, ISO UTC, seconds), `null` until
  another tester is heard, kept after the claim expires so a CAN pause stays
  explained. The decoder gives every frame a `tester` address
  (`SniffedFrame.tester`: an addressed frame's source for a request / target
  for a reply, inherited by length-byte frames from the StartCommunication
  reply and forgotten at StopCommunication or wake-up, like `module`); the
  sniffer calls `tx_gate.note_foreign_tester` for each of another tester's
  frames beside the claim it renews; `reset_ecm_session_claims` clears it.
  `src/dashboard/status.py` unchanged: the field rides on `tx_gate.snapshot()`,
  which both `/status` views already embed. Tests: UpRev's session names
  tester `0xFC` on every frame and the ABS builder's mid-session frames
  `None` (`tests/test_kline_sniff.py`); snapshot, expiry-survival and reset
  (`tests/test_tx_gate.py`); enter / hold / release with UpRev's address
  (`test_status_names_the_foreign_tester_and_when_it_was_last_heard`,
  `tests/test_kline_collector.py`); full and compact `/status`
  (`tests/test_control_passive.py`); the process-state reset covers it.
## Truck

- [ ] **H06.5 Known answer: sniffed values equal UpRev's own log.** **Do
  (truck):** UpRev logging with CANary sniffing; save UpRev's CSV. Import it
  (`tools/import_uprev_log.py`) and compare each sniffed channel with the CSV
  channel of the same quantity through `compare_channels`. **Accept:** 1:1
  for every channel the definition names; recorded in the research doc.
- [ ] **H06.6 A scan tool's protocol.** **Do (truck):** edge-capture an
  NT510 (or another scan tool) session on this truck with `kline_edges.py`,
  decode it, and record the protocol (baud, init, addresses) in the vehicle
  skill. If it is KWP at 10400, H06.1 covers it; if not, add its framing to
  the decoder. **Accept:** one decoded session in `data/analyses/`.
- [ ] **H06.7 HAT measurement 2, remainder.** Sniff UpRev with the pull-up
  **on**, once, deliberately, to answer whether the 1 kΩ disturbs a foreign
  session. Whatever the answer, the rule above stands; the measurement decides
  whether rev 3.0 keeps the FET. **Accept:** result in
  `CANary-HAT-Revisions.md` §13.1.1.
