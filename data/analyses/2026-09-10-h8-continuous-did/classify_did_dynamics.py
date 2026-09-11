"""H8 — classify the polled DIDs into fast dynamics vs slow context."""

import glob

import numpy as np
import pandas as pd
import yaml

D = "/data/canary/sessions/nissan-xterra-2008/_capture-tuning-did-2026-09-11T003700"
cat = {f"DID_{e['did']:04X}": e for e in yaml.safe_load(open("data/dids.yaml"))["dids"]}

files = sorted(glob.glob(D + "/telemetry-*.parquet"))
df = pd.concat(
    [pd.read_parquet(f, columns=["timestamp", "source", "channel", "value_text"]) for f in files],
    ignore_index=True,
)
o = df[df["source"] == "obd"].copy()
span = (o["timestamp"].max() - o["timestamp"].min()).total_seconds()


def decode(ch, hexes):
    e = cat.get(ch)
    if e is None:
        return None, ch, ""
    b0, bl = e.get("byte_start", 0), e.get("byte_length", 1)
    scale, off = e.get("scale", 1.0), e.get("offset", 0.0)
    big = e.get("byte_order", "big") == "big"
    out = []
    for h in hexes:
        try:
            raw = bytes.fromhex(h)[b0 : b0 + bl]
            if len(raw) < bl:
                out.append(np.nan)
                continue
            out.append(int.from_bytes(raw, "big" if big else "little") * scale + off)
        except Exception:
            out.append(np.nan)
    return np.array(out, dtype=float), e.get("name", ch), e.get("unit", "")


print(f"span {span:.0f}s ({span / 60:.1f} min)\n")
hdr = (
    f"{'DID':10s} {'name':30s} {'unit':6s} {'n':>7} {'Hz':>6} "
    f"{'range':>16} {'moves':>6} {'p95|d/dt|':>10} {'tau_s':>7}"
)
print(hdr)
print("-" * len(hdr))
rows = []
for ch, sub in o.groupby("channel"):
    sub = sub.sort_values("timestamp")
    vals, name, unit = decode(ch, sub["value_text"].tolist())
    if vals is None:
        continue
    t = sub["timestamp"]
    dt_ = t.diff().dt.total_seconds().to_numpy()[1:]
    v = pd.Series(vals)
    hz = len(v) / span
    moves = float((v.diff().abs() > 0).mean())
    d = v.diff().abs().to_numpy()[1:] / np.where(dt_ == 0, np.nan, dt_)
    p95 = float(np.nanpercentile(d, 95)) if np.isfinite(d).any() else 0.0
    rng = v.max() - v.min()
    # crude autocorrelation time: lag (s) at which r drops below 1/e
    x = v.to_numpy()
    x = x[np.isfinite(x)]
    tau = np.nan
    if len(x) > 2000 and x.std() > 0:
        xc = x - x.mean()
        for lag in (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000):
            if lag >= len(xc):
                break
            r = float(np.corrcoef(xc[:-lag], xc[lag:])[0, 1])
            if r < 0.3679:
                tau = lag / hz
                break
    span_txt = f"{v.min():.1f}..{v.max():.1f}"
    rows.append((ch, name, unit, len(v), hz, span_txt, moves, p95, tau, rng))

for r in sorted(rows, key=lambda r: -r[6]):
    ch, name, unit, n, hz, rng, mv, p95, tau, _ = r
    ts = f"{tau:7.2f}" if np.isfinite(tau) else "   >big"
    print(
        f"{ch:10s} {name[:30]:30s} {unit[:6]:6s} {n:7d} {hz:6.2f} "
        f"{rng:>16s} {mv:5.1%} {p95:10.2f} {ts}"
    )
