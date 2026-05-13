#!/usr/bin/env python3
"""Inventory + RE-candidate report over the captured sessions corpus.

Mines the local ``data/sessions/`` corpus for:

* per-session summary — duration, frame count, OBD presence, tags
* CAN ID inventory across the corpus — top IDs by frame count, period
* bound-vs-unbound coverage — what fraction of CAN traffic has a binding
* binding candidates — unbound bytes with high cross-corpus variance
* 16-bit signal candidates — adjacent bound/unbound bytes whose values
  correlate strongly (Pearson r ≥ ``--correlation-threshold``), suggesting
  they belong to the same big-endian 16-bit word
* OBD PID coverage — which PIDs were polled across the corpus + their value
  ranges (the dashboard's "is the ECU actually reporting X?" view)

Output is a Markdown report written to stdout by default, or to
``--out FILE``. Designed to be re-run as the corpus grows; the report
header carries the timestamp + corpus shape so previous runs are
comparable in git diff.

This is a *read-only* analyzer — no Parquet writes, no edits to
bindings.yaml. The output's "binding candidates" / "16-bit pairs"
sections are inputs to the manual RE process documented in
``docs/can-re-workflow.md``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from statistics import median

import numpy as np

from src.decode.bindings import Bindings, Signal, load_bindings

FRAME_RE = re.compile(r"^\(([\d.]+)\)\s+\S+\s+([0-9A-Fa-f]+)#([0-9A-Fa-f]+)")


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class SessionInfo:
    path: Path
    started_at: datetime | None = None
    duration_seconds: float | None = None
    frame_count: int = 0
    obd_present: bool = False
    obd_pids: set[str] = field(default_factory=set)
    tag_count: int = 0


@dataclass
class CanIdSummary:
    can_id: int
    frame_count: int
    median_period_ms: float | None
    dlc: int
    sessions: set[str] = field(default_factory=set)
    # Per-byte: list of observed integer values; trimmed to last ``BYTE_SAMPLE_CAP``
    # per session to bound memory on long sessions.
    byte_samples: dict[int, list[int]] = field(default_factory=lambda: defaultdict(list))


# Each session contributes at most this many samples per byte to the corpus —
# enough for variance / correlation, cheap on memory for hours of CAN.
BYTE_SAMPLE_CAP = 4000


# ---------------------------------------------------------------------------
# Frame parsing
# ---------------------------------------------------------------------------


def _iter_frames(path: Path) -> Iterator[tuple[float, int, bytes]]:
    """Yield ``(timestamp, can_id, payload_bytes)`` for each frame in a candump log."""
    with path.open() as handle:
        for line in handle:
            m = FRAME_RE.match(line)
            if not m:
                continue
            try:
                ts = float(m.group(1))
                can_id = int(m.group(2), 16)
                payload = bytes.fromhex(m.group(3))
            except ValueError:
                continue
            yield ts, can_id, payload


def _scan_session(session: Path, ids: dict[int, CanIdSummary]) -> SessionInfo:
    info = SessionInfo(path=session)
    name = session.name
    try:
        info.started_at = datetime.strptime(name, "%Y-%m-%dT%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        pass

    frames_log = session / "frames.log"
    if frames_log.exists():
        first_ts: float | None = None
        last_ts: float | None = None
        per_id_timestamps: dict[int, list[float]] = defaultdict(list)
        per_session_bytes: dict[int, dict[int, list[int]]] = defaultdict(lambda: defaultdict(list))
        for ts, can_id, payload in _iter_frames(frames_log):
            if first_ts is None:
                first_ts = ts
            last_ts = ts
            info.frame_count += 1
            per_id_timestamps[can_id].append(ts)
            id_summary = ids.setdefault(
                can_id,
                CanIdSummary(can_id=can_id, frame_count=0, median_period_ms=None, dlc=len(payload)),
            )
            id_summary.frame_count += 1
            id_summary.sessions.add(name)
            id_summary.dlc = max(id_summary.dlc, len(payload))
            # Sample bytes (cap per session per byte position).
            for i, value in enumerate(payload):
                sb = per_session_bytes[can_id][i]
                if len(sb) < BYTE_SAMPLE_CAP:
                    sb.append(value)

        if first_ts is not None and last_ts is not None and last_ts > first_ts:
            info.duration_seconds = last_ts - first_ts

        # Median period per ID (rough cadence indicator)
        for can_id, timestamps in per_id_timestamps.items():
            if len(timestamps) < 2:
                continue
            deltas_ms = [(b - a) * 1000.0 for a, b in zip(timestamps, timestamps[1:], strict=False)]
            current = ids[can_id].median_period_ms
            new = median(deltas_ms)
            ids[can_id].median_period_ms = (
                new if current is None else (current + new) / 2  # simple corpus average
            )

        # Merge byte samples into the corpus-level summary
        for can_id, by_byte in per_session_bytes.items():
            for i, samples in by_byte.items():
                ids[can_id].byte_samples[i].extend(samples)

    # OBD presence
    pids_jsonl = session / "pids.jsonl"
    if pids_jsonl.exists():
        info.obd_present = True
        with pids_jsonl.open() as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                pid = rec.get("pid")
                if isinstance(pid, str):
                    info.obd_pids.add(pid)

    tags_jsonl = session / "tags.jsonl"
    if tags_jsonl.exists():
        info.tag_count = sum(1 for line in tags_jsonl.read_text().splitlines() if line.strip())

    return info


# ---------------------------------------------------------------------------
# Analyses
# ---------------------------------------------------------------------------


def _byte_variance(samples: list[int]) -> float:
    """Spread of a byte's observed values; values in [0, 255]."""
    if len(samples) < 8:
        return 0.0
    return float(np.var(np.asarray(samples, dtype=float)))


