"""The tuning half of a post-drive digest.

"Drive's over" should produce a report, not an hour of ad-hoc SQL. This
assembles what the operator actually wants to know after a tuning capture:
how much usable WOT it caught, whether the capture ran at the rate it was
armed for, what the delivery error was, and how the cam tracked its command.

Deliberately separate from :mod:`src.analysis.digest`: this section only
exists for capture sessions, needs an AFR calibration and a tune-revision
registry the ordinary digest has no use for, and must degrade to ``None``
rather than failing a normal drive's digest when a channel is missing.

See ``docs/roadmap/tuning-analysis-toolkit.md`` Phase 5.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

from src.analysis.afr import AfrCalibration, TargetAfrMap, load_afr_calibration, voltage_to_afr
from src.analysis.channels import read_aligned, read_channels
from src.analysis.noise_floor import load_noise_floor
from src.analysis.tune_revisions import (
    load_tune_revisions,
    resolve_session_revision,
    session_started_at,
)
from src.analysis.tuning import (
    DEFAULT_RPM_BANDS,
    GateError,
    delivery_error,
    delivery_error_from_semantic_frame,
)
from src.analysis.tuning_frame import WotCriterion, build_tuning_frame, section_capabilities
from src.collection.capture_profiles import FUEL_DELIVERY_TUPLE
from src.config import CanaryConfig
from src.control.capture_manifest import read_capture_manifest, read_capture_quality
from src.session import load_tags

#: Load floor for the WOT inventory. The OBD throttle percentage tops out
#: around 89 % and understates true WOT, so load is the better gate.
WOT_LOAD_FLOOR = 85.0
WOT_RPM_FLOOR = 2500.0

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
    except Exception:  # noqa: BLE001 — fall back to per-spec so one bad name is not fatal
        out: dict[str, pd.DataFrame] = {}
        for spec in specs:
            try:
                rows = read_channels(session_dir, [spec], acquired_time=acquired_time)
            except Exception:  # noqa: BLE001, PERF203 — an absent channel is normal
                continue
            if not rows.empty:
                out[spec] = rows
        return out
    if frame.empty:
        return {}
    return {str(spec): rows for spec, rows in frame.groupby("spec", sort=False)}


def _achieved_hz(session_dir: Path, specs: list[str]) -> dict[str, float]:
    """Real per-channel rate over the session, from the rows themselves.

    Computed from stored data rather than read off `/status`, so it still
    works long after the drive — and so the number in the digest is what was
    actually written, not what the scheduler believed it was doing.
    """
    out: dict[str, float] = {}
    for spec, rows in _read_specs(session_dir, specs).items():
        if rows.empty:
            continue
        span = (rows["timestamp"].max() - rows["timestamp"].min()).total_seconds()
        if span > 0:
            out[spec] = round(len(rows) / span, 2)
    return out


def _rate_summary(session_dir: Path, specs: list[str]) -> dict[str, dict[str, float | int | None]]:
    """Summarize persisted polling cadence and its worst gap by provider.

    A nominal target rate cannot tell a post-drive reader whether a channel
    disappeared for ten seconds.  The session has no reliable "paused" state,
    so these are deliberately wall-clock sample timings, not a claim about
    active scheduler time.
    """
    out: dict[str, dict[str, float | int | None]] = {}
    for spec, frame in _read_specs(session_dir, specs, acquired_time=True).items():
        timestamps = frame["timestamp"].dropna().sort_values().drop_duplicates()
        count = len(timestamps)
        if not count:
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
        except Exception:  # noqa: BLE001 — capability report carries the ordinary reason
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
        "available": bool(banks),
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


def _bands(per_second: pd.Series) -> list[dict[str, Any]]:
    """Seconds per rpm band, in the shared band edges."""
    counts = per_second.groupby(pd.cut(per_second, list(DEFAULT_RPM_BANDS)), observed=True).size()
    return [{"rpm_band": str(band), "seconds": int(seconds)} for band, seconds in counts.items()]


def _infer_wot(
    session_dir: Path,
    *,
    criterion: WotCriterion,
    rpm_spec: str,
    demand_spec: str,
    measured_afr_specs: list[str],
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
        except Exception:  # noqa: BLE001
            measured = 0
    return WotInventory(
        seconds=int(len(at_wot)),
        # Always 0 on an all-DID capture: a commanded target needs Mode 01,
        # which this profile disables. Kept rather than dropped so the number
        # that gates delivery error stays visible.
        with_afr_pair=0,
        by_band=_bands(at_wot[rpm_spec]),
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
) -> WotInventory:
    """Seconds at WOT by rpm band, and how many carry a usable AFR pair.

    The pair count is the number that matters: WOT seconds without both a
    measured and a commanded AFR in the same second cannot answer anything,
    and the July corpus had 25 such seconds against 4 usable ones.
    """
    tags = load_tags(session_dir / "tags.jsonl")
    starts = [
        float(tag["t"]) for tag in tags if tag.get("label") in {"tuning_pull_start", "wot_pull"}
    ]
    ends = [float(tag["t"]) for tag in tags if tag.get("label") == "tuning_pull_end"]
    if starts and rpm_spec is not None:
        try:
            rpm = read_channels(session_dir, [rpm_spec], acquired_time=True)
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
                tagged = rpm[selected & (rpm["value"] > WOT_RPM_FLOOR)].copy()
                if not tagged.empty:
                    tagged["second"] = tagged["timestamp"].dt.floor("1s")
                    per_second = tagged.groupby("second", sort=True)["value"].last()
                    return WotInventory(
                        seconds=int(len(per_second)),
                        with_afr_pair=0,
                        by_band=_bands(per_second),
                        source="tagged",
                    )
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
    try:
        frame = read_aligned(
            session_dir,
            ["obd:RPM", "obd:ENGINE_LOAD"],
            grid="1s",
            require_all=True,
        )
    except Exception as exc:  # noqa: BLE001
        return WotInventory(0, 0, [], "unavailable", reason=f"obd:RPM/obd:ENGINE_LOAD: {exc}")
    if frame.empty:
        return WotInventory(
            0, 0, [], "unavailable", reason="no seconds carry both obd:RPM and obd:ENGINE_LOAD"
        )
    wot = frame[(frame["obd:RPM"] > WOT_RPM_FLOOR) & (frame["obd:ENGINE_LOAD"] > WOT_LOAD_FLOOR)]
    if wot.empty:
        return WotInventory(
            0,
            0,
            [],
            "inferred",
            reason=(
                f"no second reached load > {WOT_LOAD_FLOOR:.0f} % above "
                f"{WOT_RPM_FLOOR:.0f} rpm on this drive"
            ),
        )

    rows = _bands(wot["obd:RPM"])

    paired = 0
    try:
        with_afr = read_aligned(
            session_dir,
            ["obd:RPM", "obd:ENGINE_LOAD", "obd:O2_WR_LAMBDA_S1", "obd:COMMANDED_EQUIV_RATIO"],
            grid="1s",
            require_all=True,
        )
        paired = int(
            (
                (with_afr["obd:RPM"] > WOT_RPM_FLOOR)
                & (with_afr["obd:ENGINE_LOAD"] > WOT_LOAD_FLOOR)
            ).sum()
        )
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
            "threshold_value": WOT_LOAD_FLOOR,
            "rpm_floor": WOT_RPM_FLOOR,
        },
    )


def _did_pull_inventory(
    session_dir: Path,
    *,
    role_capabilities: dict[str, Any],
    target_lookup_ready: bool,
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
        if tag.get("label") in {"tuning_pull_start", "wot_pull"}
        and isinstance(tag.get("t"), int | float)
    ]
    ends = [
        float(tag["t"])
        for tag in tags
        if tag.get("label") == "tuning_pull_end" and isinstance(tag.get("t"), int | float)
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
    except Exception:  # noqa: BLE001 — surfaced as absent evidence below
        return {"available": False, "reason": "RPM provider could not be read"}
    if rpm.empty:
        return {"available": False, "reason": "RPM provider has no samples"}

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
        except Exception:  # noqa: BLE001 — optional evidence stays unavailable
            evidence[label] = None
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
        "rpm_over_2500_seconds": int((per_second > WOT_RPM_FLOOR).sum()),
        "target_bearing_samples": 0 if target_lookup_ready else None,
        "target_lookup_ready": target_lookup_ready,
        **evidence,
    }


def _cam_tracking(
    session_dir: Path, *, command_spec: str, measured_spec: str
) -> dict[str, Any] | None:
    """How the measured cam angle followed its command.

    Only meaningful on an all-DID capture — cam angle exists as a DID and
    nowhere else, so a Mode 01 capture legitimately has nothing here.
    """
    try:
        frame = read_aligned(
            session_dir,
            [command_spec, measured_spec],
            grid="1s",
            require_all=True,
        )
    except Exception:  # noqa: BLE001
        return None
    if len(frame) < 3:
        return None
    commanded = frame[command_spec].astype(float)
    measured = frame[measured_spec].astype(float)
    error = measured - commanded
    return {
        "n": int(len(frame)),
        "commanded_range_deg": [round(float(commanded.min()), 1), round(float(commanded.max()), 1)],
        "measured_range_deg": [round(float(measured.min()), 1), round(float(measured.max()), 1)],
        "mean_tracking_error_deg": round(float(error.mean()), 2),
        "max_tracking_error_deg": round(float(error.abs().max()), 2),
        "correlation": round(float(np.corrcoef(commanded, measured)[0, 1]), 4)
        if commanded.std() > 0 and measured.std() > 0
        else None,
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


def compute_tuning_digest(
    session_dir: Path,
    config: CanaryConfig,
) -> TuningDigest | None:
    """Tuning section for a capture session, or ``None`` for an ordinary drive.

    Returns ``None`` rather than raising whenever the inputs are not a tuning
    capture — a normal drive's digest must not fail because this section had
    nothing to say.
    """
    profile = _capture_profile_name(session_dir)
    is_capture = profile is not None or session_dir.name.startswith("_")
    if not is_capture:
        return None

    notes: list[str] = []
    profile_data = config.daemon.afr_calibration.parent
    calibration_path = config.daemon.afr_calibration
    calibration = load_afr_calibration(calibration_path) if calibration_path.exists() else None
    providers_path = profile_data / "tuning_providers.yaml"
    capabilities: dict[str, Any] = {}
    semantic = None
    if providers_path.exists():
        semantic = build_tuning_frame(
            session_dir, providers_path=providers_path, calibration=calibration
        )
        capabilities = {
            "roles": semantic.capability,
            "sections": section_capabilities(semantic.capability),
        }
    revisions = load_tune_revisions(profile_data / "tune_revisions.yaml")
    revision, revision_source, revision_conflict = resolve_session_revision(session_dir, revisions)
    if revision_conflict:
        notes.append(
            "capture manifest tune revision conflicts with the timestamp registry; "
            "automatic previous-capture comparison is refused."
        )

    role_capabilities = capabilities.get("roles", {})
    rpm_provider = role_capabilities.get("rpm", {}).get("provider")
    did_continuous = profile == "tuning-did"
    demand_provider = role_capabilities.get("operator_demand", {}).get("provider")
    afr_providers = [
        str(role_capabilities.get(name, {}).get("provider"))
        for name in ("measured_afr_b1", "measured_afr_b2")
        if role_capabilities.get(name, {}).get("available", False)
    ]
    wot = _wot_inventory(
        session_dir,
        rpm_spec=rpm_provider if isinstance(rpm_provider, str) else None,
        did_continuous=did_continuous,
        criterion=None if semantic is None else semantic.wot_criterion,
        demand_spec=demand_provider if isinstance(demand_provider, str) else None,
        measured_afr_specs=afr_providers,
    )
    if did_continuous and not any(
        tag.get("label") in {"tuning_pull_start", "wot_pull"}
        for tag in load_tags(session_dir / "tags.jsonl")
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

    # An all-DID report measures exactly the semantic providers that were
    # actually present; hard-coded Mode-01 names here previously made a DID
    # capture look slower or emptier than it was.
    #
    # But the semantic registry is DID-oriented, so on a Mode-01 fuelling
    # capture it resolves almost nothing and the report fell back to three
    # hard-coded channels — RPM, lambda, timing. Those are the *fast* ones. The
    # 2026-08-10 capture's rate summary therefore showed 0.67-0.78 Hz and
    # omitted every channel that was actually running at 0.28 Hz and gating the
    # analysis. A rate summary that silently drops the limiting channel is
    # worse than none. The profile snapshot names what the capture asked for,
    # so measure exactly that.
    rate_specs = sorted(
        {
            str(role["provider"])
            for role in role_capabilities.values()
            if role.get("available", False) and isinstance(role.get("provider"), str)
        }
        | {f"obd:{name.upper()}" for name in _profile_mode01_channels(session_dir)}
    )
    if not rate_specs and not did_continuous:
        rate_specs = ["obd:RPM", "obd:O2_WR_LAMBDA_S1", "obd:TIMING_ADVANCE"]
    achieved = _achieved_hz(session_dir, rate_specs)
    rate_summary = _rate_summary(session_dir, rate_specs)
    required_tuple, tuple_source = _resolve_required_tuple(session_dir)
    complete_tuples = _complete_tuple_summary(session_dir, required_tuple, source=tuple_source)
    capture_quality = read_capture_quality(session_dir)
    measured_afr = _measured_afr_summary(
        session_dir, calibration=calibration, role_capabilities=role_capabilities
    )
    target = None if calibration is None else calibration.target_map(revision)
    target_map = _target_map_summary(target, revision=revision, role_capabilities=role_capabilities)
    pull_inventory = (
        _did_pull_inventory(
            session_dir,
            role_capabilities=role_capabilities,
            target_lookup_ready=bool(target_map.get("lookup_ready", False)),
        )
        if did_continuous
        else {}
    )

    delivery: dict[str, Any] | None = None
    delivery_available = (
        capabilities.get("sections", {}).get("delivery_error", {}).get("available", False)
    )
    if did_continuous and not delivery_available:
        missing = (
            capabilities.get("sections", {}).get("delivery_error", {}).get("missing_roles", [])
        )
        notes.append(
            "delivery error unavailable: all-DID capture lacks confirmed semantic roles "
            + ", ".join(map(str, missing))
        )
    elif did_continuous and semantic is not None:
        values = semantic.values.rename(
            columns={
                "measured_afr_b1": "measured_afr",
                "measured_afr_b1_censored": "measured_afr_censored",
            }
        )
        provider = role_capabilities.get("measured_afr_b1", {}).get("provider")
        try:
            result = delivery_error_from_semantic_frame(
                values,
                measured_spec=f"semantic:measured_afr_b1 ({provider})",
                # The ROM's Base Fuel Schedule axis is not yet observable, so
                # an all-DID delivery result cannot claim the Mode-01 load
                # gate. Physical pull tags remain the evidence for demand.
                min_load=None,
                tune_revision=revision,
                tune_revision_source=revision_source,
            )
            delivery = result.as_dict() if result.n else None
            if result.n == 0:
                notes.append(
                    "no samples survived the semantic delivery-error gates: "
                    + ", ".join(f"{k} -{v}" for k, v in result.gates["filtered"].items())
                )
        except (GateError, ValueError) as exc:
            notes.append(f"delivery error unavailable: {exc}")
    elif calibration is not None:
        try:
            result = delivery_error(
                session_dir,
                calibration=calibration,
                revisions=revisions,
            )
            delivery = result.as_dict() if result.n else None
            if result.n == 0:
                notes.append(
                    "no samples survived the delivery-error gates: "
                    + ", ".join(f"{k} -{v}" for k, v in result.gates["filtered"].items())
                )
        except (GateError, ValueError) as exc:
            notes.append(f"delivery error unavailable: {exc}")

    # Coverage in the terms an operator can act on, stated before any result:
    # "captured but never co-observed" and "never captured" demand opposite
    # responses, and a bare delivery-error `n` distinguishes neither.
    if complete_tuples.get("available"):
        if complete_tuples.get("absent_members"):
            notes.append(
                "required-tuple members absent from this session — "
                f"{', '.join(complete_tuples['absent_members'])} — so the tuple could never "
                "complete. Nothing about the drive can change this; fix the profile."
            )
        else:
            qualifier = (
                " (tuple inferred, not declared by this capture's profile)"
                if complete_tuples.get("source") == "inferred"
                else ""
            )
            notes.append(
                f"{wot.seconds} WOT seconds, {wot.with_afr_pair} carrying an AFR pair, "
                f"{complete_tuples['complete_cells']} seconds where the full required tuple "
                f"({', '.join(complete_tuples['members'])}) was co-observed{qualifier}."
            )
    if capture_quality is None:
        notes.append(
            "no capture_quality.json in this session, so whether the capture met its "
            "profile's rate contract is unknown — not verified, and not a pass."
        )
    elif capture_quality.get("verdict") != "pass":
        clauses = ", ".join(
            str(item.get("clause")) for item in capture_quality.get("failures") or []
        )
        notes.append(
            f"capture contract verdict: {capture_quality.get('verdict')}"
            + (f" ({clauses})" if clauses else "")
            + " — read capture_quality.json before trusting any rate-sensitive result here."
        )

    floor = load_noise_floor(profile_data / "noise_floor.yaml")
    if floor is None:
        notes.append(
            "no measured noise floor for this vehicle, so deltas in any comparison against "
            "this drive carry no significance verdict; record one from a same-tune pair."
        )

    previous: dict[str, Any] | None = None
    earlier = (
        None
        if revision_conflict
        else _previous_capture(session_dir, profile=profile, revision=revision, revisions=revisions)
    )
    if earlier is not None:
        previous = {"session_id": earlier.name}
        if delivery is not None and calibration is not None:
            try:
                before = delivery_error(
                    earlier,
                    calibration=calibration,
                    revisions=revisions,
                )
            except (GateError, ValueError):
                before = None
            if before is not None and before.n:
                previous["delivery_band_averaged_pct"] = before.band_averaged_pct
                previous["delta_pct"] = delivery["band_averaged_pct"] - before.band_averaged_pct
                sd = floor.sd_for("delivery_error_pct") if floor is not None else None
                if sd:
                    sigma = previous["delta_pct"] / sd
                    previous["delta_sigma"] = round(sigma, 2)
                    previous["distinguishable"] = abs(sigma) >= 2.0
                else:
                    previous["delta_sigma"] = None
                    previous["distinguishable"] = None

    cam: dict[str, Any] | None = None
    cam_roles = role_capabilities
    if capabilities.get("sections", {}).get("cam_tracking", {}).get("available"):
        command_spec = cam_roles["cam_command"]["provider"]
        measured_spec = cam_roles["cam_measured"]["provider"]
        if isinstance(command_spec, str) and isinstance(measured_spec, str):
            cam = _cam_tracking(session_dir, command_spec=command_spec, measured_spec=measured_spec)

    return TuningDigest(
        session_id=session_dir.name,
        capture_profile=profile,
        tune_revision=revision,
        wot=wot,
        achieved_hz=achieved,
        pull_inventory=pull_inventory,
        rate_summary=rate_summary,
        complete_tuples=complete_tuples,
        capture_quality=capture_quality,
        measured_afr=measured_afr,
        target_map=target_map,
        delivery=delivery,
        cam=cam,
        previous=previous,
        capabilities=capabilities,
        notes=[*notes, f"tune revision source: {revision_source}"],
    )


__all__ = ["TuningDigest", "compute_tuning_digest"]
