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

- [ ] **H06.1 A streaming frame decoder.** **Do:** turn the batch logic into
  a streaming decoder in `src/collection/kline/sniff.py` fed by UART bytes
  with timestamps: frames by length byte (and by format byte during init),
  checksum per frame, direction by protocol (StartCommunication and requests
  from the tester address; replies are the request's service + 0x40 or
  `7F`), request/reply pairing, and the wake-up pulse recognised as a UART
  break. **Accept:** the 2026-09-23 capture, replayed byte by byte through
  it, gives the same 1,462 frames with every checksum good, paired.
- [ ] **H06.2 Definition tracking.** **Do:** remember each `AC` definition by
  local id (including `01 <lid>` chains), and lay out later `21 <lid>`
  replies into `kline:DID_*`/`kline:MEM_*` values using `data/dids.yaml`
  widths and scales; replies whose definition was not seen are kept raw with
  the reason. **Accept:** the captured logging stream decodes into named
  channels; a capture cut to start after the definition yields raw frames
  and a named reason, no guessed values.
- [ ] **H06.3 Sniff mode in the daemon.** **Do:** the K-line collector
  (H05.8) runs in `sniff` mode whenever it is not commanding: EN high,
  pull-up off, transmit impossible (the transport refuses writes in this
  mode), emitting decoded channels and `kline:FRAME` rows with
  `metadata.tester` set to the source address seen (`0xFC` for UpRev).
  **Accept:** fake-serial tests that sniff mode never writes, and that a
  switch to command mode is refused while a foreign tester is active.
- [ ] **H06.4 Foreign tester detection.** **Do:** traffic from a tester that
  is not CANary moves session ownership to `foreign`, which holds until the
  line has been quiet for a configured time after that tester's
  StopCommunication or last frame. **Accept:** tests for enter, hold and
  release; `/status` shows the foreign tester's address and when it was last
  heard.

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
