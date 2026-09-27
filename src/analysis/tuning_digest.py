"""The tuning half of a post-drive digest.

"Drive's over" should produce a report, not an hour of ad-hoc SQL. This
assembles what the operator actually wants to know after a tuning capture:
how much usable WOT it caught, whether the capture ran at the rate it was
armed for, what the delivery error was, and how the cam tracked its command.

Deliberately separate from :mod:`src.analysis.digest`: this section only
exists for capture sessions, needs an AFR calibration and a tune-revision
registry the ordinary digest has no use for, and must degrade to ``None``
rather than failing a normal drive's digest when a channel is missing.

See ``docs/roadmap/archive/tuning-analysis-toolkit.md`` Phase 5.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import pandas as pd

from src.analysis.channels import (
    AS_OF_ATTR,
    ChannelSpecError,
    as_of_refusal,
    as_of_validity,
    read_aligned,
    read_channels,
)
from src.analysis.compare import align_pairs, compare_channels
from src.analysis.noise_floor import FloorRefusal, Significance
from src.analysis.pull_tags import is_pull_end, is_pull_start
from src.analysis.tune_revisions import (
    resolve_session_revision,
    session_started_at,
)
from src.analysis.tuning import (
    DeliveryError,
    GateError,
    delivery_error,
    semantic_route,
)
from src.analysis.tuning_context import TuningContext
from src.analysis.tuning_frame import (
    LoadWotCriterion,
    TuningFrame,
    WotCriterion,
    build_tuning_frame,
    section_capabilities,
)
from src.collection.capture_profiles import FUEL_DELIVERY_TUPLE
from src.config import CanaryConfig
from src.session import load_tags
from src.signals.afr import AfrCalibration, TargetAfrMap, voltage_to_afr
from src.signals.pairing import ComparisonRefusedError, InputNotCapturedError, PairingPolicy
from src.storage.capture_manifest import read_capture_manifest, read_capture_quality

# The WOT load and rpm floors and the rpm band edges are vehicle facts: they
# come from the profile's ``tuning_providers.yaml`` ``mode01_wot`` (R05.13),
# never from constants here. A vehicle that declares none gets an
# "unavailable" WOT inventory that says so.

#: How a WOT inventory knows its seconds were WOT. See :class:`WotInventory`.
WotSource = Literal["tagged", "inferred", "unavailable"]


@dataclass(frozen=True)
class WotInventory:
    """WOT seconds, and how we know they were WOT.

    ``source`` is not decorative metadata. Seconds delimited by a physical
    pull tag, seconds inferred from a throttle threshold, and "we could not
    tell" are three different kinds of evidence, and a reader that cannot
    distinguish them will quote one as another — which is how a capture that
    answered nothing came to read as a clean zero. The counts cannot be
    constructed without stating which kind they are.

    ``inferred`` is genuinely weaker than ``tagged``: it knows the throttle
    was open, not what the operator was doing or why, so it cannot separate a
    deliberate pull from a merge onto a highway. Use it to find data worth
    analysing, not to attribute a result to an intended manoeuvre.
    """

    seconds: int
    with_afr_pair: int
    by_band: list[dict[str, Any]]
    source: WotSource
    #: Populated for every source, including ``inferred`` with zero seconds —
    #: "the throttle never opened" is an answer about the drive, while "no
    #: demand channel" is not an answer at all.
    reason: str | None = None
    #: Seconds carrying a measured AFR. Distinct from ``with_afr_pair``, which
    #: also needs a *commanded* target and is therefore always 0 on an all-DID
    #: capture; reporting only the pair would hide usable measured data.
    with_measured_afr: int = 0
    criterion: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "seconds": self.seconds,
            "with_afr_pair": self.with_afr_pair,
            "with_measured_afr": self.with_measured_afr,
            "by_band": list(self.by_band),
            "source": self.source,
            "reason": self.reason,
            "criterion": self.criterion,
        }


#: The tuning section's own schema, inside ``digest.json`` (whose
#: ``SCHEMA_VERSION`` is the whole sidecar's). 2 (R05.16): ``delivery`` comes
#: from the acquisition-paired engine and carries ``engine_version`` and
#: ``pairing``. A section without ``schema_version`` is 1, the 1 s last-cell
#: engine; its delivery numbers are not comparable with a 2.
TUNING_SCHEMA_VERSION = 2


@dataclass
class TuningDigest:
    """Tuning section of a capture session's digest."""

    session_id: str
    capture_profile: str | None
    tune_revision: str | None
    #: The WOT inventory *and the evidence it rests on*. Kept whole rather
    #: than flattened into bare counts, because `wot_seconds: 0` read as "the
    #: drive had no WOT" when it meant "nothing here could tell".
    wot: WotInventory
    achieved_hz: dict[str, float]
    #: Evidence inside physically tagged all-DID pulls. Separate counts avoid
    #: treating absent command/fueling/target roles as a measured zero.
    pull_inventory: dict[str, Any] = field(default_factory=dict)
    #: Per-provider stored-sample timing.  Unlike the live scheduler's
    #: trailing observation window, this is the evidence preserved with the
    #: completed drive and makes long polling stalls visible.
    rate_summary: dict[str, dict[str, float | int | None]] = field(default_factory=dict)
    #: How often the profile's ``required_tuple`` was actually co-observed —
    #: the number that predicts whether the headline analysis has any samples
    #: at all, and which no per-channel rate can substitute for.
    complete_tuples: dict[str, Any] = field(default_factory=dict)
    #: The contract verdict recorded when the capture stopped, verbatim.
    #: ``None`` means no sidecar was written, which is *unknown* — not a pass.
    capture_quality: dict[str, Any] | None = None
    #: Voltage-derived readings remain a distinct instrument from an external
    #: wideband.  Carry the transform and censoring evidence beside the result.
    measured_afr: dict[str, Any] = field(default_factory=dict)
    #: The exact ROM-backed target artifact selected for this session.  This
    #: does not claim a target lookup happened when the load provider is absent.
    target_map: dict[str, Any] = field(default_factory=dict)
    delivery: dict[str, Any] | None = None
    cam: dict[str, Any] | None = None
    #: Delta against the previous capture sharing this profile *and* tune
    #: revision — the "did the flash do what I meant" answer. Requires both
    #: to match: across a flash boundary the delta is the flash, not drift.
    previous: dict[str, Any] | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": TUNING_SCHEMA_VERSION,
            "session_id": self.session_id,
            "capture_profile": self.capture_profile,
            "tune_revision": self.tune_revision,
            "wot": self.wot.to_dict(),
            # Flat mirrors for existing consumers. `wot_source` sits alongside
            # them deliberately: any reader that finds `wot_seconds` also finds
            # what kind of evidence produced it, in the same dict.
            "wot_seconds": self.wot.seconds,
            "wot_with_afr_pair": self.wot.with_afr_pair,
            "wot_by_band": list(self.wot.by_band),
            "wot_source": self.wot.source,
            "achieved_hz": dict(self.achieved_hz),
            "pull_inventory": self.pull_inventory,
            "rate_summary": self.rate_summary,
            "complete_tuples": self.complete_tuples,
            "capture_quality": self.capture_quality,
            "measured_afr": self.measured_afr,
            "target_map": self.target_map,
            "delivery": self.delivery,
            "cam": self.cam,
            "previous": self.previous,
            "capabilities": self.capabilities,
            "notes": list(self.notes),
        }


