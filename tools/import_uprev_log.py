#!/usr/bin/env python3
"""Import an UpRev Osiris K-line datalog CSV as queryable telemetry.

data-architecture.md is explicit that CANary's job is "to treat UpRev logs
as an importable reference, not a live source" — but nothing did the
importing; every cross-reference was a fresh CSV parser written by hand.
This script normalizes an UpRev log into the same long-format Parquet
schema every other telemetry source uses (:class:`~src.telemetry.TelemetryRecord`,
source ``"uprev"``) and lands it under ``<storage_path>/_uprev/<session>/`` —
quarantined from training discovery exactly like ``_archive`` (leading-``_``
convention, AGENTS.md), but still reachable via
``query_telemetry(session="<session>", ...)``.

Unit normalization is the actual point, not a nicety: UpRev labels most
columns with a unit suffix (``"ENGINE RPM (rpm)"``, ``"CAL/LD VALUE (%)"``)
but *temperature* columns are the exception — ``"INTAKE AIR TMP"``,
``"COOLANT TEMP"`` and friends carry no suffix and are logged in Fahrenheit.
A filter written against the raw column (``coolant > 70``) silently passes
almost everything, at readings that look physically absurd once you notice
(196-199 in what you assumed was Celsius). This importer detects any
column whose name says TMP/TEMP and has no explicit unit suffix, converts
F -> C, and labels the result ``degC`` — the same unit CANary's own
COOLANT_TEMP/INTAKE_TEMP channels use — so the bug can't recur silently on
imported data either.

Absolute time anchor: UpRev's own ``Time`` column is *milliseconds*-since-
log-start, not wall-clock, so ``--started-at`` (timezone-aware ISO-8601) is
required — this script never guesses a timezone from the UpRev filename's
local-time encoding (``UpRev_Log7-26-2026_5-41-32_PM.csv``), only prints it
as a hint.

The millisecond unit is not cosmetic: every real Osiris export sampled to
date carries a median row step of 41-72 (a plausible 14-24 Hz K-line poll
rate in ms, an absurd one in seconds), and reading the column as seconds
stretches a 5-minute drive across 3.6 days — which then trips the writer's
session-gap rotation and shatters one log into hundreds of sessions.

Usage:
  tools/import_uprev_log.py --csv UpRev_Log7-26-2026_5-41-32_PM.csv \
      --started-at 2026-07-26T17:41:32-07:00 --label post-flash-validation
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from src.analysis.channels import read_channels
from src.analysis.clock_align import Alignment, AlignmentError, align_clocks
from src.analysis.tune_revisions import load_tune_revisions
from src.config import CanaryConfig
from src.control.sessions import resolve_session
from src.session import format_session_timestamp
from src.storage.parquet_writer import ParquetTelemetryWriter
from src.telemetry import TelemetryRecord

#: Raw UpRev header (post strip, pre unit-suffix-extraction) -> canonical
#: channel name, canonical unit, and whether the raw value needs F->C
#: (matching the equivalent CAN-side channel so the two are directly
#: comparable once queried). Proven against real logs by
#: ``scripts/timing_compare.py``'s ``UPREV_COLUMNS`` (this table is its
#: superset — extend as new column names are seen in practice).
KNOWN_COLUMNS: dict[str, tuple[str, str, bool]] = {
    "IGN TIMING (BTDC)": ("TIMING_ADVANCE", "degBTDC", False),
    "ENGINE RPM (rpm)": ("RPM", "rpm", False),
    "CAL/LD VALUE (%)": ("ENGINE_LOAD", "%", False),
    "INTAKE AIR TMP": ("INTAKE_TEMP", "degC", True),
}

#: Header carries an explicit unit in parens: "ENGINE RPM (rpm)" -> ("ENGINE
#: RPM", "rpm"). Temperature columns are conspicuously the ones that don't.
_UNIT_SUFFIX = re.compile(r"^(?P<name>.*?)\s*\((?P<unit>[^()]+)\)\s*$")
_TEMP_NAME = re.compile(r"\b(TMP|TEMP)\b", re.IGNORECASE)
_NOT_ALNUM = re.compile(r"[^A-Z0-9]+")

#: Never real channels — pandas' index-column artifact and the elapsed-time
#: column consumed separately as the timestamp anchor.
_SKIP_COLUMNS = frozenset({"TIME"})


def _normalize_channel_name(name: str) -> str:
    return _NOT_ALNUM.sub("_", name.upper()).strip("_")


def parse_column(header: str) -> tuple[str, str, bool]:
    """Return ``(channel, unit, is_fahrenheit)`` for a raw CSV column header.

    Known columns (`KNOWN_COLUMNS`) win outright. Otherwise: split off a
    ``(unit)`` suffix if present; a name containing TMP/TEMP with *no* unit
    suffix is assumed Fahrenheit (UpRev's convention for this vehicle/ROM —
    see the module docstring) and reported for F->C conversion by the
    caller; anything else keeps whatever unit (if any) the header stated.
    """
    stripped = header.strip()
    known = KNOWN_COLUMNS.get(stripped)
    if known is not None:
        return known
    match = _UNIT_SUFFIX.match(stripped)
    if match is not None:
        return _normalize_channel_name(match.group("name")), match.group("unit").strip(), False
    if _TEMP_NAME.search(stripped):
        return _normalize_channel_name(stripped), "degC", True
    return _normalize_channel_name(stripped), "", False


#: UpRev's own local-time filename convention — parsed only to print a
#: sanity-check hint against the operator-supplied ``--started-at``, never
#: used to compute anything (the timezone it's in is unknowable from the
#: filename alone).
_FILENAME_TIME = re.compile(
    r"UpRev_Log(?P<month>\d{1,2})-(?P<day>\d{1,2})-(?P<year>\d{4})_"
    r"(?P<hour>\d{1,2})-(?P<minute>\d{2})-(?P<second>\d{2})_(?P<meridiem>AM|PM)",
    re.IGNORECASE,
)


def filename_time_hint(name: str) -> str | None:
    match = _FILENAME_TIME.search(name)
    if match is None:
        return None
    hour = int(match.group("hour")) % 12
    if match.group("meridiem").upper() == "PM":
        hour += 12
    return (
        f"{match.group('year')}-{int(match.group('month')):02d}-{int(match.group('day')):02d} "
        f"{hour:02d}:{match.group('minute')}:{match.group('second')} (local, tz unknown)"
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="import_uprev_log",
        description="Import an UpRev Osiris K-line datalog CSV as queryable telemetry.",
    )
    parser.add_argument("--csv", type=Path, required=True, help="UpRev-exported CSV path.")
    parser.add_argument(
        "--started-at",
        required=True,
        help=(
            "Timezone-aware ISO-8601 timestamp for row 0 (UpRev's 'Time' column is "
            "milliseconds-since-log-start, not wall-clock). E.g. 2026-07-26T17:41:32-07:00."
        ),
    )
    parser.add_argument(
        "--label", default=None, help="Tag for this import (default: the CSV's filename stem)."
    )
    parser.add_argument(
        "--tune-revision", default=None, help="Explicit ECU tune revision for this log."
    )
    parser.add_argument(
        "--align-session",
        default=None,
        help=(
            "Session id recorded at the same time. The filename anchor is the laptop's "
            "local clock and the two genuinely differ (measured -1.3 s / -1.6 s), so "
            "RPM is cross-correlated against that session to refine --started-at. "
            "Refuses below r=0.99 rather than claiming a bogus offset — that threshold "
            "is what caught a nominal 314 s overlap containing zero real samples."
        ),
    )
    parser.add_argument(
        "--config", type=Path, default=Path("canary.yaml"), help="canary.yaml path."
    )
    return parser.parse_args(argv)


def _parse_started_at(raw: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise SystemExit(f"error: --started-at is not a valid ISO-8601 timestamp: {raw!r}") from exc
    if parsed.tzinfo is None:
        raise SystemExit(
            "error: --started-at must be timezone-aware (e.g. '...-07:00' or '...Z') — "
            "a naive timestamp can't be converted to the UTC session clock without guessing."
        )
    return parsed.astimezone(UTC)


def build_records(frame: pd.DataFrame, *, started_at: datetime) -> list[TelemetryRecord]:
    """Long-format records, chronological — required so the writer's
    session-gap rotation (``ParquetTelemetryWriter``) never sees an
    out-of-order timestamp and spuriously starts a second session."""
    if "Time" not in frame.columns:
        raise SystemExit("error: CSV has no 'Time' column (required to anchor row timestamps)")
    elapsed_ms = pd.to_numeric(frame["Time"], errors="coerce")
    start_epoch = started_at.timestamp()

    columns: list[tuple[str, str, str, bool]] = []
    for header in frame.columns:
        stripped = header.strip()
        if stripped.upper() in _SKIP_COLUMNS or stripped.startswith("Unnamed"):
            continue
        channel, unit, is_fahrenheit = parse_column(header)
        columns.append((header, channel, unit, is_fahrenheit))

    records: list[TelemetryRecord] = []
    for row_index in range(len(frame)):
        row_elapsed_ms = elapsed_ms.iloc[row_index]
        if pd.isna(row_elapsed_ms):
            continue
        timestamp = start_epoch + float(row_elapsed_ms) / 1000.0
        for header, channel, unit, is_fahrenheit in columns:
            raw_value = pd.to_numeric(frame[header].iloc[row_index], errors="coerce")
            if pd.isna(raw_value):
                continue
            value = (float(raw_value) - 32.0) * 5.0 / 9.0 if is_fahrenheit else float(raw_value)
            records.append(TelemetryRecord.uprev(timestamp, channel, value, unit))
    return records


def _align_to_session(
    frame: pd.DataFrame, started_at: datetime, session_id: str, config_path: Path
) -> Alignment:
    """Cross-correlate this log's RPM against a session's, for a real anchor.

    RPM is the signal because it is already confirmed on both sides, so a
    strong peak validates the offset, the binding, and that both loggers saw
    the same events — all at once.
    """
    if "ENGINE RPM (rpm)" not in frame.columns:
        raise ValueError("log has no 'ENGINE RPM (rpm)' column to align on")
    config = CanaryConfig.from_yaml(config_path)
    session_dir = resolve_session(config.collection.storage_path, session_id)

    pi = read_channels(session_dir, ["can:ECM_RPM"])
    if pi.empty:
        pi = read_channels(session_dir, ["decoded:ECM_RPM"])
    if pi.empty:
        raise ValueError(f"session {session_id} has no RPM to align against")
    reference = pd.Series(pi["value"].to_numpy(), index=pd.DatetimeIndex(pi["timestamp"]))

    candidate = pd.Series(
        pd.to_numeric(frame["ENGINE RPM (rpm)"], errors="coerce").to_numpy(),
        index=started_at
        + pd.to_timedelta(pd.to_numeric(frame["Time"], errors="coerce"), unit="ms"),
    )
    return align_clocks(reference, candidate)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.csv.exists():
        print(f"error: {args.csv} does not exist", file=sys.stderr)
        return 1
    started_at = _parse_started_at(args.started_at)
    hint = filename_time_hint(args.csv.name)
    if hint is not None:
        print(f"import_uprev_log: filename suggests local start ~{hint}", file=sys.stderr)

    raw = pd.read_csv(args.csv)

    alignment = None
    if args.align_session is not None:
        try:
            alignment = _align_to_session(raw, started_at, args.align_session, args.config)
        except (AlignmentError, FileNotFoundError, ValueError) as exc:
            print(f"error: clock alignment failed: {exc}", file=sys.stderr)
            return 1
        started_at = started_at + timedelta(seconds=alignment.offset_seconds)
        print(
            f"import_uprev_log: aligned to {args.align_session}: "
            f"{alignment.offset_seconds:+.2f}s (r={alignment.r:.5f}, "
            f"{alignment.overlapping_samples} overlapping samples)",
            file=sys.stderr,
        )

    records = build_records(raw, started_at=started_at)
    if not records:
        print("error: no usable rows (every row missing Time or every value NaN)", file=sys.stderr)
        return 1

    config = CanaryConfig.from_yaml(args.config)
    out_root = config.collection.storage_path / "_uprev"
    out_root.mkdir(parents=True, exist_ok=True)
    session_dir = out_root / format_session_timestamp(started_at)
    try:
        writer = ParquetTelemetryWriter(out_root, session_started_at=started_at)
    except FileExistsError:
        print(
            f"error: a session already exists at {session_dir} — an import at this exact "
            "--started-at (to the second) was already run. Pass a different --started-at "
            "(even by a second) to re-import, or leave the existing one.",
            file=sys.stderr,
        )
        return 1
    writer.write_many(records)
    writer.close()

    label = args.label or args.csv.stem
    channels = sorted({record.channel for record in records})
    manifest: dict[str, Any] = {
        "source": "uprev",
        "label": label,
        "original_filename": args.csv.name,
        "imported_at": datetime.now(UTC).isoformat(),
        "started_at": started_at.isoformat(),
        "row_count": len(records),
        "channels": channels,
    }
    registry = load_tune_revisions(config.daemon.afr_calibration.parent / "tune_revisions.yaml")
    inferred = registry.at(started_at).revision if registry and registry.at(started_at) else None
    manifest["tune_revision"] = args.tune_revision or inferred
    manifest["tune_revision_source"] = (
        "import_manifest"
        if args.tune_revision is not None
        else "registry"
        if inferred
        else "unknown"
    )
    if alignment is not None:
        # Recorded so a later analysis can tell a measured anchor from a
        # filename guess, and re-check the correlation that justified it.
        manifest["clock_alignment"] = {
            "session": args.align_session,
            **alignment.as_dict(),
        }
    writer.paths.manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True))

    print(
        f"import_uprev_log: wrote {len(records)} rows across {len(channels)} channels "
        f"-> {writer.paths.root}",
        file=sys.stderr,
    )
    print(
        f"  query_telemetry(session={writer.paths.root.name!r}, ...) to read it back",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
