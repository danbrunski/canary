"""Cross-reference captured Mode 22 DIDs against Mode 01 PID ground truth."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from json import JSONDecodeError
from math import sqrt
from pathlib import Path

from src.analysis.diagnostics import DEFAULT_DIAG_IDS, body_as_scalar_int
from src.analysis.iso_tp import decode_session, iter_frames
from src.session import FRAMES_FILENAME, PIDS_FILENAME


@dataclass(frozen=True)
class CrossRefBinding:
    """One scored DID -> PID binding from cross-reference regression."""

    did: int
    pid: str
    pid_unit: str
    n_pairs: int
    r_squared: float
    slope: float
    offset: float
    raw_range: tuple[int, int]
    decoded_range: tuple[float, float]


def load_pids_jsonl(path: Path) -> dict[str, list[tuple[float, float]]]:
    """PID -> list of (timestamp, decoded_value), sorted by timestamp.

    Skips non-numeric values (VIN, DTCs, etc.) because those cannot be
    regressed against raw DID values.
    """
    by_pid: defaultdict[str, list[tuple[float, float]]] = defaultdict(list)
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        value = rec.get("value")
        if not isinstance(value, (int, float)):
            continue
        try:
            pid = str(rec["pid"])
            timestamp = float(rec["t"])
        except (KeyError, TypeError, ValueError):
            continue
        by_pid[pid].append((timestamp, float(value)))
    for pid in by_pid:
        by_pid[pid].sort()
    return dict(by_pid)


def load_pid_units(path: Path) -> dict[str, str]:
    """First-seen unit per PID; units are expected to be stable across reads."""
    units: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        try:
            pid = str(rec["pid"])
        except KeyError:
            continue
        if pid not in units:
            units[pid] = str(rec.get("unit", ""))
    return units


def load_did_timeseries(
    frames_path: Path, diag_ids: set[int] | None = None
) -> dict[int, list[tuple[float, int]]]:
    """DID -> list of (timestamp, raw_value), sorted by timestamp."""
    ids = set(DEFAULT_DIAG_IDS) if diag_ids is None else diag_ids
    by_did: defaultdict[int, list[tuple[float, int]]] = defaultdict(list)
    for msg in decode_session(iter_frames(frames_path), diag_ids=ids):
        if msg.is_response and msg.service_id == 0x22 and msg.did is not None and msg.body:
            value = body_as_scalar_int(msg.body)
            if value is None:
                continue
            by_did[msg.did].append((msg.timestamp, value))
    for did in by_did:
        by_did[did].sort()
    return dict(by_did)


def pair_by_timestamp(
    a: Sequence[tuple[float, float]],
    b: Sequence[tuple[float, float]],
    tolerance_seconds: float = 0.5,
) -> list[tuple[float, float]]:
    """Pair each point in `a` with the nearest point in `b` within tolerance.

    The implementation is O(n+m) using a single forward sweep. Both inputs must
    be sorted by timestamp.
    """
    if not a or not b:
        return []
    pairs: list[tuple[float, float]] = []
    j = 0
    for ta, va in a:
        while j < len(b) and b[j][0] < ta - tolerance_seconds:
            j += 1
        if j >= len(b):
            break

        best_idx = j
        if j + 1 < len(b) and abs(b[j + 1][0] - ta) < abs(b[j][0] - ta):
            best_idx = j + 1

        if abs(b[best_idx][0] - ta) <= tolerance_seconds:
            pairs.append((va, b[best_idx][1]))
            j = best_idx + 1
        elif b[best_idx][0] < ta:
            j = best_idx + 1

    return pairs


def linear_regression(pairs: list[tuple[float, float]]) -> tuple[float, float, float]:
    """OLS fit `y = slope * x + offset`; returns `(slope, offset, r2)`."""
    n = len(pairs)
    if n < 3:
        return 0.0, 0.0, 0.0
    sx = sy = sxx = syy = sxy = 0.0
    for x, y in pairs:
        sx += x
        sy += y
        sxx += x * x
        syy += y * y
        sxy += x * y
    denom = n * sxx - sx * sx
    if denom == 0:
        return 0.0, 0.0, 0.0
    slope = (n * sxy - sx * sy) / denom
    offset = (sy - slope * sx) / n
    var_y = n * syy - sy * sy
    if var_y == 0:
        return slope, offset, 0.0
    r = (n * sxy - sx * sy) / sqrt(denom * var_y)
    return slope, offset, r * r


def cross_reference(
    session_dir: Path,
    *,
    min_pairs: int = 20,
    min_r_squared: float = 0.5,
    tolerance_seconds: float = 0.5,
    diag_ids: set[int] | None = None,
) -> list[CrossRefBinding]:
    """Load both streams, pair, regress, and return DID -> PID bindings."""
    frames_path = session_dir / FRAMES_FILENAME
    pids_path = session_dir / PIDS_FILENAME
    if not pids_path.exists():
        raise FileNotFoundError(
            f"no {PIDS_FILENAME} in {session_dir} - re-record with "
            "`record.py --obd-port /dev/ttyUSB0 ...` to capture Mode 01 "
            "ground truth alongside the CAN stream."
        )
    if not frames_path.exists():
        raise FileNotFoundError(f"no {FRAMES_FILENAME} in {session_dir}")

    did_series = load_did_timeseries(frames_path, diag_ids)
    pid_series = load_pids_jsonl(pids_path)
    pid_units = load_pid_units(pids_path)

    bindings: list[CrossRefBinding] = []
    for did, dpoints in did_series.items():
        for pid, ppoints in pid_series.items():
            pairs = pair_by_timestamp(dpoints, ppoints, tolerance_seconds=tolerance_seconds)
            if len(pairs) < min_pairs:
                continue
            slope, offset, r2 = linear_regression(pairs)
            if r2 < min_r_squared:
                continue
            raw_vals = [int(x) for x, _ in pairs]
            dec_vals = [y for _, y in pairs]
            bindings.append(
                CrossRefBinding(
                    did=did,
                    pid=pid,
                    pid_unit=pid_units.get(pid, ""),
                    n_pairs=len(pairs),
                    r_squared=r2,
                    slope=slope,
                    offset=offset,
                    raw_range=(min(raw_vals), max(raw_vals)),
                    decoded_range=(min(dec_vals), max(dec_vals)),
                )
            )

    bindings.sort(key=lambda b: -b.r_squared)
    return bindings