def has_tuning_section(session_dir: Path) -> bool:
    """Whether a digest of ``session_dir`` computes this section: it is a
    capture (a manifest names its profile, or its directory is ``_``-prefixed).

    The section reads the whole session raw (``read_channels``), unlike the
    rest of the digest, so the digest's pre-flight budgets such a session by
    its rows (``digest_preflight``); both ask here, so they cannot disagree.
    """
    return _capture_profile_name(session_dir) is not None or session_dir.name.startswith("_")


def _capture_profile_name(session_dir: Path) -> str | None:
    manifest = read_capture_manifest(session_dir)
    return None if manifest is None else manifest.profile


def _profile_mode01_channels(session_dir: Path) -> tuple[str, ...]:
    """Mode-01 channels the capture manifest says were requested."""
    manifest = read_capture_manifest(session_dir)
    if manifest is None:
        return ()
    return tuple(name for name in manifest.mode01_channels if name != "AUTO")


def _read_specs(
    session_dir: Path,
    specs: list[str],
    *,
    acquired_time: bool = False,
) -> dict[str, pd.DataFrame]:
    """Read every spec in **one** pass and split the long frame per spec.

    ``read_channels`` already loads the session once and demultiplexes, so
    calling it per spec re-reads every Parquet chunk once per channel. That
    cost nothing while the digest measured three hard-coded channels; measuring
    what the profile actually asked for took it to 16, and a 24-minute capture
    went from seconds to 228 s. Same numbers, one read.

    A spec that raises is dropped rather than failing the digest — but only the
    whole batch can raise here, so a single unresolvable spec is retried alone
    to keep one bad name from hiding fifteen good ones.
    """
    if not specs:
        return {}
    try:
        frame = read_channels(session_dir, specs, acquired_time=acquired_time)
    except MemoryError:
        # Out of memory is the digest child's to report (child_out_of_memory,
        # src.control.trip_end), never an absent channel or a failed section:
        # swallowed here, a capture over the ceiling read as "inputs never
        # captured" (P02.1, 2026-09-26).
        raise
    except Exception:  # noqa: BLE001 — fall back to per-spec so one bad name is not fatal
        out: dict[str, pd.DataFrame] = {}
        for spec in specs:
            try:
                rows = read_channels(session_dir, [spec], acquired_time=acquired_time)
            except MemoryError:
                raise
            except Exception:  # noqa: BLE001, PERF203 — an absent channel is normal
                continue
            if not rows.empty:
                out[spec] = rows
        return out
    if frame.empty:
        return {}
    held = as_of_validity(frame)
    out = {str(spec): rows for spec, rows in frame.groupby("spec", sort=False)}
    for spec, rows in out.items():
        # P02.10: a change-only broadcast spec's rows are its payload changes;
        # the split drops the reader's mark, so it is put back per spec.
        rows.attrs.pop(AS_OF_ATTR, None)
        if spec in held:
            rows.attrs[AS_OF_ATTR] = {spec: held[spec]}
    return out


def _change_only_rate_refusal(spec: str, rows: pd.DataFrame) -> str | None:
    """Why ``spec``'s stored rows are no rate (P02.10), or ``None``."""
    return as_of_refusal(rows, reader="tuning digest rate", specs=[spec])


def _achieved_hz(session_dir: Path, specs: list[str]) -> dict[str, float]:
    """Real per-channel rate over the session, from the rows themselves.

    Computed from stored data rather than read off `/status`, so it still
    works long after the drive — and so the number in the digest is what was
    actually written, not what the scheduler believed it was doing.
    """
    out: dict[str, float] = {}
    for spec, rows in _read_specs(session_dir, specs).items():
        # A change-only broadcast spec has no rate here: its rows are its
        # payload changes (P02.10); ``_rate_summary`` names that.
        if rows.empty or _change_only_rate_refusal(spec, rows) is not None:
            continue
        span = (rows["timestamp"].max() - rows["timestamp"].min()).total_seconds()
        if span > 0:
            out[spec] = round(len(rows) / span, 2)
    return out


def _rate_summary(
    session_dir: Path, specs: list[str]
) -> dict[str, dict[str, float | int | str | None]]:
    """Summarize persisted polling cadence and its worst gap by provider.

    A nominal target rate cannot tell a post-drive reader whether a channel
    disappeared for ten seconds.  The session has no reliable "paused" state,
    so these are deliberately wall-clock sample timings, not a claim about
    active scheduler time.
    """
    out: dict[str, dict[str, float | int | str | None]] = {}
    for spec, frame in _read_specs(session_dir, specs, acquired_time=True).items():
        timestamps = frame["timestamp"].dropna().sort_values().drop_duplicates()
        count = len(timestamps)
        if not count:
            continue
        refusal = _change_only_rate_refusal(spec, frame)
        if refusal is not None:
            out[spec] = {"stored_rows": int(count), "refused": refusal}
            continue
        intervals = timestamps.diff().dropna().dt.total_seconds()
        positive = intervals[intervals > 0]
        span = (
            float((timestamps.iloc[-1] - timestamps.iloc[0]).total_seconds()) if count > 1 else 0.0
        )
        out[spec] = {
            "samples": int(count),
            "duration_seconds": round(span, 3),
            "observed_hz": round((count - 1) / span, 3) if span > 0 else None,
            "median_interval_seconds": round(float(positive.median()), 3)
            if not positive.empty
            else None,
            # The median says what the cadence usually was; p95 says how bad
            # the tail got without letting one stop-and-restart dominate the
            # way `longest_gap_seconds` alone does. A channel can hold a good
            # median and still be unusable for transients.
            "p95_interval_seconds": round(float(positive.quantile(0.95)), 3)
            if not positive.empty
            else None,
            "longest_gap_seconds": round(float(positive.max()), 3) if not positive.empty else None,
        }
    return out


def _resolve_required_tuple(session_dir: Path) -> tuple[list[str], str]:
    """The tuple to score, and *how we know it* — declared or inferred.

    Same discipline as :class:`WotInventory`'s ``source``: a capture recorded
    before ``required_tuple`` existed has no declared contract, and silently
    substituting one would present an inference as the profile's own promise.
    But refusing outright would make the count unavailable for exactly the
    captures that motivated it, so a Mode-01 capture polling all five delivery
    channels gets the delivery tuple, labelled ``inferred``.
    """
    manifest = read_capture_manifest(session_dir)
    if manifest is None:
        return [], "none"
    declared = list(manifest.required_tuple)
    if declared:
        return declared, "declared"
    polled = set(manifest.mode01_channels)
    if polled and set(FUEL_DELIVERY_TUPLE).issubset(polled):
        return list(FUEL_DELIVERY_TUPLE), "inferred"
    return [], "none"


