"""Measure the trip-end digest's peak RSS against a real session (LAPTOP ONLY).

D03.6 claims the digest's peak memory is O(rows x channels) because the pivot
precedes the resample, and that a long live drive is only ~2x from the Pi's
8 GB. That claim was extrapolated from a 397k-row session at 1.49 KB/row; this
measures it directly at 2.6 M rows.
"""

import resource
import sys
from pathlib import Path

from src.config import CanaryConfig
from src.preprocessing.loader import read_session, resample_and_segment, to_wide

session = Path(sys.argv[1])
cfg = CanaryConfig.from_yaml(Path(sys.argv[2]))


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


print(f"baseline max_rss {rss_mb():.0f} MB")
long = read_session(session)
print(f"read_session: {len(long):,} rows x {len(long.columns)} cols -> max_rss {rss_mb():.0f} MB")
by_source = long.groupby("source", observed=True).size().sort_values(ascending=False)
print("  rows by source: " + ", ".join(f"{k} {v:,}" for k, v in by_source.items()))
distinct_ts = long["timestamp"].nunique()
print(f"  distinct timestamps {distinct_ts:,} ({distinct_ts / len(long):.3f} of rows)")
wide = to_wide(long, value="value_number")
peak = rss_mb()
print(f"to_wide: {len(wide):,} rows x {len(wide.columns)} cols -> max_rss {peak:.0f} MB")
print(f"\nPEAK {peak:.0f} MB for {len(long):,} rows = {peak * 1024 / len(long):.2f} KB per row")
print(f"extrapolated to 5.86 M rows: {peak / len(long) * 5_860_000 / 1024:.1f} GB")

# and the real chain the trip-end hook runs, in one process, from cold
print("\n--- resample_and_segment (the actual digest call) ---")
seg = resample_and_segment([session], cfg)
print(
    f"resample_and_segment: {len(seg):,} rows x {len(seg.columns)} cols -> peak {rss_mb():.0f} MB"
)
print(f"final frame is {len(seg) / len(long) * 100:.3f}% of the rows it loaded")