def _unique_byte_count(samples: list[int]) -> int:
    return len(set(samples))


def _adjacent_byte_correlation(a: list[int], b: list[int], min_samples: int = 200) -> float | None:
    """Pearson correlation between two same-length byte streams.

    Returns ``None`` when either side is too short or has zero variance —
    correlation isn't meaningful in those cases. The cap is per-byte ≤
    ``BYTE_SAMPLE_CAP``, matched across the pair by truncation.
    """
    n = min(len(a), len(b))
    if n < min_samples:
        return None
    arr_a = np.asarray(a[:n], dtype=float)
    arr_b = np.asarray(b[:n], dtype=float)
    if float(np.var(arr_a)) == 0 or float(np.var(arr_b)) == 0:
        return None
    corr = float(np.corrcoef(arr_a, arr_b)[0, 1])
    if not np.isfinite(corr):
        return None
    return corr


def _bindings_index(bindings: Bindings | None) -> dict[tuple[int, int], Signal]:
    """``(can_id, byte_offset)`` → owning binding."""
    if bindings is None:
        return {}
    out: dict[tuple[int, int], Signal] = {}
    for sig in bindings.signals:
        for off in range(sig.byte_start, sig.byte_start + sig.byte_length):
            out[(sig.can_id, off)] = sig
    return out


