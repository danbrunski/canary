"""Close the rich-side instrument question: PID 0x24 lambda vs the A/F voltage DID transfer.

afr_calibration.yaml: "NOT settled: this drive never sustained deep enrichment ...
A sustained WOT pull on this profile puts both instruments there and closes it in one run."
Pairing is align_pairs (same-instant). Stability filter is the one that file names:
|dAFR/dt| < 1 AFR/s.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from src.analysis.afr import AfrCalibration, voltage_to_afr
from src.analysis.compare import align_pairs
from src.analysis.pairing import PairingPolicy

sess = Path(sys.argv[1])
cal = AfrCalibration(**yaml.safe_load(open(sys.argv[2])))
STOICH = 14.7
for bank in (1, 2):
    lam_spec, v_spec = f"obd:O2_WR_LAMBDA_S{bank}", f"did:ECM_AF_SENSOR_V_B{bank}"
    try:
        ap = align_pairs(sess, lam_spec, v_spec, policy=PairingPolicy.within("300ms"))
    except Exception as exc:  # noqa: BLE001
        print(f"bank {bank}: {type(exc).__name__}: {exc}")
        continue
    p = ap.pairs.sort_values("timestamp_a").reset_index(drop=True)
    afr_pid = p["a"].to_numpy() * STOICH
    afr_v = voltage_to_afr(pd.Series(p["b"].to_numpy()), cal.voltage_transfer).to_numpy()
    t = p["timestamp_a"].to_numpy(dtype="datetime64[ns]").astype("int64") / 1e9
    ok = np.isfinite(afr_pid) & np.isfinite(afr_v) & (afr_pid > 0)
    d = np.gradient(afr_pid, t)
    stable = ok & (np.abs(d) < 1.0)
    rich = stable & (afr_pid < 13.0)
    deep = stable & (afr_pid < 12.8)
    print(
        f"\nbank {bank}: pairs {len(p)} sep_p95 {np.percentile(p.separation_s, 95):.3f}s  "
        f"usable {ok.sum()} stable {stable.sum()} stable&<13.0AFR {rich.sum()} stable&<12.8AFR {deep.sum()}"  # noqa: E501
    )
    for label, m in (
        ("near stoich 14.2-15.2", stable & (afr_pid > 14.2) & (afr_pid < 15.2)),
        ("rich <13.0", rich),
        ("deep <12.8", deep),
    ):
        if m.sum() >= 3:
            gap = (afr_v[m] - afr_pid[m]) / afr_pid[m] * 100.0
            print(
                f"  {label}: n={m.sum()} AFR_pid med {np.median(afr_pid[m]):.2f} "
                f"AFR_volt med {np.median(afr_v[m]):.2f} gap med {np.median(gap):+.2f}% "
                f"p10/p90 {np.percentile(gap, 10):+.2f}/{np.percentile(gap, 90):+.2f}%"
            )
