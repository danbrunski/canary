# K01 — K-line tooling: module attribution, exchange reports, decode, active reads

**Why:** on 2026-09-24 the operator's scan tool read the ABS module (address
0x28) over K-line, showing wheel speeds, yaw rate, decel G, battery, engine
speed and lamp/switch states. CANary's sniffer recorded every exchange. It
could not say which module a reply came from, had no report of what the scan
tool asked for, and had no path from bytes to units. It also cannot ask the
ABS itself. Evidence:
[ABS over K-line analysis](../../../data/analyses/2026-09-24-abs-kline-scan-tool.md).

Ownership: the operator handed this to canary-a2 on 2026-09-24; canary-b8,
who wrote the sniffer, is finished with `src/collection/kline/*` for now.
Follow AGENTS.md: vehicle facts (module addresses, DID meanings, scales) go
in profile data, not code; every binding needs a known answer; an empty
result says which kind of empty it is.

## Checklist

- [x] **K01.1 — Attribute every K-line frame to its module.** After a KWP2000
  StartCommunication (`81 <target> <source> 81`) the scan tool switches to
  length-only frames with no address bytes, so the sniffer stores replies as
  bare `DID_xxxx`. That collides with the ECM's CAN DID names: UpRev's
  `22 11 03` already lands as `kline:DID_1103`, and today's ABS 0x1210 (yaw)
  is not the ECM's 0x1210 (TPS2).
  - Carry the addressed target from `FrameDecoder._frame`'s StartCommunication
    tracking into the session state, and stamp it on every later length-frame
    record until StopCommunication or a new StartCommunication.
  - Channels become module-scoped. Name the module from profile data (for
    example `data/nissan-xterra-2008/kline.yaml` modules: `0x28: ABS`,
    `0x10: ECM`), so `kline:ABS:1303`. An address with no profile name keeps
    the hex (`kline:28:1303`); never a bare `DID_`.
  - A frame seen before any StartCommunication (mid-session attach) is
    `module: unknown` and says so, rather than being guessed.
  - Test: an addressed StartCommunication followed by length frames yields
    module-scoped rows; the UpRev `22 11 03` case and today's ABS case don't
    collide.

  **Evidence (2026-09-25):** `FrameDecoder` stamps `SniffedFrame.module` from
  the positive StartCommunication reply until StopCommunication / wake-up /
  the next start, `None` before any; direction is now from the service byte
  (`kp.is_response_sid`), so a reply whose request was missed stays a reply.
  Names live in `src/collection/kline/names.py` (`<module>:<hex>`,
  `<module>:RAM_…`, `ECM:MEM_…`; module labels from `kline.yaml` `modules:`,
  unnamed addresses in hex, `unknown` before a start). Legacy `kline:DID_*`
  rows are refused, not decoded, by `decode_raw_did_reading(source=…)` (offline,
  live, REST), by `channels._did_series` (`did:` never reads a kline row) and by
  `kline:` specs (`ChannelSpecError` naming `attribute_modules`), which recovers
  a legacy session's modules from its own frames. Tests:
  `test_kline_sniff.py::test_a_scan_tool_session_on_the_abs_is_stamped_until_it_stops`,
  `::test_uprevs_whole_session_is_the_ecms`,
  `::test_a_reply_whose_request_was_missed_still_reads_as_a_reply`;
  `test_kline_collector.py::test_idle_collector_sniffs_uprevs_session_frame_for_frame`
  (UpRev's `22 11 03` -> `ECM:1103`, no bare `DID_`) and
  `::test_a_scan_tool_on_the_abs_is_named_and_leaves_can_polling_alone`
  (`ABS:1210` ffff vs `ECM:1210` 001a, `unknown:1202`);
  `test_kline_exchanges.py::test_a_legacy_session_gets_its_modules_back_from_its_own_frames`,
  `::test_a_legacy_kline_did_is_refused_by_the_channel_reader` (`did:` over
  only-legacy kline rows refuses naming `attribute_modules`);
  `test_kline_names.py` (5); `test_did_decode.py::test_a_legacy_kline_did_row_is_not_decoded_against_the_ecm_catalog`.
- [~] **K01.2 — An exchange report for a sniffed session.** One command over
  a session's `kline` rows, giving:
  - request/response pairs by module;
  - service, DID or local id, reply bytes;
  - NRCs by name;
  - per-DID poll rate;
  - the DID cycle a scan-tool screen repeats, versus one-off reads at screen
    load;
  - timing uncertainty (the sniffer's `metadata.timing_uncertainty_s`).
  
  Output is Markdown and JSON under `data/analyses/`. Test with a fixture built
  from today's capture.

  **Partial (2026-09-25):** `src/analysis/kline_exchanges.py`
  (`wire_frames`, `pair_exchanges`, `exchange_report`, `render_markdown`) and
  the command `tools/kline_exchange_report.py <session> --kline-profile
  data/<vehicle>/kline.yaml` (writes `<session>-kline-exchanges.md`/`.json`,
  default `data/analyses/`; refuses a session with no kline rows / no FRAME
  rows naming which). Rates are median per-DID intervals; NRCs by name;
  cycle vs one-off; orphans and corrected directions counted; timing
  uncertainty summarised or "not recorded". Tests: `test_kline_exchanges.py`
  (report over the ABS screen built from the documented 2026-09-24 bytes,
  `tests.builders.abs_scan_tool_frames`, and over the real UpRev fixture:
  12 StartCommunications, 590 local-id reads, all `ECM`),
  `test_kline_exchange_report_tool.py` (2). **Remaining:** session
  2026-09-24T162507 is only on the Pi, so the test runs on a builder modelled
  on its bytes, not on the capture; pull it, run the tool on it, and commit a
  slice as the fixture.
- [x] **K01.3 — Bit-flag and value discovery helpers.** For flag words, list
  each bit's transitions with times, so an operator action ("brake at 16:42:55,
  VDC OFF at 16:43:35") can be matched to a bit. For values, pair a candidate
  DID with a reference channel through `compare_channels` (same-instant, per-sample
  ratio), never by aggregates.

  **Evidence (2026-09-25):** `kline_exchanges.bit_transitions` /
  `transitions_between` (one entry per bit per change, with the word before
  and after) and `pair_value` (`compare_channels` plus the per-sample ratio's
  median/range/spread; `kline:<module>:<hex>` reads as raw counts through
  `channels._kline_series`); both on the tool as `--bits` / `--pair
  CANDIDATE=REFERENCE --within`. Tests:
  `test_kline_exchanges.py::test_each_bit_transition_is_listed_with_its_time`,
  `::test_a_value_is_paired_by_same_instant_ratio` (ratio 3.125 recovered,
  24 pairs), `::test_pairing_refuses_rather_than_matching_aggregates`
  (constant signal, pairs too far apart).
- [~] **K01.4 — Bind the ABS channels with known answers.** Profile data
  (`data/nissan-xterra-2008/kline.yaml`, ABS section), each entry with its
  known-answer evidence:
  - engine speed 0x1202: about 3.125 rpm/count, paired against the ECM's CAN
    rpm (done 2026-09-24, 21 pairs, 0.28 % spread);
  - decel G 0x111A: (raw − 128) × 0.01 G (matches the display; confirm under
    braking);
  - battery 0x1118: about 0.078 V/count (two points; confirm against a
    same-instant voltage);
  - stop-lamp switch: 0x1303 bit 0x0200 (brake hold, 2026-09-24);
  - VDC-off state: 0x1303 bit 0x4000 (two OFF-switch presses, 2026-09-24);
  - wheel speeds 0x1102–0x1105 and yaw rate 0x1210: **need a drive** with the
    screen open. Pair them against the CAN 0x6F7 per-wheel speeds and GPS
    speed/heading rate, including the wheel order and the yaw sign from left
    and right turns.

  **Partial (2026-09-25):** `kline.yaml` `modules: 0x28 ABS` carries every
  cycle DID's width with its evidence, and the bindings a known answer backs:
  ENGINE_SPEED 0x1202 ×3.125 (confirmed), DECEL_G 0x111A and BATTERY_VOLTAGE
  0x1118 (provisional), flags STOP_LAMP_SWITCH 0x0200 and OFF_SWITCH 0x4000 of
  0x1303 (confirmed), OFF_LAMP 0x2000 and SLIP_LIGHT 0x8000 (provisional,
  operator-relayed). The schema (`profile.ModuleDid`) refuses a binding
  without `known_answer`/`status`, a multi-bit or over-wide flag mask.
  Tests: `test_kline_profile.py::test_the_abs_bindings_decode_their_known_answers`
  and the four refusal tests beside it. **Remaining (truck):** the drive with
  the screen open for wheel speeds 0x1102–0x1105 and yaw 0x1210 (still
  hypotheses, unbound), decel G under braking, battery against a same-instant
  voltage; the 0x1202 pairing re-derived offline once 2026-09-24T162507 is
  pulled (only on the Pi).
- [~] **K01.5 — An active K-line reader for non-ECM modules.** The sniffer only
  sees what a scan tool asks for. `KlineCollector._session` already runs a
  command-mode reader (wake, define, local-id poll,
  `claim_ecm_session("kline")`), but for the ECM (0x10, from `kline.yaml`)
  only.
  - Generalize it to a per-module target from profile data. Start
    communication with a module, read its bound DIDs at a configured rate,
    keep it alive with tester-present, and stop cleanly.
  - Arbitrate per module: claim the module's session the way CAN diagnostics
    claim the ECM's, refuse or pause while a foreign tool owns it, and never
    run while passive mode is armed.
  - Respect the reply-size limits (the ECM caps a local-id reply at about 61
    bytes; `7F AC 12` beyond that, per the vehicle skill's K-line notes).
  - A capture profile names module channels (for example
    `kline: [ABS:WHEEL_SPEED_FL, …]`); `default` stays unchanged until the
    operator opts in.
  - Test against a scripted K-line module; then one KOEO read on the truck
    (a truck item).

  **Partial (2026-09-25):** `plan.resolve_module_plan` resolves a capture
  profile's `kline:` list to one module (`ABS:ENGINE_SPEED`, `ABS:1102`; bare
  = ECM), refusing two modules, unknown names, a module with no `reader`, a DID
  with no recorded width, and an ECM definition past
  `max_local_id_reply_bytes` (61, "7F AC 12"). `KlineCollector` starts
  communication with the module's address (`transport.start_communication(target)`
  refuses a reply from another source), reads the ECM by `AC`/`21` and any
  other module by one `22` per DID checked against its recorded width, sends
  `3E` at the module's `tester_present_seconds` through a pause, refuses a
  pause past P3max for a module never seen answering `3E`, and stops cleanly.
  Arbitration: `kline.sessions.MODULE_SESSIONS` per module (reset by
  `process_state`); the ECM also claims `tx_gate`'s ECM session, the ABS does
  not pause CAN; nothing starts while any foreign claim exists or
  `tx_gate.passive_armed()`, re-checked after the plan resolves and before the
  transceiver opens; arming passive mid-session ends it with no further byte,
  not even StopCommunication (the module times out at P3max), and
  `KlineTransport` itself refuses every wake-up and frame while passive is armed
  (`PassiveArmedError`); a garbled echo hands the line to the other tester. `default` has no `kline:` list
  (unchanged). Tests: `test_kline_collector.py` (K01.5 section, 9 tests incl.
  `test_an_abs_read_polls_each_did_by_common_id_and_leaves_can_alone`,
  `test_arming_passive_mid_session_sends_nothing_more`,
  `test_arming_passive_after_the_plan_resolves_sends_nothing`,
  `test_tester_present_keeps_a_paused_session_open`), `test_kline_plan.py` (12 cases),
  `test_kline_sessions.py` (5), `test_kline_transport.py::test_start_communication_addresses_the_module_asked_for`,
  `test_tx_gate.py::test_passive_armed_is_the_operators_switch_not_a_session_claim`,
  `test_kline_transport.py::test_armed_passive_refuses_every_byte_by_name`.
  **Remaining (truck):** one KOEO read of the ABS by CANary itself.
- [ ] **K01.6 — Truck verification.** With the reader on, a short drive logs
  ABS wheel speeds and yaw at the configured rate without a scan tool, with no
  loss on the ECM/CAN side (reads/s, missed ticks), and passive mode still
  stops it.

## Acceptance

K01.1–K01.3 are desk work. K01.4 needs one drive with the scan tool on the
ABS screen. K01.5 is desk work plus one KOEO check. K01.6 needs the truck.
