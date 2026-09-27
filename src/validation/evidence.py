"""Turn a scenario's raw artifacts into the C10.4–C10.7 evidence.

Everything here is read back from the Parquet the real writer produced —
never from a scheduler counter alone. The one equation every scenario must
satisfy exactly, overloaded or not::

    records the collector pushed == rows on disk (raw)
                                    + handoff buffer drops
                                    + ingest-queue drops
                                    + storage-sink drops

and, separately, every broadcast sequence number the adapter delivered is on
disk exactly once or is listed as missing (with the missing count bounded by
the drops above). No ``decoded:*`` row may be on disk at all (P02.8: decoded
rows are never stored, the readers decode the frames on read); that is its
own identity, never mixed into raw counts.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.analysis.capture_coverage import recompute_capture_quality
from src.analysis.channels import ACQUIRED_AT_COLUMN, read_acquisition_frame
from src.analysis.compare import ComparisonRefusedError, compare_channels
from src.analysis.pairing import PairingPolicy, to_seconds
from src.collection.capture_losses import (
    RECORDING,
    VERIFIED,
    CounterDelta,
    LossEpoch,
    reconcile_losses,
    summarize_losses,
)
from src.paths import DIDS_YAML
from src.storage.broadcast_storage import ChangeOnlyStorageError, refuse_change_only
from src.telemetry import CAPTURED_SOURCES, DECODED_SOURCE
from src.validation.harness import RunArtifacts
from src.validation.synthetic_bus import (
    SYNTH_BROADCAST_ID,
    responder_of,
    sequence_of_payload,
    service_of,
)

log = logging.getLogger(__name__)

BROADCAST_CHANNEL = f"0x{SYNTH_BROADCAST_ID:X}".upper()
RAW_SOURCES = ("can", "obd")
MAX_LISTED_SEQUENCES = 50
DIDS_PATH = DIDS_YAML


# ---------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------


def read_rows(session_dirs: Sequence[Path]) -> pd.DataFrame:
    """Every persisted row across the writer's session dirs, on the
    acquisition clock, tagged with the dir it came from.

    As recorded: this is an account of what the writer put on disk, so the
    loader's decode on read (P02.8) must not add rows to it.
    """
    frames: list[pd.DataFrame] = []
    for session_dir in session_dirs:
        if not any(session_dir.glob("telemetry-*.parquet")):
            continue
        frame = read_acquisition_frame(session_dir, decoded="as_recorded")
        frame["session_dir"] = str(session_dir)
        frames.append(frame)
    if not frames:
        return pd.DataFrame(
            columns=["timestamp", "source", "channel", "payload", ACQUIRED_AT_COLUMN, "session_dir"]
        )
    return pd.concat(frames, ignore_index=True)


def row_totals(frame: pd.DataFrame) -> dict[str, Any]:
    by_source = {str(k): int(v) for k, v in frame["source"].value_counts().items()}
    can = frame[frame["source"] == "can"]
    by_can_id = {str(k): int(v) for k, v in can["channel"].value_counts().items()}
    return {
        "total": int(len(frame)),
        "by_source": by_source,
        "raw": int(sum(by_source.get(source, 0) for source in RAW_SOURCES)),
        "can_by_id": by_can_id,
        "per_session_dir": {str(k): int(v) for k, v in frame["session_dir"].value_counts().items()}
        if "session_dir" in frame
        else {},
    }


# ---------------------------------------------------------------------------
# C10.5 — sequence and loss accounting
# ---------------------------------------------------------------------------


def persisted_sequences(frame: pd.DataFrame) -> list[int]:
    rows = frame[(frame["source"] == "can") & (frame["channel"].str.upper() == BROADCAST_CHANNEL)]
    out: list[int] = []
    for payload in rows["payload"]:
        if isinstance(payload, (bytes, bytearray)):
            seq = sequence_of_payload(bytes(payload))
            if seq is not None:
                out.append(seq)
    return out


def sequence_reconciliation(
    frame: pd.DataFrame, artifacts: RunArtifacts, *, drops_total: int = 0
) -> dict[str, Any]:
    """Every adapter-delivered sequence exactly once on disk, or named missing.

    ``drops_total`` is the loss the daemon *counted* (handoff, ingest queue,
    storage sink); a missing sequence beyond that count is unexplained loss.
    """
    ledger = artifacts.ledger
    if ledger is None or artifacts.scenario.kind == "replay":
        return {
            "status": "not_applicable",
            "reason": "replay run: rows come from a recorded session, not a numbered adapter",
        }
    delivered = ledger.delivered
    delivered_set = set(delivered)
    persisted = persisted_sequences(frame)
    counts: dict[int, int] = {}
    for seq in persisted:
        counts[seq] = counts.get(seq, 0) + 1
    present_once = sum(1 for seq in delivered_set if counts.get(seq, 0) == 1)
    duplicated = sorted(seq for seq, n in counts.items() if n > 1)
    missing = sorted(seq for seq in delivered_set if seq not in counts)
    foreign = sorted(seq for seq in counts if seq not in delivered_set)
    exact = present_once + len(missing) == len(delivered_set)
    if not exact or duplicated or foreign:
        status = "MISMATCH"
    elif not missing:
        status = "reconciled"
    elif len(missing) <= drops_total:
        status = "reconciled_with_counted_loss"
    else:
        status = "UNEXPLAINED_LOSS"
    return {
        "status": status,
        "counted_drops": drops_total,
        "generated": ledger.generated,
        "delivered": len(delivered),
        "delivered_distinct": len(delivered_set),
        "adapter_dropped_on_fault": ledger.dropped_on_fault,
        "adapter_pending_at_stop": ledger.pending_at_stop,
        "persisted_rows": len(persisted),
        "present_exactly_once": present_once,
        "missing_count": len(missing),
        "missing": missing[:MAX_LISTED_SEQUENCES],
        "duplicated_count": len(duplicated),
        "duplicated": duplicated[:MAX_LISTED_SEQUENCES],
        "foreign_count": len(foreign),
        "identity": "present_exactly_once + missing == delivered_distinct",
        "identity_holds": exact,
    }


def decoded_rows_stored(frame: pd.DataFrame) -> dict[str, Any]:
    """P02.8's identity: the writer stored no ``decoded`` row at all.

    Decoded rows are never stored (operator decision 2026-09-26); the storage
    sink refuses them whatever the profile or config says, and the readers
    decode the persisted frames on read. Until then this block reconciled
    them as ``persisted frames x persisted signals``.
    """
    decoded = frame[frame["source"] == DECODED_SOURCE]
    return {
        "decoded_rows": int(len(decoded)),
        "decoded_by_channel": {
            str(k): int(v) for k, v in decoded["channel"].value_counts().items()
        },
        "expected_decoded_rows": 0,
        "identity_holds": len(decoded) == 0,
        "note": "decoded rows are never stored (P02.8); readers decode the frames on read",
    }


def replay_accounting(frame: pd.DataFrame, artifacts: RunArtifacts) -> dict[str, Any]:
    """Replay rows persisted, per source, against the source session (D03.7).

    D02.9's live-GPS contamination (the Pi's receiver writing into
    ``_replay-*``) is a row-count identity against the source, and so is
    P02.8's rule that no ``decoded`` row is stored:

    - ``decoded`` persisted == 0, always. A payload-bearing source's stored
      ``decoded`` rows are skipped at the emit path, and a legacy
      payload-less source's are emitted (the live pipeline needs them) but
      never stored; the storage sink stores none whatever reaches it. Until
      P02.8 the identity was stored-emitted + re-derived by the storage
      decoder, which is how D02.9's doubling showed.
    - every captured source persisted == its count in the source, once a full
      pass completed with nothing dropped; any excess is listed as
      ``foreign_rows`` (rows no recording contained). ``can`` counts the
      frames the writer's change filter did not store as persisted (P02.10).
    """
    stats = artifacts.replay_emission
    if artifacts.scenario.kind != "replay" or stats is None or artifacts.replay_source is None:
        return {"status": "not_a_replay"}
    source = read_rows([artifacts.replay_source])
    source_by = {str(k): int(v) for k, v in source["source"].value_counts().items()}
    persisted_by = {str(k): int(v) for k, v in frame["source"].value_counts().items()}
    storage_dropped = int(_sink(artifacts, "storage").get("dropped_records") or 0)
    complete = stats.passes >= 1 and stats.rows_dropped == 0 and storage_dropped == 0
    out: dict[str, Any] = {
        "source_rows_by_source": source_by,
        "persisted_rows_by_source": persisted_by,
        "rederivable": stats.rederivable,
        "rederivable_reason": stats.rederivable_reason,
        "skipped_sources": list(stats.skipped_sources),
        "pass_complete": complete,
    }
    decoded_persisted = persisted_by.get(DECODED_SOURCE, 0)
    out["decoded"] = {
        "persisted": decoded_persisted,
        "expected": 0,
        "stored_in_source": source_by.get(DECODED_SOURCE, 0),
        "emitted_by_replay": 0 if stats.rederivable else source_by.get(DECODED_SOURCE, 0),
        "identity_holds": decoded_persisted == 0,
        "note": "decoded rows are never stored (P02.8), whatever the replay emits",
    }
    if complete:
        # P02.10: the writer stores a CAN frame only when its payload changes,
        # so a source stored every frame persists fewer ``can`` rows; each one
        # not stored is counted by the writer and closes the identity.
        suppressed = int(artifacts.can_frames_suppressed)
        accounted = dict(persisted_by)
        if suppressed:
            accounted["can"] = accounted.get("can", 0) + suppressed
        captured = sorted((set(source_by) | set(accounted)) & set(CAPTURED_SOURCES))
        foreign = {
            name: accounted.get(name, 0) - source_by.get(name, 0)
            for name in captured
            if accounted.get(name, 0) > source_by.get(name, 0)
        }
        missing = {
            name: source_by.get(name, 0) - accounted.get(name, 0)
            for name in captured
            if accounted.get(name, 0) < source_by.get(name, 0)
        }
        out["captured"] = {
            "identity_holds": not foreign and not missing,
            "can_frames_suppressed": suppressed,
            "foreign_rows": foreign,
            "missing_rows": missing,
        }
    else:
        out["captured"] = {
            "identity_holds": None,
            "reason": "no complete, drop-free pass over the source",
        }
    return out


def _sink(artifacts: RunArtifacts, name: str) -> dict[str, Any]:
    sinks = artifacts.health_snapshot.get("telemetry_sinks") or {}
    return dict(sinks.get(name) or {})


def loss_accounting(frame: pd.DataFrame, artifacts: RunArtifacts) -> dict[str, Any]:
    """The push equation above, plus queue high-water marks."""
    storage = _sink(artifacts, "storage")
    handoff = artifacts.handoff or {}
    receive = artifacts.receive_stats or {}
    if artifacts.scenario.kind == "canable":
        pushed = int(handoff.get("accepted", 0)) + int(handoff.get("buffer_dropped", 0))
        buffer_dropped = int(handoff.get("buffer_dropped", 0))
        queue_dropped = int(handoff.get("queue_dropped", 0))
        stage = "obd_collect"
    elif artifacts.scenario.kind == "passive":
        pushed = int(receive.get("frames", 0))
        buffer_dropped = int(receive.get("buffer_dropped", 0))
        queue_dropped = int(receive.get("queue_dropped", 0))
        stage = "can_receive"
    else:
        pushed = -1
        buffer_dropped = queue_dropped = 0
        stage = "replay"
    storage_dropped = int(storage.get("dropped_records") or 0)
    raw_rows = int(frame["source"].isin(RAW_SOURCES).sum())
    # P02.10: a CAN frame repeating its id's payload is not stored, by design;
    # the writer counts each one, so the identity still closes exactly.
    suppressed = int(artifacts.can_frames_suppressed)
    accounted = raw_rows + suppressed + buffer_dropped + queue_dropped + storage_dropped
    delivered_frames = sum(len(bus.handed_out) for bus in artifacts.buses)
    readings = None
    if artifacts.scheduler_metrics is not None and artifacts.reconnect_attempts == 0:
        readings = int(artifacts.scheduler_metrics.get("readings_emitted") or 0)
    sinks = artifacts.health_snapshot.get("telemetry_sinks") or {}
    high_water = {
        name: {
            "queue_depth_last": state.get("queue_depth"),
            "queue_high_water": state.get("queue_high_water"),
            "queue_max_size": state.get("queue_max_size"),
            "buffer_high_water": state.get("buffer_high_water"),
            "buffer_capacity": state.get("buffer_capacity"),
            "dropped_records": state.get("dropped_records"),
            "errors": state.get("errors"),
        }
        for name, state in sorted(sinks.items())
    }
    return {
        "stage": stage,
        "collector_pushed": pushed,
        "persisted_raw_rows": raw_rows,
        "can_frames_suppressed": suppressed,
        "buffer_dropped": buffer_dropped,
        "queue_dropped": queue_dropped,
        "storage_dropped": storage_dropped,
        "accounted": accounted,
        "identity": (
            "collector_pushed == persisted_raw_rows + can_frames_suppressed + buffer_dropped"
            " + queue_dropped + storage_dropped"
        ),
        "identity_holds": pushed == accounted if pushed >= 0 else None,
        "adapter_delivered_frames": delivered_frames,
        "readings_emitted_last_attempt": readings,
        "frames_tapped_identity": (
            None
            if readings is None
            else {
                "identity": "collector_pushed - readings_emitted == adapter_delivered_frames",
                "holds": pushed - readings == delivered_frames,
                "lhs": pushed - readings,
                "rhs": delivered_frames,
            }
        ),
        "queue_high_water": high_water,
        "kernel_counters": "unavailable (synthetic adapter; no can0 on this host)",
        "dashboard_subscriber": artifacts.dashboard,
    }


# ---------------------------------------------------------------------------
# C10.4 — transport evidence
# ---------------------------------------------------------------------------


def _interarrival(values: np.ndarray) -> dict[str, Any]:
    if len(values) < 2:
        return {
            "n": int(len(values)),
            "median_s": None,
            "p95_s": None,
            "p99_s": None,
            "max_s": None,
        }
    gaps = np.diff(np.sort(values))
    return {
        "n": int(len(gaps)),
        "median_s": round(float(np.median(gaps)), 6),
        "p95_s": round(float(np.percentile(gaps, 95)), 6),
        "p99_s": round(float(np.percentile(gaps, 99)), 6),
        "max_s": round(float(gaps.max()), 6),
    }


def _requested_rates(artifacts: RunArtifacts) -> dict[str, dict[str, Any]]:
    metrics = artifacts.scheduler_metrics or {}
    out: dict[str, dict[str, Any]] = {}
    for row in metrics.get("requests") or []:
        channel = str(row.get("channel", "")).upper()
        rate = row.get("configured_rate_hz", row.get("rate_hz"))
        out[channel] = {"kind": row.get("kind"), "requested_hz": rate}
    return out


def channel_cadence(frame: pd.DataFrame, artifacts: RunArtifacts) -> list[dict[str, Any]]:
    """Per-channel interarrival (median/p95/p99/max) and achieved vs requested,
    from the rows in the capture session dir."""
    capture = frame
    if artifacts.capture_dir is not None and "session_dir" in frame:
        capture = frame[frame["session_dir"] == str(artifacts.capture_dir)]
    requested = _requested_rates(artifacts)
    rows: list[dict[str, Any]] = []
    obd = capture[capture["source"] == "obd"]
    for channel, group in obd.groupby(obd["channel"].str.upper()):
        acquired = group[ACQUIRED_AT_COLUMN].astype("Float64")
        basis = "acquired_at"
        values = acquired.dropna().to_numpy(dtype=float)
        if len(values) < len(group):
            basis = "stored_timestamp"
            values = to_seconds(group["timestamp"])
        stats = _interarrival(values)
        want = requested.get(str(channel), {})
        median = stats["median_s"]
        rows.append(
            {
                "channel": str(channel),
                "kind": want.get("kind"),
                "requested_hz": want.get("requested_hz"),
                "achieved_hz": None if not median else round(1.0 / median, 3),
                "observations": int(len(group)),
                "clock": basis,
                "interarrival": stats,
            }
        )
    can = capture[capture["source"] == "can"]
    # P02.10: a change-only session stores a frame only when its payload
    # changes, so its stored inter-arrival times are not the bus's. Each
    # broadcast row says so instead of reporting a rate.
    refusal = _change_only_refusal(capture, reader="validation channel_cadence")
    for channel, group in can.groupby(can["channel"].str.upper()):
        values = to_seconds(group["timestamp"])
        stats = _interarrival(values)
        median = stats["median_s"]
        row: dict[str, Any] = {
            "channel": str(channel),
            "kind": "broadcast",
            "requested_hz": (
                artifacts.scenario.broadcast.rate_hz if str(channel) == BROADCAST_CHANNEL else None
            ),
            "achieved_hz": None if not median else round(1.0 / median, 3),
            "observations": int(len(group)),
            "clock": "stored_timestamp (adapter handout)",
            "interarrival": stats,
        }
        if refusal is not None:
            row.update(achieved_hz=None, interarrival=None, refused=refusal)
        rows.append(row)
    return sorted(rows, key=lambda r: (str(r["kind"]), r["channel"]))


def _change_only_refusal(capture: pd.DataFrame, *, reader: str) -> str | None:
    """The named refusal when any session dir in ``capture`` stores CAN frames
    change-only (P02.10), else ``None``."""
    if "session_dir" not in capture or capture.empty:
        return None
    try:
        refuse_change_only(
            [Path(p) for p in capture["session_dir"].dropna().unique()], reader=reader
        )
    except ChangeOnlyStorageError as exc:
        return str(exc)
    return None


def service_accounting(artifacts: RunArtifacts) -> dict[str, Any]:
    requests: dict[str, int] = {}
    responders: dict[str, int] = {}
    for bus in artifacts.buses:
        for _at, msg in bus.sent:
            name = service_of(msg)
            requests[name] = requests.get(name, 0) + 1
        for msg in bus.handed_out:
            name = responder_of(msg)
            responders[name] = responders.get(name, 0) + 1
    metrics = artifacts.scheduler_metrics or {}
    before_stop = artifacts.scheduler_metrics_before_stop or metrics
    return {
        "requests_on_wire_by_service": dict(sorted(requests.items())),
        "frames_delivered_by_responder": dict(sorted(responders.items())),
        "ecu": {
            "mode01_answered": sum(bus.mode01_answered for bus in artifacts.buses),
            "mode01_batch_requests": sum(bus.batch_requests for bus in artifacts.buses),
            "did_single_requests": sum(bus.did_single_requests for bus in artifacts.buses),
            "did_batch_requests": sum(bus.did_batch_requests for bus in artifacts.buses),
            "did_responses": sum(bus.did_responses for bus in artifacts.buses),
            "session_requests": sum(bus.session_requests for bus in artifacts.buses),
            "session_changes": sum(len(bus.session_changes) for bus in artifacts.buses),
            "lapses": sum(bus.lapses for bus in artifacts.buses),
            "support_probes": sum(len(bus.support_probes) for bus in artifacts.buses),
        },
        "scheduler_last_attempt": {
            key: metrics.get(key)
            for key in (
                "requests_sent",
                "readings_emitted",
                "request_timeouts",
                "negative_responses",
                "bus_errors",
                "response_rate",
                "bus_budget_requests_per_second",
                "rounds",
                "session_control",
                "discovery",
                "batching",
                "resolved_plan",
            )
        },
        "did_profile": before_stop.get("did_profile"),
        "did_profile_note": "last scheduler snapshot before the stop boundary (the boundary "
        "resets cycle accounting); may lag the stop by up to one metrics interval",
        "by_tier": metrics.get("by_tier"),
        "deferred": metrics.get("deferred"),
        "reconnect_attempts": artifacts.reconnect_attempts,
    }


def batch_occupancy(artifacts: RunArtifacts) -> dict[str, Any]:
    metrics = artifacts.scheduler_metrics or {}
    batching = metrics.get("batching") or {}
    ecu = {
        "mode01_batched_requests_seen": sum(bus.batch_requests for bus in artifacts.buses),
        "mode22_batched_requests_seen": sum(bus.did_batch_requests for bus in artifacts.buses),
        "mode22_single_requests_seen": sum(bus.did_single_requests for bus in artifacts.buses),
    }
    return {
        "scheduler": batching if batching else "unavailable",
        "ecu_side": ecu,
        "resolved_plan_demand": (metrics.get("resolved_plan") or {}).get(
            "diagnostic_requests_per_second", "unavailable (pre-C09 tree)"
        ),
    }


# ---------------------------------------------------------------------------
# C10.7 — persisted quality and known answers
# ---------------------------------------------------------------------------


def _drop_can0(deltas: Mapping[str, CounterDelta], dropped: int) -> dict[str, CounterDelta]:
    kept = {name: delta for name, delta in deltas.items() if not name.startswith("can0.")}
    kept["adapter.rx_dropped"] = CounterDelta(
        "adapter.rx_dropped", RECORDING, 0, dropped, dropped, VERIFIED
    )
    return kept


def quality_verdicts(artifacts: RunArtifacts) -> dict[str, Any]:
    """The stop-time C03 sidecar as written, and the same arithmetic with the
    kernel layer replaced by the synthetic adapter's delivery ledger."""
    if artifacts.capture_dir is None or artifacts.finalize is None:
        return {"status": "not_a_capture", "reason": "replay runs carry no capture profile"}
    stop_sidecar = artifacts.finalize
    result: dict[str, Any] = {
        "stop_sidecar": stop_sidecar,
        "verdict": stop_sidecar.get("verdict"),
        "failures": [f.get("clause") for f in stop_sidecar.get("failures") or []],
        "unverified_counters": (stop_sidecar.get("losses") or {})
        .get("recording", {})
        .get("unverified"),
    }
    if artifacts.start_epoch is None or artifacts.stop_epoch is None or artifacts.ledger is None:
        return result
    stop_epoch = LossEpoch.from_dict(artifacts.stop_epoch)
    deltas = reconcile_losses(artifacts.start_epoch.loss_epoch, stop_epoch)
    losses = summarize_losses(_drop_can0(deltas, artifacts.ledger.dropped_on_fault))
    try:
        adapter = recompute_capture_quality(
            artifacts.capture_dir,
            losses=losses,
            persistence=stop_sidecar.get("persistence"),
            scheduler=artifacts.scheduler_metrics,
            scope="recompute",
            batching_start=artifacts.start_epoch.batching,
            # Scored after the run has stopped; nothing on this loop is
            # waiting on it (the daemon path goes through a child process).
            allow_event_loop=True,
        )
        result["with_adapter_ledger"] = {
            "verdict": adapter.verdict,
            "failures": [f.get("clause") for f in adapter.failures],
            "losses_recording": adapter.losses.get("recording"),
            "tuples": adapter.tuples,
            "note": "can0.* (unknown on this host) replaced by adapter.rx_dropped from the "
            "synthetic ledger (frames queued in a faulted adapter); the kernel layer "
            "remains unobserved until the Pi run",
        }
    except Exception as exc:  # noqa: BLE001 — the failure is reported, not hidden
        result["with_adapter_ledger"] = {"error": f"{type(exc).__name__}: {exc}"}
    return result


