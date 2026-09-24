# UpRev/Cipher USB K-line protocol research

Status: USB framing, PyUSB transport, cold-start ECU-session bootstrap,
supported-DID discovery, and dynamic-stream reads are proven against the live
vehicle. This is diagnostic-streaming work, not a reflash path.

## Why this exists

UpRev datalogging is K-line traffic and is invisible to CANary's CAN capture.
On 2026-09-20, Windows USB traffic between ROM Editor and the UpRev/Cipher
cable was captured and decoded. A standalone Python probe then opened the
cable and completed its local handshake without ROM Editor.

Do not conflate this with UpRev reflashing, which uses CAN. Everything here is
diagnostic streaming, and this collector stays a read path. AGENTS.md's policy
on non-volatile writes applies unchanged: explicit operator permission per
operation, and never with the vehicle moving or the engine running. Captured
reflashing traffic does not belong in this collector or its fixtures; the
flash research lives in `docs/skunkworks/`.

## Hardware and ownership

- USB ID: `0403:ccb8`
- Product: `Cipher USB Cable (Nissan)`
- Windows name: `UpRev Vehicle Interface`
- Captured serial: `12345678`
- USB 2.0, one vendor-class (`0xff`) interface
- Bulk IN `0x81`, bulk OUT `0x02`, 64-byte maximum packets
- Windows API used by UpRev: FTDI D2XX, not a COM port
- D2XX access is exclusive; ROM Editor must be fully exited first

The installed UpRev 64-bit D2XX DLL was sufficient for a standalone cable
probe. It enumerated and opened the device, sent cable-local payload `07`,
received `06`, and closed it. That exchange does not address the ECU.

## Evidence files

The packet captures and Windows-only analysis utilities are intentionally
outside Git:

| Capture | SHA-256 | Scope |
|---|---|---|
| `uprev-kline-rpm-coolant.pcapng` | `1d61dc1be2a70dac81603d2b518f4c09bd8cc3ad5be2423b5fabb9ebfc158f91` | startup plus coolant/RPM logging |
| `uprev-kline-stream-layout.pcapng` | `cef8591f820af5a2ee654a24de9485994072fe80820d50a24325e1b66d56254f` | coolant-only, RPM-only, then combined |
| `uprev-bfs.pcapng` | `9a2f5805f0fa2d7a0b9db840061cf1beec094b2e7a19f94462eedb1f8a684b21` | BFS held near the displayed 13.6 ms value |
| `uprev-target_afr.pcapng` | `64422d8fdaac9df98dbd79862ab18872b8ef66b3c96069e6114150c9219e4525` | Target AFR, using two absolute-RAM byte descriptors |
| `uprev-bfs-2.pcapng` | `72cadf53e26419f0299a88656ff68a3067039415ff412f5930a252aee88404ea` | BFS varied over a wide range |
| `uprev-ign_timing.pcapng` | `bb31112ccf37a6b3be9ed0c24702ab7d317101ec0561a45780bceeba7b4292dc` | ignition timing selected; capture begins after its definition |
| `uprev-afr_wb_bank1.pcapng` | `66f9d9bd76eacc9419dd546e8312be1cc5031f305dd5b2711b6a968cd23ab296` | A/F wideband Bank 1, directly defining DID `0x1225` |
| `uprev-afr_wb_bank2.pcapng` | `2d5367edc69f61948010675475081d2e240a5b153619fab507a4eb41c7659b64` | A/F wideband Bank 2; capture begins after its definition |
| `uprev-inj_pulse_b1.pcapng` | `fe39ed2584bfb7c4e5aed6129ed4fb9e65202b12f4f111ecb3d53163789c486c` | injector pulse Bank 1, directly defining DID `0x1206` |
| `uprev-inj_pulse_b2.pcapng` | `0b32c43d4bac84fd17e8a6894822a599498a97883aab7ba4ed2f4410e60a3dd8` | injector pulse Bank 2, directly defining DID `0x1207` |
| `uprev-inj_pulse-af_wb-bfs-afr.pcapng` | `bc31a91fdf5ab45838fd951fb408d40850b4f706c5e3e8dddb6cbae9f57a8a14` | combined Target AFR, both injector pulses, both A/F sensors, and BFS |

Windows location:

```text
C:\Users\thebo\Documents\Codex\2026-09-20\i-hav\outputs
```

WSL location:

```text
/mnt/c/Users/thebo/Documents/Codex/2026-09-20/i-hav/outputs
```

