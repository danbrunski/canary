"""Regime detail + cross-window reproduction check for did_log candidates."""

import json
import sys
from collections import defaultdict

import numpy as np


def load(path):
    series = defaultdict(lambda: ([], []))
    for line in open(path):
        r = json.loads(line)
        if r.get("kind") != "read":
            continue
        t = (r["t_send"] + r["t_recv"]) / 2
        for k, hexv in r["values"].items():
            series[k][0].append(t)
            series[k][1].append(int(hexv, 16))
    return {k: (np.array(v[0]), np.array(v[1], dtype=float)) for k, v in series.items()}


def regimes(S):  # noqa: N803
    grid = S["0x1219"][0]

    def og(did):
        t, v = S[did]
        return v[np.searchsorted(t, grid).clip(0, len(t) - 1)]

    rpm, pw, tps, af = og("0x1219"), og("0x1208"), og("0x120F"), og("0x1225")
    cut = (rpm > 1500) & (pw < np.percentile(pw, 5))
    high = (tps > np.percentile(tps, 95)) & (rpm > 2000)
    return grid, og, rpm, pw, tps, af, cut, high, (~cut & ~high)


for path in sys.argv[1:]:
    S = load(path)
    grid, og, rpm, pw, tps, af, cut, high, cruise = regimes(S)
    print(f"\n=== {path.split('/')[-1]} n={len(grid)} span={grid[-1] - grid[0]:.0f}s")
    for did in ("0x1112", "0x1134", "0x1149", "0x120D", "0x120E", "0x122A", "0x1138", "0x1204"):
        if did not in S:
            continue
        g = og(did)
        modal = float(np.bincount(g.astype(np.int64) - int(g.min())).argmax() + int(g.min()))

        def d(m):
            x = g[m]  # noqa: B023
            u = np.unique(x)
            return f"{x.min():.0f}/{np.median(x):.0f}/{x.max():.0f} uniq{len(u)}"

        print(
            f"  {did} modal {modal:.0f} ({np.mean(g == modal) * 100:.0f}%): cut [{d(cut)}] high [{d(high)}] cruise [{d(cruise)}]"  # noqa: E501
        )
    # fuel-cut flag check: 0x1112 vs the A/F sensor at its lean rail (5.065 V = 1013 counts)
    if "0x1112" in S:
        f = og("0x1112")
        rail = af >= 1000
        print(
            f"  0x1112==1 in {f.sum():.0f}/{len(f)} samples; A/F at lean rail in {rail.sum()} samples; "  # noqa: E501
            f"overlap {(np.logical_and(f > 0, rail)).sum()}; flag&&!rail {(np.logical_and(f > 0, ~rail)).sum()}; "  # noqa: E501
            f"rail&&!flag {(np.logical_and(f == 0, rail)).sum()}"
        )
    for _ in (1,):
        if "0x1134" in S:
            h = og("0x1134")
            off = h != 64
            print(
                f"  0x1134 off-64 in {off.sum()} samples: values {np.unique(h[off])[:8]}; "
                f"of those, high-load {int((off & high).sum())}, cut {int((off & cut).sum())}, cruise {int((off & cruise).sum())}; "  # noqa: E501
                f"tps when off: {tps[off].min() * 0.005:.2f}-{tps[off].max() * 0.005:.2f} V, maf-proxy pw {pw[off].min():.0f}-{pw[off].max():.0f}"  # noqa: E501
            )
