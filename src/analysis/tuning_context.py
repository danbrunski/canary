"""The vehicle's tuning context, resolved once from a scoped config (R05.1).

Every tuning reader — the trip-end digest, the workbench child, the MCP tuning
tools, ``scripts/tuning_report.py``, the ROM-maps route — used to assemble its
own copy of these facts, and they drifted:

- four of them read ``<profile>/tune_revisions.yaml`` directly, the *source*
  registry, while ``append_flash`` writes the *operational* one, so a flash
  recorded from the dashboard was invisible to the digest's and MCP's revision
  resolution;
- the report CLI loaded the noise floor from beside the session store instead
  of the profile directory;
- stoichiometry was a literal ``14.7`` default in a dozen signatures that no
  caller overrode, while the serve path read it from the configured fuel.

:meth:`TuningContext.from_config` is the one place those are resolved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.analysis.afr import AfrCalibration, load_afr_calibration
from src.analysis.derived import fuel_properties
from src.analysis.lambda_delivery import RegimeFloor, load_lambda_delivery
from src.analysis.noise_floor import NoiseFloor, load_noise_floor
from src.analysis.tune_revisions import TuneRevisionRegistry, load_tune_revisions
from src.analysis.tuning_frame import LoadWotCriterion, ProviderRegistry, load_provider_registry
from src.config import CanaryConfig
from src.profiles import active_registry_path, profile_dir


@dataclass(frozen=True, kw_only=True)
class TuningContext:
    """Vehicle facts every tuning analysis needs, from profile data only.

    Plain data so it pickles into the analysis child. ``afr_stoich`` has no
    default on purpose: it comes from the configured fuel
    (:func:`src.analysis.derived.fuel_properties`), and a gasoline literal is
    wrong for every other profile.
    """

    afr_stoich: float
    calibration: AfrCalibration | None = None
    providers: ProviderRegistry | None = None
    providers_path: Path | None = None
    registry: TuneRevisionRegistry | None = None
    noise_floor: NoiseFloor | None = None
    #: ``preprocessing.operating_modes["idle"]`` as a dict (rpm_min, rpm_max,
    #: speed_max …), or ``None`` when this vehicle's config defines no idle.
    idle_spec: dict[str, float | None] | None = None
    #: The vehicle's ``lambda_delivery.yaml`` declaration, when it has one.
    #: A path, not the loaded model: loading cross-checks the DID catalog.
    lambda_delivery_path: Path | None = None
    #: Which measured noise floor each fuelling regime may quote, and under
    #: what conditions (the declaration's ``noise_floors``). The only source a
    #: tuning comparison takes its ``conditions`` from (R05.2): a regime with
    #: no entry gets no significance verdict.
    regime_floors: dict[str, RegimeFloor] = field(default_factory=dict)
    #: The profile-data directory these were read from.
    profile_data: Path | None = None

    @property
    def mode01_wot(self) -> LoadWotCriterion | None:
        """Mode 01 WOT floors and rpm bands (profile data, R05.13)."""
        return None if self.providers is None else self.providers.mode01_wot

    @property
    def rpm_bands(self) -> tuple[float, ...] | None:
        """The vehicle's rpm band edges; the top edge is its redline."""
        wot = self.mode01_wot
        return None if wot is None else wot.rpm_bands

    def resolve_min_load(self, min_load: float | None) -> float:
        """The load floor for a delivery-error call: the caller's, else the profile's.

        ``None`` means "this vehicle's WOT floor" (``mode01_wot.load_floor_pct``,
        operator decision 2026-09-25 for the MCP tool and the CLI), so no
        caller restates the dev vehicle's 85 %. Refuses when the profile
        declares no ``mode01_wot`` rather than guessing a number.
        """
        if min_load is not None:
            return min_load
        wot = self.mode01_wot
        if wot is None:
            raise ValueError(
                "no min_load given and no mode01_wot.load_floor_pct in this vehicle's "
                "tuning_providers.yaml"
            )
        return wot.load_floor_pct

    @property
    def mode01_exclusion_note(self) -> str | None:
        """Why this vehicle's all-DID captures lack Mode 01 (profile data)."""
        return None if self.providers is None else self.providers.mode01_exclusion_note

    @classmethod
    def from_config(cls, config: CanaryConfig) -> TuningContext:
        return cls(**context_fields(config))


def context_fields(config: CanaryConfig) -> dict[str, Any]:
    """:class:`TuningContext`'s fields for ``config`` — shared with subclasses."""
    data = profile_dir(config)
    calibration_path = config.daemon.afr_calibration
    providers_path = data / "tuning_providers.yaml"
    lambda_path = data / "lambda_delivery.yaml"
    idle = config.preprocessing.operating_modes.get("idle")
    declaration = load_lambda_delivery(lambda_path) if lambda_path.exists() else None
    return {
        "afr_stoich": fuel_properties(config.vehicle.parameters).afr_stoich,
        "calibration": (
            load_afr_calibration(calibration_path) if calibration_path.exists() else None
        ),
        "providers": load_provider_registry(providers_path) if providers_path.exists() else None,
        "providers_path": providers_path if providers_path.exists() else None,
        "registry": load_tune_revisions(active_registry_path(config)),
        "noise_floor": load_noise_floor(data / "noise_floor.yaml"),
        "idle_spec": (
            None
            if idle is None
            else {"rpm_min": idle.rpm_min, "rpm_max": idle.rpm_max, "speed_max": idle.speed_max}
        ),
        "lambda_delivery_path": lambda_path if lambda_path.exists() else None,
        "regime_floors": {} if declaration is None else dict(declaration.noise_floors),
        "profile_data": data,
    }


__all__ = ["TuningContext", "context_fields"]
