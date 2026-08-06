"""Vehicle-scoped semantic inputs for all-DID tuning analysis."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field

from src.analysis.afr import AfrCalibration, voltage_to_afr
from src.analysis.channels import ChannelSpecError, read_channels
from src.control.capture_manifest import read_capture_manifest

Role = Literal[
    "rpm",
    "operator_demand",
    "measured_afr_b1",
    "measured_afr_b2",
    "injector_pw_b1",
    "coolant_temp",
    "cam_command",
    "cam_measured",
]


class Provider(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role
    spec: str
    transform: Literal["identity", "voltage_afr"] = "identity"
    confidence: Literal["confirmed", "candidate"] = "confirmed"


class ProviderRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    providers: list[Provider] = Field(default_factory=list)


@dataclass(frozen=True)
class TuningFrame:
    values: pd.DataFrame
    capability: dict[str, dict[str, str | bool | None]]
    capture_profile: str | None
    tune_revision: str | None


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
            series = read_channels(session_dir, [provider.spec], acquired_time=True)
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
            values[role] = voltage_to_afr(values[role], transfer)
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
    )


__all__ = [
    "Provider",
    "ProviderRegistry",
    "Role",
    "TuningFrame",
    "build_tuning_frame",
    "load_provider_registry",
]
