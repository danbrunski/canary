"""Candidate screen over a did_log JSONL: which registers hold a modal value and move only in a regime."""  # noqa: E501

import json
import sys
from collections import defaultdict

import numpy as np

path = sys.argv[1]
series = defaultdict(lambda: ([], []))
for line in open(path):
    r = json.loads(line)
    if r.get("kind") != "read":
        continue
    t = (r["t_send"] + r["t_recv"]) / 2
    for k, hexv in r["values"].items():
        series[k][0].append(t)
        series[k][1].append(int(hexv, 16))
S = {k: (np.array(v[0]), np.array(v[1], dtype=float)) for k, v in series.items()}

RPM, PW, MAF, TPS = "0x1219", "0x1208", "0x1209", "0x120F"
grid = S[RPM][0]  # one pass per rpm read; use it as the clock


def on_grid(did):
    t, v = S[did]
    idx = np.searchsorted(t, grid).clip(0, len(t) - 1)
    return v[idx]


rpm, pw, maf, tps = (on_grid(d) for d in (RPM, PW, MAF, TPS))
print(
    f"n={len(grid)} span={grid[-1] - grid[0]:.0f}s rpm {rpm.min():.0f}-{rpm.max():.0f} "
    f"pw {pw.min():.0f}-{pw.max():.0f} maf(g/s) {maf.min() * 0.01:.1f}-{maf.max() * 0.01:.1f} tps(V) {tps.min() * 0.005:.2f}-{tps.max() * 0.005:.2f}"  # noqa: E501
)

# regimes, from the same instrument
pw_per_rev = pw / np.maximum(rpm, 1)
cut = (rpm > 1500) & (pw < np.percentile(pw, 5))  # decel fuel cut candidate
high = (tps > np.percentile(tps, 95)) & (rpm > 2000)  # heaviest load seen
cruise = ~cut & ~high
print(f"regimes: decel-cut-like {cut.sum()}, high-load {high.sum()}, cruise {cruise.sum()}")

print("\nHOLDS-THEN-MOVES candidates (modal >=40% of samples, >=10 changes):")
print(f"{'did':7} {'modal':>9} {'%modal':>6} {'chg':>4} {'off-modal regime: cut/high/cruise':>34}")
for did, (t, v) in sorted(S.items()):  # noqa: B007
    if len(np.unique(v)) < 2:
        continue
    g = on_grid(did)
    modal = float(np.bincount(g.astype(np.int64) - int(g.min())).argmax() + int(g.min()))
    frac = float(np.mean(g == modal))
    chg = int(np.sum(np.diff(g) != 0))
    if frac < 0.40 or chg < 10:
        continue
    off = g != modal
    c1, c2, c3 = (float(np.mean(off & m) / max(m.mean(), 1e-9)) for m in (cut, high, cruise))
    print(
        f"{did:7} {modal:>9.0f} {frac * 100:>5.1f}% {chg:>4}   cut {c1:>5.2f}  high {c2:>5.2f}  cruise {c3:>5.2f}"  # noqa: E501
    )

print("\nregime detail for shortlist:")
for did in (
    "0x122A",
    "0x1246",
    "0x1248",
    "0x1125",
    "0x1116",
    "0x1112",
    "0x1149",
    "0x120D",
    "0x1138",
):
    if did not in S:
        continue
    g = on_grid(did)

    def d(m):
        x = g[m]  # noqa: B023
        return f"n{len(x)} {x.min():.0f}/{np.median(x):.0f}/{x.max():.0f}"

    print(f"  {did}: cut [{d(cut)}]  high [{d(high)}]  cruise [{d(cruise)}]")
print("\n0x122A vs AF sensor B1 (0x1225, x0.005 V) and MAF, worst-load samples:")
g = on_grid("0x122A")
af = on_grid("0x1225")
order = np.argsort(-tps)[:12]
print("   t_rel   rpm     pw  maf_g/s  tps_V  0x122A  af_b1_V")
for i in sorted(order):
    print(
        f"  {grid[i] - grid[0]:6.0f} {rpm[i]:5.0f} {pw[i]:6.0f} {maf[i] * 0.01:8.1f} {tps[i] * 0.005:6.2f} {g[i]:7.0f} {af[i] * 0.005:8.3f}"  # noqa: E501
    )
print("\n0x122A during decel-cut-like samples:")
for i in np.where(cut)[0][:12]:
    print(
        f"  {grid[i] - grid[0]:6.0f} {rpm[i]:5.0f} {pw[i]:6.0f} {maf[i] * 0.01:8.1f} {tps[i] * 0.005:6.2f} {g[i]:7.0f} {af[i] * 0.005:8.3f}"  # noqa: E501
    )