def _complete_tuple_summary(
    session_dir: Path,
    required_tuple: list[str],
    *,
    source: str = "declared",
    grid: str = "1s",
) -> dict[str, Any]:
    """How often every member of ``required_tuple`` was actually co-observed.

    The exact count the scheduler cannot produce. ``capture_quality.json``
    reports an upper *bound* (the slowest member's rate); this reads the rows
    and counts grid cells in which every member independently appears, which is
    the same pairing rule the delivery-error path applies and therefore the
    number that predicts its yield.

    This is the figure whose absence let a 24-minute capture with healthy-looking
    per-channel rates report two usable delivery samples with no earlier
    warning. It is reported even when it is zero — especially then, since
    "captured but never co-observed" and "never captured" demand opposite
    responses and only this can tell them apart.
    """
    if not required_tuple:
        return {
            "available": False,
            "source": source,
            "reason": (
                "this capture declares no required_tuple and does not poll the delivery "
                "channels, so there is no joint requirement to count"
            ),
        }
    specs = [name if ":" in name else f"obd:{name}" for name in required_tuple]
    rows_by_spec = _read_specs(session_dir, specs)
    per_channel: dict[str, int] = {}
    cells: list[set[Any]] = []
    for spec in specs:
        frame = rows_by_spec.get(spec)
        if frame is None or frame.empty:
            per_channel[spec] = 0
            cells.append(set())
            continue
        stamps = pd.to_datetime(frame["timestamp"], utc=True).dt.floor(grid)
        per_channel[spec] = int(len(frame))
        cells.append(set(stamps.unique()))

    absent = sorted(spec for spec, count in per_channel.items() if count == 0)
    complete = set.intersection(*cells) if cells and all(cells) else set()
    span_cells = len(set.union(*cells)) if cells and any(cells) else 0
    return {
        "available": True,
        # `inferred` means the capture never promised this tuple — it is the
        # delivery tuple applied to a profile that predates the field. Weaker
        # evidence than `declared`, and never to be quoted as the profile's own.
        "source": source,
        "grid": grid,
        "members": specs,
        "samples_per_member": per_channel,
        "absent_members": absent,
        "complete_cells": len(complete),
        "cells_with_any_member": span_cells,
        "reason": (
            "members absent from this session, so the tuple can never complete: "
            + ", ".join(absent)
            if absent
            else None
        ),
    }


def _measured_afr_summary(
    session_dir: Path,
    *,
    calibration: AfrCalibration | None,
    role_capabilities: dict[str, Any],
) -> dict[str, Any]:
    """Report transformed factory A/F readings without presenting them as independent WB data."""
    transfer = None if calibration is None else calibration.voltage_transfer
    if transfer is None:
        return {"available": False, "reason": "voltage-to-AFR transfer is unavailable", "banks": []}
    assert calibration is not None

    banks: list[dict[str, Any]] = []
    for bank in (1, 2):
        role = f"measured_afr_b{bank}"
        provider = role_capabilities.get(role, {}).get("provider")
        if not isinstance(provider, str) or not role_capabilities.get(role, {}).get(
            "available", False
        ):
            continue
        try:
            samples = read_channels(session_dir, [provider], acquired_time=True)
        except MemoryError:
            raise
        except Exception:  # noqa: BLE001 — capability report carries the ordinary reason
            continue
        # Sample and censor counts count rows: P02.10 change-only rows are
        # payload changes, so the bank is reported refused, by name.
        change_only = as_of_refusal(samples, reader=f"measured AFR bank {bank} sample counts")
        if change_only is not None:
            banks.append({"bank": bank, "provider": provider, "refused": change_only})
            continue
        afr = voltage_to_afr(samples["value"], transfer).dropna()
        if afr.empty:
            continue
        floor = int(afr.le(transfer.censored_floor_afr).sum())
        ceiling = int(afr.ge(transfer.censored_ceiling_afr).sum())
        banks.append(
            {
                "bank": bank,
                "provider": provider,
                "samples": int(len(afr)),
                "uncensored_samples": int(len(afr) - floor - ceiling),
                "censored_floor_samples": floor,
                "censored_ceiling_samples": ceiling,
                "afr_range": [round(float(afr.min()), 3), round(float(afr.max()), 3)],
                "afr_median": round(float(afr.median()), 3),
            }
        )
    provenance = calibration.provenance
    return {
        "available": any("refused" not in bank for bank in banks),
        "instrument": "factory A/F voltage transformed to AFR",
        "independence_caveat": (
            "This is a calibrated transform of the ECU A/F voltage, not an independent wideband "
            "measurement; use the recorded external-reference provenance below."
        ),
        "transfer_provenance": {
            key: provenance[key]
            for key in ("method", "session", "source_log", "validated_2026-08-04")
            if key in provenance
        },
        "censor_bounds_afr": [transfer.censored_floor_afr, transfer.censored_ceiling_afr],
        "banks": banks,
    }


def _target_map_summary(
    target: TargetAfrMap | None,
    *,
    revision: str | None,
    role_capabilities: dict[str, Any],
) -> dict[str, Any]:
    """Identify the target artifact and say explicitly whether its load is observable."""
    if target is None:
        return {
            "available": False,
            "revision": revision,
            "reason": "no ROM target map is recorded for this tune revision",
        }
    provenance = target.provenance
    result: dict[str, Any] = {
        "available": True,
        "revision": target.revision,
        "load_axis": {"name": target.load_axis_name, "unit": target.load_axis_unit},
        "rpm_breakpoints": target.rpm_breakpoints,
        "load_breakpoints": target.load_breakpoints,
    }
    if provenance is not None:
        result["rom_artifact"] = {
            "file": provenance.rom_file,
            "sha256": provenance.rom_sha256,
            "modified_at": provenance.rom_modified_at.isoformat(),
            "map_slot": provenance.map_slot,
            "cell_decode": provenance.cell_decode,
            "guide_reference": provenance.guide_reference,
        }
    load_provider = role_capabilities.get("airflow_or_table_load", {})
    result["lookup_ready"] = bool(load_provider.get("available", False))
    if not result["lookup_ready"]:
        result["lookup_reason"] = (
            "Base Fuel Schedule/load provider is not confirmed in this capture"
        )
    return result


def _bands(per_second: pd.Series, rpm_bands: Sequence[float]) -> list[dict[str, Any]]:
    """Seconds per rpm band, in the vehicle's band edges."""
    counts = per_second.groupby(pd.cut(per_second, list(rpm_bands)), observed=True).size()
    return [{"rpm_band": str(band), "seconds": int(seconds)} for band, seconds in counts.items()]