# ---------------------------------------------------------------------------
# CLI / report rendering
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="corpus_report",
        description="Inventory + RE-candidate report over the captured sessions corpus.",
    )
    parser.add_argument(
        "--sessions-root",
        type=Path,
        default=Path("data/sessions"),
        help="Where session directories live (default data/sessions).",
    )
    parser.add_argument(
        "--bindings",
        type=Path,
        default=Path("data/bindings.yaml"),
        help="bindings.yaml path; used for the bound-vs-unbound coverage section.",
    )
    parser.add_argument(
        "--top-ids",
        type=int,
        default=30,
        help="How many high-volume CAN IDs to list (default 30).",
    )
    parser.add_argument(
        "--candidate-limit",
        type=int,
        default=20,
        help="Cap on each candidate list — variance hot list + 16-bit pairs.",
    )
    parser.add_argument(
        "--correlation-threshold",
        type=float,
        default=0.95,
        help="Pearson r above which adjacent bytes are flagged as 16-bit pairs.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write to file instead of stdout (e.g. data/analyses/corpus-2026-05-13.md).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.sessions_root.exists():
        print(f"error: {args.sessions_root} does not exist", file=sys.stderr)
        return 1

    bindings = load_bindings(args.bindings) if args.bindings.exists() else None
    bindings_index = _bindings_index(bindings)

    ids: dict[int, CanIdSummary] = {}
    sessions: list[SessionInfo] = []
    for session_dir in sorted(args.sessions_root.iterdir()):
        if not session_dir.is_dir():
            continue
        sessions.append(_scan_session(session_dir, ids))

    out = _render(sessions, ids, bindings, bindings_index, args)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(out)
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(out)
    return 0


def _render(
    sessions: list[SessionInfo],
    ids: dict[int, CanIdSummary],
    bindings: Bindings | None,
    bindings_index: dict[tuple[int, int], Signal],
    args: argparse.Namespace,
) -> str:
    lines: list[str] = []
    now = datetime.now(UTC).isoformat()
    lines.append(f"# Corpus report ({now})\n")
    lines.append(_render_sessions(sessions))
    lines.append(_render_can_inventory(ids, args.top_ids, bindings_index))
    lines.append(_render_coverage(ids, bindings_index))
    lines.append(_render_binding_candidates(ids, bindings_index, args.candidate_limit))
    lines.append(
        _render_16bit_candidates(
            ids, bindings_index, args.correlation_threshold, args.candidate_limit
        )
    )
    lines.append(_render_obd_coverage(sessions))
    lines.append(_render_next_steps())
    return "\n".join(lines)


def _render_sessions(sessions: list[SessionInfo]) -> str:
    lines = ["## Sessions\n"]
    n_with_can = sum(1 for s in sessions if s.frame_count > 0)
    n_with_obd = sum(1 for s in sessions if s.obd_present)
    lines.append(
        f"{len(sessions)} session directory(ies) under `data/sessions/`; "
        f"{n_with_can} with `frames.log`, {n_with_obd} with `pids.jsonl`.\n"
    )
    lines.append("| Session | Duration | Frames | OBD PIDs | Tags |")
    lines.append("|---|---:|---:|---:|---:|")
    for s in sessions:
        dur = f"{s.duration_seconds:.0f}s" if s.duration_seconds else "—"
        obd = str(len(s.obd_pids)) if s.obd_pids else "—"
        lines.append(f"| `{s.path.name}` | {dur} | {s.frame_count:,} | {obd} | {s.tag_count} |")
    lines.append("")
    return "\n".join(lines)


def _render_can_inventory(
    ids: dict[int, CanIdSummary], limit: int, bindings_index: dict[tuple[int, int], Signal]
) -> str:
    lines = ["## CAN ID inventory\n"]
    if not ids:
        lines.append("_No CAN frames in the corpus._\n")
        return "\n".join(lines)
    by_count = sorted(ids.values(), key=lambda s: s.frame_count, reverse=True)
    total = sum(s.frame_count for s in by_count)
    lines.append(
        f"{len(by_count)} distinct CAN IDs across {total:,} total frames. "
        f"Top {min(limit, len(by_count))} by frame count:\n"
    )
    lines.append("| CAN ID | Frames | Period (ms) | DLC | Sessions | Bound? |")
    lines.append("|---|---:|---:|---:|---:|:---:|")
    for summary in by_count[:limit]:
        bound = any((summary.can_id, off) in bindings_index for off in range(summary.dlc))
        period = f"{summary.median_period_ms:.0f}" if summary.median_period_ms is not None else "—"
        lines.append(
            f"| `0x{summary.can_id:03X}` | {summary.frame_count:,} | {period} | "
            f"{summary.dlc} | {len(summary.sessions)} | {'✓' if bound else '—'} |"
        )
    lines.append("")
    return "\n".join(lines)


def _render_coverage(
    ids: dict[int, CanIdSummary], bindings_index: dict[tuple[int, int], Signal]
) -> str:
    lines = ["## Bound-vs-unbound coverage\n"]
    if not ids or not bindings_index:
        lines.append(
            "_No bindings or no CAN frames._ Once `data/bindings.yaml` carries "
            "signals, this section quantifies what fraction of CAN traffic is "
            "decoded.\n"
        )
        return "\n".join(lines)
    bound_ids = {can_id for (can_id, _off) in bindings_index}
    bound_frames = sum(s.frame_count for s in ids.values() if s.can_id in bound_ids)
    total_frames = sum(s.frame_count for s in ids.values())
    by_id_pct = 100 * len(bound_ids & set(ids)) / max(1, len(ids))
    by_frame_pct = 100 * bound_frames / max(1, total_frames)
    lines.append(
        f"* By CAN ID:    {len(bound_ids & set(ids))} / {len(ids)} ({by_id_pct:.1f}%)\n"
        f"* By frame count: {bound_frames:,} / {total_frames:,} ({by_frame_pct:.1f}%)\n"
    )
    return "\n".join(lines)


def _render_binding_candidates(
    ids: dict[int, CanIdSummary],
    bindings_index: dict[tuple[int, int], Signal],
    limit: int,
    min_samples: int = 1000,
) -> str:
    lines = ["## Binding candidates (highest-variance unbound bytes)\n"]
    candidates: list[tuple[float, int, int, int, int]] = []
    for summary in ids.values():
        for i, samples in summary.byte_samples.items():
            if (summary.can_id, i) in bindings_index:
                continue
            # Filter out diagnostic-response IDs (0x7Ex) by sample count — they
            # only carry traffic during ISO-TP exchanges (DID scans, NT510
            # module reads), so their byte variance is high but the population
            # is tiny and not a steady-state signal.
            if len(samples) < min_samples:
                continue
            var = _byte_variance(samples)
            if var < 1.0:
                continue
            candidates.append((var, _unique_byte_count(samples), summary.can_id, i, len(samples)))
    if not candidates:
        lines.append("_No unbound high-variance bytes found._\n")
        return "\n".join(lines)
    candidates.sort(reverse=True)
    lines.append(
        "Bytes with high observed variance that don't yet have a binding. "
        "Ranked candidates for `tools/broadcast_signals.py` differential or "
        "`tools/cross_reference_broadcast.py` regression next.\n"
    )
    lines.append("| CAN ID | Byte | Variance | Unique values | Samples |")
    lines.append("|---|:---:|---:|---:|---:|")
    for var, uniq, can_id, byte, n in candidates[:limit]:
        lines.append(f"| `0x{can_id:03X}` | {byte} | {var:.1f} | {uniq} | {n:,} |")
    lines.append("")
    return "\n".join(lines)


def _render_16bit_candidates(
    ids: dict[int, CanIdSummary],
    bindings_index: dict[tuple[int, int], Signal],
    threshold: float,
    limit: int,
) -> str:
    lines = ["## 16-bit signal candidates (adjacent-byte correlation)\n"]
    pairs: list[tuple[float, int, int, int]] = []
    for summary in ids.values():
        for byte in sorted(summary.byte_samples):
            next_byte = byte + 1
            if next_byte not in summary.byte_samples:
                continue
            # Skip pairs that are *both* already bound (already a known signal).
            if (summary.can_id, byte) in bindings_index and (
                summary.can_id,
                next_byte,
            ) in bindings_index:
                continue
            corr = _adjacent_byte_correlation(
                summary.byte_samples[byte], summary.byte_samples[next_byte]
            )
            if corr is None or abs(corr) < threshold:
                continue
            n = min(
                len(summary.byte_samples[byte]),
                len(summary.byte_samples[next_byte]),
            )
            pairs.append((abs(corr), summary.can_id, byte, n))
    if not pairs:
        lines.append(
            f"_No pairs with |r| ≥ {threshold:.2f}._ Lower `--correlation-threshold` if needed.\n"
        )
        return "\n".join(lines)
    pairs.sort(reverse=True)
    lines.append(
        f"Adjacent unbound byte pairs whose values correlate (|r| ≥ {threshold:.2f}). "
        "Strong correlation suggests the pair is the low+high bytes of a 16-bit "
        "big-endian signal — exactly the form `decode_raw(byte_length=2, byte_order=big)` "
        "expects. Worth running `cross_reference_broadcast.py` against PIDs / FSM names.\n"
    )
    lines.append("| CAN ID | Bytes | \\|r\\| | Samples |")
    lines.append("|---|:---:|---:|---:|")
    for corr, can_id, byte, n in pairs[:limit]:
        lines.append(f"| `0x{can_id:03X}` | {byte}-{byte + 1} | {corr:.3f} | {n:,} |")
    lines.append("")
    return "\n".join(lines)


def _render_obd_coverage(sessions: list[SessionInfo]) -> str:
    lines = ["## OBD-II PID coverage across the corpus\n"]
    obd_sessions = [s for s in sessions if s.obd_present]
    if not obd_sessions:
        lines.append("_No `pids.jsonl` sessions found yet._\n")
        return "\n".join(lines)
    pid_seen_in: dict[str, set[str]] = defaultdict(set)
    for s in obd_sessions:
        for pid in s.obd_pids:
            pid_seen_in[pid].add(s.path.name)
    lines.append(
        f"{len(obd_sessions)} session(s) carry OBD telemetry; "
        f"{len(pid_seen_in)} distinct PIDs observed.\n"
    )
    lines.append("| PID | Seen in N sessions |")
    lines.append("|---|---:|")
    for pid in sorted(pid_seen_in, key=lambda p: (-len(pid_seen_in[p]), p)):
        lines.append(f"| `{pid}` | {len(pid_seen_in[pid])} |")
    lines.append("")
    return "\n".join(lines)


def _render_next_steps() -> str:
    return (
        "## Suggested next analyses\n\n"
        "* **For each binding candidate**, run "
        "`tools/broadcast_signals.py <session>` against a tagged session to "
        "see if the byte's value clusters by tag — that's the simplest "
        "differential and the same idiom that pinned `ECM_RPM`.\n"
        "* **For each 16-bit pair**, run "
        "`tools/cross_reference_broadcast.py <session>` over the pair "
        "against the OBD PID set; high r² names the signal + scaling.\n"
        "* **Behavior models**: `scripts/refit_behavior_models.py --all` "
        "over the OBD sessions to fit the surfaces that have the data "
        "(coolant warmup, idle-RPM-vs-coolant, timing-vs-RPM).\n"
        "* **Anomaly baseline**: `scripts/train_anomaly_model.py` over the "
        "longest clean session, then `scripts/fp_benchmark.py` on a "
        "held-out one for the v1 done-definition's FPR claim.\n"
        "* **DID inventory** (vehicle): `tools/scan_constant_dids.py` "
        "to bring the Mode 22 calibration / live-DID space into the corpus.\n"
    )


if __name__ == "__main__":
    sys.exit(main())
