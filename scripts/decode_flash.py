#!/usr/bin/env python3
"""Reassemble ISO-TP and decode a UDS/KWP2000 reprogramming session from a
candump log.

    .venv/bin/python decode_flash.py flash-20260726T203000Z.log
    .venv/bin/python decode_flash.py flash.log --extract-image fw.bin

Deliberately ID-agnostic: it does not assume 7E0/7E8. It finds every CAN ID
whose traffic looks like ISO-TP (single/first/consecutive/flow-control PCI)
and reassembles each independently, so a Nissan-specific or 29-bit
reprogramming channel shows up on its own.

Sequence-number gaps are reported, not silently healed — on an MCP2515 HAT a
saturated TransferData burst can genuinely drop frames, and a firmware image
reconstructed across a gap would be quietly wrong.

Status: exercised against synthetic candump logs (session/security-access/
download/transfer/NRC paths, plus induced sequence gaps) and against one real
capture — the 2026-07-26 Xterra ZS04C->ZS44A reflash, 410k frames. That capture
is what motivated `ISOTP_PCI_THRESHOLD`; see the note there.

Known limitation: for platforms like this one, which move the firmware over a
raw non-ISO-TP channel, this script tells you the shape and size of the
transfer but cannot decode its contents. There is no `RequestDownload` to
give you an address/length, and no image to extract.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field
from pathlib import Path

# candump -L format:  (1690000000.123456) can0 7E0#0210920304050607
LINE = re.compile(
    r"^\((?P<ts>\d+\.\d+)\)\s+(?P<iface>\S+)\s+(?P<id>[0-9A-Fa-f]+)#(?P<data>[0-9A-Fa-f]*)"
)

UDS_SERVICES = {
    0x10: "DiagnosticSessionControl",
    0x11: "ECUReset",
    0x14: "ClearDiagnosticInformation",
    0x18: "ReadDTCByStatus (KWP)",
    0x19: "ReadDTCInformation",
    0x22: "ReadDataByIdentifier",
    0x23: "ReadMemoryByAddress",
    0x27: "SecurityAccess",
    0x28: "CommunicationControl",
    0x2E: "WriteDataByIdentifier",
    0x2F: "InputOutputControlByIdentifier",
    0x31: "RoutineControl",
    0x34: "RequestDownload",
    0x35: "RequestUpload",
    0x36: "TransferData",
    0x37: "RequestTransferExit",
    0x3D: "WriteMemoryByAddress",
    0x3E: "TesterPresent",
    0x83: "AccessTimingParameter",
    0x85: "ControlDTCSetting",
    0x87: "LinkControl",
    # KWP2000-on-CAN flavours seen on Nissan/J2534 tooling
    0x81: "StartCommunication (KWP)",
    0x82: "StopCommunication (KWP)",
    0x1A: "ReadECUIdentification (KWP)",
    0x21: "ReadDataByLocalId (KWP)",
    0x30: "IOControlByLocalId (KWP)",
    0x38: "RequestRoutineByLocalId (KWP)",
}

NRC = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat",
    0x21: "busyRepeatRequest",
    0x22: "conditionsNotCorrect",
    0x24: "requestSequenceError",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x35: "invalidKey",
    0x36: "exceedNumberOfAttempts",
    0x37: "requiredTimeDelayNotExpired",
    0x70: "uploadDownloadNotAccepted",
    0x71: "transferDataSuspended",
    0x72: "generalProgrammingFailure",
    0x73: "wrongBlockSequenceCounter",
    0x78: "responsePending",
    0x7E: "subFunctionNotSupportedInActiveSession",
    0x7F: "serviceNotSupportedInActiveSession",
}

SESSION_TYPES = {
    0x01: "default",
    0x02: "programming",
    0x03: "extendedDiagnostic",
    0x04: "safetySystemDiagnostic",
    0x81: "default (KWP)",
    0x85: "programming (KWP/Nissan)",
    0xC0: "Nissan diagnostic (0xC0)",
}


@dataclass
class Message:
    ts: float
    can_id: int
    payload: bytes
    gap: bool = False  # reassembled across a detected frame loss


#: Fraction of a channel's frames whose PCI nibble must be a valid ISO-TP type
#: (0=single 1=first 2=consecutive 3=flow-control) before we believe the
#: channel is ISO-TP at all.
#:
#: Without this gate the reassembler happily runs over raw bulk-data channels
#: and the UDS layer then reads arbitrary payload bytes as service IDs. That is
#: not hypothetical: on the 2026-07-26 Xterra reflash, UpRev streamed 576 KiB
#: of firmware as raw 8-byte frames on 0x743, and this script reported 23
#: fabricated SecurityAccess seed/key pairs decoded out of firmware bytes.
#: Invented security material is far worse than no output, so channels that
#: don't look like ISO-TP are reported as opaque bulk streams instead.
#:
#: Real ISO-TP sits near 1.0 (0x7E0 in that capture: 1.00). Raw data lands near
#: 4/16 = 0.25 by chance (0x743: 0.28).
ISOTP_PCI_THRESHOLD = 0.85


@dataclass
class Channel:
    can_id: int
    frames: int = 0
    first_ts: float | None = None
    last_ts: float | None = None
    messages: list[Message] = field(default_factory=list)
    # in-flight reassembly
    _expect: int = 0
    _buf: bytearray = field(default_factory=bytearray)
    _sn: int = 0
    _start: float = 0.0
    _gap: bool = False
    flow_control: list[tuple[float, int, int, int]] = field(default_factory=list)
    sn_gaps: int = 0
    #: frames whose PCI nibble is a valid ISO-TP type, and frames with any data
    pci_valid: int = 0
    pci_total: int = 0

    @property
    def isotp_ratio(self) -> float:
        return self.pci_valid / self.pci_total if self.pci_total else 0.0

    @property
    def looks_isotp(self) -> bool:
        # A handful of frames can hit the ratio by luck; require both.
        return self.pci_total >= 8 and self.isotp_ratio >= ISOTP_PCI_THRESHOLD


def parse(path: Path) -> tuple[dict[int, Channel], int, list[str]]:
    channels: dict[int, Channel] = {}
    total = 0
    bad: list[str] = []

    for raw in path.read_text(errors="replace").splitlines():
        m = LINE.match(raw.strip())
        if not m:
            if raw.strip():
                bad.append(raw.strip())
            continue
        total += 1
        ts = float(m.group("ts"))
        can_id = int(m.group("id"), 16)
        try:
            data = bytes.fromhex(m.group("data"))
        except ValueError:
            # One corrupt line must not abort analysis of a capture that
            # cannot be retaken. Count it and carry on.
            bad.append(raw.strip())
            continue

        ch = channels.setdefault(can_id, Channel(can_id))
        ch.frames += 1
        ch.first_ts = ts if ch.first_ts is None else ch.first_ts
        ch.last_ts = ts
        if not data:
            continue

        pci = data[0] >> 4
        ch.pci_total += 1
        if pci <= 0x3:
            ch.pci_valid += 1
        if pci == 0x0:  # single frame
            n = data[0] & 0x0F
            if n and n <= len(data) - 1:
                ch.messages.append(Message(ts, can_id, data[1 : 1 + n]))
            ch._expect = 0
            ch._buf = bytearray()
        elif pci == 0x1:  # first frame
            ch._expect = ((data[0] & 0x0F) << 8) | data[1]
            ch._buf = bytearray(data[2:])
            ch._sn = 0
            ch._start = ts
            ch._gap = False
        elif pci == 0x2:  # consecutive frame
            if ch._expect:
                sn = data[0] & 0x0F
                expected = (ch._sn + 1) & 0x0F
                if sn != expected:
                    ch.sn_gaps += 1
                    ch._gap = True
                    # infer how many CFs vanished and pad so offsets stay honest
                    missing = (sn - expected) & 0x0F
                    ch._buf.extend(b"\x00" * (7 * missing))
                ch._sn = sn
                ch._buf.extend(data[1:])
                if len(ch._buf) >= ch._expect:
                    ch.messages.append(
                        Message(ch._start, can_id, bytes(ch._buf[: ch._expect]), ch._gap)
                    )
                    ch._expect = 0
                    ch._buf = bytearray()
                    ch._gap = False
        elif pci == 0x3:  # flow control
            fs = data[0] & 0x0F
            bs = data[1] if len(data) > 1 else 0
            st = data[2] if len(data) > 2 else 0
            ch.flow_control.append((ts, fs, bs, st))

    return channels, total, bad


def describe(payload: bytes) -> str:
    if not payload:
        return "(empty)"
    sid = payload[0]

    if sid == 0x7F and len(payload) >= 3:
        svc = UDS_SERVICES.get(payload[1], f"0x{payload[1]:02X}")
        code = payload[2]
        return f"NEGATIVE  {svc} -> {NRC.get(code, f'0x{code:02X}')}"

    positive = sid >= 0x40 and (sid - 0x40) in UDS_SERVICES
    base = sid - 0x40 if positive else sid
    name = UDS_SERVICES.get(base, f"unknown 0x{base:02X}")
    tag = "resp" if positive else "req "
    body = payload[1:]
    extra = ""

    if base == 0x10 and body:
        extra = f" session={SESSION_TYPES.get(body[0], f'0x{body[0]:02X}')}"
    elif base == 0x27 and body:
        lvl = body[0]
        kind = "requestSeed" if lvl % 2 == 1 else "sendKey"
        extra = f" level=0x{lvl:02X} ({kind})"
        if len(body) > 1:
            extra += f" data={body[1:].hex()}"
    elif base == 0x34 and len(body) >= 2:
        fmt, alfid = body[0], body[1]
        asize, lsize = alfid & 0x0F, alfid >> 4
        addr = int.from_bytes(body[2 : 2 + asize], "big") if asize else 0
        length = int.from_bytes(body[2 + asize : 2 + asize + lsize], "big") if lsize else 0
        extra = f" dataFormat=0x{fmt:02X} addr=0x{addr:08X} len=0x{length:X} ({length} bytes)"
    elif base == 0x36 and body:
        extra = f" block#{body[0]} ({len(body) - 1} bytes)"
    elif base == 0x31 and len(body) >= 3:
        extra = f" routine=0x{body[1] << 8 | body[2]:04X} sub=0x{body[0]:02X}"
    elif base == 0x22 and len(body) >= 2:
        extra = f" DID=0x{body[0] << 8 | body[1]:04X}"
    elif base == 0x11 and body:
        extra = f" resetType=0x{body[0]:02X}"
    elif base == 0x85 and body:
        extra = f" setting=0x{body[0]:02X}"

    return f"{tag} {name}{extra}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("log", type=Path)
    ap.add_argument("--extract-image", type=Path, help="write TransferData payload to file")
    ap.add_argument("--max-lines", type=int, default=400, help="transcript lines to print")
    args = ap.parse_args()

    channels, total, bad = parse(args.log)
    span = 0.0
    all_ts = [t for c in channels.values() for t in (c.first_ts, c.last_ts) if t]
    if all_ts:
        span = max(all_ts) - min(all_ts)

    print(f"=== {args.log.name} ===")
    print(f"{total} frames over {span:.1f}s across {len(channels)} CAN IDs")
    if bad:
        print(f"{len(bad)} unparseable lines (first: {bad[0][:80]!r})")
    print()

    print("=== per-ID activity (sorted by frame count) ===")
    print(f"{'ID':>8}  {'frames':>8}  {'msgs':>6}  {'sn_gaps':>7}  window")
    for ch in sorted(channels.values(), key=lambda c: -c.frames):
        if ch.first_ts is None or ch.last_ts is None:
            continue
        t0 = ch.first_ts - min(all_ts)
        t1 = ch.last_ts - min(all_ts)
        print(
            f"{ch.can_id:>8X}  {ch.frames:>8}  {len(ch.messages):>6}  "
            f"{ch.sn_gaps:>7}  {t0:7.1f}s .. {t1:7.1f}s"
        )
    print()

    # Diagnostic channels = any ID carrying a recognised UDS/KWP service.
    diag = {
        cid: ch
        for cid, ch in channels.items()
        if ch.looks_isotp
        and any(
            m.payload
            and (
                m.payload[0] in UDS_SERVICES
                or (m.payload[0] - 0x40) in UDS_SERVICES
                or m.payload[0] == 0x7F
            )
            for m in ch.messages
        )
    }

    bulk = sorted(
        (ch for ch in channels.values() if not ch.looks_isotp and ch.frames >= 500),
        key=lambda c: -c.frames,
    )
    if bulk:
        print("=== bulk / non-ISO-TP channels ===")
        print("These carry raw framed data, not a request/response protocol, so no")
        print("service decoding is attempted on them. On this platform the firmware")
        print("payload itself travels this way.")
        for ch in bulk:
            if ch.first_ts is None or ch.last_ts is None:
                continue
            dur = ch.last_ts - ch.first_ts
            rate = ch.frames / dur if dur > 0 else 0.0
            print(
                f"  {ch.can_id:03X}  {ch.frames} frames  ~{ch.frames * 8 / 1024:.1f} KiB  "
                f"{dur:.1f}s  {rate:.0f} f/s  isotp_ratio={ch.isotp_ratio:.2f}"
            )
        print()

    if not diag:
        print("=== NO UDS/KWP TRAFFIC ON CAN ===")
        print("No CAN ID carried recognisable diagnostic services. Strong evidence")
        print("the reflash ran over K-line. The per-ID windows above still tell you")
        print("which modules dropped off the bus and when — that's the finding.")
        return 0

    print("=== diagnostic transcript ===")
    events = sorted(
        ((m.ts, cid, m) for cid, ch in diag.items() for m in ch.messages),
        key=lambda e: e[0],
    )
    base_ts = events[0][0] if events else 0.0
    shown = 0
    suppressed_tp = 0
    suppressed_xfer = 0
    for ts, cid, m in events:
        sid = m.payload[0] if m.payload else 0
        # TesterPresent and bulk TransferData drown the transcript; count them.
        if sid in (0x3E, 0x7E):
            suppressed_tp += 1
            continue
        if sid in (0x36, 0x76):
            suppressed_xfer += 1
            if suppressed_xfer > 6:
                continue
        if shown >= args.max_lines:
            continue
        flag = "  [GAP]" if m.gap else ""
        print(f"{ts - base_ts:9.3f}s  {cid:03X}  {describe(m.payload)}{flag}")
        shown += 1
    if suppressed_tp:
        print(f"... {suppressed_tp} TesterPresent exchanges suppressed")
    if suppressed_xfer > 6:
        print(f"... {suppressed_xfer - 6} further TransferData messages suppressed")
    print()

    # --- flash-specific summary -------------------------------------------
    print("=== reprogramming summary ===")
    seeds: list[tuple[float, int, bytes]] = []
    keys: list[tuple[float, int, bytes]] = []
    downloads: list[tuple[float, int, int]] = []
    blocks: list[bytes] = []
    gapped_blocks = 0

    for ts, _cid, m in events:
        p = m.payload
        if not p:
            continue
        if p[0] == 0x67 and len(p) > 2:
            seeds.append((ts, p[1], p[2:]))
        elif p[0] == 0x27 and len(p) > 2 and p[1] % 2 == 0:
            keys.append((ts, p[1], p[2:]))
        elif p[0] == 0x34 and len(p) >= 4:
            alfid = p[2]
            asize, lsize = alfid & 0x0F, alfid >> 4
            addr = int.from_bytes(p[3 : 3 + asize], "big") if asize else 0
            ln = int.from_bytes(p[3 + asize : 3 + asize + lsize], "big") if lsize else 0
            downloads.append((ts, addr, ln))
        elif p[0] == 0x36 and len(p) > 2:
            blocks.append(p[2:])
            if m.gap:
                gapped_blocks += 1

    for ts, lvl, seed in seeds:
        print(f"  seed  @{ts - base_ts:8.3f}s level=0x{lvl:02X}  {seed.hex()}")
    for ts, lvl, key in keys:
        print(f"  key   @{ts - base_ts:8.3f}s level=0x{lvl:02X}  {key.hex()}")
    for ts, addr, ln in downloads:
        print(f"  RequestDownload @{ts - base_ts:8.3f}s addr=0x{addr:08X} len={ln} bytes")

    if blocks:
        image = b"".join(blocks)
        print(f"  TransferData: {len(blocks)} blocks, {len(image)} bytes total")
        if gapped_blocks:
            print(f"  !! {gapped_blocks} block(s) reassembled across a frame loss —")
            print("     the image is NOT byte-exact. Treat it as structural only.")
        if args.extract_image:
            args.extract_image.write_bytes(image)
            print(f"  wrote {len(image)} bytes -> {args.extract_image}")
    else:
        print("  no TransferData payload seen")

    # Only ISO-TP channels have meaningful sequence numbers. Counting "gaps"
    # from a raw bulk stream reports adapter frame loss that may not have
    # happened — on the Xterra capture 0x743 alone contributed 131 phantom gaps.
    total_gaps = sum(c.sn_gaps for c in channels.values() if c.looks_isotp)
    if total_gaps:
        print()
        print(f"!! {total_gaps} ISO-TP sequence gaps — the adapter dropped frames.")
        print("   Compare rx_dropped/rx_over_errors before vs after the capture.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
