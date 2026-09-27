"""Block 6 arbitration: does the PW+MAF mass balance see the enrichment Mode 01 commands?

Pairing is chained align_pairs (same-instant, nothing interpolated or averaged):
  PW <-> MAF (both DIDs, same round), then that instant <-> DID rpm, then <-> Mode 01 phi.
phi_implied = 14.7 * PW_counts * (rpm/20) / (914943 * MAF_gps); PID 0x44 is phi on this ECM
(data/nissan-xterra-2008/afr_calibration.yaml, verified 2026-08-07).
"""

import sys
from pathlib import Path

import numpy as np

from src.analysis.channels import read_channels
from src.analysis.compare import align_pairs
from src.signals.pairing import PairingPolicy

sess = Path(sys.argv[1])
TOL = PairingPolicy.within("300ms")
pw_maf = align_pairs(sess, "did:ECM_INJECTOR_PW_B1", "did:ECM_MAF_GPS", policy=TOL).pairs
print(f"PW<->MAF pairs: {len(pw_maf)} sep_p95={np.percentile(pw_maf.separation_s, 95):.3f}s")


def join(base, t_col, spec, tol_s):
    long = read_channels(sess, [spec], acquired_time=True)
    side = long[long.spec == spec].sort_values("timestamp")
    t = side["timestamp"].to_numpy(dtype="datetime64[ns]").astype("int64") / 1e9
    v = side["value"].to_numpy(dtype=float)
    bt = base[t_col].to_numpy(dtype="datetime64[ns]").astype("int64") / 1e9
    idx = np.searchsorted(t, bt).clip(1, len(t) - 1)
    pick = np.where(np.abs(t[idx] - bt) < np.abs(t[idx - 1] - bt), idx, idx - 1)
    sep = np.abs(t[pick] - bt)
    out = base.copy()
    out[spec] = np.where(sep <= tol_s, v[pick], np.nan)
    out[spec + "_sep"] = sep
    return out


df = join(pw_maf, "timestamp_a", "did:ECM_RPM", 0.3)
df = join(df, "timestamp_a", "obd:COMMANDED_EQUIV_RATIO", 0.3)
df = join(df, "timestamp_a", "obd:FUEL_STATUS", 0.5)
df = join(df, "timestamp_a", "obd:O2_WR_LAMBDA_S1", 0.3)
df = df.dropna(subset=["did:ECM_RPM", "obd:COMMANDED_EQUIV_RATIO", "obd:FUEL_STATUS"])
pw, maf, rpm = df["a"].to_numpy(), df["b"].to_numpy(), df["did:ECM_RPM"].to_numpy()
phi_cmd = df["obd:COMMANDED_EQUIV_RATIO"].to_numpy()
lam = df["obd:O2_WR_LAMBDA_S1"].to_numpy()
phi_imp = 14.7 * pw * (rpm / 20.0) / (914943.0 * np.maximum(maf, 1e-6))
open_loop = df["obd:FUEL_STATUS"].to_numpy() >= 4
rich_cmd = phi_cmd > 1.05
print(f"joined rows {len(df)}; open loop {open_loop.sum()}; commanded phi>1.05 {rich_cmd.sum()}")
print("\n  phi_cmd  phi_imp  lambda  1/lambda   rpm    pw   maf  (commanded-rich samples, all)")
for i in np.where(rich_cmd)[0]:
    inv = 1.0 / lam[i] if lam[i] > 0 else float("nan")
    print(
        f"  {phi_cmd[i]:7.3f} {phi_imp[i]:8.3f} {lam[i]:7.3f} {inv:9.3f} {rpm[i]:5.0f} {pw[i]:6.0f} {maf[i]:5.1f}"  # noqa: E501
    )
for label, m in (
    ("closed loop, phi_cmd==1", ~rich_cmd & ~open_loop),
    ("commanded rich (phi>1.05)", rich_cmd),
):
    if m.sum() >= 3:
        print(
            f"\n{label}: n={m.sum()}  phi_cmd med {np.median(phi_cmd[m]):.3f}  "
            f"phi_imp p10/med/p90 {np.percentile(phi_imp[m], 10):.3f}/{np.median(phi_imp[m]):.3f}/{np.percentile(phi_imp[m], 90):.3f}  "  # noqa: E501
            f"1/lambda med {np.median(1.0 / np.where(lam[m] > 0, lam[m], np.nan)):.3f}"
        )
