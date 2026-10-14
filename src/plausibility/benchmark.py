"""The A05.8 plumbing-fault benchmark's shared pieces (one harness, two CLIs).

``scripts/evaluate_plausibility.py`` (the plausibility evaluation) and
``scripts/evaluate_committee_coverage.py`` (which detector owns and catches
each fault, operator decision 2026-10-13) grade the *same* injections: the
same benchmark channels, vehicle context, stream loading, per-channel
restriction and grading. Scripts may not import one another
(``tests/test_script_imports.py``), so those pieces live here.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from src.config import CanaryConfig
from src.decode.bindings import Bindings, load_bindings
from src.decode.dids import DidMap, load_did_map
from src.paths import BINDINGS_YAML, DIDS_YAML
from src.plausibility.evaluator import EvidenceRecord, PlausibilityEvaluator
from src.plausibility.limits import (
    LimitsCatalog,
    channel_group,
    plausibility_path,
    resolve_limits_for_config,
)
from src.plausibility.offline import SessionStream, detections_in, load_stream, run_evaluator

#: The 2026-09-20 benchmark's channels (``bench_new.json``).
BENCHMARK_CHANNELS = (
    "obd:COOLANT_TEMP",
    "obd:ENGINE_LOAD",
    "obd:RPM",
    "obd:SPEED",
    "obd:THROTTLE_POS",
)
#: A detection counts for a fault inside ``[start, end + grace)``.
DEFAULT_GRACE_SECONDS = 60.0


@dataclass(frozen=True)
class BenchmarkContext:
    """The vehicle-scoped inputs every benchmark run reads."""

    config: CanaryConfig
    catalog: LimitsCatalog
    bindings: Bindings
    did_map: DidMap
    roles: Mapping[str, Sequence[str]]


def load_benchmark_context(config_path: Path, profile: str | None) -> BenchmarkContext:
    """Config scoped to ``profile``, the resolved limits catalog, the catalogs
    and the vehicle's realistic-fault channel roles."""
    from src.preprocessing.anomaly_injection import load_fault_channel_roles
    from src.vehicle.profiles import resolve_profile_for_tooling, scope_config

    config = CanaryConfig.from_yaml(config_path)
    if config.profiles.enabled:
        config = scope_config(config, resolve_profile_for_tooling(config, profile))
    bindings = load_bindings(BINDINGS_YAML)
    did_map = load_did_map(DIDS_YAML)
    catalog = resolve_limits_for_config(config, bindings=bindings, did_map=did_map)
    roles_path = plausibility_path(config).parent / "fault_injection.yaml"
    roles = load_fault_channel_roles(roles_path if roles_path.exists() else None)
    return BenchmarkContext(config, catalog, bindings, did_map, roles)


def _decoded_key(catalog: LimitsCatalog) -> str:
    names = sorted(
        name
        for name, limits in catalog.channels.items()
        if name.startswith("decoded:") and limits.is_checked()
    )
    return hashlib.sha256("\n".join(names).encode()).hexdigest()[:8]


def load_benchmark_stream(
    session: Path, context: BenchmarkContext, cache_dir: str | None
) -> SessionStream:
    """``session``'s arrived-sample stream, cached under ``cache_dir`` (keyed
    by the checked decoded channels) when one is given."""
    cache = None
    if cache_dir is not None:
        key = _decoded_key(context.catalog)
        cache = Path(cache_dir) / f"{session.parent.name}__{session.name}__{key}.parquet"
        if cache.exists():
            frame = pd.read_parquet(cache)
            modes = frame.pop("mode").replace({"None": None}).to_numpy(dtype=object)
            return SessionStream(session.name, frame.reset_index(drop=True), modes)
    stream = load_stream(
        session,
        context.config,
        context.catalog,
        bindings=context.bindings,
        did_map=context.did_map,
    )
    if cache is not None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        stream.samples.assign(mode=stream.modes.astype(str)).to_parquet(cache)
    return stream


def evaluate_stream(
    stream: SessionStream, context: BenchmarkContext, session: str
) -> list[EvidenceRecord]:
    """A fresh evaluator over ``stream``; every record it writes."""
    evaluator = PlausibilityEvaluator(context.catalog, session=session)
    return run_evaluator(evaluator, stream)


def restrict_to_groups(stream: SessionStream, channels: Sequence[str]) -> SessionStream:
    """Only the answer groups of ``channels``.

    Every check on a channel reads its own samples and, for freshness, its
    group's; nothing else can change a detection on it, so a faulted drive is
    scored on this subset (a tenth of the samples: broadcast frames dominate)
    with identical results for the faulted channels.
    """
    groups = {channel_group(c) for c in channels}
    keep = stream.samples["channel"].map(channel_group).isin(groups).to_numpy()
    return SessionStream(
        stream.name, stream.samples.loc[keep].reset_index(drop=True), stream.modes[keep]
    )


def epoch_of(iso: str) -> float:
    """An evidence record's ISO time as epoch seconds."""
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def fault_hits(
    records: Sequence[EvidenceRecord],
    clean_keys: set[tuple[str, str, str]],
    *,
    channels: Sequence[str],
    start: float,
    end: float,
) -> list[EvidenceRecord]:
    """Records on ``channels`` inside ``[start, end)`` that the clean drive
    did not also produce (keyed by channel, check and detection time)."""
    return [
        r
        for r in detections_in(records, channels=channels, start=start, end=end)
        if (r.channel, r.check, r.detected_at) not in clean_keys
    ]


def grade_hits(hits: Sequence[EvidenceRecord], start: float) -> dict[str, Any]:
    """``detected`` / ``confirmed``, the checks that fired and both latencies."""
    confirmed = [r for r in hits if r.state == "confirmed"]
    first = min((epoch_of(r.detected_at) for r in hits), default=None)
    first_confirmed = min((epoch_of(r.detected_at) for r in confirmed), default=None)
    return {
        "detected": bool(hits),
        "confirmed": bool(confirmed),
        "checks": sorted({r.check for r in hits}),
        "latency_seconds": None if first is None else round(first - start, 3),
        "confirm_latency_seconds": (
            None if first_confirmed is None else round(first_confirmed - start, 3)
        ),
    }


__all__ = [
    "BENCHMARK_CHANNELS",
    "DEFAULT_GRACE_SECONDS",
    "BenchmarkContext",
    "epoch_of",
    "evaluate_stream",
    "fault_hits",
    "grade_hits",
    "load_benchmark_context",
    "load_benchmark_stream",
    "restrict_to_groups",
]