KnownAnswerPair = tuple[str, str, str, float, float, float]


def synthetic_pairs(artifacts: RunArtifacts) -> list[KnownAnswerPair]:
    """(a, b, expectation, policy_ms, expected_slope, bias_tolerance)."""
    pairs: list[KnownAnswerPair] = [
        ("can:SYNTH_RPM", "obd:RPM", "unit_agreement", 50.0, 1.0, 15.0),
        ("can:SYNTH_RPM_HALF", "obd:RPM", "known_different", 50.0, 2.0, 15.0),
        ("can:SYNTH_SPEED", "obd:SPEED", "unit_agreement", 50.0, 1.0, 1.5),
    ]
    profile = artifacts.profile
    if profile.mode22:
        pairs.append(("did:ECM_RPM", "can:SYNTH_RPM", "unit_agreement", 50.0, 1.0, 30.0))
        if profile.mode01:
            pairs.append(("did:ECM_RPM", "obd:RPM", "cross_mode_bound", 250.0, 1.0, 150.0))
    return pairs


REPLAY_PAIRS: list[KnownAnswerPair] = [
    ("can:ECM_RPM", "obd:RPM", "unit_agreement", 250.0, 1.0, 40.0),
    ("can:WHEEL_SPEED_6F7_A", "obd:SPEED", "unit_agreement", 250.0, 1.0, 3.0),
]


