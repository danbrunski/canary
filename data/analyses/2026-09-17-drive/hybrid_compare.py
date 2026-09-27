"""Block 6: check DID scales against the Mode 01 PID for the same quantity (1:1 in engineering units)."""  # noqa: E501

import sys
from pathlib import Path

from src.analysis.channels import read_channels
from src.analysis.compare import ComparisonRefusedError, compare_channels
from src.signals.pairing import PairingPolicy

sess = Path(sys.argv[1])
specs = [
    "obd:MAF",
    "did:ECM_MAF_GPS",
    "obd:RPM",
    "did:ECM_RPM",
    "obd:COMMANDED_EQUIV_RATIO",
    "obd:O2_WR_LAMBDA_S1",
    "obd:FUEL_STATUS",
    "obd:ENGINE_LOAD",
    "obd:TIMING_ADVANCE",
    "did:ECM_INJECTOR_PW_B1",
    "did:ECM_AF_SENSOR_V_B1",
    "did:ECM_THROTTLE_POS_SENSOR_1",
]
long = read_channels(sess, specs)
for s in specs:
    r = long[long.spec == s]
    if r.empty:
        print(f"{s}: NO ROWS")
        continue
    v = r["value"]
    print(
        f"{s}: n={len(v)} unit={r['unit'].iloc[0]} min/p50/max={v.min():.3f}/{v.median():.3f}/{v.max():.3f}"  # noqa: E501
    )

for a, b, tol in [
    ("obd:RPM", "did:ECM_RPM", "500ms"),
    ("obd:MAF", "did:ECM_MAF_GPS", "500ms"),
    ("obd:RPM", "did:ECM_RPM", "250ms"),
    ("obd:MAF", "did:ECM_MAF_GPS", "250ms"),
]:
    try:
        c = compare_channels(sess, a, b, policy=PairingPolicy.within(tol))
        print(
            f"CMP[{tol}] {a} -> {b}: n_pairs={c.n_pairs} r2={c.r_squared:.4f} slope={c.slope:.4f} "
            f"off={c.offset:.3f} verdict={c.scale_verdict} drift={c.slope_drift_pct} "
            f"diff_bias={c.difference_bias:.4f} resid_spread={c.residual_spread:.4f} "
            f"sep_p95={c.separation_p95_s:.3f} basis={c.timestamp_basis_a}/{c.timestamp_basis_b}"
        )
    except ComparisonRefusedError as e:
        print(f"CMP[{tol}] {a} -> {b}: REFUSED {e}")
