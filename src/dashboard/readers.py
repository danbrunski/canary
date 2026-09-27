"""Pure file-backed dashboard readers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from src.alerts.event_logger import read_events_dir
from src.alerts.events import AnomalyEvent
from src.config import CanaryConfig
from src.dashboard.paths import DEFAULT_BINDINGS_PATH
from src.dashboard.status import _resolve_latest_session
from src.decode.accepted_baselines import (
    DEFAULT_ACCEPTED_BASELINES_PATH,
    load_accepted_baselines,
)
from src.decode.behavior_models import (
    DEFAULT_BEHAVIOR_MODELS_PATH,
    artifact_identity,
    behavior_policy_path,
    load_behavior_models,
    policy_unmatched_entries,
    serving_status,
    validation_scope,
)
from src.decode.bindings import load_bindings
from src.decode.dbc import DEFAULT_DBC_DIR, dbc_channel, load_dbc_dir
from src.decode.on_read import on_read_catalog
from src.preprocessing.decode import decode_on_read, decoded_channel
from src.storage.session_files import PARQUET_PATTERN
from src.timefmt import parse_iso_utc


def read_latest_telemetry_rows(
    storage_path: Path,
    *,
    n: int = 100,
) -> list[dict[str, Any]]:
    """Return the last ``n`` rows of the newest session, spanning chunks.

    ``decoded`` rows are decoded on read from the frames (P02.8), as every
    session reader sees them: a session recorded since then stores none.
    """
    latest = _resolve_latest_session(storage_path)
    if latest is None:
        return []
    chunks = sorted(latest.glob(PARQUET_PATTERN))
    if not chunks:
        return []
    catalog = on_read_catalog()
    collected: list[pd.DataFrame] = []
    total = 0
    for chunk in reversed(chunks):  # newest-first; stop once we have >= n
        if chunk.stat().st_size == 0:
            continue
        df = decode_on_read(pd.read_parquet(chunk), catalog)
        collected.append(df)
        total += len(df)
        if total >= n:
            break
    if not collected:
        return []
    table = (
        collected[0]
        if len(collected) == 1
        else pd.concat(collected[::-1], ignore_index=True)  # back to chronological
    )
    if len(table) > n:
        table = table.tail(n)
    rows: list[dict[str, Any]] = []
    for record in table.to_dict(orient="records"):
        rows.append(_normalize_telemetry_row({str(k): v for k, v in record.items()}))
    return rows


_PER_CHANNEL_COLUMNS = [
    "timestamp",
    "source",
    "channel",
    "value_number",
    "value_text",
    "value_bool",
    "unit",
]
#: ``_PER_CHANNEL_COLUMNS`` plus what the decode on read needs (P02.8).
_PER_CHANNEL_READ_COLUMNS = [*_PER_CHANNEL_COLUMNS, "payload"]


def read_latest_per_channel(
    storage_path: Path,
    *,
    window_seconds: float = 90.0,
    max_chunks: int = 300,
    session_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Return the most recent row for every distinct channel in the newest session.

    ``session_dir`` overrides the "newest session" search. Capture sessions are
    ``_``-prefixed and therefore *deliberately* invisible to that search (they
    must not reach the training corpus), so a caller that wants live state
    during a capture has to name the directory — otherwise it silently reads
    the previous session, which stopped being written when the capture started.
    That is exactly how `vehicle_state` came to report `no_data` for the whole
    of every tuning capture.

    ``decoded:`` rows are decoded on read from the window's frames (P02.8):
    the dashboard seed and the Parquet ``vehicle_state`` (broadcast rpm when
    Mode 01 is dark) get the rows a session recorded before then stored, and
    the same rows from one recorded after it, which stores none.
    """
    latest = session_dir if session_dir is not None else _resolve_latest_session(storage_path)
    if latest is None:
        return []
    chunks = [c for c in sorted(latest.glob(PARQUET_PATTERN)) if c.stat().st_size > 0]
    if not chunks:
        return []
    collected: list[pd.DataFrame] = []
    earliest: pd.Timestamp | None = None
    latest_seen: pd.Timestamp | None = None
    for chunk in reversed(chunks[-max_chunks:]):  # newest-first
        frame = pd.read_parquet(chunk, columns=_PER_CHANNEL_READ_COLUMNS)
        collected.append(frame)
        # The span so far, from each chunk's own extremes: re-concatenating
        # every chunk read so far (payloads included) made this quadratic.
        ts = pd.to_datetime(frame["timestamp"], utc=True)
        if not ts.empty:
            earliest = ts.min() if earliest is None else min(earliest, ts.min())
            latest_seen = ts.max() if latest_seen is None else max(latest_seen, ts.max())
        if (
            earliest is not None
            and latest_seen is not None
            and (latest_seen - earliest).total_seconds() >= window_seconds
        ):
            break
    table = pd.concat(collected[::-1], ignore_index=True)
    table = decode_on_read(table, on_read_catalog())[_PER_CHANNEL_COLUMNS]
    table["timestamp"] = pd.to_datetime(table["timestamp"], utc=True)
    table = table.sort_values("timestamp", kind="stable")
    # The literal newest row per channel. ``groupby(...).last()`` takes the
    # last *non-null* value of each column separately, so a newest row with no
    # number (``NO DATA``, a NaN) came back wearing its own fresh timestamp
    # and an older row's ``value_number``: a stale 12.6 V read as current,
    # which the non-volatile write gate's voltage check would have accepted
    # (H03.2 verification, 2026-09-26). The newest row says what it says.
    latest_rows = (
        table.groupby(["source", "channel"], sort=False)
        .tail(1)
        .sort_values(["source", "channel"], kind="stable")
    )
    rows: list[dict[str, Any]] = []
    for record in latest_rows.to_dict(orient="records"):
        rows.append(_normalize_telemetry_row({str(k): v for k, v in record.items()}))
    return rows


