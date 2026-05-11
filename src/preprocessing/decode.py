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
from pathlib import Path
from typing import Literal

import pandas as pd

from src.decode.bindings import Bindings, Signal, load_bindings

DECODED_SOURCE = "decoded"
DEFAULT_BINDINGS_PATH = Path("data/bindings.yaml")


def decoded_channel(signal_name: str) -> str:
    """Return the wide-column name a decoded signal will occupy after `to_wide`."""
    return f"{DECODED_SOURCE}:{signal_name}"


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
    if not signals:
        return long_frame

    by_can_id: dict[int, list[Signal]] = {}
    for sig in signals:
        by_can_id.setdefault(sig.can_id, []).append(sig)

    decoded_frames: list[pd.DataFrame] = []
    # Group by the hex channel string so we parse the CAN ID once per group.
    for channel, group in can_rows.groupby("channel", sort=False):
        can_id = _parse_can_id(str(channel))
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
            end = sig.byte_start + sig.byte_length
            meta = json.dumps(
                {"can_id": can_id, "binding_confidence": sig.confidence},
                sort_keys=True,
            )

            def _extract(
                p: object,
                _start: int = sig.byte_start,
                _end: int = end,
                _order: Literal["big", "little"] = sig.byte_order,
            ) -> float:
                if not isinstance(p, (bytes, bytearray)) or len(p) < _end:
                    return float("nan")
                return float(int.from_bytes(bytes(p[_start:_end]), byteorder=_order))

            values = payloads.map(_extract).to_numpy()
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


def _parse_can_id(channel: str) -> int | None:
    """Parse a CAN channel string ('0x551' / '551') into an int, or None."""
    try:
        return int(channel, 16)
    except (ValueError, TypeError):
        return None