That directory also contains `parse-uprev-usb.py`, `uprev-cable-probe.py`,
and the field report `UPREV_KLINE_REVERSE_ENGINEERING.md`.

Capture facts:

- First: 220,154 packets, 13,740,296 bytes, 274.332186 s; 1,668 OUT and
  1,673 IN UPRV messages; zero discarded bytes or envelope-checksum failures.
- Stream layout: 106,284 packets, 6,687,748 bytes, 78.242928 s; 1,797 OUT and
  1,799 IN UPRV messages; zero discarded bytes or envelope-checksum failures.
- Target AFR: 25,214 packets, 1,593,264 bytes, 16.696650 s; 538 OUT and
  537 IN UPRV messages; zero discarded bytes or envelope-checksum failures.
- Varying BFS: 18,346 packets, 1,160,460 bytes, 11.963129 s; 403 OUT and
  405 IN UPRV messages; zero discarded bytes or envelope-checksum failures.
- Ignition timing: 22,814 packets, 1,442,092 bytes, 15.026172 s; 493 OUT and
  494 IN UPRV messages; zero discarded bytes or envelope-checksum failures.
- A/F WB Bank 1: 23,248 packets, 1,468,616 bytes, 15.636464 s; 485 OUT and
  485 IN UPRV messages; zero discarded bytes or envelope-checksum failures.
- A/F WB Bank 2: 26,464 packets, 1,677,376 bytes, 15.935805 s; 665 OUT and
  665 IN UPRV messages; zero discarded bytes or envelope-checksum failures.
- Injector pulse Bank 1: 27,388 packets, 1,732,848 bytes, 17.545482 s;
  630 OUT and 631 IN UPRV messages; zero discarded bytes or envelope-checksum
  failures.
- Injector pulse Bank 2: 22,162 packets, 1,401,560 bytes, 14.500860 s;
  490 OUT and 491 IN UPRV messages; zero discarded bytes or envelope-checksum
  failures.
- Combined fuel/AFR: 26,523 packets, 1,672,192 bytes, 20.135170 s; 408 OUT
  and 409 IN UPRV messages; zero discarded bytes or envelope-checksum
  failures. All 387 combined stream replies also passed the inner checksum.

Compact, sanitized application transcripts and an aggregate descriptor/control
transfer fixture are committed under `tests/fixtures/uprev_usb/`. The PCAPNG
files remain the source evidence and preserve every individual transfer.

## Windows-to-WSL hardware handoff

Close UpRev, Wireshark, and USBPcap before moving the cable. From elevated
PowerShell:

```powershell
usbipd bind --busid 2-5 --force
```

`--force` is necessary because USBPcap's filter is reported as incompatible.
The bus ID is host-enumeration state; verify it rather than assuming `2-5`
after reconnect/reboot. Then, from ordinary PowerShell while WSL is open:

```powershell
usbipd attach --wsl --busid 2-5
```

Inside WSL:

```bash
lsusb -d 0403:ccb8
sudo lsusb -v -d 0403:ccb8
```

An attached USBIP device is unavailable to Windows until detached. This is
therefore an explicit ownership handoff, like D2XX's exclusive open.

Use PyUSB/libusb for the first Linux transport rather than `/dev/ttyUSB0`.
The custom PID is not in the current WSL kernel's `ftdi_sio` alias table, and
direct libusb preserves exact control transfers and USB packet boundaries.
Re-evaluate PyFtdi/libftdi only after the initialization behavior is known.

## FTDI USB layer

Every bulk-IN USB packet begins with two FTDI modem-status bytes. Remove those
two bytes from each packet before concatenating application bytes. Do not parse
USB packets independently: one UPRV message may span packets, and several
messages may be coalesced.

Captured vendor control requests from the first session:

| bmRequestType | request | value | index | length | count | Meaning |
|---:|---:|---:|---:|---:|---:|---|
| `0x40` | `0` | `0` | `0` | `0` | 1 | FTDI reset |
| `0xc0` | `5` | `0` | `0` | `2` | 1 | FTDI modem-status read |
| `0x40` | `9` | `2` | `0` | `0` | 1 | latency timer = 2 ms |
| `0x40` | `0` | `1` | `0` | `0` | 10,014 | purge RX |

