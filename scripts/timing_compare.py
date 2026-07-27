#!/usr/bin/env python3
"""Compare ignition-timing surfaces between two runs, for incremental tuning.

    # two CANary session sets (run on the Pi, where the parquet lives)
    scripts/timing_compare.py \
        --before-sessions 2026-07-22T161632 2026-07-18T215941 \
        --after-sessions  2026-07-26T225058

    # an UpRev K-line log against a CANary baseline
    scripts/timing_compare.py \
        --before-sessions 2026-07-22T161632 \
        --after-csv "UpRev_Log7-26-2026_5-41-32_PM.csv"

    # two UpRev logs (the normal before/after loop once you are tuning)
    scripts/timing_compare.py --before-csv before.csv --after-csv after.csv

Supports the protocol the operator settled on: change timing 1-2 degrees in one
cell region at a time, then verify before pushing further. It answers one
question per cell -- did the ECU actually run the timing you asked for, or did
it pull some back.

WHY WITHIN-CELL SCATTER IS THE KNOCK DETECTOR
---------------------------------------------
This ECM does not report knock. `HI DET` and `KNOCK STRENGTH` read constant
(1 and 0x500) across K-line sessions including a 4575 rpm pull, so there is no
knock channel on any transport. That is not a failed sensor -- the pre-flash
DTC baseline was clean on the stock calibration, with no P0325/P0330.

But knock retard is still observable, because the ECU pulls timing when it
hears detonation and we know what it *should* be running:

* When knock control is not intervening, timing for a cell is deterministic.
  Measured: 1000-1500 rpm x 15-30% load read a flat 17.0 deg with sd = 0.0
  across 33 samples.
* So scatter within a single cell, or a drop versus the previous run at
  matched conditions, means the ECU retarded. That is the signal.

If you can read the commanded value out of the Osiris map editor, pass
`--commanded` and the tool reports `commanded - actual` directly, which is the
unambiguous version of the same measurement.

WHAT THIS TOOL REFUSES TO DO
----------------------------
Report a difference it cannot support. Cells are suppressed unless the delta
clears `--sigma` times the standard error of the difference, because CANary
polls TIMING_ADVANCE at 0.06-0.26 Hz and routinely yields n=1-9 per cell over
a whole drive. A per-cell claim at that n is noise.
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

SESSION_ROOT = "/data/canary/sessions/nissan-xterra-2008"

#: Raw 0xFF on PID 0x0E. The decoder rejects this as of the sentinel fix, but
#: historical parquet still stores it as a real 63.5 degBTDC reading.
SATURATED_TIMING = 63.5

#: Below this load, decel / overrun / DFCO swing timing for reasons unrelated
#: to the calibration -- measured within-cell sd of 4-8 deg there.
DEFAULT_LOAD_FLOOR = 15.0

#: Comparing runs at materially different intake air temperature is invalid;
#: knock margin is IAT-dependent and observed IAT already spans 138-153 F.
DEFAULT_IAT_TOLERANCE_C = 8.0

#: Smallest delta worth calling a retard, in degrees. Statistical significance
#: alone is NOT sufficient here: with n in the hundreds the standard error
#: shrinks until a 1.0 deg difference "clears" any sigma threshold, and two
#: logs of the SAME calibration then light up as retard everywhere. Since the
#: tuning increment itself is 1-2 deg, a difference smaller than that cannot
#: be attributed to a change you made. A cell must clear BOTH this floor and
#: the sigma test.
DEFAULT_MIN_DELTA_DEG = 1.5

#: Minimum samples per cell before any verdict is issued. n=2 produces a
#: standard deviation but not a trustworthy one.
MIN_CELL_N = 5

RPM_EDGES = [600, 1000, 1500, 2000, 2500, 3500, 5000]
LOAD_EDGES = [15, 30, 50, 70, 100]

UPREV_COLUMNS = {
    "IGN TIMING (BTDC)": "timing",
    "ENGINE RPM (rpm)": "rpm",
    "CAL/LD VALUE (%)": "load",
    "INTAKE AIR TMP": "air_temp",
}


@dataclass
class Run:
    label: str
    frame: pd.DataFrame
    source: str


def _f_to_c(f: pd.Series) -> pd.Series:
    return (f - 32.0) * 5.0 / 9.0


def load_uprev(path: Path, label: str) -> Run:
    raw = pd.read_csv(path)
    raw = raw.loc[:, ~raw.columns.str.match("^Unnamed")]
    raw.columns = [c.strip() for c in raw.columns]
    missing = [c for c in ("IGN TIMING (BTDC)", "ENGINE RPM (rpm)") if c not in raw.columns]
    if missing:
        raise SystemExit(f"{path.name}: missing required column(s) {missing}")
    df = raw.rename(columns=UPREV_COLUMNS)
    keep = [c for c in ("timing", "rpm", "load", "air_temp") if c in df.columns]
    df = df[keep].apply(pd.to_numeric, errors="coerce")
    if "air_temp" in df:
        # UpRev logs intake air temp in F; CANary stores C.
        df["air_temp"] = _f_to_c(df["air_temp"])
    # Both are optional in the UpRev picker and genuinely absent from older
    # logs (the 2026-07-26 16:53 log has no INTAKE AIR TMP column at all).
    # Materialise them as NaN so downstream aggregation is uniform; the IAT
    # comparability check then simply cannot fire, which is the honest
    # outcome when the temperature is unknown.
    for optional in ("load", "air_temp"):
        if optional not in df:
            df[optional] = float("nan")
    return Run(label, df.dropna(subset=["timing", "rpm"]), f"uprev:{path.name}")


def load_sessions(sessions: list[str], label: str, root: str) -> Run:
    try:
        import duckdb
    except ImportError:  # pragma: no cover - duckdb ships with the venv
        raise SystemExit("duckdb is required to read CANary sessions") from None

    globs = ", ".join(f"'{root}/{s}/telemetry-*.parquet'" for s in sessions)
    sql = f"""
    WITH raw AS (
      SELECT timestamp, channel, value_number FROM read_parquet([{globs}])
      WHERE channel IN ('TIMING_ADVANCE','RPM','ENGINE_LOAD','INTAKE_TEMP')
        AND value_number IS NOT NULL
    ),
    tim AS (
      SELECT timestamp ts, value_number timing FROM raw
      WHERE channel = 'TIMING_ADVANCE' AND value_number <> {SATURATED_TIMING}
    ),
    r AS (SELECT timestamp AS ts, value_number AS rpm FROM raw WHERE channel = 'RPM'),
    -- "load" is a DuckDB reserved word; quote it rather than renaming the
    -- column, since the rest of the tool keys on that name.
    l AS (SELECT timestamp AS ts, value_number AS "load" FROM raw WHERE channel = 'ENGINE_LOAD'),
    i AS (SELECT timestamp AS ts, value_number AS air_temp FROM raw WHERE channel = 'INTAKE_TEMP')
    SELECT t.timing, r.rpm, l."load", i.air_temp
    FROM tim t
    ASOF LEFT JOIN r ON t.ts >= r.ts
    ASOF LEFT JOIN l ON t.ts >= l.ts
    ASOF LEFT JOIN i ON t.ts >= i.ts
    """
    df = duckdb.connect().sql(sql).df()
    return Run(label, df.dropna(subset=["timing", "rpm"]), f"canary:{len(sessions)} session(s)")


def _cells(df: pd.DataFrame, load_floor: float) -> pd.DataFrame:
    out = df[(df.rpm >= RPM_EDGES[0]) & (df.rpm < RPM_EDGES[-1])].copy()
    out = out[out.load >= load_floor]
    out["rpm_cell"] = pd.cut(out.rpm, RPM_EDGES, right=False)
    out["load_cell"] = pd.cut(out.load, LOAD_EDGES, right=False)
    return out.dropna(subset=["rpm_cell", "load_cell"])


def compare(
    before: Run,
    after: Run,
    *,
    load_floor: float,
    sigma: float,
    iat_tol: float,
    min_delta: float,
) -> int:
    b = _cells(before.frame, load_floor)
    a = _cells(after.frame, load_floor)
    if b.empty or a.empty:
        print("no cells survived filtering — check load floor and channel coverage")
        return 1

    def agg(df: pd.DataFrame) -> pd.DataFrame:
        g = df.groupby(["rpm_cell", "load_cell"], observed=True)
        return pd.DataFrame(
            {
                "n": g.timing.size(),
                "mean": g.timing.mean(),
                "sd": g.timing.std(),
                "air_temp": g["air_temp"].mean(),
            }
        )

    bb, aa = agg(b), agg(a)
    joined = bb.join(aa, lsuffix="_b", rsuffix="_a", how="outer")

    print(f"BEFORE  {before.label}  ({before.source})  {len(b)} samples in-window")
    print(f"AFTER   {after.label}  ({after.source})  {len(a)} samples in-window")
    print(f"filters: load >= {load_floor}%, timing != {SATURATED_TIMING} (0xFF sentinel)")
    print(
        f"reporting deltas that clear BOTH {sigma}x the standard error "
        f"AND {min_delta} deg (the practical floor)"
    )
    print(
        "assumes the two runs are at comparable conditions -- this tool cannot "
        "verify that, and an uncontrolled A/B will show differences that are "
        "not attributable to a calibration change\n"
    )

    hdr = (
        f"{'rpm':>10} {'load':>8} {'n_b':>4} {'before':>7} {'sd':>5} "
        f"{'n_a':>4} {'after':>7} {'sd':>5} {'delta':>7}  verdict"
    )
    print(hdr)
    print("-" * len(hdr))

    retard_cells: list[str] = []
    iat_warn: list[str] = []
    for (rc, lc), row in joined.iterrows():
        nb, na = row.get("n_b"), row.get("n_a")
        cell = f"{rc.left:.0f}-{rc.right:.0f}  {lc.left:.0f}-{lc.right:.0f}%"
        if pd.isna(nb) or pd.isna(na) or nb < MIN_CELL_N or na < MIN_CELL_N:
            print(
                f"{rc.left:5.0f}-{rc.right:<4.0f} {lc.left:3.0f}-{lc.right:<4.0f} "
                f"{0 if pd.isna(nb) else int(nb):>4} {'-':>7} {'-':>5} "
                f"{0 if pd.isna(na) else int(na):>4} {'-':>7} {'-':>5} {'-':>7}  "
                "insufficient samples"
            )
            continue

        delta = row["mean_a"] - row["mean_b"]
        sb = 0.0 if pd.isna(row["sd_b"]) else row["sd_b"]
        sa = 0.0 if pd.isna(row["sd_a"]) else row["sd_a"]
        se = math.sqrt(sb**2 / nb + sa**2 / na)
        clears_se = abs(delta) > sigma * se if se > 0 else abs(delta) > 0
        clears_floor = abs(delta) >= min_delta

        if not clears_floor:
            verdict = f"no change ({delta:+.1f} < {min_delta} deg floor)"
        elif not clears_se:
            verdict = f"within noise (se={se:.1f})"
        elif delta < 0:
            verdict = f"*** RETARD {delta:+.1f} — candidate knock limit ***"
            retard_cells.append(cell)
        else:
            verdict = f"advanced {delta:+.1f}, took cleanly"

        # IAT sanity: knock margin is temperature-dependent.
        ib, ia = row.get("air_temp_b"), row.get("air_temp_a")
        if not pd.isna(ib) and not pd.isna(ia) and abs(ia - ib) > iat_tol:
            verdict += f"  [IAT {ib:.0f}->{ia:.0f}C — NOT COMPARABLE]"
            iat_warn.append(cell)

        print(
            f"{rc.left:5.0f}-{rc.right:<4.0f} {lc.left:3.0f}-{lc.right:<4.0f} "
            f"{int(nb):>4} {row['mean_b']:>7.1f} {sb:>5.1f} "
            f"{int(na):>4} {row['mean_a']:>7.1f} {sa:>5.1f} {delta:>7.1f}  {verdict}"
        )

    print()
    if retard_cells:
        print(f"!! {len(retard_cells)} cell(s) ran LESS timing than the before run:")
        for c in retard_cells:
            print(f"     {c}")
        print(
            "   If the runs were at comparable conditions and you advanced these\n"
            "   cells, that is the ECU pulling timing back -- treat it as the knock\n"
            "   limit, back off, and re-verify. If conditions differed, this is not\n"
            "   evidence of anything."
        )
    else:
        print("No cell shows a significant retard versus the before run.")
    if iat_warn:
        print(
            f"\n!! {len(iat_warn)} cell(s) compared across an intake-air-temp gap "
            f"> {iat_tol}C. Knock margin is IAT-dependent; those rows are not a "
            "valid before/after comparison."
        )
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--before-sessions", nargs="+")
    p.add_argument("--after-sessions", nargs="+")
    p.add_argument("--before-csv", type=Path)
    p.add_argument("--after-csv", type=Path)
    p.add_argument("--sessions-root", default=SESSION_ROOT)
    p.add_argument("--load-floor", type=float, default=DEFAULT_LOAD_FLOOR)
    p.add_argument("--sigma", type=float, default=2.0)
    p.add_argument("--iat-tolerance", type=float, default=DEFAULT_IAT_TOLERANCE_C)
    p.add_argument(
        "--min-delta",
        type=float,
        default=DEFAULT_MIN_DELTA_DEG,
        help="Practical floor in degrees; deltas smaller than this are reported "
        "as no change regardless of statistical significance.",
    )
    p.add_argument(
        "--commanded",
        type=float,
        help="Commanded timing for the changed cells, read from the Osiris map "
        "editor. When given, a cell whose actual falls below this is an "
        "unambiguous retard rather than an inference from scatter.",
    )
    args = p.parse_args()

    def build(sessions: list[str] | None, csv: Path | None, label: str) -> Run:
        if csv is not None:
            return load_uprev(csv, label)
        if sessions:
            return load_sessions(sessions, label, args.sessions_root)
        raise SystemExit(f"provide --{label}-sessions or --{label}-csv")

    before = build(args.before_sessions, args.before_csv, "before")
    after = build(args.after_sessions, args.after_csv, "after")

    rc = compare(
        before,
        after,
        load_floor=args.load_floor,
        sigma=args.sigma,
        iat_tol=args.iat_tolerance,
        min_delta=args.min_delta,
    )

    if args.commanded is not None:
        a = _cells(after.frame, args.load_floor)
        pulled = a[a.timing < args.commanded - 0.5]
        print(
            f"\ncommanded {args.commanded:.1f} degBTDC: "
            f"{len(pulled)}/{len(a)} after-run samples fell below it"
        )
        if len(pulled):
            print("   -> direct evidence of knock retard, independent of scatter")
    return rc


if __name__ == "__main__":
    sys.exit(main())