def _infer_wot(
    session_dir: Path,
    *,
    criterion: WotCriterion,
    rpm_spec: str,
    demand_spec: str,
    measured_afr_specs: list[str],
    rpm_bands: Sequence[float] | None,
) -> WotInventory:
    """WOT seconds from the throttle's own saturation, with no tags.

    Weaker evidence than a physical tag and labelled as such, but it answers
    a question the tagged path cannot: whether there is anything in an
    untagged capture worth looking at. The criterion is vehicle data
    (:class:`~src.analysis.tuning_frame.WotCriterion`), never a constant
    here — a throttle stop is a property of the vehicle.
    """
    try:
        frame = read_aligned(session_dir, [rpm_spec, demand_spec], grid="1s", require_all=True)
    except MemoryError:
        raise
    except Exception as exc:  # noqa: BLE001
        return WotInventory(0, 0, [], "unavailable", reason=f"{demand_spec}: {exc}")
    if frame.empty:
        return WotInventory(
            0, 0, [], "unavailable", reason=f"no seconds carry both {rpm_spec} and {demand_spec}"
        )

    demand = frame[demand_spec]
    observed_max = float(demand.max())
    detail: dict[str, Any] = {
        "role": criterion.role,
        "provider": demand_spec,
        "unit": criterion.unit,
        "threshold_value": criterion.threshold_value,
        "wide_open_value": criterion.wide_open_value,
        "observed_max": round(observed_max, 4),
        "rpm_floor": criterion.rpm_floor,
    }
    # "The throttle never opened" is a finding about the drive. Distinguish it
    # from "there was no demand channel", which is not a finding at all.
    if observed_max < criterion.wide_open_value - criterion.reached_stop_tolerance:
        return WotInventory(
            0,
            0,
            [],
            "inferred",
            reason=(
                f"demand peaked at {observed_max:.3g} {criterion.unit}, short of the measured "
                f"stop {criterion.wide_open_value:.3g} — the throttle was never opened on this "
                "drive, so there is no WOT to analyse (this is an answer, not a gap)"
            ),
            criterion=detail,
        )

    at_wot = frame[(demand >= criterion.threshold_value) & (frame[rpm_spec] > criterion.rpm_floor)]
    if at_wot.empty:
        return WotInventory(
            0,
            0,
            [],
            "inferred",
            reason=(
                f"the throttle reached its stop but never above {criterion.rpm_floor:.0f} rpm, "
                "so no second qualifies as a WOT pull"
            ),
            criterion=detail,
        )

    measured = 0
    if measured_afr_specs:
        try:
            with_afr = read_aligned(
                session_dir,
                [rpm_spec, demand_spec, *measured_afr_specs],
                grid="1s",
                require_all=True,
            )
            measured = int(
                (
                    (with_afr[demand_spec] >= criterion.threshold_value)
                    & (with_afr[rpm_spec] > criterion.rpm_floor)
                ).sum()
            )
        except MemoryError:
            raise
        except Exception:  # noqa: BLE001
            measured = 0
    return WotInventory(
        seconds=int(len(at_wot)),
        # Always 0 on an all-DID capture: a commanded target needs Mode 01,
        # which this profile disables. Kept rather than dropped so the number
        # that gates delivery error stays visible.
        with_afr_pair=0,
        by_band=[] if rpm_bands is None else _bands(at_wot[rpm_spec], rpm_bands),
        source="inferred",
        reason=None,
        with_measured_afr=measured,
        criterion=detail,
    )


def _wot_inventory(
    session_dir: Path,
    *,
    rpm_spec: str | None,
    did_continuous: bool,
    criterion: WotCriterion | None = None,
    demand_spec: str | None = None,
    measured_afr_specs: list[str] | None = None,
    load_wot: LoadWotCriterion | None = None,
) -> WotInventory:
    """Seconds at WOT by rpm band, and how many carry a usable AFR pair.

    The pair count is the number that matters: WOT seconds without both a
    measured and a commanded AFR in the same second cannot answer anything,
    and the July corpus had 25 such seconds against 4 usable ones.
    """
    rpm_bands = None if load_wot is None else load_wot.rpm_bands
    tags = load_tags(session_dir / "tags.jsonl")
    starts = [float(tag["t"]) for tag in tags if is_pull_start(tag)]
    ends = [float(tag["t"]) for tag in tags if is_pull_end(tag)]
    if starts and rpm_spec is not None and load_wot is not None:
        try:
            rpm = read_channels(session_dir, [rpm_spec], acquired_time=True)
            # Seconds are counted from stored rows (one per second that has
            # one); a change-only broadcast rpm (P02.10) has rows only where
            # it changed, so it would undercount held seconds. Refused by
            # name, never left to the fallbacks below.
            change_only = as_of_refusal(rpm, reader="tagged WOT seconds")
            if change_only is not None:
                return WotInventory(0, 0, [], "unavailable", reason=change_only)
            if not rpm.empty:
                # Parquet can round-trip timestamps as microseconds rather
                # than nanoseconds; ``astype(int64) / 1e9`` then puts every
                # sample in 1970 and makes valid physical tags miss. Convert
                # the timezone-aware values through their own epoch method.
                epoch = rpm["timestamp"].map(lambda value: value.timestamp())
                selected = pd.Series(False, index=rpm.index)
                for start in starts:
                    end = next((item for item in ends if item > start), float(epoch.max()))
                    selected |= epoch.between(start, end, inclusive="left")
                tagged = rpm[selected & (rpm["value"] > load_wot.rpm_floor)].copy()
                if not tagged.empty:
                    tagged["second"] = tagged["timestamp"].dt.floor("1s")
                    per_second = tagged.groupby("second", sort=True)["value"].last()
                    return WotInventory(
                        seconds=int(len(per_second)),
                        with_afr_pair=0,
                        by_band=_bands(per_second, load_wot.rpm_bands),
                        source="tagged",
                    )
        except MemoryError:
            raise
        except Exception:  # noqa: BLE001
            pass
    # A DID-continuous capture must never borrow Mode 01 to make an empty
    # all-DID report look populated. Tags are the strongest physical-demand
    # evidence for these captures; failing those, the throttle's own measured
    # saturation is admissible but is labelled `inferred`, never `tagged`.
    if did_continuous:
        if criterion is not None and rpm_spec is not None and demand_spec is not None:
            return _infer_wot(
                session_dir,
                criterion=criterion,
                rpm_spec=rpm_spec,
                demand_spec=demand_spec,
                measured_afr_specs=measured_afr_specs or [],
                rpm_bands=rpm_bands,
            )
        missing = "wot_criterion" if criterion is None else "an rpm or operator_demand provider"
        return WotInventory(
            0,
            0,
            [],
            "unavailable",
            reason=(
                f"all-DID capture has no pull tags and {missing} is not configured for this "
                "vehicle, so nothing establishes which seconds were WOT"
            ),
        )
    if load_wot is None:
        return WotInventory(
            0,
            0,
            [],
            "unavailable",
            reason=(
                "no mode01_wot (load floor, rpm floor, rpm bands) is declared in this "
                "vehicle's tuning_providers.yaml, so nothing establishes which seconds were WOT"
            ),
        )
    load_floor, rpm_floor = load_wot.load_floor_pct, load_wot.rpm_floor
    try:
        frame = read_aligned(
            session_dir,
            ["obd:RPM", "obd:ENGINE_LOAD"],
            grid="1s",
            require_all=True,
        )
    except MemoryError:
        raise
    except Exception as exc:  # noqa: BLE001
        return WotInventory(0, 0, [], "unavailable", reason=f"obd:RPM/obd:ENGINE_LOAD: {exc}")
    if frame.empty:
        return WotInventory(
            0, 0, [], "unavailable", reason="no seconds carry both obd:RPM and obd:ENGINE_LOAD"
        )
    wot = frame[(frame["obd:RPM"] > rpm_floor) & (frame["obd:ENGINE_LOAD"] > load_floor)]
    if wot.empty:
        return WotInventory(
            0,
            0,
            [],
            "inferred",
            reason=(
                f"no second reached load > {load_floor:.0f} % above "
                f"{rpm_floor:.0f} rpm on this drive"
            ),
        )

    rows = _bands(wot["obd:RPM"], load_wot.rpm_bands)

    paired = 0
    try:
        with_afr = read_aligned(
            session_dir,
            ["obd:RPM", "obd:ENGINE_LOAD", "obd:O2_WR_LAMBDA_S1", "obd:COMMANDED_EQUIV_RATIO"],
            grid="1s",
            require_all=True,
        )
        paired = int(
            ((with_afr["obd:RPM"] > rpm_floor) & (with_afr["obd:ENGINE_LOAD"] > load_floor)).sum()
        )
    except MemoryError:
        raise
    except Exception:  # noqa: BLE001
        paired = 0
    # The Mode 01 path gates on measured load, not on a tag, so it is inferred
    # too — it just has a better instrument than the throttle-stop criterion.
    return WotInventory(
        seconds=int(len(wot)),
        with_afr_pair=paired,
        by_band=rows,
        source="inferred",
        with_measured_afr=paired,
        criterion={
            "role": "airflow_or_table_load",
            "provider": "obd:ENGINE_LOAD",
            "unit": "%",
            "threshold_value": load_floor,
            "rpm_floor": rpm_floor,
        },
    )


