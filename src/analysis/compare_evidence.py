"""Mint evidence from the sanctioned channel comparison (E01.2).

:func:`mint_channel_comparison` runs
:func:`~src.analysis.compare.compare_channels_with_pairs` on a stored
session, writes one :class:`~src.evidence.records.Evidence` record to the
vehicle's memory ledger and returns its id. A card or investigation cites
that id; the numbers it shows are rendered from the record.

A refusal is evidence too: an absent input becomes an ``unevaluable``
record with reason ``missing_input``, any other refusal ``refused``, with the
refusal text as its detail and no values or residual. "Could not compare"
is an answer that must not be re-asked as if nobody had tried.

Re-running on the same subject (session, specs, regime) with a different
code version, catalog or result supersedes the earlier record through the
ledger; identical inputs return the existing id.

The tool states its own conditions and identity rather than taking them from
the caller. It pairs every sample of the session, so its regime is
:data:`ALL_CONDITIONS` — a caller cannot stamp an unfiltered comparison
"WOT". The calibration is resolved from the session's capture manifest or the
vehicle's ``tune_revisions.yaml`` for the session's start
(``src.vehicle.tune_revisions.resolve_session_revision``), and is ``None``
(unknown) when neither names one. The profile is the ledger's.
"""

from __future__ import annotations

import hashlib
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

import src.analysis.channels as channels_module
import src.analysis.compare as compare_module
import src.signals.pairing as pairing_module
from src.analysis.compare import (
    MIN_PAIRS,
    AlignedPairs,
    ChannelComparison,
    PairingPolicy,
    compare_channels_with_pairs,
)
from src.artifact_hash import sha256_file
from src.evidence.ledger import MemoryLedger
from src.evidence.provenance import code_version, hash_json, hash_session_files
from src.evidence.records import (
    Conditions,
    Evidence,
    Provenance,
    Quality,
    Quantity,
    Result,
    SourceRef,
    UnevaluableReason,
    VehicleIdentity,
)
from src.paths import BINDINGS_YAML, DIDS_YAML
from src.signals.pairing import ComparisonRefusedError, InputNotCapturedError
from src.storage.session_files import session_files_fingerprint, session_footer_span
from src.storage.sessions import session_started_at
from src.vehicle.tune_revisions import TuneRevisionRegistry, resolve_session_revision

__all__ = [
    "ALL_CONDITIONS",
    "COMPARE_TOOL",
    "COMPARE_TOOL_VERSION",
    "comparison_identity",
    "mint_channel_comparison",
]

#: The only regime this tool can claim: it filters nothing before pairing.
ALL_CONDITIONS = "all conditions: every paired sample in the session, unfiltered"

COMPARE_TOOL = "compare_channels"
#: Bump when the mapping from a comparison to evidence values changes.
COMPARE_TOOL_VERSION = "2"

_UNSPECIFIED = "unspecified"


def _pairs_hash(pairs: pd.DataFrame) -> str:
    digest = hashlib.sha256()
    for column in ("timestamp_a", "timestamp_b"):
        stamps = pd.to_datetime(pairs[column], utc=True)
        digest.update(stamps.astype("int64").to_numpy().tobytes())
    for column in ("a", "b"):
        digest.update(pairs[column].to_numpy(dtype=np.float64).tobytes())
    digest.update(pairs["held"].to_numpy(dtype=bool).tobytes())
    return digest.hexdigest()


def _aware(stamp: pd.Timestamp | datetime) -> datetime:
    value = pd.Timestamp(stamp)
    value = value.tz_localize(UTC) if value.tzinfo is None else value.tz_convert(UTC)
    # Record times are microsecond-resolution; truncate explicitly rather
    # than let pandas warn about discarded nanoseconds on every session.
    return value.floor("us").to_pydatetime()


def _session_window(session: Path) -> tuple[datetime, datetime]:
    span = session_footer_span(session)
    if span.first is not None and span.last is not None:
        return _aware(span.first), _aware(span.last)
    started = session_started_at(session.name)
    if started is None:
        raise ValueError(
            f"{session.name}: no footer timestamps and no parseable start; evidence "
            "needs the window it rests on"
        )
    return _aware(started), _aware(started)


def _pairs_window(aligned: AlignedPairs) -> tuple[datetime, datetime]:
    stamps = pd.concat(
        [
            pd.to_datetime(aligned.pairs["timestamp_a"], utc=True),
            pd.to_datetime(aligned.pairs["timestamp_b"], utc=True),
        ]
    )
    return _aware(stamps.min()), _aware(stamps.max())


def _values(comparison: ChannelComparison, unit_a: str, unit_b: str) -> dict[str, Quantity]:
    ratio = f"{unit_b}/{unit_a}" if unit_a != _UNSPECIFIED or unit_b != _UNSPECIFIED else "1"
    values = {
        "r_squared": Quantity(value=comparison.r_squared, unit="1"),
        "slope": Quantity(value=comparison.slope, unit=ratio),
        "offset": Quantity(value=comparison.offset, unit=unit_b),
        "residual_bias": Quantity(value=comparison.residual_bias, unit=unit_b),
        "residual_spread": Quantity(value=comparison.residual_spread, unit=unit_b),
        "difference_bias": Quantity(value=comparison.difference_bias, unit=unit_b),
        "difference_spread": Quantity(value=comparison.difference_spread, unit=unit_b),
        "separation_p95": Quantity(value=comparison.separation_p95_s, unit="s"),
        "separation_max": Quantity(value=comparison.separation_max_s, unit="s"),
    }
    if comparison.slope_drift_pct is not None:
        values["slope_drift"] = Quantity(value=comparison.slope_drift_pct, unit="%")
    return values


