"""Block 2 (tuning-did, road load): VVT solenoid duty ranges and B1/B2 relation."""

import sys
from pathlib import Path

import numpy as np

from src.analysis.channels import read_channels
from src.analysis.compare import ComparisonRefusedError, compare_channels
from src.signals.pairing import PairingPolicy

sess = Path(sys.argv[1])
specs = [
    "did:ECM_INTAKE_VVT_SOLENOID_DUTY",
    "did:ECM_INTAKE_VVT_SOLENOID_DUTY_CANDIDATE_B2",
    "did:ECM_INTAKE_CAM_ANGLE_COMMANDED",
    "did:ECM_INTAKE_CAM_ANGLE_MEASURED",
    "did:ECM_INTAKE_CAM_ANGLE_MEASURED_B",
    "did:ECM_RPM",
    "did:ECM_INJECTOR_PW_B1",
    "did:ECM_COOLANT_TEMP",
    "gps:speed",
]
long = read_channels(sess, specs)
for s in specs:
    v = long[long.spec == s]["value"].to_numpy(dtype=float)
    if len(v) == 0:
        print(f"{s}: no rows")
        continue
    moves = float(np.mean(np.diff(v) != 0)) if len(v) > 1 else 0.0
    q = np.percentile(v, [0, 1, 50, 99, 100])
    uniq = np.unique(v)
    ts = long[long.spec == s]["timestamp"]
    print(
        f"{s}: n={len(v)} unit={long[long.spec == s]['unit'].iloc[0]} span={ts.min()}..{ts.max()} "
        f"min/p1/p50/p99/max={q.round(2).tolist()} distinct={len(uniq)} moves={moves:.3f} "
        f"frac_zero={float(np.mean(v == 0)):.3f} over100={int(np.sum(v > 100))} over200={int(np.sum(v > 200))}"  # noqa: E501
    )
    if len(uniq) <= 40:
        print("   values:", uniq.tolist())
pol = PairingPolicy.within("70ms")
for a, b in [
    ("did:ECM_INTAKE_VVT_SOLENOID_DUTY", "did:ECM_INTAKE_VVT_SOLENOID_DUTY_CANDIDATE_B2"),
    ("did:ECM_INTAKE_CAM_ANGLE_COMMANDED", "did:ECM_INTAKE_VVT_SOLENOID_DUTY"),
    ("did:ECM_INTAKE_CAM_ANGLE_COMMANDED", "did:ECM_INTAKE_VVT_SOLENOID_DUTY_CANDIDATE_B2"),
    ("did:ECM_INTAKE_CAM_ANGLE_MEASURED", "did:ECM_INTAKE_CAM_ANGLE_MEASURED_B"),
]:
    try:
        c = compare_channels(sess, a, b, policy=pol)
        print(
            f"CMP {a} -> {b}: n_pairs={c.n_pairs} r2={c.r_squared:.3f} slope={c.slope:.4f} off={c.offset:.3f} "  # noqa: E501
            f"verdict={c.scale_verdict} drift={c.slope_drift_pct} diff_bias={c.difference_bias:.3f} "  # noqa: E501
            f"resid_spread={c.residual_spread:.3f} sep_p95={c.separation_p95_s:.3f}"
        )
    except ComparisonRefusedError as e:
        print(f"CMP {a} -> {b}: REFUSED {e}")
