#!/usr/bin/env python3
"""Find WOT pulls in a stored session and report a dRPM/dt torque proxy.

In a fixed gear, dRPM/dt is proportional to (engine torque − drag), so
repeated pulls over the same road are a dyno-free way to compare tunes.
This automates what was previously ad-hoc analysis: detection is purely
signal-driven (sustained RPM slope), honoring that CANary is a passive
recorder — no in-drive tagging required.

RPM source, best first:
  1. ``decoded:ECM_RPM`` rows (~100 Hz), decoded on read from the raw 0x1F9
     frames (P02.8: nothing stores them since 2026-09-26) — scaled to rpm via
     ``bindings.yaml``
  2. ``uprev:RPM`` (~16 Hz UpRev K-line import)
  3. ``obd:RPM`` (~1 Hz Mode 01 poll) — reported with a loud caveat: at
     this rate the derivative is qualitative at best

For UpRev-imported sessions the report adds AFR-vs-target error per pull
(banks averaged), with railed-wideband counts (<= 10.05 AFR is the sensor
floor, a lower bound on richness, not a reading).

Usage:
  tools/pull_report.py --config canary.dev.example.yaml --session 2026-07-28T164657
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pandas as pd

from src.analysis.pulls import Pull, RpmSeries, detect_pulls
from src.config import CanaryConfig
from src.dashboard.paths import DEFAULT_BINDINGS_PATH
from src.decode.bindings import apply_scaling, load_bindings
from src.decode.on_read import on_read_catalog
from src.preprocessing.decode import decode_on_read
from src.storage.broadcast_storage import session_can_storage

#: Fraction of the wideband's low rail treated as "pegged" (10.0 + margin).
WIDEBAND_RAIL_CEILING = 10.05


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        "pull_report", description="dRPM/dt torque-proxy report for WOT pulls."
    )
    parser.add_argument("--config", type=Path, default=Path("canary.yaml"))
    parser.add_argument(
        "--session", action="append", required=True, help="Session id (repeatable)."
    )
    parser.add_argument(
        "--bindings", type=Path, default=DEFAULT_BINDINGS_PATH, help="bindings.yaml path."
    )
    parser.add_argument(
        "--grid-ms", type=int, default=100, help="Resample grid for the derivative (ms)."
    )
    parser.add_argument(
        "--min-drdt",
        type=float,
        default=120.0,
        help="Sustained RPM slope (rpm/s) that counts as a pull.",
    )
    parser.add_argument(
        "--min-duration", type=float, default=1.5, help="Minimum pull duration (s)."
    )
    parser.add_argument("--min-span", type=float, default=500.0, help="Minimum RPM gained.")
    parser.add_argument(
        "--band", type=int, default=250, help="RPM band width for the per-band medians."
    )
    return parser.parse_args(argv)


def _read_session_frame(session_dir: Path) -> pd.DataFrame:
    """The session's numeric rows, ``decoded:`` rows decoded on read from the
    frames (P02.8), chunk by chunk so no payload outlives its chunk."""
    chunks = sorted(session_dir.glob("telemetry-*.parquet"))
    if not chunks:
        raise SystemExit(f"error: no telemetry parquet in {session_dir}")
    catalog = on_read_catalog()
    columns = ["timestamp", "source", "channel", "value_number"]
    frames = [
        decode_on_read(pd.read_parquet(c, columns=[*columns, "payload"]), catalog)[columns]
        for c in chunks
    ]
    return pd.concat(frames, ignore_index=True).sort_values("timestamp", kind="stable")


def _rpm_series(frame: pd.DataFrame, bindings_path: Path, *, held: bool = False) -> RpmSeries:
    decoded = frame[(frame["source"] == "decoded") & (frame["channel"] == "ECM_RPM")]
    if not decoded.empty and bindings_path.exists():
        bindings = load_bindings(bindings_path)
        sig = next((s for s in bindings.signals if s.name == "ECM_RPM"), None)
        if sig is not None and sig.scale is not None:
            t0 = decoded["timestamp"].iloc[0]
            secs = (decoded["timestamp"] - t0).dt.total_seconds().to_numpy()
            scaled = np.array(
                [apply_scaling(sig, int(v)) for v in decoded["value_number"].to_numpy()],
                dtype=float,
            )
            if held:
                return RpmSeries(
                    "decoded:ECM_RPM (change-only storage, held between changes)",
                    secs,
                    scaled,
                    t0,
                    held=True,
                )
            return RpmSeries("decoded:ECM_RPM (~100 Hz)", secs, scaled, t0)

    uprev = frame[(frame["source"] == "uprev") & (frame["channel"] == "RPM")]
    if not uprev.empty:
        t0 = uprev["timestamp"].iloc[0]
        secs = (uprev["timestamp"] - t0).dt.total_seconds().to_numpy()
        return RpmSeries("uprev:RPM (~16 Hz)", secs, uprev["value_number"].to_numpy(float), t0)

    obd = frame[(frame["source"] == "obd") & (frame["channel"] == "RPM")]
    if not obd.empty:
        t0 = obd["timestamp"].iloc[0]
        secs = (obd["timestamp"] - t0).dt.total_seconds().to_numpy()
        return RpmSeries(
            "obd:RPM (~1 Hz — derivative is QUALITATIVE at this rate; the session has "
            "no decodable 0x1F9 frames for the 100 Hz series)",
            secs,
            obd["value_number"].to_numpy(float),
            t0,
            low_rate=True,
        )
    raise SystemExit("error: session has no RPM channel (decoded/uprev/obd)")


def _afr_report(frame: pd.DataFrame, t0: pd.Timestamp, pull: Pull) -> str | None:
    channels = {"AFR_WB_B1", "AFR_WB_B2", "TARGET_AFR"}
    uprev = frame[(frame["source"] == "uprev") & frame["channel"].isin(channels)]
    if uprev.empty:
        return None
    window = uprev[
        ((uprev["timestamp"] - t0).dt.total_seconds() >= pull.start_s)
        & ((uprev["timestamp"] - t0).dt.total_seconds() <= pull.end_s)
    ]
    if window.empty:
        return None
    wide = window.pivot_table(index="timestamp", columns="channel", values="value_number")
    if "TARGET_AFR" not in wide or ("AFR_WB_B1" not in wide and "AFR_WB_B2" not in wide):
        return None
    banks = [c for c in ("AFR_WB_B1", "AFR_WB_B2") if c in wide]
    afr = wide[banks].mean(axis=1)
    railed = int((wide[banks] <= WIDEBAND_RAIL_CEILING).any(axis=1).sum())
    err = ((afr / wide["TARGET_AFR"] - 1) * 100).median()
    rail_note = f", railed {railed}/{len(wide)} (err is a lower bound)" if railed else ""
    return f"AFR err {err:+.1f}% vs target{rail_note}"


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    config = CanaryConfig.from_yaml(args.config)
    root = config.collection.storage_path

    for session_id in args.session:
        session_dir = root / session_id
        if not session_dir.is_dir():
            # Underscore-quarantined trees (e.g. _uprev imports) are fair game
            # for analysis — they're excluded from training, not from humans.
            matches = list(root.glob(f"_*/{session_id}"))
            if not matches:
                print(f"error: session not found: {session_id}", file=sys.stderr)
                continue
            session_dir = matches[0]

        frame = _read_session_frame(session_dir)
        series = _rpm_series(
            frame, args.bindings, held=session_can_storage(session_dir).any_change_only
        )
        pulls = detect_pulls(
            series,
            grid_ms=args.grid_ms,
            min_drdt=args.min_drdt,
            min_duration=args.min_duration,
            min_span=args.min_span,
            band=args.band,
        )
        print(f"=== {session_id}  [rpm source: {series.label}]")
        if not pulls:
            print("  no pulls detected")
            continue
        for i, pull in enumerate(pulls, 1):
            start = (series.t0 + pd.Timedelta(seconds=pull.start_s)).strftime("%H:%M:%S")
            flags = "  [DIP mid-pull — lift/shift?]" if pull.dip else ""
            print(
                f"  #{i} {start}  {pull.rpm_start:.0f}->{pull.rpm_end:.0f} rpm "
                f"in {pull.end_s - pull.start_s:.1f}s  peak {pull.peak_drdt:.0f} rpm/s{flags}"
            )
            bands = "  ".join(f"{b}:{v:.0f}" for b, v in sorted(pull.band_drdt.items()))
            print(f"      dRPM/dt by band: {bands}")
            afr = _afr_report(frame, series.t0, pull)
            if afr is not None:
                print(f"      {afr}")
        if series.low_rate:
            print("  note: 1 Hz OBD RPM — band values above are rough shapes, not comparisons")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
