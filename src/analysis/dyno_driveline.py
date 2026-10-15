"""The driveline model: stored inertia facts and the gear/inertia arithmetic (H07.6).

:class:`InertiaData` is the ``inertia`` section of a vehicle's ``dyno.yaml``
(estimates, until a two-gear measurement, :class:`EngineInertiaRecord`,
replaces the engine side); :class:`Driveline` turns it and the vehicle
profile's facts into ratios, effective mass and the ``inertia: estimated`` /
``measured_engine`` label every result carries. The one inertia convention is
:data:`INERTIA_CONVENTION`.

Depends only on :mod:`src.analysis.dyno_contract` (and the vehicle parameters
type, for annotation). Moved verbatim out of ``dyno.py`` (R16).
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import Field, model_validator

from src.analysis.dyno_contract import DynoRefusal, StrictModel

if TYPE_CHECKING:
    from src.config import VehicleParameters

InertiaFlag = Literal["estimated", "measured_engine"]


class InertiaConditions(StrictModel):
    road: str = Field(min_length=1)
    date: dt.date
    sessions: list[str] = Field(min_length=1)
    payload_kg: float = Field(ge=0.0)
    tune_revision: str = Field(min_length=1)


#: The one inertia convention (H07.6). The crank, flywheel and clutch sit
#: upstream of the gearbox, so the torque that accelerates them never reaches
#: the lossy gears: the wheel sees ``η·N/r·(T − I_e·α)``. Two gears measure the
#: product ``η·I_e``; the physical ``I_e`` is that divided by the assumed ``η``.
INERTIA_CONVENTION = (
    "engine inertia upstream of torque-proportional driveline losses: wheel force = "
    "eta*N/r*(T - I_e*alpha); the two-gear fit measures eta*I_e (reflected_kg_m2) and "
    "engine_kg_m2 = reflected_kg_m2 / driveline_efficiency; eta equal in both gears"
)


class EngineInertiaRecord(StrictModel):
    """A two-gear measurement of the engine-side inertia (H07.6)."""

    #: What the fit measures: ``η·I_e``, the engine inertia as the wheels see it.
    reflected_kg_m2: float = Field(gt=0.0)
    reflected_kg_m2_se: float = Field(ge=0.0)
    #: The ``η`` assumed to turn it into a crank inertia (1 − the profile's
    #: ``drivetrain_loss_fraction`` when measured).
    driveline_efficiency: float = Field(gt=0.0, le=1.0)
    #: Physical crank-side inertia, ``reflected_kg_m2 / driveline_efficiency``.
    engine_kg_m2: float = Field(gt=0.0)
    engine_kg_m2_se: float = Field(ge=0.0)
    gears: tuple[int, int]
    rpm_min: float
    rpm_max: float
    n_bins: int = Field(ge=2)
    imu_pitch_offset_deg: float
    #: The IMU frame of record the pulls and the offset were resolved in.
    imu_reference_id: str | None = None
    squat_corrected: bool
    #: A different η in each gear breaks the fit: a 1 % difference moves the
    #: result by roughly 1 % of torque over ``I·Δα``.
    #: Must equal :data:`INERTIA_CONVENTION`; a record in another convention is
    #: refused, not reinterpreted.
    assumption: str = INERTIA_CONVENTION
    conditions: InertiaConditions

    @model_validator(mode="after")
    def _one_convention(self) -> EngineInertiaRecord:
        if self.assumption != INERTIA_CONVENTION:
            raise ValueError(
                f"inertia record in another convention ({self.assumption!r}); this module "
                f"uses: {INERTIA_CONVENTION}"
            )
        expected = self.reflected_kg_m2 / self.driveline_efficiency
        if not math.isclose(self.engine_kg_m2, expected, rel_tol=1e-6):
            raise ValueError(
                f"engine_kg_m2 {self.engine_kg_m2} is not reflected_kg_m2 / "
                f"driveline_efficiency = {expected}: {INERTIA_CONVENTION}"
            )
        return self


class InertiaData(StrictModel):
    """Rotating-inertia facts for one vehicle, as stored in ``dyno.yaml``.

    Wheel-side inertia is built from the profile's measured
    ``wheel_tire_assembly_kg`` and the tire radius, through a stated shape
    factor; the engine side (crank, flywheel, clutch, gearbox input) is an
    estimate until :attr:`measured_engine` holds a two-gear measurement.
    """

    #: Road wheels that turn with the vehicle (the spare does not).
    road_wheels: int = Field(ge=2)
    #: One wheel and tire's inertia as a fraction of ``m·r²`` (a tire's mass
    #: sits near the rim, so 0.6-0.75; a solid disc is 0.5).
    wheel_shape_factor: float = Field(gt=0.0, le=1.0)
    #: Propshaft, differentials, axle shafts, hubs and rotors, referred to the
    #: road wheels (kg·m² at wheel speed).
    driveline_output_kg_m2: float = Field(ge=0.0)
    #: Crank, flywheel, clutch and gearbox input shaft at engine speed (kg·m²),
    #: an estimate of the *physical* inertia, upstream of driveline losses
    #: (:data:`INERTIA_CONVENTION`); the wheels see ``η`` times it.
    engine_side_kg_m2: float = Field(gt=0.0)
    evidence: str = Field(min_length=1)
    measured_engine: EngineInertiaRecord | None = None


@dataclass(frozen=True)
class Driveline:
    """Everything the dyno needs to turn engine speed into wheel speed and back."""

    tire_radius_m: float
    final_drive: float
    gearbox_ratios: tuple[float, ...]
    #: All road wheels plus output driveline, at wheel speed (kg·m²).
    wheel_side_kg_m2: float
    #: Engine side at engine speed (kg·m²), physical: upstream of the losses.
    engine_side_kg_m2: float
    #: ``η`` = 1 − the profile's ``drivetrain_loss_fraction``: torque-proportional
    #: losses between the crank and the wheels (:data:`INERTIA_CONVENTION`).
    efficiency: float
    inertia: InertiaFlag

    @classmethod
    def from_vehicle(
        cls, params: VehicleParameters, inertia: InertiaData | None
    ) -> Driveline | DynoRefusal:
        """Build from profile facts, or refuse naming every fact that is missing."""
        missing = [
            label
            for label, value in (
                ("final_drive", params.final_drive),
                ("gearbox_ratios", params.gearbox_ratios),
                ("tire_loaded_circumference_m", params.tire_loaded_circumference_m),
                ("wheel_tire_assembly_kg", params.wheel_tire_assembly_kg),
                ("dyno.yaml inertia", inertia),
            )
            if not value
        ]
        if missing:
            return DynoRefusal(
                "driveline",
                "not_configured",
                "the vehicle profile lacks " + ", ".join(missing),
            )
        assert params.final_drive is not None
        assert params.tire_loaded_circumference_m is not None
        assert params.wheel_tire_assembly_kg is not None
        assert inertia is not None
        radius = params.tire_loaded_circumference_m / (2.0 * math.pi)
        wheel = inertia.wheel_shape_factor * params.wheel_tire_assembly_kg * radius**2
        efficiency = 1.0 - params.drivetrain_loss_fraction
        if not 0.0 < efficiency <= 1.0:
            return DynoRefusal(
                "driveline",
                "invalid_input",
                f"drivetrain_loss_fraction {params.drivetrain_loss_fraction} leaves no "
                "driveline efficiency in (0, 1]",
            )
        measured = inertia.measured_engine
        if measured is not None and not math.isclose(
            measured.driveline_efficiency, efficiency, rel_tol=1e-9
        ):
            return DynoRefusal(
                "driveline",
                "conditions_mismatch",
                f"the measured engine inertia assumed driveline efficiency "
                f"{measured.driveline_efficiency}; the profile now gives {efficiency:g} "
                "(1 - drivetrain_loss_fraction). The measurement is reflected_kg_m2 = "
                f"{measured.reflected_kg_m2:.4f}; re-derive engine_kg_m2 with the new value.",
            )
        return cls(
            tire_radius_m=radius,
            final_drive=params.final_drive,
            gearbox_ratios=tuple(params.gearbox_ratios),
            wheel_side_kg_m2=inertia.road_wheels * wheel + inertia.driveline_output_kg_m2,
            engine_side_kg_m2=(
                inertia.engine_side_kg_m2 if measured is None else measured.engine_kg_m2
            ),
            efficiency=efficiency,
            inertia="estimated" if measured is None else "measured_engine",
        )

    def overall_ratio(self, gear: int) -> float:
        """Gearbox × final drive for a 1-based gear number."""
        if not 1 <= gear <= len(self.gearbox_ratios):
            raise ValueError(f"gear {gear} is not one of 1..{len(self.gearbox_ratios)}")
        return self.gearbox_ratios[gear - 1] * self.final_drive

    def neutral_effective_mass(self, mass_kg: float) -> float:
        """``m_eff`` with the clutch in: only the wheel side turns with the road."""
        return mass_kg + self.wheel_side_kg_m2 / self.tire_radius_m**2

    def effective_mass(self, mass_kg: float, gear: int) -> float:
        """``m_eff`` in a gear, as the wheel force sees it.

        Wheel side plus ``η·I_e·N²/r²``: the engine's inertia torque is taken
        off at the crank, upstream of the losses (:data:`INERTIA_CONVENTION`).
        """
        ratio = self.overall_ratio(gear)
        return (
            self.neutral_effective_mass(mass_kg)
            + self.efficiency * self.engine_side_kg_m2 * ratio**2 / self.tire_radius_m**2
        )