There was no request `3` (FTDI baud-rate programming), no line-characteristics
request, and no captured flow-control/DTR/RTS setup. This strongly indicates
that the cable's own MCU runs the K-line state machine; the FTDI channel carries
the proprietary `UPRV` protocol, not transparent UART bytes.

The high purge count is real, not a recommendation. Determine the minimum
required behavior experimentally instead of reproducing 10,014 purges.

## Live Linux result (2026-09-20)

`src/collection/uprev_usb.py` opens the USBIP-attached cable through PyUSB,
performs the required FTDI reset/status/latency/purge controls, strips the two
status bytes from every bulk-IN packet, and reassembles UPRV envelopes across
USB packet boundaries. `src/collection/uprev_session.py` reproduces the
captured read-only session bootstrap; `tools/uprev_kline_probe.py` exposes
supported-DID discovery and dynamic streams without a raw-request escape
hatch.

The progression was checked against known answers before novel probes:

- cable-local `07 -> 06` works;
- `10 85` returns `50 85` from the ECM;
- the captured capability bootstrap enters `10 C0` and reads known DIDs;
- `0x1101` returned raw `0x8d` = 91 °C;
- `0x1201` returned raw `0x00d8` = 2,700 rpm at the same instant CANary
  reported approximately 2,700 rpm;
- seven definitions of at most 12 DIDs streamed all 75 ECU-advertised DIDs:
  7,000 replies, no timeout, no partial reply, and no checksum failure.

A single definition containing all 75 DIDs is too large for this cable/session
and timed out. Chunking at 12 is proven and is the CLI default. This is a
measured transport constraint, not a vehicle-independent DID limit.

The Pi's CAN diagnostic client and the K-line logger must not both own the ECM
diagnostic session. A simultaneous attempt caused negative responses and
session loss even though the physical transports differ; the ECM session
state is shared internally. Put CANary in TTL-bounded passive mode while the
cable owns K-line. Raw broadcast CAN, decoded broadcast channels, GPS, and
storage continue normally and provide the simultaneous reference instrument.

A representative isolated capture and comparison from the repository root:

```bash
# On the Pi control surface first: passive=true with a bounded TTL.
.venv/bin/python tools/uprev_kline_probe.py \
  --stream 0x1201:2 --stream 0x1101:1 --samples 1000 \
  --out /tmp/uprev.jsonl --quiet

.venv/bin/python tools/analyze_uprev_kline_capture.py \
  --capture /tmp/uprev.jsonl --session /path/to/simultaneous/pi/session \
  --tolerance 50ms --out /tmp/uprev-analysis.json
```

The analyzer preserves every raw observation and routes comparisons through
`compare_channels`; `n_pairs` is a count of one-to-one acquisition-time pairs,
and a high r² without a stable scale is not reported as an identity.

## UPRV envelope

The pure protocol implementation is `src/collection/uprev_protocol.py`.

```text
offset  size  meaning
0       4     ASCII "UPRV" (55 50 52 56)
4       2     payload length, unsigned big-endian
6       4     reserved/unknown; zero in both captures
10      2     sum(payload) & 0xffff, unsigned big-endian
12      N     payload
```

Cable-local ping: OUT payload `07`, IN payload `06`.

## Dynamic K-line stream

The definition payload is:

```text
02 00 ac 81 [02 DID_HI DID_LO]...
```

Each `02` is the captured descriptor width for a 16-bit identifier. Descriptor
order is reply-value order. Observed definitions:

```text
coolant run  02 00 ac 81 02 11 01 02 11 25
RPM run      02 00 ac 81 02 12 01 02 11 25
both run     02 00 ac 81 02 12 01 02 11 01 02 11 25
earlier run  02 00 ac 81 02 11 01 02 12 01
BFS          02 00 ac 81 02 12 08
A/F WB B1    02 00 ac 81 02 12 25
inj pulse B1 02 00 ac 81 02 12 06
inj pulse B2 02 00 ac 81 02 12 07
```

The later combined capture proves that RAM and DID descriptors may coexist and
that descriptor order is reply-value order across descriptor types:

```text
02 00 ac 81
83 ff ff 26 be  83 ff ff 26 bf
02 12 07  02 12 25  02 12 26  02 12 08  02 12 06
```

This is Target AFR's two RAM bytes first, followed by DIDs `0x1207`, `0x1225`,
`0x1226`, `0x1208`, and `0x1206`. The individual A/F WB Bank 2 and ignition
captures started after logging was already active, so their definitions are
absent from those files. The combined capture now resolves Bank 2 as `0x1226`;
ignition was not included and remains unresolved.