def known_answer_checks(
    session_dir: Path, bindings_path: Path, pairs: Sequence[KnownAnswerPair]
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for a, b, expectation, policy_ms, expected_slope, bias_tol in pairs:
        policy = PairingPolicy.within(f"{policy_ms:g}ms")
        entry: dict[str, Any] = {
            "a": a,
            "b": b,
            "expectation": expectation,
            "policy_ms": policy_ms,
            "expected_slope": expected_slope,
        }
        try:
            comparison = compare_channels(
                session_dir, a, b, policy=policy, bindings_path=bindings_path, dids_path=DIDS_PATH
            )
        except ComparisonRefusedError as exc:
            entry.update({"outcome": "refused", "reason": str(exc)})
            results.append(entry)
            continue
        except Exception as exc:  # noqa: BLE001 — surface, never hide
            entry.update({"outcome": "error", "reason": f"{type(exc).__name__}: {exc}"})
            results.append(entry)
            continue
        slope_ok = abs(comparison.slope - expected_slope) <= 0.03 * max(1.0, expected_slope)
        bias_ok = abs(comparison.difference_bias) <= bias_tol
        if expectation == "unit_agreement":
            outcome = "agrees_1to1" if slope_ok and bias_ok else "FAILED"
        elif expectation == "known_different":
            outcome = "differs_as_expected" if slope_ok and not bias_ok else "FAILED"
        else:
            outcome = "bounded" if slope_ok else "FAILED"
        entry.update(
            {
                "outcome": outcome,
                "n_pairs": comparison.n_pairs,
                "slope": round(comparison.slope, 5),
                "offset": round(comparison.offset, 4),
                "r_squared": round(comparison.r_squared, 5),
                "difference_bias": round(comparison.difference_bias, 4),
                "difference_spread": round(comparison.difference_spread, 4),
                "residual_spread": round(comparison.residual_spread, 4),
                "separation_median_s": round(comparison.separation_median_s, 6),
                "separation_p95_s": round(comparison.separation_p95_s, 6),
                "separation_max_s": round(comparison.separation_max_s, 6),
                "scale_verdict": comparison.scale_verdict,
                "timestamp_basis": [comparison.timestamp_basis_a, comparison.timestamp_basis_b],
                "unmatched": [comparison.unmatched_a, comparison.unmatched_b],
            }
        )
        results.append(entry)
    return results


# ---------------------------------------------------------------------------
# C10.6 — timing
# ---------------------------------------------------------------------------


def timing_evidence(artifacts: RunArtifacts, frame: pd.DataFrame) -> dict[str, Any]:
    storage = _sink(artifacts, "storage")
    bytes_on_disk = sum(
        path.stat().st_size for d in artifacts.session_dirs for path in d.glob("*.parquet")
    )
    active_s = max(1e-9, artifacts.ended_wall - artifacts.armed_wall)
    return {
        "event_loop_heartbeat_overshoot": {
            **artifacts.heartbeat,
            "interval_ms": 10.0,
            "note": "delay beyond a 10 ms asyncio.sleep on the daemon loop",
        },
        "inference": artifacts.inference
        if artifacts.inference is not None
        else "disabled for this run",
        "isolation_forest_stub_calls": artifacts.if_stub_calls,
        "writer_flush_ms": artifacts.writer_flush,
        "writer_stalls_injected": artifacts.writer_stalls,
        "storage_sink": {
            key: storage.get(key)
            for key in (
                "write_calls",
                "records_processed",
                "flushes",
                "max_batch_size",
                "last_write_ms",
                "max_write_ms",
                "max_flushing_write_ms",
                "last_write_records_per_second",
            )
        },
        "cpu_memory": artifacts.rusage,
        "storage_growth": {
            "bytes_on_disk": bytes_on_disk,
            "rows_on_disk": int(len(frame)),
            "bytes_per_second_active": round(bytes_on_disk / active_s, 1),
            "rows_per_second_active": round(len(frame) / active_s, 1),
        },
        "power_thermal": "unavailable (workstation; no vcgencmd)",
        "kernel_socket_counters": "unavailable (synthetic adapter)",
        "wall_clock_steps": {
            "count": len(artifacts.wall_clock_steps),
            "steps": artifacts.wall_clock_steps,
            "attempts_used": artifacts.attempts_used,
            "valid": not artifacts.wall_clock_steps,
            "note": "a host wall-clock step during the run invalidates every wall-stamped "
            "pairing and cadence figure; such runs are rerun by run_scenario_clean",
        },
    }


# ---------------------------------------------------------------------------
# the limiting stage
# ---------------------------------------------------------------------------


def limiting_stage(
    artifacts: RunArtifacts,
    losses: Mapping[str, Any],
    quality: Mapping[str, Any],
    timing: Mapping[str, Any],
) -> dict[str, Any]:
    """Name the first stage that limited the run, with the evidence."""
    metrics = artifacts.scheduler_metrics or {}
    finalize = artifacts.finalize or {}
    persistence = finalize.get("persistence") or {}
    if losses.get("storage_dropped", 0) or persistence.get("drain_timed_out"):
        return {
            "stage": "storage_sink",
            "evidence": {
                "storage_dropped": losses.get("storage_dropped"),
                "drain_timed_out": persistence.get("drain_timed_out"),
                "flush_error": persistence.get("flush_error"),
                "writer_stalls_injected": artifacts.writer_stalls,
            },
        }
    if losses.get("buffer_dropped", 0):
        return {"stage": "thread_handoff", "evidence": {"buffer_dropped": losses["buffer_dropped"]}}
    if losses.get("queue_dropped", 0):
        return {"stage": "ingest_queue", "evidence": {"queue_dropped": losses["queue_dropped"]}}
    inference = timing.get("inference")
    if isinstance(inference, Mapping) and inference.get("missed_ticks"):
        return {
            "stage": "inference_loop",
            "evidence": {
                "missed_ticks": inference.get("missed_ticks"),
                "missed_by_reason": inference.get("missed_by_reason"),
                "lateness": inference.get("lateness"),
            },
        }
    failures = list(quality.get("failures") or [])
    budget = float(metrics.get("bus_budget_requests_per_second") or 0.0)
    sent = int(metrics.get("requests_sent") or 0)
    active_s = max(1e-9, artifacts.ended_wall - artifacts.started_wall)
    utilisation = round(sent / (budget * active_s), 3) if budget > 0 else None
    timeouts = int(metrics.get("request_timeouts") or 0)
    if "rate_shortfall" in failures or "tuple_incomplete" in failures:
        if utilisation is not None and utilisation >= 0.8:
            return {
                "stage": "bus_budget_request_pacing",
                "evidence": {
                    "budget_rps": budget,
                    "requests_sent": sent,
                    "utilisation": utilisation,
                    "failures": failures,
                },
            }
        if timeouts == 0 and utilisation is not None and utilisation < 0.5:
            # The rate was missed with the bus mostly idle and every request
            # answered: neither the budget nor the ECU was the constraint, so
            # the schedule simply did not ask often enough. Measured in the
            # C04.12 policy comparison — the interval policy missed 4.0 Hz by
            # 26 % at 0.266 utilisation with zero timeouts, where the cycle
            # policy hit it exactly under the identical workload. Calling that
            # `ecu_response_time` blames the ECM for an end-anchored schedule.
            return {
                "stage": "scheduling_policy_idle_bus",
                "evidence": {
                    "timeouts": timeouts,
                    "utilisation": utilisation,
                    "failures": failures,
                    "note": (
                        "requested rate missed with an idle bus and no timeouts — the "
                        "schedule, not the budget or the ECU"
                    ),
                },
            }
        return {
            "stage": "ecu_response_time",
            "evidence": {"timeouts": timeouts, "utilisation": utilisation, "failures": failures},
        }
    if "unanswered_input" in failures:
        # A saturated budget starves channels off the wire entirely, and a
        # channel that was never *requested* cannot be evidence that the ECU
        # does not serve it. An unsupported channel answers with a negative
        # response or times out; a starved one does neither. Measured in the
        # C10.12 sweep: fuel-map at 4 req/s (demand ~41) reported three
        # channels unanswered, zero timeouts, and only 13 of 16 channels
        # observed at all — attributing that to the ECU would have "proved"
        # the ECM does not serve three PIDs it serves fine at 8 req/s.
        if utilisation is not None and utilisation >= 0.8 and timeouts == 0:
            return {
                "stage": "bus_budget_request_pacing",
                "evidence": {
                    "budget_rps": budget,
                    "requests_sent": sent,
                    "utilisation": utilisation,
                    "failures": failures,
                    "note": (
                        "unanswered channels with a saturated budget and no timeouts are "
                        "starved, not unsupported"
                    ),
                },
            }
        return {
            "stage": "ecu_unsupported_channel",
            "evidence": {"failures": failures, "timeouts": timeouts, "utilisation": utilisation},
        }
    heartbeat = timing.get("event_loop_heartbeat_overshoot") or {}
    p99 = heartbeat.get("p99_ms")
    if isinstance(p99, (int, float)) and p99 > 5.0:
        feature: Mapping[str, Any] = {}
        if isinstance(inference, Mapping):
            feature = (inference.get("stages") or {}).get("feature") or {}
        feature_p95 = feature.get("p95_ms")
        on_loop = isinstance(feature_p95, (int, float)) and feature_p95 >= 0.6 * p99
        return {
            "stage": "inference_feature_stage_on_loop" if on_loop else "event_loop_occupancy",
            "data_loss": False,
            "evidence": {
                "heartbeat_p99_ms": p99,
                "heartbeat_max_ms": heartbeat.get("max_ms"),
                "feature_stage_p95_ms": feature_p95,
                "note": "no record was lost; the loop was held for this long per inference "
                "tick (C08: the feature stage runs on the loop by design)",
            },
        }
    return {
        "stage": "none_reached",
        "data_loss": False,
        "evidence": {
            "budget_utilisation": utilisation,
            "budget_rps": budget,
            "requests_sent": sent,
            "note": "no stage lost data or missed a deadline at this workload; the "
            "headroom is the unused budget fraction",
        },
    }


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def analyse(artifacts: RunArtifacts) -> dict[str, Any]:
    frame = read_rows(artifacts.session_dirs)
    synthetic = artifacts.scenario.kind != "replay"
    losses = loss_accounting(frame, artifacts)
    quality = quality_verdicts(artifacts)
    timing = timing_evidence(artifacts, frame)
    known_answer: list[dict[str, Any]] = []
    if artifacts.scenario.known_answer:
        if synthetic and artifacts.capture_dir is not None:
            known_answer = known_answer_checks(
                artifacts.capture_dir, artifacts.bindings_path, synthetic_pairs(artifacts)
            )
        elif not synthetic and artifacts.session_dirs:
            known_answer = known_answer_checks(
                artifacts.session_dirs[-1], artifacts.bindings_path, REPLAY_PAIRS
            )
    drops_total = int(
        losses["buffer_dropped"] + losses["queue_dropped"] + losses["storage_dropped"]
    )
    invalid_reasons: list[str] = []
    if artifacts.clock_stepped:
        invalid_reasons.append("host_wall_clock_step")
    if synthetic and losses["adapter_delivered_frames"] == 0:
        # A bus factory that raises is swallowed by the reconnect policy; a
        # run that never received a frame is not evidence of anything.
        invalid_reasons.append("collector_never_received_a_frame")
    return {
        "valid": not invalid_reasons,
        "invalid_reasons": invalid_reasons,
        "rows": row_totals(frame),
        "sequence_reconciliation": sequence_reconciliation(
            frame, artifacts, drops_total=drops_total
        ),
        "decoded_rows_stored": decoded_rows_stored(frame),
        "replay_accounting": replay_accounting(frame, artifacts),
        "losses": losses,
        "transport": {
            "service_accounting": service_accounting(artifacts),
            "batch_occupancy": batch_occupancy(artifacts),
            "channel_cadence": channel_cadence(frame, artifacts),
            "generation_events": artifacts.generation_events,
        },
        "quality": quality,
        "known_answer": known_answer,
        "timing": timing,
        "limiting_stage": limiting_stage(artifacts, losses, quality, timing),
    }


__all__ = [
    "REPLAY_PAIRS",
    "analyse",
    "channel_cadence",
    "known_answer_checks",
    "limiting_stage",
    "loss_accounting",
    "read_rows",
    "replay_accounting",
    "sequence_reconciliation",
]