def read_obd_coverage(
    storage_path: Path,
    *,
    custom_pids_path: Path | None = None,
) -> dict[str, Any]:
    """OBD capability/coverage derived from observed telemetry.

    ``build_coverage_report`` is strictly the public Mode 01 registry view
    (Task 6.3) — a user-defined custom PID (Task 2.6/2.8) has no PID number
    in that registry, so it can never appear as one of its rows. They're
    listed separately here, straight off ``data/custom_pids.yaml``, so the
    Data Sources panel's OBD tab can still show every custom PID's live
    value without misrepresenting the honest J1979 supported/decoded counts
    above it.
    """
    from src.analysis.obd_coverage import build_coverage_report, channels_to_pids
    from src.collection.custom_pids import load_custom_pids

    latest = _resolve_latest_session(storage_path)
    observed: set[str] = set()
    if latest is not None:
        for chunk in sorted(latest.glob(PARQUET_PATTERN)):
            try:
                df = pd.read_parquet(chunk, columns=["source", "channel"])
            except (OSError, ValueError, KeyError):
                continue
            obd_rows = df[df["source"] == "obd"]
            observed.update(str(c) for c in obd_rows["channel"].unique())
    supported = channels_to_pids(observed)
    report = build_coverage_report(supported).to_dict()
    custom_pids = (
        load_custom_pids(custom_pids_path).custom_pids if custom_pids_path is not None else []
    )
    report["custom_pids"] = [
        {
            "name": p.name,
            "display_name": p.display_name,
            "channel": f"custom:{p.name}",
            "unit": p.unit,
            "category": p.category,
        }
        for p in custom_pids
    ]
    return report