Definitions can also contain absolute-memory byte descriptors. Target AFR used
two consecutive descriptors, each encoded as `83` followed by a 32-bit
big-endian address:

```text
02 00 ac 81 83 ff ff 26 be 83 ff ff 26 bf
```

The reply bytes are concatenated in descriptor order, just as they are for DID
descriptors. This definition returned `40 00` in all 525 samples. Do not assume
that every stream descriptor is the three-byte DID form when building stream
definitions or reply layouts.

The earlier run's positive response was `66 02 ec 81 6f`. Once defined, every
sample used the same poll: `02 00 21 81 04 01`.

Example reply:

```text
66 05 61 81 4e 00 60 95
   LL --------data-- KK
```

- `LL` counts `61 81` plus value bytes.
- Values are concatenated in definition order and are not self-delimiting.
  Widths must be known before parsing a multi-DID reply.
- `KK = sum(LL, 61, 81, data...) & 0xff`.

For this example, `05 + 61 + 81 + 4e + 00 + 60 = 0x195`, so `KK=95`.
All 1,682 isolated samples passed this inner checksum.

## Observed DIDs

These identities/scales are vehicle/UpRev-definition facts and must not become
universal shared-code assumptions:

| DID | Width | UpRev meaning | Captured conversion |
|---|---:|---|---|
| `0x1101` | 1 | coolant temperature | `raw - 50` °C |
| `0x1201` | 2, big-endian | engine speed | `raw * 12.5` rpm |
| `0x1208` | 2, big-endian | BFS | approximately `raw / 2040..2048` ms; exact divisor unresolved |
| `0x1225` | 2, big-endian | A/F WB Bank 1 | `raw * 0.005` V (existing independently pinned scale) |
| `0x1206` | 2, big-endian | injector pulse Bank 1 | `raw * 0.01` ms (directly pinned against UpRev display export) |
| `0x1207` | 2, big-endian | injector pulse Bank 2 | `raw * 0.01` ms (directly pinned against UpRev display export) |
| `0x1125` | 1 | unknown | raw 95–99 in this capture |

The coolant conversion is pinned to the UpRev display, not inferred from a
similarly numbered CAN Mode-22 DID. Raw `0x4e` and RPM raw `0x0060` displayed
as 82 °F and 1,200 rpm: `0x4e - 50 = 28 °C = 82.4 °F` and
`0x0060 * 12.5 = 1200`.

| Phase | Definition order | Valid samples | Rate | Values |
|---|---|---:|---:|---|
| coolant | `1101,1125` | 505 | 48.267 Hz | coolant 92 °C; `1125=95` |
| RPM | `1201,1125` | 671 | 48.461 Hz | 737.5–762.5 rpm; `1125=95` |
| both | `1201,1101,1125` | 506 | 48.196 Hz | 750–1912.5 rpm; coolant 92 °C; `1125=95..99` |

Median request/reply latency was about 19.5 ms. The first combined capture
delivered 1,041 samples at 47.725 Hz with 19.093 ms median latency.

The first BFS capture held raw `0x6d4c` (27,980) for 507 samples while ROM
Editor showed approximately 13.6 ms. A second capture varied BFS and returned
399 values from 3,265 through 25,657, with 398 request/reply pairs at
47.217 Hz. A binary fixed-point scale of `raw / 2048` maps those observations
to 1.594–12.528 ms and maps the first capture to 13.662 ms. The independent
era-ratio evidence in `data/dids.yaml` estimates approximately 2,040 counts/ms
and explicitly includes 2,048 within its error. These PCAPs do not record the
GUI's value text at each instant and therefore cannot distinguish 2,040 from
2,048. Keep the conversion provisional until several exact, simultaneous ROM
Editor display values are recorded beside their raw samples.

Target AFR did not use a DID in its captured definition. It read bytes at
`0xffff26be` and `0xffff26bf` and returned big-endian `0x4000` in all 525
samples at 48.636 Hz. Subsequent direct Linux experiments resolved the
conversion and direction, as described below.

### Live Target AFR RAM result

The Linux probe successfully mixed ordinary DID descriptors and absolute-RAM
descriptors in one definition. This is not an aggregate comparison: each
reply contained all values at the same acquisition instant.