def _did_pull_inventory(
    session_dir: Path,
    *,
    role_capabilities: dict[str, Any],
    target_lookup_ready: bool,
    rpm_floor: float | None,
) -> dict[str, Any]:
    """Count evidence inside physical-pull tags without inventing WOT thresholds.

    The all-DID profile has no validated universal throttle/load threshold.
    Tags state what the operator physically did; this inventory then says which
    semantic observations were actually present during those intervals.
    """
    tags = load_tags(session_dir / "tags.jsonl")
    starts = [
        float(tag["t"])
        for tag in tags
        if is_pull_start(tag) and isinstance(tag.get("t"), int | float)
    ]
    ends = [
        float(tag["t"])
        for tag in tags
        if is_pull_end(tag) and isinstance(tag.get("t"), int | float)
    ]
    rpm_spec = role_capabilities.get("rpm", {}).get("provider")
    if not starts:
        return {
            "available": False,
            "reason": "no physical pull tags (`tuning_pull_start` or `wot_pull`)",
        }
    if not isinstance(rpm_spec, str) or not role_capabilities.get("rpm", {}).get(
        "available", False
    ):
        return {"available": False, "reason": "confirmed RPM provider is unavailable"}
    try:
        rpm = read_channels(session_dir, [rpm_spec], acquired_time=True)
    except MemoryError:
        raise
    except Exception:  # noqa: BLE001 — surfaced as absent evidence below
        return {"available": False, "reason": "RPM provider could not be read"}
    if rpm.empty:
        return {"available": False, "reason": "RPM provider has no samples"}
    # Samples and seconds below are stored rows; a change-only broadcast rpm
    # (P02.10) stores one per payload change.
    change_only = as_of_refusal(rpm, reader="pull evidence rpm seconds")
    if change_only is not None:
        return {"available": False, "reason": change_only}

    epoch = rpm["timestamp"].map(lambda value: value.timestamp())
    intervals: list[tuple[float, float]] = []
    for start in starts:
        intervals.append((start, next((end for end in ends if end > start), float(epoch.max()))))

    def in_intervals(series: pd.Series) -> pd.Series:
        seconds = series.map(lambda value: value.timestamp())
        selected = pd.Series(False, index=series.index)
        for start, end in intervals:
            selected |= seconds.between(start, end, inclusive="left")
        return selected

    tagged_rpm = rpm[in_intervals(rpm["timestamp"])].copy()
    # One count per role is intentionally not an alignment claim. These are
    # independent observations present during the physical pull; delivery
    # analysis still requires same-cell semantic alignment.
    evidence: dict[str, int | None] = {}
    refused: dict[str, str] = {}
    for role, label in (
        ("operator_demand", "operator_demand_samples"),
        ("measured_afr_b1", "measured_afr_samples"),
        ("fueling_active", "fueled_samples"),
        ("commanded_afr", "commanded_afr_samples"),
    ):
        provider = role_capabilities.get(role, {}).get("provider")
        if not isinstance(provider, str) or not role_capabilities.get(role, {}).get(
            "available", False
        ):
            evidence[label] = None
            continue
        try:
            samples = read_channels(session_dir, [provider], acquired_time=True)
        except MemoryError:
            raise
        except Exception:  # noqa: BLE001 — optional evidence stays unavailable
            evidence[label] = None
            continue
        change_only = as_of_refusal(samples, reader=f"pull evidence {label}")
        if change_only is not None:
            # A count of payload changes, not of samples (P02.10).
            evidence[label] = None
            refused[label] = change_only
            continue
        evidence[label] = int(in_intervals(samples["timestamp"]).sum())

    tagged_rpm["second"] = tagged_rpm["timestamp"].dt.floor("1s")
    per_second = tagged_rpm.groupby("second", sort=True)["value"].last()
    return {
        "available": True,
        "method": "physical pull tags + semantic providers",
        "pulls": len(intervals),
        "rpm_samples": int(len(tagged_rpm)),
        "rpm_seconds": int(len(per_second)),
        # Seconds above *this* vehicle's ``mode01_wot.rpm_floor`` (None
        # without one), with the floor beside it. Was ``rpm_over_2500_seconds``
        # (the dev vehicle's floor in a key name) until R05.13; no reader parses
        # the tuning section of a stored digest, so there is no alias.
        "rpm_floor": rpm_floor,
        "rpm_over_floor_seconds": (
            None if rpm_floor is None else int((per_second > rpm_floor).sum())
        ),
        "target_bearing_samples": 0 if target_lookup_ready else None,
        "target_lookup_ready": target_lookup_ready,
        **evidence,
        **({"refused_counts": refused} if refused else {}),
    }


#: How far apart a commanded and a measured cam read may have been acquired
#: and still count as one observation. Both are Mode 22 DIDs read in the same
#: poll round, ~1.5 ms apart (the ECM refuses batched reads), and a round of
#: the tuning DID set takes well under this; a looser bound would pair a
#: command with the cam's response to the *previous* command.
CAM_PAIRING = PairingPolicy.within("250ms")


