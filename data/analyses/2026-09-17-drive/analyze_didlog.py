"""Per-DID movement stats from a did_log JSONL, plus load-context slicing."""

import json
import sys
from collections import defaultdict

import numpy as np

path = sys.argv[1]
vals = defaultdict(list)  # did -> [(t, int)]
for line in open(path):
    r = json.loads(line)
    if r.get("kind") != "read":
        continue
    t = (r["t_send"] + r["t_recv"]) / 2
    for k, hexv in r["values"].items():
        vals[k].append((t, int(hexv, 16), len(hexv) // 2))

KNOWN = {
    "0x1101": "ECM_COOLANT_TEMP",
    "0x1219": "?",
    "0x123F": "?",
    "0x1208": "INJECTOR_PW_B1",
    "0x1209": "MAF_GPS",
    "0x1117": "CALC_LOAD_PCT",
    "0x1107": "AF_HEATER_DUTY",
    "0x1225": "AF_SENSOR_V_B1",
    "0x1226": "AF_SENSOR_V_B2",
    "0x120F": "?",
    "0x1210": "?",
    "0x1110": "VVT_DUTY",
    "0x1206": "INTAKE_PRESS?",
    "0x1207": "?",
    "0x1201": "ECM_RPM",
    "0x113A": "CAM_CMD",
    "0x1135": "CAM_MEAS",
    "0x1163": "CAM_MEAS_B",
    "0x1139": "VVT_DUTY_B2?",
}

rows = []
for did, series in sorted(vals.items()):
    t = np.array([s[0] for s in series])
    v = np.array([s[1] for s in series], dtype=float)
    w = series[0][2]
    moves = float(np.mean(np.diff(v) != 0)) if len(v) > 1 else 0.0
    rows.append((did, w, len(v), len(np.unique(v)), moves, v.min(), v.max(), np.median(v), t, v))

print(
    f"{'did':7} {'w':>2} {'n':>6} {'uniq':>5} {'moves':>6} {'min':>7} {'med':>8} {'max':>7}  name"
)
for did, w, n, uniq, moves, vmin, med, vmax, t, v in [  # noqa: B007
    (r[0], r[1], r[2], r[3], r[4], r[5], r[7], r[6], r[8], r[9]) for r in rows
]:
    print(
        f"{did:7} {w:>2} {n:>6} {uniq:>5} {moves:>6.3f} {vmin:>7.0f} {med:>8.1f} {vmax:>7.0f}  {KNOWN.get(did, '')}"  # noqa: E501
    )


# load context from the log's own channels: rpm 0x1201, injector pw 0x1208
def series_of(did):
    for r in rows:
        if r[0] == did:
            return r[8], r[9]
    return None, None


def interp(did, t_query):
    t, v = series_of(did)
    if t is None:
        return None
    return np.interp(t_query, t, v)


trpm, vrpm = series_of("0x1201")
print(
    f"\nrpm counts: min {vrpm.min():.0f} max {vrpm.max():.0f} (x0.125 -> {vrpm.min() * 0.125:.0f}..{vrpm.max() * 0.125:.0f} rpm)"  # noqa: E501
    if trpm is not None
    else "no rpm did"
)