A 5,000-reply mixed stream included RPM, throttle, air charge, injector pulse,
wideband voltage, and the RAM word. It observed eight target states between
`0x0000` and `0x4300`. Every one of the 98 zero-target replies coincided with
injector pulse `0x1206` raw 54-57, the independently established DFCO floor,
while RPM remained 2,687-2,950 rpm and throttle was closed. Thus zero is the
no-fuel/fuel-cut state, not a zero-AFR command.

The decisive known-answer run placed DID `0x1134` and the RAM word in the same
5,000 replies. The following piecewise relation held for every sample, with no
exceptions:

```text
0x1134 raw = max(64, floor(target_ram_word / 256))
```

The run covered `0x0000`, `0x4000`, `0x4080`, `0x4100`, `0x4150`, `0x4180`,
`0x4380`, and `0x4a80`. In particular, `0x4a80 / 16384 = 1.1640625` maps to
DID raw 74, exactly reproducing the independently observed Mode-01 PID `0x44`
commanded-phi value associated with `0x1134=74`. This pins the live word as a
Q14 final commanded equivalence ratio in the vehicle's verified phi
convention:

```text
phi = target_ram_word / 16384
target_AFR = fuel_stoich_AFR / phi     # target_ram_word != 0
target_ram_word == 0                   # fuel cut / no AFR target
```

For gasoline stoich 14.7, `0x4a80` is a 12.63 AFR target. DID `0x1134` is its
floor/clamped, low-resolution rich-side view and intentionally stays at 64
when the final target is zero. A straight-line `compare_channels` fit is
unresolved (`r2=0.078`) precisely because the measured transform is piecewise
and clamped; the raw per-reply equality above is the applicable result.

The Windows combined capture independently reproduced this result over 387
same-reply observations: the RAM word covered zero plus 21 nonzero states from
`0x4000` through `0x4a00` (phi 1.000000–1.156250). All 131 zero-target samples
put both injector registers at raw 45–49 while both A/F sensors reached the
5.065 V lean rail, directly identifying fuel cut.

The addresses are ROM/calibration-specific vehicle-profile facts. Do not put
them in shared protocol code or assume another ECU exposes the same RAM map.

### CAN collection parity

The UpRev discoveries are not limited to the USB/K-line cable. Every high-value
fuel channel in the combined definition is reachable through the ECM's CAN
diagnostic endpoint (`0x7E0` request, `0x7E8` response):

| UpRev item | CAN request | CANary status |
|---|---|---|
| injector pulse Bank 1/2 | Mode 22 DIDs `0x1206` / `0x1207` | confirmed, `raw * 0.01 ms` |
| BFS / air-charge coordinate | Mode 22 DID `0x1208` | confirmed identity; exact `2040` vs `2048` counts/ms remains provisional |
| A/F WB Bank 1/2 | Mode 22 DIDs `0x1225` / `0x1226` | confirmed, `raw * 0.005 V` |
| coarse rich-side target phi | Mode 22 DID `0x1134` | confirmed as a clamped/floored view, not the final target |
| final Target AFR / phi | service `0x23`, KWP32 body `FF FF 26 BE 00 02` | positive response; Q14 word, zero means fuel cut |

The decisive service-`0x23` check was made through the Pi's normal ISO-TP CAN
client while the daemon stayed passive. After session `0xC0`, the KWP32 request
returned payload `4000`; the KWP24 and UDS-ALFID forms both returned NRC `0x12`.
The result is preserved as
`/data/canary/_uprev-usb/2026-09-20/can-target-afr-memory-probe.json` on the Pi.
This proves transport parity for the exact target word; it is not yet a
first-class recurring CANary channel: `data/dids.yaml` carries a `memory:`
catalog, the scheduler issues service-0x23 reads inside the Mode-22 session
window, and values land as `mem:<NAME>` telemetry. Only catalogued addresses
are polled -- arbitrary `0x23` bodies remain a probe operation, and service
0x3D (WriteMemoryByAddress) is not implemented at all.

These are **active diagnostic CAN reads**, not passive broadcast bindings. No
passive frame has been proven to contain the same injector, BFS, A/F-voltage,
or final-target values. In particular, aggregate similarity is not evidence of
a broadcast duplicate; any future binding must pass same-instant
`compare_channels` validation.

The transports also cannot be collected actively at the same time on this ECM.
During a controlled test, a K-line stream kept responding while the Pi's CAN
Mode 01 collection went silent for roughly 132 seconds; passive broadcast CAN
continued normally. A production capture should therefore use either UpRev
K-line diagnostics or CAN diagnostics for the active ECM session, while the
passive broadcast collector may run beside either.

