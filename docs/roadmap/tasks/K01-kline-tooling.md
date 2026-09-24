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

- [ ] **K01.1 — Attribute every K-line frame to its module.** After a KWP2000
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
- [ ] **K01.2 — An exchange report for a sniffed session.** One command over
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
- [ ] **K01.3 — Bit-flag and value discovery helpers.** For flag words, list
  each bit's transitions with times, so an operator action ("brake at 16:42:55,
  VDC OFF at 16:43:35") can be matched to a bit. For values, pair a candidate
  DID with a reference channel through `compare_channels` (same-instant, per-sample
  ratio), never by aggregates.
- [ ] **K01.4 — Bind the ABS channels with known answers.** Profile data
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
- [ ] **K01.5 — An active K-line reader for non-ECM modules.** The sniffer only
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
    bytes; `7F AC 12` beyond that, per `docs/uprev-usb-kline-research.md`).
  - A capture profile names module channels (for example
    `kline: [ABS:WHEEL_SPEED_FL, …]`); `default` stays unchanged until the
    operator opts in.
  - Test against a scripted K-line module; then one KOEO read on the truck
    (a truck item).
- [ ] **K01.6 — Truck verification.** With the reader on, a short drive logs
  ABS wheel speeds and yaw at the configured rate without a scan tool, with no
  loss on the ECM/CAN side (reads/s, missed ticks), and passive mode still
  stops it.

## Acceptance

K01.1–K01.3 are desk work. K01.4 needs one drive with the scan tool on the
ABS screen. K01.5 is desk work plus one KOEO check. K01.6 needs the truck.