def comparison_identity(
    profile: str,
    session: Path,
    *,
    vin: str | None,
    program_id: str | None,
    tune_registry: TuneRevisionRegistry | None,
) -> tuple[VehicleIdentity, bool]:
    """Vehicle identity for a stored session, with the calibration resolved
    for the session itself — never supplied by the caller.

    The flag is true when the capture manifest and the flash history name
    different calibrations; the manifest wins, and the record must say so.
    """
    calibration, _source, conflict = resolve_session_revision(session, tune_registry)
    identity = VehicleIdentity(
        profile=profile, vin=vin or None, program_id=program_id, calibration=calibration
    )
    return identity, conflict


def mint_channel_comparison(
    ledger: MemoryLedger,
    session: Path,
    a: str,
    b: str,
    *,
    policy: PairingPolicy,
    vin: str | None,
    program_id: str | None,
    tune_registry: TuneRevisionRegistry | None,
    min_pairs: int = MIN_PAIRS,
    bindings_path: Path = BINDINGS_YAML,
    dids_path: Path = DIDS_YAML,
    incident: str | None = None,
) -> str:
    """Compare ``a`` and ``b`` in ``session``, persist the evidence, return its id.

    ``vin`` and ``program_id`` are stated (``None`` = unknown);
    ``tune_registry`` is the vehicle's flash history the calibration is
    resolved from (``None``: no registry, so the calibration is unknown unless
    the capture manifest names it). ``incident`` names the recurring
    phenomenon this comparison is an occurrence of (E01.3): the same incident
    measured on another drive, even through other channel specs, is a
    recurrence (``MemoryLedger.recurrences``), never a supersession.
    """
    vehicle, calibration_conflict = comparison_identity(
        ledger.vehicle,
        Path(session),
        vin=vin,
        program_id=program_id,
        tune_registry=tune_registry,
    )
    conditions = Conditions(regime=ALL_CONDITIONS, vehicle_state=None)
    session = Path(session)
    inputs = {
        "session_files": hash_session_files(session_files_fingerprint(session)),
        "bindings": sha256_file(bindings_path),
        "dids": sha256_file(dids_path),
    }
    evidence_policy: dict[str, object] = {
        "pairing": policy.as_dict(),
        "min_pairs": min_pairs,
    }
    provenance = Provenance(
        producer=COMPARE_TOOL,
        producer_kind="tool",
        version=COMPARE_TOOL_VERSION,
        code_version=code_version(
            sys.modules[__name__], compare_module, channels_module, pairing_module
        ),
    )
    # A different tolerance or pair floor asks a different question, so it is a
    # different subject: it must not supersede the other policy's record.
    subject = (
        f"{COMPARE_TOOL}:{session.name}:{a}:{b}:{conditions.regime}"
        f":{hash_json(evidence_policy)[:16]}"
    )
    conflict_flags = ("calibration_conflict",) if calibration_conflict else ()
    try:
        comparison, aligned = compare_channels_with_pairs(
            session,
            a,
            b,
            policy=policy,
            min_pairs=min_pairs,
            bindings_path=bindings_path,
            dids_path=dids_path,
        )
    except ComparisonRefusedError as refusal:
        start, end = _session_window(session)
        reason: UnevaluableReason = (
            "missing_input" if isinstance(refusal, InputNotCapturedError) else "refused"
        )
        record = Evidence(
            vehicle=vehicle,
            provenance=provenance,
            conditions=conditions,
            valid_from=start,
            valid_to=end,
            subject=subject,
            incident=incident,
            sources=(
                SourceRef(
                    session=session.name,
                    window_start=start,
                    window_end=end,
                    channels=(a, b),
                    n_samples=0,
                ),
            ),
            input_hashes=inputs,
            policy=evidence_policy,
            quality=Quality(n_observations=0, timestamp_basis=None, flags=conflict_flags),
            result=Result(status="unevaluable", reason=reason, detail=str(refusal)),
        )
        return ledger.append(record).id

    unit_a = aligned.unit_a or _UNSPECIFIED
    unit_b = aligned.unit_b or _UNSPECIFIED
    start, end = _pairs_window(aligned)
    inputs["pairs"] = _pairs_hash(aligned.pairs)
    weakest = sorted({comparison.timestamp_basis_a, comparison.timestamp_basis_b})
    record = Evidence(
        vehicle=vehicle,
        provenance=provenance,
        conditions=conditions,
        valid_from=start,
        valid_to=end,
        subject=subject,
        incident=incident,
        sources=(
            SourceRef(
                session=session.name,
                window_start=start,
                window_end=end,
                channels=(a, b),
                n_samples=comparison.n_pairs,
            ),
        ),
        input_hashes=inputs,
        policy=evidence_policy,
        values=_values(comparison, unit_a, unit_b),
        labels={
            "scale_verdict": comparison.scale_verdict,
            "reference": comparison.reference,
            "unit_a": unit_a,
            "unit_b": unit_b,
        },
        quality=Quality(
            n_observations=comparison.n_pairs,
            timestamp_basis="/".join(weakest),
            flags=conflict_flags
            + ((f"held_pairs={comparison.n_held_pairs}",) if comparison.n_held_pairs else ()),
        ),
        # The residual of the 1:1 hypothesis (b - a): the statistic a unit
        # agreement rests on; the post-fit residual is blind to scale/offset.
        result=Result(
            status="evaluated",
            residual=Quantity(
                value=comparison.difference_bias,
                unit=unit_b,
                uncertainty=comparison.difference_spread,
            ),
        ),
    )
    return ledger.append(record).id