`tools/can_memory_log.py` now provides the recurring read path needed for
research. It emits only session control and the proven KWP32 service-`0x23`
form, requires the daemon to remain passive, timestamps every response, and
can interleave named Mode 22 DIDs after each memory read. A 32-byte window at
`0xFFFF26B0` completed 1,000/1,000 memory reads plus 5,000/5,000 DID reads in
35.2 seconds on the road. The window exposes two similar 14-byte bank records,
but the sanctioned same-instant comparisons did not identify their changing
fields: a short-window injector fit at r2 0.80 fell to r2 0.26 over the longer
run, and every candidate scale was `drifting` or `unresolved`. Nothing in that
block besides the already-proven Target AFR word is promoted.

## Additional single-channel captures (2026-09-20)

The five later files contain 2,723 checksum-valid stream request/reply pairs.
Every reply passes the inner checksum as well as the outer UPRV checksum.
The capture filename records the operator-selected UpRev item; the USB stream
itself contains values and descriptors, not the GUI label or converted display
text.

| UpRev selection | Direct definition in file | Valid pairs | Rate | Median latency | Raw range | Supported interpretation |
|---|---|---:|---:|---:|---:|---|
| IGN TIMING (BTDC) | missing | 484 | 47.577 Hz | 19.454 ms | 69–112, 1 byte | `110 - raw` gives -2..41 degrees, but identifier and scale remain unproven from this file |
| A/F WB Bank 1 | DID `0x1225` | 472 | 46.244 Hz | 19.478 ms | 217–749 | existing `raw * 0.005` scale gives 1.085–3.745 V |
| A/F WB Bank 2 | missing here; later combined definition proves `0x1226` | 664 | 47.211 Hz | 19.463 ms | 276–696 | existing `raw * 0.005` scale gives 1.380–3.480 V |
| injector pulse Bank 1 | DID `0x1206` | 621 | 46.796 Hz | 19.505 ms | 156–803 | `raw * 0.01` gives 1.56–8.03 ms |
| injector pulse Bank 2 | DID `0x1207` | 482 | 46.149 Hz | 19.496 ms | 51–755 | `raw * 0.01` gives 0.51–7.55 ms |

The Bank 1 A/F capture is direct transport-level confirmation that UpRev's
selected item uses DID `0x1225`; its voltage scale was already independently
pinned by NT510 and earlier cross-transport comparisons. The later combined
definition directly confirms Bank 2 as the established paired DID `0x1226`.

The injector captures directly bind UpRev's operator-selected Bank 1 and Bank
2 items to `0x1206` and `0x1207`, respectively. This is stronger evidence for
the bank ordering than the prior fuel-behavior work, which had established an
injected-pulse pair but deliberately left its bank ordering open. UpRev also
wrote `autosave_2026-09-20 13_48_55Z.csv` during the Bank 2 capture. Its 481
displayed `INJ PULSE-B2 (ms)` rows align in sequence with 481 PCAP replies;
every display value equals `raw * 0.01` within the CSV's 0.05 ms precision,
and 458 equal Python's one-decimal rounding exactly. The sanctioned
same-instant comparison at a 10 ms tolerance produced 312 pairs,
`r2=0.995112`, slope `0.0100504 ms/count`, and offset `-0.00325 ms`.
That independently pins the shared injector scale to `0.01 ms/count`. The
CSV SHA-256 is
`d154b64c4db5931e3bdaf7edf82dda63a7309396a52ad6353937d432a3f69b2b`.

The ignition capture begins with a stream reply before its first captured
poll, proving that capture began mid-stream. Its one-byte raw values produce a
physically plausible -2..41 degree range under the older `110 - raw` candidate
conversion. That coincidence does not identify the descriptor: the repository
already documents why steady-state correlation with the one-byte A/F-heater
block is misleading. Re-capture ignition with USBPcap running before logging
starts; preferably include a fast throttle transient and record a few exact
UpRev display/raw pairs.

## Combined fuel/AFR stream

The first combined reply was:

```text
66 0e 61 81 40 00 00 c0 01 a8 01 ba 14 ec 00 c7 1b
            -----  ----- ----- ----- ----- -----
            target 1207  1225  1226  1208  1206
```

