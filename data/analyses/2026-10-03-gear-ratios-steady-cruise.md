# Gear ratios from steady cruise, every September drive on disk (H07.11, 2026-10-03)

**Question.** Do the FS6R31A's published gearbox ratios (4.368 / 2.518 /
1.743 / 1.283 / 1.000 / 0.769), with the 4.08 final drive and the profile's
2.609 m loaded tyre circumference, match what the truck does? H07.11 had 5th
exact (the circumference was solved from it), 3rd within 0.2 % and 4th 2.0 %
high, from three cruise clusters.

**Method.** Every `2026-09-*` session under `~/canary-data`. Broadcast engine
speed (`decoded:ECM_RPM`, ~100 Hz) was paired with GPS speed over ground
(`gps:speed`, 1 Hz) on the acquisition clock with `align_pairs`
(`PairingPolicy.within("60ms")`): same-instant pairs, nothing averaged before
the ratio. A pair counts as steady cruise when the five GPS samples centred on
it (spanning at most 6 s) hold speed within 1 % and rpm/speed within 1 %, at
more than 20 km/h. Each steady pair's rpm per km/h was assigned to the nearest
published gear and kept if within 8 % (clutch and slip rejects), then
converted to the gearbox ratio it implies through 4.08 and 2.609 m. Script
below.

| gear | n pairs | sessions | published | implied (median) | p10–p90 | vs published |
|---|---|---|---|---|---|---|
| 1 | 2 | 1 | 4.368 | 4.605 | — | (too few) |
| 2 | 24 | 6 | 2.518 | 2.577 | 2.544–2.642 | +2.34 % |
| 3 | 412 | 13 | 1.743 | 1.779 | 1.749–1.809 | +2.04 % |
| 4 | 1,070 | 14 | 1.283 | 1.307 | 1.290–1.327 | +1.85 % |
| 5 | 9,611 | 22 | 1.000 | 1.019 | 1.008–1.031 | +1.94 % |
| 6 | 5,952 | 15 | 0.769 | 0.784 | 0.778–0.792 | +1.99 % |

**Reading.** Every gear sits about 2 % high, and by the same amount. Divided
by 5th's implied ratio, the gears are 2.528 / 1.745 / 1.282 / 1.000 / 0.769,
against published 2.518 / 1.743 / 1.283 / 1.000 / 0.769: within 0.4 % (2nd,
24 pairs) and 0.1 % (3rd, 4th, 6th). So:

- **The published gearbox ratios are confirmed for 2nd–6th.** 1st has too
  few steady pairs (2) to say.
- **The common +1.9 % is in final drive × tyre, not the gearbox.** With the
  4.08 regear as given, the loaded circumference these drives imply is
  2.609 × 1.0194 ≈ **2.660 m**, not 2.609 m. The 2.609 m figure was solved
  from 5th on a smaller set and does not reproduce here. 4th's "2.0 % high"
  in H07.11 was this same common factor, not a 4th-gear anomaly.
- A 2 % circumference error is 2 % of wheel force and speed in the dyno
  (H07.7), so it matters there. `derived:GEAR_ESTIMATE`'s nearest-gear
  match is unaffected: adjacent gears are 25–30 % apart.

**Not done here.** `profiles.yaml` still carries 2.609 m. Changing a
vehicle fact is the operator's call, and the published-ratio confirmation
does not need it. If accepted: `tyre_loaded_circumference_m: 2.660`, with
this note as the evidence.

```python
"""H07.11 desk: per-sample rpm / GPS speed on steady cruise, clustered by gear."""
import glob, json, sys
from pathlib import Path
import numpy as np, pandas as pd
from src.analysis.compare import align_pairs, ComparisonRefusedError
from src.signals.pairing import PairingPolicy

FD, CIRC = 4.08, 2.609
PUB = [4.368, 2.518, 1.743, 1.283, 1.000, 0.769]
K = FD * 1000.0 / 60.0 / CIRC          # rpm per km/h per unit gearbox ratio
table = np.array(PUB) * K
roots = sorted(set(glob.glob(str(Path.home()/"canary-data/sessions*/2026-09-*")) +
                   glob.glob(str(Path.home()/"canary-data/sessions*/*/2026-09-*"))))
rows = []
for s in roots:
    p = Path(s)
    if not p.is_dir() or not list(p.glob("telemetry-*.parquet")): continue
    try:
        al = align_pairs(p, "decoded:ECM_RPM", "gps:speed", policy=PairingPolicy.within("60ms"))
    except (ComparisonRefusedError, Exception) as e:
        continue
    pr = al.pairs.sort_values("timestamp_b").reset_index(drop=True)
    if len(pr) < 20: continue
    v = pr["b"].to_numpy(float); r = pr["a"].to_numpy(float)
    t = pd.to_datetime(pr["timestamp_b"], utc=True).astype("int64").to_numpy() / 1e9
    ratio = np.where(v > 20, r / np.maximum(v, 1e-9), np.nan)
    # steady: the 5 GPS samples centred here (~±2 s) hold speed within 1 %
    # and the ratio within 1 %, and span no more than 6 s of real time
    for i in range(2, len(pr) - 2):
        w = slice(i - 2, i + 3)
        if t[i + 2] - t[i - 2] > 6.0: continue
        vv, rr = v[w], ratio[w]
        if np.isnan(rr).any(): continue
        if (vv.max() - vv.min()) > 0.01 * vv.mean(): continue
        if (rr.max() - rr.min()) > 0.01 * rr.mean(): continue
        rows.append((p.name, t[i], v[i], r[i], ratio[i]))
df = pd.DataFrame(rows, columns=["session", "t", "kph", "rpm", "ratio"])
df["gear"] = np.argmin(np.abs(df["ratio"].to_numpy()[:, None] - table[None, :]), axis=1) + 1
df["off"] = df["ratio"] / table[df["gear"] - 1] - 1
df = df[df["off"].abs() < 0.08]   # within 8 % of a published gear (clutch/slip rejects)
out = {}
for g, grp in df.groupby("gear"):
    implied = grp["ratio"] / K
    out[int(g)] = {
        "n_samples": int(len(grp)), "sessions": int(grp["session"].nunique()),
        "published": PUB[g - 1],
        "implied_median": round(float(implied.median()), 4),
        "p10": round(float(implied.quantile(0.1)), 4), "p90": round(float(implied.quantile(0.9)), 4),
        "vs_published_pct": round(float((implied.median() / PUB[g - 1] - 1) * 100), 2),
        "kph_range": [round(float(grp["kph"].min()), 1), round(float(grp["kph"].max()), 1)],
    }
print(json.dumps({"sessions_scanned": len(roots), "steady_samples": int(len(df)), "gears": out}, indent=1))
```
