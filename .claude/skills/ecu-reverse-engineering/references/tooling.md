# Tooling

The software you actually reach for, and when. From *The Car Hacker's Handbook*
Ch. 3–6 and appendices. All Linux/SocketCAN-centric.

## can-utils (SocketCAN suite)

The core toolkit; works on `canX` (native/HAT), `vcanX` (virtual), `slcanX`
(serial — some tools need it renamed to `canX`).

| Tool | Use |
|---|---|
| `candump` | Sniff raw frames. `-l` logs to file; `-b` bridges one interface to another. |
| `cansend` | Send one frame: `cansend can0 7DF#02010D`. |
| `canplayer` | Replay a `candump -l` logfile. |
| `cansniffer` | **Grouped** view by arbitration ID, highlighting changed bytes; `-c` colorize; interactive `+/-` ID filters with bitmasks. The workhorse for broadcast reversing. |
| `cangen` | Generate traffic — random (fuzzing) or fixed: `cangen -g 1000 -I 7DF -D 013E -L 2 can0` (tester-present heartbeat). |
| `isotpsend` / `isotpsniffer` / `isotprecv` | Message-level ISO-TP (needs `can-isotp` kernel module) — reassembles multi-frame so you see whole VINs/DIDs, not fragments. |
| `isotptun` | Proof-of-concept IP-over-CAN tunnel. |

## CaringCaribou

Python/SocketCAN diagnostic discovery — the clean alternative to blind fuzzing.
Config in `~/.canrc` (`interface`, `channel`). Modules:
- `dcm discovery` — TesterPresent-sweeps every arbitration ID, reports IDs that
  answer (`0x40+svc` valid, `0x7F` error), i.e. finds diagnostic endpoints.
  Restart past a found ID with `-min 0x245`.
- `dcm services <req> <resp>` — probes which UDS services an endpoint supports.

Note (per the book's era): early versions mis-parse legacy sub-`0x0A` modes and
emit spurious `0x00` "services" — treat those as false positives.

## Kayak

Cross-platform GUI over socketcand: colorized grouped capture, record/playback with
an in/out **slider** (fast bisection), and packet documentation in `.kcd` XML that
can drive virtual instrument clusters. Doesn't suppress repeats the way `cansniffer`
does, but the GUI playback controls are its advantage.

## ICSim

Instrument Cluster Simulator (`github.com/zombieCraig/ICSim`, Linux/vcan). Practice
rig producing realistic CAN noise + a controllable cluster. `./icsim vcan0` and
`./controls vcan0`; difficulty `-l 0..3`, randomized IDs/positions via `-r`/`-s`
seed. Use it to rehearse the record→act→playback→bisect method with no vehicle.

## OpenXC

Open CAN-translation platform (Ford-led). A vehicle interface translates proprietary
CAN into named JSON signals; custom firmware (JSON message-set → compiled) exposes
signals you've reversed. `openxc-dump`, `openxc-diag`, `openxc-control`. Read-only
unless a bus/signal is explicitly set writable. See `reversing-broadcast.md`.

## J2534 / firmware tooling

- **J2534 shim DLL** — MITM-logs OEM tool ↔ vehicle API calls (front-door recon).
- **KWP2000 flashers** — serial/USB pre-J2534 flash/diag tools.
- **Disassemblers** — DASMx, Dis51/Dis66k (single-arch), objdump/binutils (rebuild
  all-arch), IDA Pro/Hopper (widest embedded coverage).
- **ROM tools** — WinOLS (table ID + checksums, Bosch Motronic templates),
  `analyze.exe` (value-colored ROM visualizer). See `firmware-reversing.md`.

## In this repo (CANary)

The daemon's control surface does the sniff/store/decode/compare loop without
raw can-utils in the loop:
- `query_telemetry` (DuckDB over stored Parquet, incl. `hex(payload)`) — re-decode
  any historical frame's raw bytes.
- `get_obd_coverage` / `get_dids` / `get_bindings` — what PIDs/DIDs/broadcast
  bindings a session has.
- `compare_channels` (`src.analysis.compare`) — the **only** sanctioned way to
  correlate two channels; pairs same-instant samples and refuses too-few-pairs /
  barely-moving signals (the aggregate-comparison trap).
- `start_replay` — run a stored session through the live pipeline with isolated
  outputs; controlled evaluation without the vehicle.
- `arm_tuning_capture` / capture profiles — targeted PID/DID/Mode-22 captures.

See `AGENTS.md` and `docs/mcp-server.md` for the full surface.
