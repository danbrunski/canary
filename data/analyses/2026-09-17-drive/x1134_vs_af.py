"""Within-instrument check on DID 0x1134: when it leaves its 64 baseline, does the
wideband voltage go rich? Both come from the same did_log pass, so no cross-source
alignment is involved. 0x1225 is scaled x0.005 V (catalogued, confidence high);
on this ECM lower voltage = richer.
"""

import json
import sys
from collections import defaultdict

import numpy as np

series = defaultdict(lambda: ([], []))
for line in open(sys.argv[1]):
    r = json.loads(line)
    if r.get("kind") != "read":
        continue
    t = (r["t_send"] + r["t_recv"]) / 2
    for k, hexv in r["values"].items():
        series[k][0].append(t)
        series[k][1].append(int(hexv, 16))
S = {k: (np.array(v[0]), np.array(v[1], dtype=float)) for k, v in series.items()}
grid = S["0x1219"][0]


def og(did):
    t, v = S[did]
    return v[np.searchsorted(t, grid).clip(0, len(t) - 1)]


raw, af, af2, tps, rpm, pw = (
    og(d) for d in ("0x1134", "0x1225", "0x1226", "0x120F", "0x1219", "0x1208")
)
afv, af2v = af * 0.005, af2 * 0.005
off = raw != 64
print(
    f"n={len(grid)}  raw!=64: {off.sum()}  raw values off baseline: {sorted(set(raw[off].astype(int)))}"  # noqa: E501
)
print(
    f"A/F B1 volts: baseline-samples med {np.median(afv[~off]):.3f} "
    f"(p10 {np.percentile(afv[~off], 10):.3f}, p90 {np.percentile(afv[~off], 90):.3f})"
)
for val in sorted(set(raw[off].astype(int))):
    m = raw == val
    print(
        f"  raw {val:3d} (phi_hyp {val / 64:.3f}): n={m.sum():3d} "
        f"A/F_B1 med {np.median(afv[m]):.3f} V [{afv[m].min():.3f}-{afv[m].max():.3f}]  "
        f"A/F_B2 med {np.median(af2v[m]):.3f} V  tps med {np.median(tps[m]) * 0.005:.2f} V  "
        f"rpm med {np.median(rpm[m]):.0f}"
    )
# how exceptional is that voltage? rank of the off-baseline samples in the whole window
thresh = np.percentile(afv, 5)
print(f"\n5th-percentile A/F_B1 voltage over the window: {thresh:.3f} V")
print(
    f"off-baseline samples below it: {int((off & (afv <= thresh)).sum())} of {int(off.sum())} "
    f"({(off & (afv <= thresh)).sum() / max(off.sum(), 1) * 100:.0f}%)"
)
print(
    f"baseline samples below it:     {int((~off & (afv <= thresh)).sum())} of {int((~off).sum())} "
    f"({(~off & (afv <= thresh)).sum() / max((~off).sum(), 1) * 100:.0f}%)"
)
deep = raw >= 74
if deep.any():
    print(
        f"\nraw>=74 (phi_hyp>=1.156): n={deep.sum()} A/F_B1 {afv[deep].min():.3f}-{afv[deep].max():.3f} V, "  # noqa: E501
        f"med {np.median(afv[deep]):.3f}; whole-window min {afv.min():.3f}"
    )
