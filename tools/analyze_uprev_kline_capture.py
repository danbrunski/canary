#!/usr/bin/env python3
"""Compare a live UpRev K-line probe capture with simultaneous CANary data.

The probe JSONL contains raw DID values sampled together in a dynamic UpRev
stream.  This tool represents those values as ``uprev:DID_xxxx`` telemetry
and delegates every comparison to :func:`src.analysis.compare.compare_channels`.
Consequently, results are based on one-to-one, same-instant observations and
carry the usual scale-stability verdict; no aggregate-to-aggregate matches
are produced.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from src.analysis.compare import ComparisonRefusedError, PairingPolicy, compare_channels
from src.preprocessing.loader import read_session

DEFAULT_REFERENCES = (
    "decoded:ECM_RPM",
    "decoded:ECM_COOLANT_TEMP",
    "decoded:ECM_THROTTLE_SENSOR_V",
    "decoded:ECM_ACCEL_PEDAL_POS_V",
    "gps:speed",
)
EXTERNAL_REFERENCE_SOURCES = frozenset({"decoded", "gps", "obd"})


def load_uprev_samples(path: Path) -> pd.DataFrame:
    """Return stream samples as canonical long telemetry rows."""
    rows: list[dict[str, object]] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                event: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            kind = event.get("kind")
            if kind not in {"stream_sample", "memory_stream_sample", "mixed_stream_sample"}:
                continue
            timestamp = event.get("timestamp")
            if not isinstance(timestamp, str):
                raise ValueError(f"{path}:{line_number}: malformed stream_sample")
            values = event.get("dids") if kind == "mixed_stream_sample" else event.get("values")
            if kind == "memory_stream_sample":
                values = {}
            if not isinstance(values, dict):
                raise ValueError(f"{path}:{line_number}: malformed DID values")
            for did_text, reading in values.items():
                if not isinstance(did_text, str) or not isinstance(reading, dict):
                    raise ValueError(f"{path}:{line_number}: malformed DID value")
                raw = reading.get("raw_int")
                raw_hex = reading.get("raw")
                if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                    raise ValueError(f"{path}:{line_number}: {did_text} has no numeric raw_int")
                if not isinstance(raw_hex, str):
                    raise ValueError(f"{path}:{line_number}: {did_text} has no raw hex value")
                did = int(did_text, 16)
                rows.append(
                    {
                        "timestamp": timestamp,
                        "source": "uprev",
                        "channel": f"DID_{did:04X}",
                        "value_number": float(raw),
                        # Preserve the response bytes as well as their raw
                        # integer. This lets the catalog-backed ``did:`` reader
                        # apply already-pinned engineering conversions for
                        # known-answer checks without a second importer.
                        "value_text": raw_hex,
                        "value_bool": None,
                        "unit": "counts",
                        "payload": None,
                        # Presence of metadata_json identifies these as stored
                        # host-receive timestamps to the shared pairing layer.
                        "metadata_json": "{}",
                    }
                )
            memory = event.get("memory") if kind == "mixed_stream_sample" else event.get("values")
            if kind == "stream_sample":
                memory = {}
            if not isinstance(memory, dict):
                raise ValueError(f"{path}:{line_number}: malformed memory values")
            ordered_memory: list[tuple[int, int, str]] = []
            for address_text, reading in memory.items():
                if not isinstance(address_text, str) or not isinstance(reading, dict):
                    raise ValueError(f"{path}:{line_number}: malformed memory value")
                raw = reading.get("raw_int")
                raw_hex = reading.get("raw")
                if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= 0xFF:
                    raise ValueError(f"{path}:{line_number}: {address_text} is not one byte")
                if not isinstance(raw_hex, str):
                    raise ValueError(f"{path}:{line_number}: {address_text} has no raw hex value")
                address = int(address_text, 16)
                ordered_memory.append((address, raw, raw_hex))
                rows.append(
                    {
                        "timestamp": timestamp,
                        "source": "uprev",
                        "channel": f"MEM_{address:08X}",
                        "value_number": float(raw),
                        "value_text": raw_hex,
                        "value_bool": None,
                        "unit": "counts",
                        "payload": None,
                        "metadata_json": "{}",
                    }
                )
            if len(ordered_memory) == 2 and ordered_memory[1][0] == ordered_memory[0][0] + 1:
                address = ordered_memory[0][0]
                raw_bytes = bytes(item[1] for item in ordered_memory)
                rows.append(
                    {
                        "timestamp": timestamp,
                        "source": "uprev",
                        "channel": f"MEM_BE16_{address:08X}",
                        "value_number": float(int.from_bytes(raw_bytes, "big")),
                        "value_text": raw_bytes.hex(),
                        "value_bool": None,
                        "unit": "counts",
                        "payload": None,
                        "metadata_json": "{}",
                    }
                )
    if not rows:
        raise ValueError(f"{path}: no stream_sample rows")
    frame = pd.DataFrame(rows)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    return frame.sort_values("timestamp", kind="stable").reset_index(drop=True)


def load_reference_rows(
    session: Path, references: Sequence[str], start: pd.Timestamp, end: pd.Timestamp
) -> pd.DataFrame:
    """Load external reference channels in the capture's time span.

    ``uprev:`` references already live in the probe frame and are deliberately
    skipped here. This permits same-reply DID-to-DID comparisons without
    duplicating their rows or pretending they came from the Pi.
    """
    wanted: set[tuple[str, str]] = set()
    for spec in references:
        source, separator, channel = spec.partition(":")
        if separator and source == "uprev":
            continue
        if not separator or source not in EXTERNAL_REFERENCE_SOURCES:
            raise ValueError(
                f"reference {spec!r} is unsupported here; use decoded:<channel>, "
                "gps:<channel>, obd:<channel>, or uprev:DID_<hex>"
            )
        wanted.add((source, channel))

    if not wanted:
        return pd.DataFrame()
    frame = read_session(session)
    in_time = (frame["timestamp"] >= start) & (frame["timestamp"] <= end)
    in_channel = pd.Series(False, index=frame.index)
    for source, channel in wanted:
        in_channel |= (frame["source"] == source) & (frame["channel"] == channel)
    return frame.loc[in_time & in_channel].copy()


def analyze(
    capture: Path,
    session: Path,
    references: Sequence[str],
    *,
    tolerance: str,
    min_pairs: int,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    uprev = load_uprev_samples(capture)
    # A small margin includes the nearest high-rate broadcast observation at
    # either edge while the explicit pairing tolerance still decides whether
    # it is usable.
    margin = pd.Timedelta(tolerance)
    reference_rows = load_reference_rows(
        session,
        references,
        pd.Timestamp(uprev["timestamp"].min()) - margin,
        pd.Timestamp(uprev["timestamp"].max()) + margin,
    )
    combined = pd.concat([reference_rows, uprev], ignore_index=True, sort=False)
    policy = PairingPolicy.within(tolerance)
    did_specs = sorted(f"uprev:{channel}" for channel in uprev["channel"].unique())
    results: list[dict[str, Any]] = []
    refusals: list[dict[str, str]] = []
    for did_spec in did_specs:
        for reference in references:
            if did_spec == reference:
                continue
            try:
                comparison = compare_channels(
                    combined,
                    did_spec,
                    reference,
                    policy=policy,
                    min_pairs=min_pairs,
                )
            except ComparisonRefusedError as exc:
                refusals.append({"did": did_spec, "reference": reference, "reason": str(exc)})
                continue
            results.append(comparison.as_dict())
    results.sort(key=lambda item: float(item["r_squared"]), reverse=True)
    return results, refusals


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True, help="uprev_kline_probe JSONL")
    parser.add_argument("--session", type=Path, required=True, help="CANary session directory")
    parser.add_argument(
        "--reference",
        action="append",
        dest="references",
        help=(
            "decoded:<channel>, gps:<channel>, obd:<channel>, or "
            "uprev:DID_<hex>; repeatable "
            "(defaults to common road references)"
        ),
    )
    parser.add_argument("--tolerance", default="50ms", help="maximum one-to-one pairing separation")
    parser.add_argument("--min-pairs", type=int, default=30)
    parser.add_argument("--min-r-squared", type=float, default=0.8, help="printed-result threshold")
    parser.add_argument("--out", type=Path, help="optional JSON report path")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    references = tuple(args.references or DEFAULT_REFERENCES)
    try:
        results, refusals = analyze(
            args.capture,
            args.session,
            references,
            tolerance=args.tolerance,
            min_pairs=args.min_pairs,
        )
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    report = {
        "capture": str(args.capture),
        "session": str(args.session),
        "policy": {"max_separation": args.tolerance},
        "references": list(references),
        "results": results,
        "refusals": refusals,
    }
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    shown = [result for result in results if float(result["r_squared"]) >= args.min_r_squared]
    for result in shown:
        drift = result["slope_drift_pct"]
        drift_text = "n/a" if drift is None else f"{float(drift):.2f}%"
        print(
            f"{result['a']} -> {result['b']}: r2={float(result['r_squared']):.5f} "
            f"n={result['n_pairs']} scale={float(result['slope']):.8g} "
            f"offset={float(result['offset']):.8g} drift={drift_text} "
            f"[{result['scale_verdict']}]"
        )
    print(
        f"analyzed {len(results)} identifiable comparisons; "
        f"{len(refusals)} refused; {len(shown)} at r2 >= {args.min_r_squared:g}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
