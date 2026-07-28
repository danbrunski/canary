"""``diff_did_snapshots`` — compare two completed ``did_sweep`` job results.

Pure read: fetches each job's record over ``/control/jobs/{id}`` (to learn
its ``log_path``), then reads the ``<job_id>.did_sweep.json`` snapshot
directly off disk — the same local-filesystem assumption
``src/mcp_server/tools/replay.py`` already makes (MCP and the daemon's job
runner are co-located). No new HTTP route: the diff itself is cheap enough
to compute in this process against two already-written snapshots.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.analysis.did_scan import DidSnapshot, classify_dids
from src.mcp_server.client import DaemonClient


def _resolve_result_path(job: dict[str, Any]) -> Path | None:
    """The snapshot JSON lives next to the log under <jobs_dir>/<job_id>.did_sweep.json."""
    log_path = job.get("log_path")
    job_id = job.get("job_id")
    if not isinstance(log_path, str) or not isinstance(job_id, str):
        return None
    return Path(log_path).parent / f"{job_id}.did_sweep.json"


async def _load_snapshot(client: DaemonClient, job_id: str) -> tuple[DidSnapshot, dict[str, Any]]:
    payload = await client.get_json(f"/control/jobs/{job_id}")
    job = payload.get("job") or payload
    if job.get("kind") != "did_sweep":
        raise ValueError(f"job {job_id} is not a did_sweep job (kind={job.get('kind')!r})")
    if job.get("status") != "succeeded":
        raise ValueError(f"job {job_id} has not succeeded yet (status={job.get('status')!r})")
    result_path = _resolve_result_path(job)
    if result_path is None or not result_path.exists():
        raise ValueError(f"did_sweep result not found for job {job_id} (expected at {result_path})")
    data = json.loads(result_path.read_text())
    values = {
        int(did_hex, 16): bytes.fromhex(raw_hex) for did_hex, raw_hex in data["values"].items()
    }
    snapshot = DidSnapshot(
        values=values,
        captured_at=float(data["captured_at"]),
        label=str(data.get("label", "")),
    )
    return snapshot, job


async def diff_did_snapshots(client: DaemonClient, *, job_a: str, job_b: str) -> dict[str, Any]:
    """Diff two completed ``did_sweep`` job results.

    Reuses ``classify_dids`` (the same classifier ``tools/scan_constant_dids.py``
    uses across passes at different engine states) on exactly two snapshots:
    `varying` = DIDs whose value differs between the two runs — the
    candidates worth investigating; `constant` = answered in both, same
    value both times; `intermittent` = answered in one run but not the other
    (a session/timing flake, or a DID gated on the condition itself). Each
    entry carries the raw hex from each snapshot it appeared in — the
    codebase has no scale/offset for most DIDs yet, so raw bytes are the
    honest thing to hand back.
    """
    snap_a, job_a_record = await _load_snapshot(client, job_a)
    snap_b, job_b_record = await _load_snapshot(client, job_b)
    classification = classify_dids([snap_a, snap_b])

    def _entry(did: int) -> dict[str, Any]:
        entry: dict[str, Any] = {"did": f"0x{did:04X}"}
        if did in snap_a.values:
            entry["a_hex"] = snap_a.values[did].hex()
        if did in snap_b.values:
            entry["b_hex"] = snap_b.values[did].hex()
        return entry

    return {
        "a": {"job_id": job_a_record.get("job_id"), "label": snap_a.label},
        "b": {"job_id": job_b_record.get("job_id"), "label": snap_b.label},
        "varying": [_entry(did) for did in sorted(classification.varying)],
        "constant": [_entry(did) for did in sorted(classification.constant)],
        "intermittent": [_entry(did) for did in sorted(classification.intermittent)],
        "counts": {
            "varying": len(classification.varying),
            "constant": len(classification.constant),
            "intermittent": len(classification.intermittent),
        },
    }


__all__ = ["diff_did_snapshots"]