def _cam_tracking(session_dir: Path, *, command_spec: str, measured_spec: str) -> dict[str, Any]:
    """How the measured cam angle followed its command, pair by pair.

    Only meaningful on an all-DID capture — cam angle exists as a DID and
    nowhere else, so a Mode 01 capture legitimately has nothing here (the
    caller does not ask). Tracking error is ``measured - commanded`` over
    same-instant pairs from :func:`~src.analysis.compare.align_pairs`, never
    over 1 s grid cells (``read_aligned``'s output is not pairing evidence).
    The scale relation comes from :func:`~src.analysis.compare.compare_channels`
    and is reported with its ``scale_verdict`` — or with the reason it was
    refused, which is routine at idle where the command does not move.

    Unavailability says which kind (R05.3): ``not_captured`` when a side has
    no observations, ``refused`` when pairing itself was refused.
    """
    try:
        aligned = align_pairs(session_dir, command_spec, measured_spec, policy=CAM_PAIRING)
    except ChannelSpecError as exc:
        return {"available": False, "kind": "not_captured", "reason": str(exc)}
    except InputNotCapturedError as exc:
        return {"available": False, "kind": "not_captured", "reason": str(exc)}
    except ComparisonRefusedError as exc:
        return {"available": False, "kind": "refused", "reason": str(exc)}
    if aligned.n_pairs < 3:
        return {
            "available": False,
            "kind": "refused",
            "reason": (
                f"only {aligned.n_pairs} same-instant pairs within "
                f"{CAM_PAIRING.max_separation_s * 1000.0:g} ms "
                f"({aligned.n_a} commanded, {aligned.n_b} measured observations)"
            ),
        }
    commanded = aligned.pairs["a"].astype(float)
    measured = aligned.pairs["b"].astype(float)
    error = measured - commanded
    fit: dict[str, Any]
    try:
        comparison = compare_channels(session_dir, command_spec, measured_spec, policy=CAM_PAIRING)
    except ComparisonRefusedError as exc:
        fit = {"available": False, "reason": str(exc)}
    else:
        fit = {
            "available": True,
            "r_squared": round(comparison.r_squared, 4),
            "slope": round(comparison.slope, 4),
            "offset": round(comparison.offset, 3),
            "slope_drift_pct": None
            if comparison.slope_drift_pct is None
            else round(comparison.slope_drift_pct, 2),
            "scale_verdict": comparison.scale_verdict,
        }
    return {
        "available": True,
        "n_pairs": aligned.n_pairs,
        "unmatched": [aligned.unmatched_a, aligned.unmatched_b],
        "policy": CAM_PAIRING.as_dict(),
        "commanded_range_deg": [round(float(commanded.min()), 1), round(float(commanded.max()), 1)],
        "measured_range_deg": [round(float(measured.min()), 1), round(float(measured.max()), 1)],
        "mean_tracking_error_deg": round(float(error.mean()), 2),
        "p95_abs_tracking_error_deg": round(float(error.abs().quantile(0.95)), 2),
        "max_tracking_error_deg": round(float(error.abs().max()), 2),
        "fit": fit,
    }


def _previous_capture(
    session_dir: Path, *, profile: str | None, revision: str | None, revisions: Any
) -> Path | None:
    """The newest earlier capture sharing this profile and tune revision.

    Both must match. Comparing across a flash boundary answers a different
    question — the delta is then whatever the flash changed, which is
    interesting but is not "is this drive like the last one".
    """
    if profile is None or revision is None:
        return None
    started = session_started_at(session_dir)
    if started is None:
        return None
    root = session_dir.parent

    # Order by the PARSED start time, never by directory name. Capture dirs
    # carry different prefixes (`_capture-fuel-map-...` vs `_tuning-...`) and
    # string order sorts by prefix first, so a `_capture-` session a month
    # later compares as "earlier" than a `_tuning-` one.
    dated: list[tuple[datetime, Path]] = []
    for path in list(root.glob("_capture-*")) + list(root.glob("_tuning-*")):
        if not path.is_dir():
            continue
        when = session_started_at(path)
        if when is not None and when < started:
            dated.append((when, path))

    for _when, candidate in sorted(dated, key=lambda item: item[0], reverse=True):
        if _capture_profile_name(candidate) != profile:
            continue
        found, _source, conflict = resolve_session_revision(candidate, revisions)
        if not conflict and found == revision:
            return candidate
    return None


#: The fuelling regime the digest's delivery error is gated to (open-loop
#: enrichment), and so the regime whose declared floor it may quote.
DELIVERY_REGIME = "enrichment"


def _delivery_significance(
    context: TuningContext, delta_pct: float
) -> Significance | FloorRefusal | None:
    """Sigma of a capture-to-capture delivery delta, or why there is none.

    ``None`` when the vehicle has no floor at all (a note already says so).
    The comparison's conditions come from the profile's declared floor for
    the enrichment regime — never from the floor itself, which would make the
    conditions check a tautology.
    """
    if context.noise_floor is None:
        return None
    declared = context.regime_floors.get(DELIVERY_REGIME)
    if declared is None:
        return FloorRefusal(
            channel="delivery_error_pct",
            kind="no_floor",
            reason=f"no noise floor is declared for the {DELIVERY_REGIME} regime",
            requested_conditions=DELIVERY_REGIME,
        )
    return context.noise_floor.significance(
        declared.channel, pd.Series([delta_pct]), conditions=declared.conditions
    )


@dataclass(frozen=True)
class _DigestInputs:
    """What every digest section reads: the session, the vehicle's tuning
    context (R05.1) and the capture's resolved identity and capabilities."""

    session_dir: Path
    context: TuningContext
    profile: str | None
    semantic: TuningFrame | None
    capabilities: dict[str, Any]
    did_continuous: bool
    revision: str | None
    revision_source: str
    revision_conflict: bool

    @property
    def roles(self) -> dict[str, Any]:
        roles: dict[str, Any] = self.capabilities.get("roles", {})
        return roles

    def section(self, name: str) -> dict[str, Any]:
        sections: dict[str, Any] = self.capabilities.get("sections", {})
        return dict(sections.get(name, {}))


def _digest_inputs(session_dir: Path, config: CanaryConfig, profile: str | None) -> _DigestInputs:
    # R05.1: one resolution of the vehicle's tuning facts — the *active* flash
    # registry (a flash recorded from the dashboard lives in the operational
    # copy), the profile's noise floor, and stoichiometry from the fuel.
    context = TuningContext.from_config(config)
    capabilities: dict[str, Any] = {}
    semantic = None
    if context.providers_path is not None:
        semantic = build_tuning_frame(
            session_dir,
            providers_path=context.providers_path,
            calibration=context.calibration,
            afr_stoich=context.afr_stoich,
        )
        capabilities = {
            "roles": semantic.capability,
            "sections": section_capabilities(semantic.capability),
        }
    revision, revision_source, revision_conflict = resolve_session_revision(
        session_dir, context.registry
    )
    manifest = read_capture_manifest(session_dir)
    return _DigestInputs(
        session_dir=session_dir,
        context=context,
        profile=profile,
        semantic=semantic,
        capabilities=capabilities,
        did_continuous=manifest is not None and manifest.did_continuous,
        revision=revision,
        revision_source=revision_source,
        revision_conflict=revision_conflict,
    )


