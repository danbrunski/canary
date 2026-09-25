"""Decode CAN payloads into named signal columns using data/bindings.yaml.

Bridges the two halves of CANary:

  RE half:  captures → data/bindings.yaml (signal byte positions) →
            src/decode/bindings.py
  ML half:  loader → resample → segment → features → scaler → IsolationForest

Without this module the preprocessing pipeline sees CAN payloads only as
opaque `object`-dtype byte blobs — `compute_features` correctly skips them,
so the anomaly detector has nothing from the CAN bus to learn from. After
this, every binding becomes a numeric column the model can use, on data we
already have (no ELM327 required).

Decoded rows use `source="decoded"`, `channel=<signal name>`, so the
existing `to_wide` produces columns like `decoded:ECM_COOLANT_TEMP`. The
value is the RAW integer extracted via the binding's byte range (NOT the
scaled engineering value) — for anomaly detection the scaler normalizes
anyway, and many bindings don't have a pinned scale yet. `decode_bindings.py`
still uses `apply_scaling` for human-readable reports.

Note on scale: a bound CAN ID at ~10ms produces ~6k decoded rows/minute per
binding; over a multi-hour capture across ~16 bindings that's millions of
transient rows during the `to_wide` pivot (which then collapses to one
column per signal, downsampled by the resampler). Fine for the session
lengths we record now; revisit if multi-hour captures strain memory.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.can_ids import parse_can_channel
from src.decode.bindings import Bindings, Signal, decode_raw, load_bindings
from src.paths import BINDINGS_YAML
from src.telemetry import DECODED_SOURCE as DECODED_SOURCE
from src.telemetry import channel_key

DEFAULT_BINDINGS_PATH = BINDINGS_YAML


def decoded_channel(signal_name: str) -> str:
    """Return the wide-column name a decoded signal will occupy after `to_wide`."""
    return channel_key(DECODED_SOURCE, signal_name)


def decode_signal_column(payloads: pd.Series, signal: Signal) -> np.ndarray[Any, np.dtype[Any]]:
    """Raw integer value of ``signal`` in each payload, as float64 (R04.4).

    NaN where a payload is not bytes or is too short for the binding's byte
    range. Built on :func:`src.decode.bindings.decode_raw`, the one byte-slice
    rule, so this batch path, the storage-side ``FrameDecoder`` and
    ``tools/backfill_decoded.py`` cannot extract a signal differently.
    """

    def extract(payload: object) -> float:
        if not isinstance(payload, (bytes, bytearray)):
            return float("nan")
        raw = decode_raw(signal, bytes(payload))
        return float("nan") if raw is None else float(raw)

    return np.asarray(payloads.map(extract).to_numpy(), dtype=np.float64)


def decode_can_rows(
    long_frame: pd.DataFrame,
    bindings: Bindings,
    *,
    confidence_levels: set[str] | None = None,
) -> pd.DataFrame:
    """Append decoded-signal rows to a long-format telemetry frame.

    For every CAN row (`source == "can"`) whose arbitration ID has at least
    one binding, emit one new row per binding:
      source="decoded", channel=signal.name, value=raw integer from the
      binding's byte range. Rows whose payload is too short for a binding's
      byte range get NaN.

    `confidence_levels`, when given, restricts decoding to bindings at those
    confidence levels (e.g. `{"high", "medium"}` to skip hypothesized).
    Default `None` decodes all bindings.

    Returns a new frame with the decoded rows concatenated and re-sorted by
    timestamp. The input is not mutated. If `long_frame` is empty, has no
    CAN rows, or no binding matches, it's returned unchanged.
    """
    if long_frame.empty or "source" not in long_frame.columns:
        return long_frame

    can_rows = long_frame[long_frame["source"] == "can"]
    if can_rows.empty:
        return long_frame

    signals = bindings.signals
    if confidence_levels is not None:
        signals = [s for s in signals if s.confidence in confidence_levels]
    # Sessions written after decoded-channel persistence landed (or run
    # through tools/backfill_decoded.py) already carry stored decoded rows
    # with identical raw-value semantics — recomputing those signals here
    # would double every row and feed duplicate observations to `to_wide`.
    # Stored rows win; re-run the backfill tool to refresh them after a
    # binding definition changes.
    already_stored = set(long_frame.loc[long_frame["source"] == DECODED_SOURCE, "channel"].unique())
    if already_stored:
        signals = [s for s in signals if s.name not in already_stored]
    if not signals:
        return long_frame

    by_can_id: dict[int, list[Signal]] = {}
    for sig in signals:
        by_can_id.setdefault(sig.can_id, []).append(sig)

    decoded_frames: list[pd.DataFrame] = []
    # Group by the hex channel string so we parse the CAN ID once per group.
    for channel, group in can_rows.groupby("channel", sort=False):
        can_id = parse_can_channel(str(channel))
        if can_id is None or can_id not in by_can_id:
            continue
        payloads = group["payload"]
        n = len(group)
        timestamps = group["timestamp"].to_numpy()
        sessions = group["session"].to_numpy()
        session_paths = (
            group["session_path"].to_numpy() if "session_path" in group.columns else [None] * n
        )
        for sig in by_can_id[can_id]:
            meta = json.dumps(
                {"can_id": can_id, "binding_confidence": sig.confidence},
                sort_keys=True,
            )
            values = decode_signal_column(payloads, sig)
            decoded_frames.append(
                pd.DataFrame(
                    {
                        "timestamp": timestamps,
                        "source": DECODED_SOURCE,
                        "channel": sig.name,
                        "value_number": values,
                        "value_text": None,
                        "value_bool": None,
                        "unit": sig.unit,
                        "payload": None,
                        "metadata_json": meta,
                        "session": sessions,
                        "session_path": session_paths,
                        "source_channel": decoded_channel(sig.name),
                        "value": values,
                    }
                )
            )

    if not decoded_frames:
        return long_frame

    combined = pd.concat([long_frame, *decoded_frames], ignore_index=True)
    return combined.sort_values(["timestamp", "source", "channel"], kind="stable").reset_index(
        drop=True
    )


def read_session_decoded(
    session_dir: Path,
    *,
    bindings_path: Path = DEFAULT_BINDINGS_PATH,
    confidence_levels: set[str] | None = None,
) -> pd.DataFrame:
    """`read_session` then `decode_can_rows` in one call.

    Convenience for the standard pipeline entry point. If `bindings_path`
    doesn't exist, falls back to a plain `read_session` (no decoded rows) —
    so the pipeline still works in a checkout without the bindings file.
    """
    # Local import to avoid a module-level loader ↔ decode dependency.
    from src.preprocessing.loader import read_session

    long_frame = read_session(session_dir)
    if not bindings_path.exists():
        return long_frame
    bindings = load_bindings(bindings_path)
    return decode_can_rows(long_frame, bindings, confidence_levels=confidence_levels)


def can_channel_spellings(can_ids: Iterable[int]) -> list[str]:
    """Every spelling the ``channel`` of a raw CAN row has used for these ids.

    Sessions from different recorder eras write the arbitration id as
    ``0x551``, ``0x0551``, ``551`` and in either case; a parquet pushdown on
    one spelling silently reads nothing from the others. Callers still match
    rows exactly with :func:`src.can_ids.parse_can_channel` afterwards.
    """
    spellings: set[str] = set()
    for can_id in can_ids:
        for width in ("", "03", "04", "08"):
            for case in ("X", "x"):
                body = format(int(can_id), f"{width}{case}")
                spellings.update((f"0x{body}", f"0X{body}", body))
    return sorted(spellings)


def read_session_decoded_subset(
    session_dir: Path,
    *,
    bindings: Bindings | None,
    decoded: Iterable[str],
) -> tuple[pd.DataFrame, Bindings | None]:
    """One bounded read of a session for a *subset* of broadcast signals (R04.14).

    Returns ``(long, subset)``: the session's long frame holding every non-CAN
    row plus only the raw CAN rows whose id one of the ``decoded`` bindings
    reads -- pushed down to parquet over every era's channel spelling
    (:func:`can_channel_spellings`) and matched exactly by parsed id -- and
    ``bindings`` narrowed to those signals (``None`` when there is nothing to
    decode). Decoding is the caller's: ``decode_can_rows(long, subset)`` or
    the resample chain. A whole-bus read of one 42 MB drive peaked above 6 GB,
    which is why both bounded readers (``plausibility.offline.session_long``,
    ``analysis.behavior_validation.build_evaluation_frame``) exist at all.
    """
    from src.preprocessing.loader import read_session

    wanted = set(decoded)
    signals = [s for s in bindings.signals if s.name in wanted] if bindings is not None else []
    subset = bindings.model_copy(update={"signals": signals}) if bindings and signals else None
    filters: list[list[tuple[str, str, Any]]] = [[("source", "!=", "can")]]
    ids = {signal.can_id for signal in signals}
    if ids:
        filters.append([("source", "==", "can"), ("channel", "in", can_channel_spellings(ids))])
    long = read_session(Path(session_dir), filters=filters)
    is_can = (long["source"] == "can").to_numpy()
    if is_can.any():
        parsed = long.loc[is_can, "channel"].map(lambda c: parse_can_channel(str(c)))
        keep = np.ones(len(long), dtype=bool)
        keep[np.flatnonzero(is_can)] = parsed.isin(ids).to_numpy(dtype=bool)
        long = long.loc[keep].reset_index(drop=True)
    return long, subset