The target occupies two one-byte RAM descriptors; every DID occupies two
bytes. Across 387 replies at 31.951 Hz, median request/reply latency was
29.564 ms (24.011–49.497 ms). This compares with about 46–49 Hz and 19.5 ms
for the single-value streams, quantifying the throughput cost of the larger
ECU read rather than indicating packet loss.

| Value | Raw range | Unique values | Interpretation |
|---|---:|---:|---|
| Target AFR RAM word | 0–18,944 | 22 | zero in fuel cut; otherwise Q14 phi 1.000000–1.156250 |
| `0x1207` injector B2 | 45–1,128 | 146 | 0.45–11.28 ms at x0.01 ms/count |
| `0x1225` A/F B1 | 68–1,013 | 205 | 0.340–5.065 V |
| `0x1226` A/F B2 | 57–1,013 | 197 | 0.285–5.065 V |
| `0x1208` BFS | 3,355–30,218 | 340 | approximately 1.64–14.81 ms under the unresolved /2040..2048 scale |
| `0x1206` injector B1 | 45–1,146 | 156 | 0.45–11.46 ms at x0.01 ms/count |

The two injector registers track at Pearson `r=0.999347`; their raw difference
has median zero. The A/F voltage pair tracks at `r=0.994601`, also with median
raw difference zero. During the 131 zero-target samples, both injector values
had median raw 47 and both A/F sensors had median raw 1,013 (5.065 V lean
rail), directly identifying those intervals as fuel cut. Meanwhile BFS
remained nonzero at median raw 3,620. This single synchronized stream therefore
reproduces the repository's prior conclusions that `0x1206/0x1207` are the
injected pulses while `0x1208` is the air-charge/base-fuel-schedule coordinate
and does not collapse during fuel cut.

`0x1125` appeared in all three isolated runs, but the earlier stream proves it
is not inherently required. Its identity remains open.

## Session observations

Stopping a log emitted payload `01 10`, then replies `06` and `7f 01`. ROM
Editor subsequently resumed this background poll:

```text
OUT  02 00 22 11 03 04 01
IN   66 04 62 11 03 xx kk
```

The startup capture includes cable configuration/discovery and ECU session
traffic such as:

```text
07 -> 06
08 -> cable metadata
06 -> 06
32 -> cable metadata
03 07 e0 07 e8 -> 06
02 00 10 85 -> 66 02 50 85
01 10 -> 06, 7f 01
...
02 00 10 c0 -> 66 02 50 c0
```

The first working bootstrap deliberately retains captured capability probes,
including expected negative replies, because deleting them before a controlled
minimization made cold starts fail. Minimize one operation at a time against a
known-answer DID; do not replace the sequence with the entire Windows startup
transcript.

## On-wire view (CANary-HAT sniff, 2026-09-23)

The USB captures above show what ROM Editor asks the cable for; the cable's own
MCU runs the K-line state machine, so they never showed the wire. The CANary-HAT
rev 2.0 was fitted to the same OBD port and sniffed K-line passively (TLIN1027
in normal mode, commander pull-up off, nothing transmitted) through ROM Editor's
startup ECU check and a logging run. 93 s, 37,403 bytes, 1,462 frames, **every
checksum good**; UpRev behaved normally. Tools: `kline_edges.py` (kernel-
timestamped RXD edges) and `kline_decode.py` in `hardware/canary-hat/firmware/`.

**Physical layer.** 10400 baud 8N1 (ISO 14230). Two wake-ups, alternating while
the cable retries: a standard fast init (25.0 ms low, 25.1 ms high, request bytes
10 ms apart) that this ECM never answered, and **518.9 ms low, 18.1 ms high,
request bytes back-to-back** — every answered StartCommunication followed this
one.

**Addressing.** `81 10 FC 81 0E` → `83 FC 10 C1 5D 8F 3C`: ECM `0x10`, tester
**`0xFC`**, key bytes `5D 8F`. After that, frames carry no address bytes:
`<len> <data…> <sum & 0xff>`.

**How the USB envelope maps onto the wire.** A USB IN payload is `0x66`
followed by the raw K-line frame: the captured positive response
`66 02 ec 81 6f` is the wire frame `02 EC 81 6F`, and the `LL`/`KK` fields in
"Dynamic K-line stream" are simply the K-line length byte and checksum. On the
way out the cable strips a `02 00` prefix and adds length and checksum: the USB
poll `02 00 21 81 04 01` corresponds to wire requests of the form
`04 21 8x 04 01 <cs>`.

**Session sequence seen on the wire.**