def _normalize_telemetry_row(record: dict[str, Any]) -> dict[str, Any]:
    """Collapse value columns into one stable dashboard ``value`` field."""
    value = record.get("value_number")
    if value is None or pd.isna(value):
        value = record.get("value_text")
    if value is None or (isinstance(value, float) and pd.isna(value)):
        value = record.get("value_bool")
    return {
        "timestamp": _iso(record.get("timestamp")),
        "source": record.get("source"),
        "channel": record.get("channel"),
        "value": None if (isinstance(value, float) and pd.isna(value)) else value,
        "unit": record.get("unit"),
    }


def _iso(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()  # type: ignore[no-any-return]
    return str(value)


def read_anomalies_since(
    anomalies_dir: Path,
    *,
    since: str | None = None,
    source: str | None = None,
    mode: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Read anomalies from ``anomalies_dir`` with optional filters.

    ``since`` compares *parsed* timestamps, not strings — the log holds a mix
    of ``T``/``Z`` and space-separated ISO forms (older daemons stringified
    ``pd.Timestamp`` for behavior/fused/IF events), and a lexicographic
    compare silently dropped every space-separated event for any ``since``
    containing a ``T``. ``limit`` keeps the newest N (events stay
    oldest-first in the response).

    Every bound is pushed into :func:`read_events_dir`, which skips whole
    daily files outside it: this runs inside the collecting daemon, and
    parsing the full history before filtering stalled collection for ~30 s
    mid-drive (D03.28). With neither ``since`` nor ``limit`` it still reads
    everything.
    """
    if not anomalies_dir.exists():
        return []
    since_ts = parse_iso_utc(since) if since is not None else None  # 400 upstream

    def matches(event: AnomalyEvent) -> bool:
        if source is not None and event.source != source:
            return False
        return mode is None or event.operating_mode == mode

    keep = None if source is None and mode is None else matches
    events = read_events_dir(anomalies_dir, since=since_ts, limit=limit, keep=keep)
    return [e.to_dict() for e in events]


def echo_config(config: CanaryConfig) -> dict[str, Any]:
    return config.model_dump(mode="json")


def read_bindings_for_display(
    path: Path = DEFAULT_BINDINGS_PATH,
    *,
    confidence_levels: tuple[str, ...] = ("confirmed", "high", "medium"),
) -> dict[str, Any]:
    """Surface the display-relevant subset of ``bindings.yaml`` for the frontend."""
    if not path.exists():
        return {}
    bindings = load_bindings(path)
    out: dict[str, dict[str, Any]] = {}
    for signal in bindings.signals:
        if signal.confidence not in confidence_levels:
            continue
        out[decoded_channel(signal.name)] = {
            "name": signal.name,
            "scale": signal.scale,
            "offset": signal.offset,
            "unit": signal.unit,
            "confidence": signal.confidence,
            "plausible_min": signal.plausible_min,
            "plausible_max": signal.plausible_max,
        }
    return out


def read_can_coverage(
    bindings_path: Path = DEFAULT_BINDINGS_PATH,
    *,
    dbc_dir: Path = DEFAULT_DBC_DIR,
) -> dict[str, Any]:
    """Static catalog of decodable CAN *broadcast* signals (bindings + DBC).

    One row per signal with its source CAN ID, so the dashboard's CAN tab can
    render ``CAN ID · signal · live value`` and flag undecoded IDs. This is just
    the catalog (cheap, no corpus read) — the live value, the discovered/active
    state, and the undecoded-ID rows are overlaid client-side from the telemetry
    stream (``channels`` keyed by ``decoded:<NAME>`` / ``dbc:<NAME>`` / ``can:0x<ID>``).
    """
    signals: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()

    if bindings_path.exists():
        for signal in load_bindings(bindings_path).signals:
            key = (signal.can_id, signal.name)
            if key in seen:
                continue
            seen.add(key)
            signals.append(
                {
                    "can_id": signal.can_id,
                    "can_id_hex": f"0x{signal.can_id:X}",
                    "name": signal.name,
                    "unit": signal.display_unit,
                    "source": "decoded",
                    "channel": decoded_channel(signal.name),
                    "scale": signal.scale,
                    "offset": signal.offset,
                    "plausible_min": signal.plausible_min,
                    "plausible_max": signal.plausible_max,
                }
            )

    database = load_dbc_dir(dbc_dir)
    if database is not None:
        for message in database.messages:
            for signal in message.signals:
                key = (int(message.frame_id), signal.name)
                if key in seen:
                    continue
                seen.add(key)
                signals.append(
                    {
                        "can_id": int(message.frame_id),
                        "can_id_hex": f"0x{int(message.frame_id):X}",
                        "name": signal.name,
                        "unit": getattr(signal, "unit", "") or "",
                        "source": "dbc",
                        "channel": dbc_channel(signal.name),
                        "scale": signal.scale,
                        "offset": signal.offset,
                        "plausible_min": signal.minimum,
                        "plausible_max": signal.maximum,
                    }
                )

    signals.sort(key=lambda row: (row["can_id"], row["name"]))
    decoded_ids = sorted({row["can_id"] for row in signals})
    return {
        "signals": signals,
        "signal_count": len(signals),
        "decoded_id_count": len(decoded_ids),
        "decoded_ids_hex": [f"0x{cid:X}" for cid in decoded_ids],
    }


def read_behavior_models(path: Path = DEFAULT_BEHAVIOR_MODELS_PATH) -> dict[str, Any]:
    """Return ``behavior_models.yaml`` as a JSON-safe payload for the dashboard."""
    policy_path = behavior_policy_path(path)
    policy_block: dict[str, Any] = {
        "path": str(policy_path),
        "present": policy_path.exists(),
        "unmatched_entries": [],
    }
    if not path.exists():
        return {"schema_version": None, "models": [], "policy": policy_block}
    models = load_behavior_models(path)
    payload = models.model_dump(mode="json")
    # B01.6 — additive: whether each model may alert, and why not.
    # B03.8 — plus which artifact is serving (identity), its permitted uses
    # and lifecycle (inside ``serving_status``) and where its evidence holds.
    for entry, model in zip(payload["models"], models.models, strict=True):
        entry["serving_status"] = serving_status(model).to_dict()
        entry["artifact"] = artifact_identity(model)
        entry["validation_scope"] = validation_scope(model)
    policy_block["unmatched_entries"] = policy_unmatched_entries(models)
    payload["policy"] = policy_block
    return payload


def read_accepted_baselines(path: Path = DEFAULT_ACCEPTED_BASELINES_PATH) -> dict[str, Any]:
    """Return ``accepted_baselines.yaml`` as a JSON-safe payload for the
    dashboard (2.3, online-training-strategy.md Phase 2).

    ``active`` counts non-expired overrides — what the StatusStrip's
    "N accepted-baseline overrides active" reads. Every override (active
    or expired) is listed under ``overrides`` with its provenance and an
    ``expired`` flag, so the dashboard can flag a lapsed one as
    needs-re-confirmation instead of just dropping it from view. A missing
    file (most profiles never author one) is zero overrides, not an error.
    """
    baselines = load_accepted_baselines(path)
    overrides = [
        {
            "target": override.target,
            "channel": override.channel,
            "behavior_model": override.behavior_model,
            "operating_mode": override.operating_mode,
            "accepted_center": override.accepted_center,
            "band": override.band.model_dump(mode="json"),
            "declared_by": override.provenance.declared_by,
            "declared_at": override.provenance.declared_at.isoformat(),
            "review_by": override.provenance.review_by.isoformat(),
            "reason": override.provenance.reason,
            "expired": override.is_expired(),
        }
        for override in baselines.overrides
    ]
    active = sum(1 for o in overrides if not o["expired"])
    return {"active": active, "overrides": overrides}


__all__ = [
    "echo_config",
    "read_accepted_baselines",
    "read_anomalies_since",
    "read_behavior_models",
    "read_bindings_for_display",
    "read_can_coverage",
    "read_latest_per_channel",
    "read_latest_telemetry_rows",
    "read_obd_coverage",
]
