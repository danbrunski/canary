"""Does 0x1134/64 equal the equivalence ratio implied by the injector/MAF mass balance?

phi_implied = PW_counts * 14.7 * (rpm/120*6) / (914943 * MAF_gps)
  914943 counts per gram of fuel per injection
  (data/dids.yaml, 0x1208, closed-loop steady-state fit)
  MAF g/s = 0x1209 * 0.01 ; rpm = 0x1219 ; 6 cylinders, one injection per 2 revs
Same instrument, same pass; within-pass read skew is ~<=1.2 s, so steady samples only.
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


rpm, pw, maf, tps, raw, af = (
    og(d) for d in ("0x1219", "0x1208", "0x1209", "0x120F", "0x1134", "0x1225")
)
phi_hyp = raw / 64.0
inj_per_s = rpm / 120.0 * 6.0
phi_imp = pw * 14.7 * inj_per_s / (914943.0 * np.maximum(maf * 0.01, 1e-6) * 6.0) * 6.0
# steady: small rpm and maf change between neighbouring passes
drpm = np.abs(np.gradient(rpm))
dmaf = np.abs(np.gradient(maf * 0.01))
steady = (drpm < 150) & (dmaf < 5) & (rpm > 900)
off = raw != 64
print(
    f"samples {len(grid)}, steady {steady.sum()}, raw!=64 {off.sum()}, steady&off {int((steady & off).sum())}"  # noqa: E501
)
print("\nraw!=64 samples (all):")
print("  raw  phi_hyp  phi_imp   rpm     pw  maf_g/s tps_V  af_V  steady")
for i in np.where(off)[0]:
    print(
        f"  {raw[i]:3.0f}  {phi_hyp[i]:7.3f} {phi_imp[i]:8.3f} {rpm[i]:5.0f} {pw[i]:6.0f} {maf[i] * 0.01:7.1f} {tps[i] * 0.005:5.2f} {af[i] * 0.005:5.2f}  {bool(steady[i])}"  # noqa: E501
    )
for label, m in (("steady, raw==64", steady & ~off), ("steady, raw!=64", steady & off)):
    x = phi_imp[m]
    if len(x):
        print(
            f"\n{label}: n={len(x)} phi_imp p10/p50/p90 = {np.percentile(x, 10):.3f}/{np.median(x):.3f}/{np.percentile(x, 90):.3f}"  # noqa: E501
            f"   phi_hyp median {np.median(phi_hyp[m]):.3f}"
        )
