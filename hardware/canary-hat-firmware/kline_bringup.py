#!/usr/bin/env python3
"""CANary-HAT rev 2.0 K-line bring-up (TLIN1027 on uart0, run on the Pi).

Needs uart0 (on in the board overlay), +12V on J2, the daemon's K-line
collector stopped, and nothing else driving K-line.

Steps, each a subcommand:
  sniff     transceiver on, pull-up off, listen only
  echo      single-wire loopback: every transmitted byte must come back.
            Exercises R6, TLIN1027 TX/RX and (with --pullup) Q2/Q3 + R5 + D5
            without involving any ECU.
  fastinit  ISO 14230 fast init + StartCommunication (0x81), then
            StopCommunication (0x82). Volatile; touches no ECU memory.

Hardware order (docs/CANary-HAT-Spec.md §11): open the tty BEFORE raising EN
(GPIO26); discard RX after the mode change; drop EN and the commander pull-up
(GPIO24) before releasing the tty, on every exit path; strip the local echo.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time

import gpiod
import serial
from gpiod.line import Direction, Value

TTY = "/dev/ttyAMA0"
GPIO_EN = 26  # TLIN1027 EN: high = normal mode
GPIO_PULLUP = 24  # Q3 -> Q2: high = 1k commander pull-up to +12V
TESTER = 0xF1  # ISO 14230-4 default; this ECM answers 0xFC


def rxd_level() -> str:
    out = subprocess.run(["pinctrl", "get", "15"], capture_output=True, text=True).stdout
    return "hi" if "| hi" in out else "lo" if "| lo" in out else out.strip()


class KLine:
    def __init__(self, baud: int, pullup: bool) -> None:
        self.ser = serial.Serial(TTY, baud, timeout=0)  # 1. tty first
        self.lines = gpiod.Chip("/dev/gpiochip0").request_lines(
            consumer="kline-bringup",
            config={
                (GPIO_EN, GPIO_PULLUP): gpiod.LineSettings(
                    direction=Direction.OUTPUT, output_value=Value.INACTIVE
                )
            },
        )
        print(f"  RXD with EN low (standby, expect lo): {rxd_level()}")
        self.lines.set_value(GPIO_PULLUP, Value.ACTIVE if pullup else Value.INACTIVE)
        self.lines.set_value(GPIO_EN, Value.ACTIVE)  # 2. then EN
        time.sleep(0.005)  # >> tMODE_CHANGE + tNOMINT
        self.ser.reset_input_buffer()  # 3. blank RX across the mode change
        print(
            f"  RXD with EN high, pull-up {'on' if pullup else 'off'} "
            f"(bus recessive, expect hi): {rxd_level()}"
        )

    def close(self) -> None:
        try:
            self.lines.set_value(GPIO_EN, Value.INACTIVE)  # 6. EN down first
            self.lines.set_value(GPIO_PULLUP, Value.INACTIVE)
            self.lines.release()
        finally:
            self.ser.close()

    def send(self, data: bytes) -> bytes:
        """Transmit and consume the echo; return the echo actually seen."""
        self.ser.reset_input_buffer()
        self.ser.write(data)
        self.ser.flush()
        return self.read(len(data), 0.05 + len(data) * 12 / self.ser.baudrate)

    def read(self, n: int, timeout: float) -> bytes:
        buf = bytearray()
        end = time.monotonic() + timeout
        while len(buf) < n and time.monotonic() < end:
            buf += self.ser.read(n - len(buf))
            time.sleep(0.001)
        return bytes(buf)

    def read_until_quiet(self, first_timeout: float, gap: float = 0.03) -> bytes:
        buf = bytearray()
        end = time.monotonic() + first_timeout
        last = None
        while True:
            chunk = self.ser.read(256)
            now = time.monotonic()
            if chunk:
                buf += chunk
                last = now
            elif last is not None and now - last > gap:
                return bytes(buf)
            elif last is None and now > end:
                return bytes(buf)
            time.sleep(0.001)

    def pulse_low(self, seconds: float) -> None:
        self.ser.break_condition = True  # TIOCSBRK: TXD low -> K-line dominant
        time.sleep(seconds)
        self.ser.break_condition = False


def kwp(target: int, payload: bytes, functional: bool = False, tester: int = TESTER) -> bytes:
    fmt = (0xC0 if functional else 0x80) | len(payload)
    msg = bytes([fmt, target, tester]) + payload
    return msg + bytes([sum(msg) & 0xFF])


def hexs(b: bytes) -> str:
    return " ".join(f"{x:02X}" for x in b) or "(nothing)"


def cmd_sniff(args: argparse.Namespace) -> int:
    for baud in (10400, 9600):
        k = KLine(baud, pullup=False)
        try:
            got = k.read_until_quiet(args.seconds, gap=args.seconds)
            print(f"  {baud} baud, {args.seconds:.0f} s: {len(got)} bytes {hexs(got[:48])}")
        finally:
            k.close()
    return 0


def cmd_echo(args: argparse.Namespace) -> int:
    pattern = bytes([0x55, 0xAA, 0x00, 0xFF, 0x0F, 0xF0, 0x33, 0xCC] * 4)
    bad = 0
    for pullup in (False, True):
        for baud in (9600, 10400):
            k = KLine(baud, pullup)
            try:
                time.sleep(0.3)  # W5-style idle before talking
                echo = k.send(pattern)
                ok = echo == pattern
                bad += not ok
                n = min(len(echo), len(pattern))
                errs = sum(a != b for a, b in zip(echo[:n], pattern[:n], strict=True))
                errs += abs(len(echo) - len(pattern))
                print(
                    f"  pull-up {'on ' if pullup else 'off'} {baud:>5} baud: "
                    f"{'PASS' if ok else 'FAIL'} {len(echo)}/{len(pattern)} bytes back, "
                    f"{errs} wrong" + ("" if ok else f"  got {hexs(echo[:16])}")
                )
            finally:
                k.close()
    return 1 if bad else 0


# Wake-up patterns observed from UpRev's cable on this ECM (capture 2026-09-23):
# (low ms, high ms before the first request byte). "std" is ISO 14230 fast init.
WAKEUPS = {"std": (25.0, 25.0), "uprev-long": (518.9, 18.1)}


def lenframe(payload: bytes) -> bytes:
    """Post-StartCommunication frame as UpRev uses it: length, data, checksum."""
    msg = bytes([len(payload)]) + payload
    return msg + bytes([sum(msg) & 0xFF])


def exchange(k: KLine, frame: bytes, label: str) -> bytes:
    time.sleep(0.06)  # P3 >= 55 ms
    echo = k.send(frame)
    resp = k.read_until_quiet(0.3)
    cs = "cs-ok" if len(resp) > 1 and sum(resp[:-1]) & 0xFF == resp[-1] else "cs-BAD"
    print(
        f"    {label}: sent {hexs(frame)} | echo {'ok' if echo == frame else hexs(echo)} "
        f"| reply {hexs(resp)} {cs if resp else ''}"
    )
    return resp


def cmd_fastinit(args: argparse.Namespace) -> int:
    k = KLine(10400, pullup=args.pullup)
    try:
        for name in args.wakeups:
            low_ms, high_ms = WAKEUPS[name]
            time.sleep(0.35)  # W5 >= 300 ms bus idle before a wake-up
            k.ser.reset_input_buffer()
            k.pulse_low(low_ms / 1000)
            time.sleep(high_ms / 1000)
            req = kwp(args.target, b"\x81", tester=args.tester)
            echo = k.send(req)
            resp = k.read_until_quiet(0.3)
            print(
                f"  wake {name} ({low_ms}/{high_ms} ms) -> 0x{args.target:02X} from "
                f"0x{args.tester:02X}: sent {hexs(req)} "
                f"| echo {'ok' if echo == req else hexs(echo)} | reply {hexs(resp)}"
            )
            if len(resp) >= 4 and resp[3] == 0xC1 and (sum(resp[:-1]) & 0xFF) == resp[-1]:
                print(f"    PASS  StartCommunication positive, key bytes {hexs(resp[4:6])}")
                # Known answer from the UpRev capture: 62 11 03 9A.
                exchange(k, lenframe(bytes.fromhex("2211030401")), "read 22 11 03 04 01")
                exchange(k, lenframe(b"\x82"), "StopCommunication")
                return 0
    finally:
        k.close()
    print("  no StartCommunication response")
    return 1


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sniff")
    s.add_argument("--seconds", type=float, default=3.0)
    sub.add_parser("echo")
    f = sub.add_parser("fastinit")
    hexint = lambda v: int(v, 16)  # noqa: E731
    f.add_argument("--target", type=hexint, default=0x10)
    f.add_argument("--tester", type=hexint, default=0xFC, help="UpRev's cable uses 0xFC")
    f.add_argument("--wakeups", nargs="+", choices=list(WAKEUPS), default=["std", "uprev-long"])
    f.add_argument("--no-pullup", dest="pullup", action="store_false")
    args = p.parse_args()
    return {"sniff": cmd_sniff, "echo": cmd_echo, "fastinit": cmd_fastinit}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
