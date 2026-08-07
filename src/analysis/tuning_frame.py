"""Vehicle-scoped semantic inputs for all-DID tuning analysis."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.analysis.afr import AfrCalibration, voltage_to_afr
from src.analysis.channels import ChannelSpecError, read_channels
from src.control.capture_manifest import read_capture_manifest

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
]


class Provider(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role
    spec: str
    transform: Literal["identity", "voltage_afr"] = "identity"
    confidence: Literal["confirmed", "candidate"] = "confirmed"


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


class ProviderRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    providers: list[Provider] = Field(default_factory=list)
    #: Optional: a vehicle with no measured stop cannot infer WOT, and must
    #: say so rather than fall back to a guess.
    wot_criterion: WotCriterion | None = None


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
) -> TuningFrame:
    """Resolve semantic roles without embedding vehicle DID names in callers."""
    registry = load_provider_registry(providers_path)
    manifest = read_capture_manifest(session_dir)
    parts: list[pd.DataFrame] = []
    capability: dict[str, dict[str, str | bool | None]] = {}
    for provider in registry.providers:
        role = provider.role
        if provider.confidence != "confirmed":
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
                session_dir, [provider.spec], acquired_time=True, calibration=calibration
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
        capability[role] = {"available": True, "provider": provider.spec, "reason": None}
        parts.append(values)
    frame = pd.DataFrame(columns=["timestamp"])
    for part in parts:
        frame = part if frame.empty else pd.merge(frame, part, on="timestamp", how="outer")
    if not frame.empty:
        frame = frame.sort_values("timestamp", kind="stable").reset_index(drop=True)
    return TuningFrame(
        values=frame,
        capability=capability,
        capture_profile=None if manifest is None else manifest.profile,
        tune_revision=None if manifest is None else manifest.tune_revision,
        wot_criterion=registry.wot_criterion,
    )


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