def _section_wot(inputs: _DigestInputs) -> tuple[WotInventory, list[str]]:
    roles = inputs.roles
    rpm_provider = roles.get("rpm", {}).get("provider")
    demand_provider = roles.get("operator_demand", {}).get("provider")
    afr_providers = [
        str(roles.get(name, {}).get("provider"))
        for name in ("measured_afr_b1", "measured_afr_b2")
        if roles.get(name, {}).get("available", False)
    ]
    wot = _wot_inventory(
        inputs.session_dir,
        rpm_spec=rpm_provider if isinstance(rpm_provider, str) else None,
        did_continuous=inputs.did_continuous,
        criterion=None if inputs.semantic is None else inputs.semantic.wot_criterion,
        demand_spec=demand_provider if isinstance(demand_provider, str) else None,
        measured_afr_specs=afr_providers,
        load_wot=inputs.context.mode01_wot,
    )
    notes: list[str] = []
    if inputs.did_continuous and not any(
        is_pull_start(tag) for tag in load_tags(inputs.session_dir / "tags.jsonl")
    ):
        notes.append(
            "pull inventory unavailable: all-DID captures require a tagged physical pull "
            "(`tuning_pull_start` or `wot_pull`); this is not evidence of zero pulls."
        )
        if wot.source == "inferred" and wot.seconds and wot.criterion is not None:
            notes.append(
                f"WOT seconds below are INFERRED from {wot.criterion['provider']} reaching its "
                f"measured stop, not from tags. They locate data worth analysing; they do not "
                "establish what the operator was doing, so do not attribute them to an "
                "intended manoeuvre."
            )
        elif wot.reason:
            notes.append(f"WOT inventory: {wot.reason}")
    if wot.seconds and not wot.with_afr_pair:
        notes.append(
            "WOT seconds were captured but none carry both a measured and a commanded AFR "
            "in the same second, so delivery error cannot be computed from this drive."
        )
    return wot, notes


def _rates(inputs: _DigestInputs) -> tuple[dict[str, float], dict[str, Any]]:
    """Achieved rate and rate summary over exactly what the capture asked for.

    An all-DID report measures exactly the semantic providers that were
    actually present; hard-coded Mode-01 names here previously made a DID
    capture look slower or emptier than it was.

    But the semantic registry is DID-oriented, so on a Mode-01 fuelling
    capture it resolves almost nothing and the report fell back to three
    hard-coded channels — RPM, lambda, timing. Those are the *fast* ones. The
    2026-08-10 capture's rate summary therefore showed 0.67-0.78 Hz and
    omitted every channel that was actually running at 0.28 Hz and gating the
    analysis. A rate summary that silently drops the limiting channel is
    worse than none. The profile snapshot names what the capture asked for,
    so measure exactly that.
    """
    rate_specs = sorted(
        {
            str(role["provider"])
            for role in inputs.roles.values()
            if role.get("available", False) and isinstance(role.get("provider"), str)
        }
        | {f"obd:{name.upper()}" for name in _profile_mode01_channels(inputs.session_dir)}
    )
    if not rate_specs and not inputs.did_continuous:
        rate_specs = ["obd:RPM", "obd:O2_WR_LAMBDA_S1", "obd:TIMING_ADVANCE"]
    return _achieved_hz(inputs.session_dir, rate_specs), _rate_summary(
        inputs.session_dir, rate_specs
    )


def _delivery(inputs: _DigestInputs) -> tuple[dict[str, Any] | None, list[str]]:
    """Delivery error by whichever route the capture supports.

    Three branches: an all-DID capture without the semantic roles (named as
    unavailable), an all-DID capture through the semantic route (the provider
    registry's target and A/F channels), and a Mode 01 capture. Both routes
    run the one acquisition-paired engine (R05.16).
    """
    context = inputs.context
    notes: list[str] = []
    delivery: dict[str, Any] | None = None
    section = inputs.section("delivery_error")
    load_wot = context.mode01_wot
    if load_wot is None:
        notes.append(
            "delivery error unavailable: no mode01_wot (rpm bands, load floor) is declared "
            "in this vehicle's tuning_providers.yaml"
        )
        return None, notes
    if inputs.did_continuous and not section.get("available", False):
        missing = section.get("missing_roles", [])
        notes.append(
            "delivery error unavailable: all-DID capture lacks confirmed semantic roles "
            + ", ".join(map(str, missing))
        )
        return None, notes
    semantic = inputs.did_continuous and inputs.semantic is not None
    if not semantic and context.calibration is None:
        return None, notes
    try:
        if semantic:
            if context.lambda_delivery is None or context.providers is None:
                raise GateError(
                    "this vehicle has no lambda_delivery.yaml declaring its target and "
                    "measured sources"
                )
            result = delivery_error(
                inputs.session_dir,
                declaration=context.lambda_delivery,
                route=semantic_route(
                    context.lambda_delivery, context.providers, measured_role="measured_afr_b1"
                ),
                calibration=context.calibration,
                revisions=context.registry,
                afr_stoich=context.afr_stoich,
                rpm_bands=load_wot.rpm_bands,
                # The ROM's Base Fuel Schedule axis is not yet observable, so
                # an all-DID delivery result cannot claim the Mode-01 load
                # gate. Physical pull tags remain the evidence for demand.
                min_load=None,
            )
        else:
            result = _mode01_delivery(inputs.session_dir, context)
        delivery = result.as_dict() if result.n else None
        sensitivity = result.delay_sensitivity
        if sensitivity is not None and not sensitivity["stable"]:
            notes.append(
                "delivery error is unstable across the transport-delay bracket: "
                f"{sensitivity['reason']}"
            )
        if result.n == 0:
            notes.append(
                (
                    "no samples survived the semantic delivery-error gates: "
                    if semantic
                    else "no samples survived the delivery-error gates: "
                )
                + ", ".join(f"{k} -{v}" for k, v in result.gates["filtered"].items())
            )
    except (GateError, ValueError) as exc:
        notes.append(f"delivery error unavailable: {exc}")
    return delivery, notes


def _mode01_delivery(session_dir: Path, context: TuningContext) -> DeliveryError:
    """The Mode 01 delivery error at the profile's WOT load floor."""
    load_wot = context.mode01_wot
    assert load_wot is not None
    return delivery_error(
        session_dir,
        declaration=context.lambda_delivery,
        calibration=context.calibration,
        revisions=context.registry,
        afr_stoich=context.afr_stoich,
        mode01_exclusion_note=context.mode01_exclusion_note,
        rpm_bands=load_wot.rpm_bands,
        min_load=load_wot.load_floor_pct,
    )


