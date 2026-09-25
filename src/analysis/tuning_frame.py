"""Vehicle-scoped semantic inputs for all-DID tuning analysis."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.analysis.afr import AfrCalibration, voltage_to_afr
from src.analysis.channels import ChannelSpecError, read_channels
from src.control.capture_manifest import read_capture_manifest
from src.paths import DIDS_YAML

Role = Literal[
    "rpm",
    "operator_demand",
    "airflow_or_table_load",
    "measured_afr_b1",
    "measured_afr_b2",
    "commanded_afr",
    "fueling_active",
    "injector_pw_b1",
    "injector_pw_b2",
    "coolant_temp",
    "ignition_timing",
    "cam_command",
    "cam_measured",
    "cam_measured_b",
    #: Nissan Base Fuel Schedule (Tp), ms — the load axis of the fuel-target
    #: map (``TargetAfrMap.load_axis_name``). See tuning_providers.yaml for the
    #: provider and the envelope it holds in.
    "base_fuel_schedule",
    #: Whether the ECU is commanding fuel at all, as a boolean. Deliberately
    #: separate from the mixture target so fuel cut is not inferred from a
    #: magic AFR value (Mode 01's overloaded ``FUEL_STATUS == 4``).
    "fueling_active",
]


class Provider(BaseModel):
    """One semantic role bound to one channel spec.

    ``transform: scale`` multiplies a raw register into engineering units. A
    scale is a measured number, so the schema makes it carry what a number
    needs: ``factor`` and ``unit`` always, and — while the provider is only a
    ``candidate`` — the operating envelope it was derived in (``conditions``)
    and where the derivation lives (``evidence``). A bare candidate factor is
    the shape of every retracted finding in this project (AGENTS.md, "a
    measured constraint belongs in a schema, not a comment"), so it is
    refused at load rather than discouraged in a comment.
    """

    model_config = ConfigDict(extra="forbid")

    role: Role
    spec: str
    transform: Literal["identity", "voltage_afr", "scale", "equiv_ratio_afr", "target_present"] = (
        "identity"
    )
    confidence: Literal["confirmed", "candidate"] = "confirmed"
    #: Multiplier for ``transform: scale`` (engineering = raw * factor).
    factor: float | None = None
    #: Engineering unit produced by ``transform: scale``.
    unit: str | None = None
    #: Operating envelope the provider holds in, and outside which it must
    #: not be used.
    conditions: str | None = None
    #: Where the derivation is recorded (report paths, session names).
    evidence: str | None = None

    @model_validator(mode="after")
    def _scale_carries_its_provenance(self) -> Provider:
        if self.transform == "scale":
            if self.factor is None or not self.factor > 0:
                raise ValueError(
                    f"{self.role}: transform 'scale' requires a positive factor (got {self.factor})"
                )
            if not self.unit:
                raise ValueError(f"{self.role}: transform 'scale' requires a unit")
            if self.confidence == "candidate":
                missing = [name for name in ("conditions", "evidence") if not getattr(self, name)]
                if missing:
                    raise ValueError(
                        f"{self.role}: a candidate scale provider must state "
                        f"{' and '.join(missing)} — a candidate factor without its "
                        "envelope and derivation invites being quoted outside them"
                    )
        if self.transform == "equiv_ratio_afr" and self.unit:
            raise ValueError(
                f"{self.role}: transform 'equiv_ratio_afr' always produces AFR; "
                "a unit here could only disagree with what it returns"
            )
        if self.transform != "scale" and self.factor is not None:
            raise ValueError(
                f"{self.role}: factor is only meaningful with transform 'scale' "
                f"(transform is {self.transform!r})"
            )
        return self


class WotCriterion(BaseModel):
    """How to recognise wide-open throttle without a load PID or a pull tag.

    Every value here is vehicle-specific — a throttle's mechanical stop is a
    property of the vehicle, not of the platform — so this lives in the
    per-vehicle provider registry and never in code. The fields are required
    rather than defaulted for the same reason: a wrong-but-plausible default
    threshold would silently relabel part throttle as WOT.
    """

    model_config = ConfigDict(extra="forbid")

    role: Role = "operator_demand"
    unit: str
    closed_value: float
    wide_open_value: float
    threshold_value: float
    reached_stop_tolerance: float
    rpm_floor: float

    @model_validator(mode="after")
    def _ordered(self) -> WotCriterion:
        if not self.closed_value < self.threshold_value <= self.wide_open_value:
            raise ValueError(
                "wot_criterion requires closed_value < threshold_value <= wide_open_value "
                f"(got {self.closed_value}, {self.threshold_value}, {self.wide_open_value})"
            )
        return self


class LoadWotCriterion(BaseModel):
    """How a Mode 01 capture recognises WOT, and the rpm bands reports use.

    Vehicle facts (R05.13): the load floor is where this engine's calculated
    load sits at wide-open throttle, the rpm floor where a pull is worth
    counting, and the band edges end at the engine's redline. All required —
    a plausible default would silently relabel another vehicle's part throttle
    as WOT, or drop its top band.
    """

    model_config = ConfigDict(extra="forbid")

    load_floor_pct: float
    rpm_floor: float
    #: Band edges delivery error and WOT seconds are binned into, ascending;
    #: the last edge is the redline.
    rpm_bands: tuple[float, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def _ascending(self) -> LoadWotCriterion:
        if any(b <= a for a, b in zip(self.rpm_bands, self.rpm_bands[1:], strict=False)):
            raise ValueError(f"mode01_wot.rpm_bands must be strictly ascending: {self.rpm_bands}")
        return self


class ProviderRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    providers: list[Provider] = Field(default_factory=list)
    #: Optional: a vehicle with no measured stop cannot infer WOT, and must
    #: say so rather than fall back to a guess.
    wot_criterion: WotCriterion | None = None
    #: Why this vehicle's Mode-22-continuous captures carry no Mode 01
    #: channels (on the dev vehicle's ECM the two services are mutually
    #: exclusive). A vehicle fact, quoted by the "inputs were never captured"
    #: refusal (R05.11) instead of a sentence baked into shared code.
    mode01_exclusion_note: str | None = None
    #: WOT recognition and rpm bands for Mode 01 captures (R05.13).
    mode01_wot: LoadWotCriterion | None = None


@dataclass(frozen=True)
class TuningFrame:
    values: pd.DataFrame
    capability: dict[str, dict[str, str | bool | None]]
    capture_profile: str | None
    tune_revision: str | None
    #: Carried from the registry so callers never re-parse it, and so a
    #: vehicle without a measured throttle stop reads as ``None`` rather than
    #: silently inheriting another vehicle's threshold.
    wot_criterion: WotCriterion | None = None


def load_provider_registry(path: Path) -> ProviderRegistry:
    return ProviderRegistry.model_validate(yaml.safe_load(path.read_text()))


def build_tuning_frame(
    session_dir: Path,
    *,
    providers_path: Path,
    calibration: AfrCalibration | None = None,
    allow_candidates: bool = False,
    afr_stoich: float,
    dids_path: Path = DIDS_YAML,
) -> TuningFrame:
    """Resolve semantic roles without embedding vehicle DID names in callers.

    ``candidate`` providers are skipped (reported unavailable) unless the
    caller opts in with ``allow_candidates=True``. Automatic analysis never
    does; the opt-in is for an operator deliberately working inside a
    candidate's stated envelope, and the capability entry then carries the
    provider's ``confidence``, ``conditions`` and ``evidence`` so the result
    cannot travel without them.
    """
    registry = load_provider_registry(providers_path)
    manifest = read_capture_manifest(session_dir)
    parts: list[pd.DataFrame] = []
    capability: dict[str, dict[str, str | bool | None]] = {}
    fuel_cut_declared = False
    for provider in registry.providers:
        role = provider.role
        if provider.confidence != "confirmed" and not allow_candidates:
            capability[role] = {
                "available": False,
                "provider": provider.spec,
                "reason": "candidate provider is not eligible for automatic analysis",
            }
            continue
        try:
            # Pass the calibration: derived specs such as `obd:COMMANDED_AFR`
            # need it to resolve at all, and without it an absent role reports
            # "needs an AFR calibration" — pointing at a config problem when
            # the real cause is that the capture profile disabled Mode 01. A
            # misleading reason on an unavailable role is worse than none.
            series = read_channels(
                session_dir,
                [provider.spec],
                acquired_time=True,
                calibration=calibration,
                afr_stoich=afr_stoich,
            )
        except ChannelSpecError as exc:
            capability[role] = {"available": False, "provider": provider.spec, "reason": str(exc)}
            continue
        if series.empty:
            capability[role] = {
                "available": False,
                "provider": provider.spec,
                "reason": "no samples",
            }
            continue
        values = series.loc[:, ["timestamp", "value"]].rename(columns={"value": role})
        if provider.transform == "voltage_afr":
            transfer = None if calibration is None else calibration.voltage_transfer
            if transfer is None:
                capability[role] = {
                    "available": False,
                    "provider": provider.spec,
                    "reason": "voltage-to-AFR transfer is unavailable",
                }
                continue
            # Preserve the instrument reading and make censoring a first-class
            # column. Replacing volts with AFR alone made it impossible for a
            # report to distinguish a real rich/lean sample from the transfer
            # boundary (or to audit the conversion later).
            values[f"{role}_raw_voltage"] = values[role]
            values[role] = voltage_to_afr(values[role], transfer)
            values[f"{role}_censored"] = values[role].le(transfer.censored_floor_afr) | values[
                role
            ].ge(transfer.censored_ceiling_afr)
        elif provider.transform == "equiv_ratio_afr":
            # phi is fuel/air, so AFR = stoich / phi. No convention flag is
            # consulted here, unlike Mode 01 PID 0x44: this channel's
            # direction is pinned by its own known-answer chain (word 0x4A80
            # -> phi 1.1640625 -> 12.63 AFR, reproducing the PID 0x44 value
            # observed at DID 0x1134 = 74). See data/dids.yaml.
            #
            # Fuel cut does not appear here at all. The ECU commands no
            # target then, and the collector records that on the companion
            # `_SENTINEL` channel rather than as a number, so the hand
            # filtering the 2026-08-04 K-step analysis needed (`TARGET AFR <
            # 13.5`, to keep closed-loop samples from diluting the result)
            # is no longer load-bearing for excluding cut samples.
            phi = pd.to_numeric(values[role], errors="coerce")
            values[f"{role}_equiv_ratio"] = phi
            values[role] = afr_stoich / phi.where(phi > 0)
        elif provider.transform == "target_present":
            # True wherever a target was commanded at all. This is not a
            # threshold on the value: fuel cut produces no row on this
            # channel at all (it is recorded as a `_SENTINEL` state), so cut
            # samples are excluded by construction and a gate built on this
            # column reports zero drops because there was nothing to drop.
            # That is the honest reading, and it is why a separate
            # fueling-active role can be sourced from the target channel
            # without circularity.
            values[role] = values[role].notna()
        elif provider.transform == "scale":
            assert provider.factor is not None  # guaranteed by Provider's validator
            values[f"{role}_raw"] = values[role]
            values[role] = pd.to_numeric(values[role], errors="coerce") * provider.factor
        capability[role] = {"available": True, "provider": provider.spec, "reason": None}
        if role == "commanded_afr" and provider.transform == "equiv_ratio_afr":
            cut = _fuel_cut_rows(session_dir, provider.spec, dids_path=dids_path)
            if cut is not None:
                fuel_cut_declared = True
                if not cut.empty:
                    parts.append(cut)
                capability[role]["fuel_cut_provider"] = f"{provider.spec}_SENTINEL"
        if provider.confidence != "confirmed":
            capability[role].update(
                {
                    "confidence": provider.confidence,
                    "conditions": provider.conditions,
                    "evidence": provider.evidence,
                }
            )
        parts.append(values)
    frame = pd.DataFrame(columns=["timestamp"])
    for part in parts:
        frame = part if frame.empty else pd.merge(frame, part, on="timestamp", how="outer")
    if not frame.empty:
        frame = frame.sort_values("timestamp", kind="stable").reset_index(drop=True)
        if fuel_cut_declared and "fuel_cut" not in frame:
            # Declared and never observed: this drive had no cut. NaN (no cut
            # observation at that instant) matches the observed-cut case.
            frame["fuel_cut"] = pd.Series(np.nan, index=frame.index, dtype=object)
    return TuningFrame(
        values=frame,
        capability=capability,
        capture_profile=None if manifest is None else manifest.profile,
        tune_revision=None if manifest is None else manifest.tune_revision,
        wot_criterion=registry.wot_criterion,
    )


def _fuel_cut_rows(session_dir: Path, target_spec: str, *, dids_path: Path) -> pd.DataFrame | None:
    """A ``fuel_cut`` column from the target word's declared sentinel, if it has one.

    The RAM target records fuel cut on a companion ``_SENTINEL`` channel and
    produces no numeric row at all, so a frame built from the target alone
    lets every cut sample vanish before a gate can count it — an empty result
    that cannot say which kind of empty it is (B06.2). This emits ``True`` at
    each sentinel observation so every consumer of the frame gets the column
    without having to remember to ask for it. (Delivery error reads the
    sentinel itself since R05.16: :func:`src.analysis.tuning.
    read_delivery_inputs` counts it as the ``fuel_cut`` state.) ``None`` when
    the target is not a memory channel with a declared sentinel.
    """
    source, _, name = target_spec.partition(":")
    if source != "mem" or not dids_path.exists():
        return None
    from src.decode.dids import load_did_map

    entry = next((m for m in load_did_map(dids_path).memory if m.name == name), None)
    if entry is None or entry.sentinel_raw is None:
        return None
    try:
        rows = read_channels(
            session_dir, [f"{target_spec}_SENTINEL"], acquired_time=True, dids_path=dids_path
        )
    except ChannelSpecError:
        return None
    # True at each cut observation only; other instants are NaN ("no cut
    # observed here"), so a grid cell's ``last()`` keeps a cut seen anywhere
    # in it. An empty frame means declared-but-never-observed: the caller
    # still adds the column, so "no cut on this drive" is distinguishable
    # from "cut was not observable".
    return pd.DataFrame({"timestamp": rows["timestamp"], "fuel_cut": True})


def section_capabilities(
    capability: dict[str, dict[str, str | bool | None]],
) -> dict[str, dict[str, object]]:
    """State the exact evidence available for each tuning conclusion."""
    required: dict[str, tuple[Role, ...]] = {
        "pull_inventory": ("rpm", "operator_demand"),
        "measured_afr": ("rpm", "measured_afr_b1"),
        "delivery_error": ("rpm", "measured_afr_b1", "commanded_afr", "fueling_active"),
        "cam_tracking": ("cam_command", "cam_measured"),
    }
    out: dict[str, dict[str, object]] = {}
    for section, roles in required.items():
        missing = [role for role in roles if not capability.get(role, {}).get("available", False)]
        out[section] = {"available": not missing, "missing_roles": missing}
    return out


__all__ = [
    "Provider",
    "ProviderRegistry",
    "Role",
    "TuningFrame",
    "build_tuning_frame",
    "load_provider_registry",
    "section_capabilities",
]
