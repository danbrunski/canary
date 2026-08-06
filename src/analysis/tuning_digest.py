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
from typing import Any

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
from src.analysis.tuning_frame import build_tuning_frame, section_capabilities
from src.config import CanaryConfig
from src.control.capture_manifest import read_capture_manifest
from src.session import load_tags

#: Load floor for the WOT inventory. The OBD throttle percentage tops out
#: around 89 % and understates true WOT, so load is the better gate.
WOT_LOAD_FLOOR = 85.0
WOT_RPM_FLOOR = 2500.0


@dataclass
class TuningDigest:
    """Tuning section of a capture session's digest."""

    session_id: str
    capture_profile: str | None
    tune_revision: str | None
    wot_seconds: int
    wot_with_afr_pair: int
    wot_by_band: list[dict[str, Any]]
    achieved_hz: dict[str, float]
    #: Evidence inside physically tagged all-DID pulls. Separate counts avoid
    #: treating absent command/fueling/target roles as a measured zero.
    pull_inventory: dict[str, Any] = field(default_factory=dict)
    #: Per-provider stored-sample timing.  Unlike the live scheduler's
    #: trailing observation window, this is the evidence preserved with the
    #: completed drive and makes long polling stalls visible.
    rate_summary: dict[str, dict[str, float | int | None]] = field(default_factory=dict)
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
            "wot_seconds": self.wot_seconds,
            "wot_with_afr_pair": self.wot_with_afr_pair,
            "wot_by_band": list(self.wot_by_band),
            "achieved_hz": dict(self.achieved_hz),
            "pull_inventory": self.pull_inventory,
            "rate_summary": self.rate_summary,
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


def _achieved_hz(session_dir: Path, specs: list[str]) -> dict[str, float]:
    """Real per-channel rate over the session, from the rows themselves.

    Computed from stored data rather than read off `/status`, so it still
    works long after the drive — and so the number in the digest is what was
    actually written, not what the scheduler believed it was doing.
    """
    out: dict[str, float] = {}
    for spec in specs:
        try:
            frame = read_channels(session_dir, [spec])
        except Exception:  # noqa: BLE001 — a missing channel is not a digest failure
            continue
        if frame.empty:
            continue
        span = (frame["timestamp"].max() - frame["timestamp"].min()).total_seconds()
        if span > 0:
            out[spec] = round(len(frame) / span, 2)
    return out


def _rate_summary(session_dir: Path, specs: list[str]) -> dict[str, dict[str, float | int | None]]:
    """Summarize persisted polling cadence and its worst gap by provider.

    A nominal target rate cannot tell a post-drive reader whether a channel
    disappeared for ten seconds.  The session has no reliable "paused" state,
    so these are deliberately wall-clock sample timings, not a claim about
    active scheduler time.
    """
    out: dict[str, dict[str, float | int | None]] = {}
    for spec in specs:
        try:
            frame = read_channels(session_dir, [spec], acquired_time=True)
        except Exception:  # noqa: BLE001 — absent optional providers are normal
            continue
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
            "longest_gap_seconds": round(float(positive.max()), 3) if not positive.empty else None,
        }
    return out


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


def _wot_inventory(
    session_dir: Path, *, rpm_spec: str | None, did_continuous: bool
) -> tuple[int, int, list[dict[str, Any]]]:
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
                    tagged_bands = per_second.groupby(
                        pd.cut(per_second, list(DEFAULT_RPM_BANDS)), observed=True
                    ).size()
                    return (
                        int(len(per_second)),
                        0,
                        [
                            {"rpm_band": str(band), "seconds": int(seconds)}
                            for band, seconds in tagged_bands.items()
                        ],
                    )
        except Exception:  # noqa: BLE001
            pass
    # A DID-continuous capture must never borrow Mode 01 to make an empty
    # all-DID report look populated. Tags are the physical-demand evidence for
    # these captures; absent tags are reported as absent evidence.
    if did_continuous:
        return 0, 0, []
    try:
        frame = read_aligned(
            session_dir,
            ["obd:RPM", "obd:ENGINE_LOAD"],
            grid="1s",
            require_all=True,
        )
    except Exception:  # noqa: BLE001
        return 0, 0, []
    if frame.empty:
        return 0, 0, []
    wot = frame[(frame["obd:RPM"] > WOT_RPM_FLOOR) & (frame["obd:ENGINE_LOAD"] > WOT_LOAD_FLOOR)]
    if wot.empty:
        return 0, 0, []

    bands = (
        wot.groupby(pd.cut(wot["obd:RPM"], list(DEFAULT_RPM_BANDS)), observed=True)
        .size()
        .reset_index(name="seconds")
    )
    rows: list[dict[str, Any]] = [
        {"rpm_band": str(band), "seconds": int(seconds)}
        for band, seconds in zip(
            bands.iloc[:, 0].astype(str), bands["seconds"].astype(int), strict=True
        )
    ]

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
    return int(len(wot)), paired, rows


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
    wot_seconds, paired, bands = _wot_inventory(
        session_dir,
        rpm_spec=rpm_provider if isinstance(rpm_provider, str) else None,
        did_continuous=did_continuous,
    )
    if did_continuous and not any(
        tag.get("label") in {"tuning_pull_start", "wot_pull"}
        for tag in load_tags(session_dir / "tags.jsonl")
    ):
        notes.append(
            "pull inventory unavailable: all-DID captures require a tagged physical pull "
            "(`tuning_pull_start` or `wot_pull`); this is not evidence of zero pulls."
        )
    if wot_seconds and not paired:
        notes.append(
            "WOT seconds were captured but none carry both a measured and a commanded AFR "
            "in the same second, so delivery error cannot be computed from this drive."
        )

    # An all-DID report measures exactly the semantic providers that were
    # actually present; hard-coded Mode-01 names here previously made a DID
    # capture look slower or emptier than it was.  Retain the legacy list only
    # for the older Mode-01 capture profile, which has no DID registry roles.
    rate_specs = sorted(
        {
            str(role["provider"])
            for role in role_capabilities.values()
            if role.get("available", False) and isinstance(role.get("provider"), str)
        }
    )
    if not rate_specs and not did_continuous:
        rate_specs = ["obd:RPM", "obd:O2_WR_LAMBDA_S1", "obd:TIMING_ADVANCE"]
    achieved = _achieved_hz(session_dir, rate_specs)
    rate_summary = _rate_summary(session_dir, rate_specs)
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
        wot_seconds=wot_seconds,
        wot_with_afr_pair=paired,
        wot_by_band=bands,
        achieved_hz=achieved,
        pull_inventory=pull_inventory,
        rate_summary=rate_summary,
        measured_afr=measured_afr,
        target_map=target_map,
        delivery=delivery,
        cam=cam,
        previous=previous,
        capabilities=capabilities,
        notes=[*notes, f"tune revision source: {revision_source}"],
    )


__all__ = ["TuningDigest", "compute_tuning_digest"]