def _coverage_notes(complete_tuples: dict[str, Any], wot: WotInventory) -> list[str]:
    """Coverage in the terms an operator can act on, stated before any result:
    "captured but never co-observed" and "never captured" demand opposite
    responses, and a bare delivery-error ``n`` distinguishes neither."""
    if not complete_tuples.get("available"):
        return []
    if complete_tuples.get("absent_members"):
        return [
            "required-tuple members absent from this session — "
            f"{', '.join(complete_tuples['absent_members'])} — so the tuple could never "
            "complete. Nothing about the drive can change this; fix the profile."
        ]
    qualifier = (
        " (tuple inferred, not declared by this capture's profile)"
        if complete_tuples.get("source") == "inferred"
        else ""
    )
    return [
        f"{wot.seconds} WOT seconds, {wot.with_afr_pair} carrying an AFR pair, "
        f"{complete_tuples['complete_cells']} seconds where the full required tuple "
        f"({', '.join(complete_tuples['members'])}) was co-observed{qualifier}."
    ]


def _capture_quality_notes(capture_quality: dict[str, Any] | None) -> list[str]:
    if capture_quality is None:
        return [
            "no capture_quality.json in this session, so whether the capture met its "
            "profile's rate contract is unknown — not verified, and not a pass."
        ]
    clauses = ", ".join(str(item.get("clause")) for item in capture_quality.get("failures") or [])
    if capture_quality.get("scope") == "trailing_snapshot":
        return [
            f"capture contract verdict: {capture_quality.get('verdict')}"
            + (f" ({clauses})" if clauses else "")
            + " — scored on the trailing 60 s at stop only (schema_version 1), not the "
            "capture; loss inside the capture is unknown. Recompute from the rows before "
            "trusting it."
        ]
    if capture_quality.get("verdict") != "pass":
        return [
            f"capture contract verdict: {capture_quality.get('verdict')}"
            + (f" ({clauses})" if clauses else "")
            + " — read capture_quality.json before trusting any rate-sensitive result here."
        ]
    return []


def _previous(
    inputs: _DigestInputs, delivery: dict[str, Any] | None
) -> tuple[dict[str, Any] | None, list[str]]:
    """The newest earlier capture on this profile and revision, and the
    delivery delta against it with its significance (R05.2)."""
    context = inputs.context
    notes: list[str] = []
    if context.noise_floor is None:
        notes.append(
            "no measured noise floor for this vehicle, so deltas in any comparison against "
            "this drive carry no significance verdict; record one from a same-tune pair."
        )
    earlier = (
        None
        if inputs.revision_conflict
        else _previous_capture(
            inputs.session_dir,
            profile=inputs.profile,
            revision=inputs.revision,
            revisions=context.registry,
        )
    )
    if earlier is None:
        return None, notes
    previous: dict[str, Any] = {"session_id": earlier.name}
    load_wot = context.mode01_wot
    if delivery is None or context.calibration is None or load_wot is None:
        return previous, notes
    try:
        before = _mode01_delivery(earlier, context)
    except (GateError, ValueError):
        before = None
    if before is not None and before.n:
        previous["delivery_band_averaged_pct"] = before.band_averaged_pct
        previous["delta_pct"] = delivery["band_averaged_pct"] - before.band_averaged_pct
        verdict = _delivery_significance(context, previous["delta_pct"])
        if isinstance(verdict, Significance):
            sigma = float(verdict.delta_sigma.iloc[0])
            previous["delta_sigma"] = round(sigma, 2)
            previous["distinguishable"] = bool(verdict.distinguishable.iloc[0])
        else:
            previous["delta_sigma"] = None
            previous["distinguishable"] = None
            if verdict is not None:
                previous["floor_refusal"] = verdict.as_dict()
    return previous, notes


def _cam(inputs: _DigestInputs) -> dict[str, Any] | None:
    if not inputs.section("cam_tracking").get("available"):
        return None
    command_spec = inputs.roles["cam_command"]["provider"]
    measured_spec = inputs.roles["cam_measured"]["provider"]
    if not (isinstance(command_spec, str) and isinstance(measured_spec, str)):
        return None
    return _cam_tracking(inputs.session_dir, command_spec=command_spec, measured_spec=measured_spec)


def compute_tuning_digest(
    session_dir: Path,
    config: CanaryConfig,
) -> TuningDigest | None:
    """Tuning section for a capture session, or ``None`` for an ordinary drive.

    Returns ``None`` rather than raising whenever the inputs are not a tuning
    capture — a normal drive's digest must not fail because this section had
    nothing to say. Each section below returns its payload and its notes; the
    notes are assembled in a fixed order so the digest reads the same way
    whichever sections had something to say.
    """
    if not has_tuning_section(session_dir):
        return None
    profile = _capture_profile_name(session_dir)

    inputs = _digest_inputs(session_dir, config, profile)
    calibration = inputs.context.calibration
    load_wot = inputs.context.mode01_wot
    notes: list[str] = []
    if inputs.revision_conflict:
        notes.append(
            "capture manifest tune revision conflicts with the timestamp registry; "
            "automatic previous-capture comparison is refused."
        )
    wot, wot_notes = _section_wot(inputs)
    notes += wot_notes
    achieved, rate_summary = _rates(inputs)
    required_tuple, tuple_source = _resolve_required_tuple(session_dir)
    complete_tuples = _complete_tuple_summary(session_dir, required_tuple, source=tuple_source)
    capture_quality = read_capture_quality(session_dir)
    measured_afr = _measured_afr_summary(
        session_dir, calibration=calibration, role_capabilities=inputs.roles
    )
    target = None if calibration is None else calibration.target_map(inputs.revision)
    target_map = _target_map_summary(
        target, revision=inputs.revision, role_capabilities=inputs.roles
    )
    pull_inventory = (
        _did_pull_inventory(
            session_dir,
            role_capabilities=inputs.roles,
            target_lookup_ready=bool(target_map.get("lookup_ready", False)),
            rpm_floor=None if load_wot is None else load_wot.rpm_floor,
        )
        if inputs.did_continuous
        else {}
    )
    delivery, delivery_notes = _delivery(inputs)
    notes += delivery_notes
    notes += _coverage_notes(complete_tuples, wot)
    notes += _capture_quality_notes(capture_quality)
    previous, previous_notes = _previous(inputs, delivery)
    notes += previous_notes

    return TuningDigest(
        session_id=session_dir.name,
        capture_profile=profile,
        tune_revision=inputs.revision,
        wot=wot,
        achieved_hz=achieved,
        pull_inventory=pull_inventory,
        rate_summary=rate_summary,
        complete_tuples=complete_tuples,
        capture_quality=capture_quality,
        measured_afr=measured_afr,
        target_map=target_map,
        delivery=delivery,
        cam=_cam(inputs),
        previous=previous,
        capabilities=inputs.capabilities,
        notes=[*notes, f"tune revision source: {inputs.revision_source}"],
    )


__all__ = ["TUNING_SCHEMA_VERSION", "TuningDigest", "compute_tuning_digest", "has_tuning_section"]