| Phase | Tester → ECM | ECM → tester |
|---|---|---|
| ECU check | `02 A1 00 A3`, `02 A2 00 A4` | `03 7F A1 12 35`, `03 7F A2 11 35` (refused) |
| | a read whose reply carries ASCII `UPRV` (`55 50 52 56`) | |
| | `05 22 11 03 04 01 40`, repeated at ~3.6 Hz | `04 62 11 03 9A 14` |
| Logging setup | `AC 81 …` (16-bit RAM descriptors `83 FF FF xx xx`), `AC 82 01 81 …` (RAM + DIDs incl. `12 25`/`12 26`), `AC 83 01 82 …` (DIDs) | `02 EC 81 6F`, `02 EC 83 71` |
| Logging | `04 21 83 04 01 AD` | `34 61 83 …` (52 data bytes), ~9 Hz |

Two points differ from the CAN side and from the USB-only reading:

- `22 11 03 04 01` — UpRev's two-DID read, which this ECM **rejects on CAN**
  (`7F 22 12`) — is **answered on K-line**, with `62 11 03` plus one value byte.
- The logging rate on the wire was ~9 replies/s for a 52-byte set, not 16 Hz.

**The HAT then did it itself** (UpRev unplugged, KOEO): wake-up 518.9/18.1 ms,
`81 10 FC 81 0E` → `83 FC 10 C1 5D 8F 3C`, `05 22 11 03 04 01 40` →
`04 62 11 03 99 13`, `01 82 83` → `01 C2 C3`. So a CANary-native K-line path
needs no UpRev cable at all; this is read-only and volatile, and the
non-volatile-write policy above applies unchanged to anything beyond it.

## Integration boundary

This should become an independent optional `uprev_usb` collector, not another
`obd_source` choice. Broadcast CAN and K-line can be recorded together, but
active CAN diagnostics must be passive while K-line owns the shared ECM
session. Decoded values can enter the existing pipeline through
`TelemetryRecord.uprev(...)`.

The current `UPREV_SOURCE` behavior is import-only. Enabling live records is a
contract change: sweep all source assertions, storage/session assumptions,
query/report callers, and tests rather than changing a single allowlist.

Implemented split:

```text
src/collection/uprev_usb.py       PyUSB/libusb transport
src/collection/uprev_protocol.py  backend-neutral framing/state parser
src/collection/uprev_session.py   read-only ECM bootstrap and DID discovery
tools/uprev_usb_probe.py          enumerate/handshake CLI
tools/uprev_kline_probe.py        DID/RAM/mixed dynamic-stream capture CLI
tools/analyze_uprev_kline_capture.py  paired DID/RAM/CANary comparison CLI
tests/fixtures/uprev_usb/         compact captured transcripts
tests/test_uprev_*.py             offline protocol/transport/session regressions
```

Daemon wiring remains deliberately pending. The research CLI is already a
complete direct-cable acquisition path and keeps captures separate from the
training corpus.

The CAN-side follow-up now has two matching research tools:
`tools/can_memory_log.py` records bounded KWP32 service-0x23 windows with
interleaved DIDs, and `tools/analyze_can_memory_log.py` runs each byte/word
candidate through same-time `compare_channels` pairing. On the ZS44A-k106
ECM, 63-byte reads succeed and 64-byte reads return NRC `0x12`; this is a
vehicle/calibration constraint, not a shared transport constant. A contiguous
`0xFFFF26A0..0xFFFF279B` road sweep found an RPM period counter at
`0xFFFF26AE` (`rpm ~= 6,250,000 / word`) and twelve 1:1 injector-count mirrors
at `0xFFFF2710..0xFFFF2726`. It also rejected a tempting BFS candidate at
`0xFFFF2742` when a later 3,000-pair reproduction changed the fitted scale.
Full statistics and quarantined evidence hashes are in
`data/analyses/2026-09-20-uprev-usb-road.md`.

## Next hardware steps

1. Minimize the working cold-start sequence one operation at a time, preserving
   the known-answer `0x1101`/`0x1201` check on every attempt.
2. Persist vehicle-specific supported-DID widths and conversions in profile
   data; keep them out of shared protocol/transport code.
3. Add timeout, reconnect, partial-frame, and session-loss tests around the
   failures observed on hardware.
4. Decide whether live `uprev` records belong in the daemon at all; the
   isolated research CLI plus paired analysis may be the cleaner boundary.
