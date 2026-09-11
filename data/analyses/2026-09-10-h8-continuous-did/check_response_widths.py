import glob
from collections import Counter

import pandas as pd
import yaml

D = "/data/canary/sessions/nissan-xterra-2008/_capture-tuning-did-2026-09-11T003700"
cat = {f"DID_{e['did']:04X}": e for e in yaml.safe_load(open("data/dids.yaml"))["dids"]}
files = sorted(glob.glob(D + "/telemetry-*.parquet"))
df = pd.concat(
    [pd.read_parquet(f, columns=["source", "channel", "value_text"]) for f in files],
    ignore_index=True,
)
o = df[df["source"] == "obd"]
print(f"{'DID':10s} {'name':34s} {'declared':>8} {'observed widths (bytes)':38s}")
for ch, sub in sorted(o.groupby("channel")):
    e = cat.get(ch, {})
    widths = Counter(len(h) // 2 for h in sub["value_text"] if isinstance(h, str))
    known = e.get("response_length")
    flag = "" if (known in widths and len(widths) == 1) else "   <-- no recorded width"
    name = str(e.get("name"))[:34]
    print(f"{ch:10s} {name:34s} {str(known):>8} {str(dict(widths)):30s}{flag}")
