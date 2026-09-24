#!/usr/bin/env python3
"""Decode a kline_edges.py capture: bit time, init pulses, bytes, messages.

Runs anywhere (no hardware). Level after each edge is recorded; the line idles
high (recessive). Long lows are reported as init/wake-up candidates rather than
decoded, since a 25 ms fast-init pulse or a 5-baud address byte is not UART
data at the session baud.
"""

from __future__ import annotations

import argparse
import statistics
from collections import Counter


def load(path: str) -> list[tuple[int, int]]:
    ev = []
    with open(path) as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            t, lvl = line.split()
            ev.append((int(t), int(lvl)))
    return ev


def pulses(ev: list[tuple[int, int]]) -> list[tuple[int, int, int]]:
    """(start_ns, width_ns, level) for each completed level interval."""
    return [(ev[i][0], ev[i + 1][0] - ev[i][0], ev[i][1]) for i in range(len(ev) - 1)]


def bit_time(ps: list[tuple[int, int, int]]) -> float:
    short = sorted(w for _, w, _ in ps if 20_000 < w < 2_000_000)
    base = short[: max(10, len(short) // 20)]  # the shortest ~5 %: single bits
    return statistics.median(base)


def decode(ev: list[tuple[int, int]], bit_ns: float, long_low_ns: float):
    """Yield ('byte', t, value, framing_ok) and ('low', t, width) records."""
    n = len(ev)
    i = 0
    while i < n:
        t, lvl = ev[i]
        if lvl != 0:  # need a falling edge (start bit)
            i += 1
            continue
        width = (ev[i + 1][0] - t) if i + 1 < n else 0
        if width > long_low_ns:
            yield ("low", t, width)
            i += 1
            continue
        byte = 0
        j = i
        stop_ok = True
        for b in range(9):
            ts = t + bit_ns * (1.5 + b)
            while j + 1 < n and ev[j + 1][0] <= ts:
                j += 1
            level = ev[j][1]
            if b < 8:
                byte |= level << b
            else:
                stop_ok = level == 1
        yield ("byte", t, byte, stop_ok)
        tend = t + bit_ns * 9.5
        while i < n and ev[i][0] < tend:
            i += 1


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("capture")
    p.add_argument("--baud", type=float, help="override the measured baud")
    p.add_argument("--gap-ms", type=float, default=4.0, help="inter-message gap")
    p.add_argument("--max-lines", type=int, default=200)
    a = p.parse_args()

    ev = load(a.capture)
    print(f"{len(ev)} edges over {(ev[-1][0] - ev[0][0]) / 1e9:.2f} s" if ev else "no edges")
    if len(ev) < 4:
        return
    ps = pulses(ev)
    bit_ns = 1e9 / a.baud if a.baud else bit_time(ps)
    print(f"bit time {bit_ns / 1e3:.2f} us -> ~{1e9 / bit_ns:.0f} baud")
    lows = sorted(w for _, w, lvl in ps if lvl == 0)
    print("longest lows (ms):", [round(w / 1e6, 2) for w in lows[-8:]])
    widths = Counter(round(w / bit_ns) for _, w, _ in ps if w < 12 * bit_ns)
    print("pulse widths in bits:", dict(sorted(widths.items())))

    t0 = ev[0][0]
    msgs: list[tuple[int, list[int], int]] = []
    cur: list[int] = []
    cur_t = 0
    last_end = None
    bad = 0
    for rec in decode(ev, bit_ns, long_low_ns=12 * bit_ns):
        if rec[0] == "low":
            if cur:
                msgs.append((cur_t, cur, bad))
                cur, bad = [], 0
            msgs.append((rec[1], [], -int(rec[2])))
            last_end = rec[1] + rec[2]
            continue
        _, t, byte, ok = rec
        if last_end is None or t - last_end > a.gap_ms * 1e6:
            if cur:
                msgs.append((cur_t, cur, bad))
            cur, cur_t, bad = [], t, 0
        cur.append(byte)
        bad += not ok
        last_end = t + 10 * bit_ns
    if cur:
        msgs.append((cur_t, cur, bad))

    print(f"\n{len(msgs)} messages/pulses (gap {a.gap_ms} ms); t = s from first edge")
    prev = None
    for t, data, bad in msgs[: a.max_lines]:
        dt = "" if prev is None else f"+{(t - prev) / 1e6:7.1f}ms"
        prev = t
        if bad < 0:
            print(f"{(t - t0) / 1e9:9.4f} {dt:>12}  LOW {-bad / 1e6:.2f} ms")
            continue
        cs = "cs-ok" if len(data) > 1 and sum(data[:-1]) & 0xFF == data[-1] else ""
        fe = f" framing-err={bad}" if bad else ""
        print(
            f"{(t - t0) / 1e9:9.4f} {dt:>12}  [{len(data):3d}] "
            f"{' '.join(f'{x:02X}' for x in data[:40])}"
            f"{' ...' if len(data) > 40 else ''} {cs}{fe}"
        )


if __name__ == "__main__":
    main()
