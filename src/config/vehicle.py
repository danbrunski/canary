"""The ``vehicle:`` section: identity and the physical parameters derived
channels need (R21).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from src.config.base import _Strict


class VehicleParameters(_Strict):
    """Physical parameters needed for derived trip/fuel/performance channels
    (Task 6.6). All optional with defaults so existing ``canary.yaml`` files
    keep loading; ``VehicleConfig.model_dump()`` carries them into every
    session manifest, so a trip's calculations stay reproducible from the
    captured data alone.
    """

    #: Curb/test mass in kg — used for acceleration-derived power sanity.
    mass_kg: float = 1900.0
    #: How ``mass_kg`` was arrived at (a scale ticket, or an itemised estimate
    #: with its range), and what it includes. Occupants, cargo and fuel below
    #: full are *test conditions*, not vehicle facts: the dyno records them
    #: per run (H07).
    mass_evidence: str | None = None
    #: Mass of one road wheel with its tire, kg: rotating mass, which the
    #: dyno's inertia term needs separately from the total (H07.6).
    wheel_tire_assembly_kg: float | None = Field(default=None, gt=0.0)
    #: The operator's usual load — driver and cargo — in kg. A default for a
    #: run's conditions; a run records what was actually aboard.
    usual_payload_kg: float | None = Field(default=None, ge=0.0)
    #: ``gasoline`` (E0) | ``e10`` | ``diesel`` | ``e85`` — selects the fuel's
    #: stoichiometric AFR, density, CO2 factor and heating value
    #: (``src.signals.derived.FUEL_PROPERTIES``). Most US pump gasoline is E10.
    #: Not the ECM's AFR display scale: that belongs to the ECM (M01.3(a)).
    fuel_type: Literal["gasoline", "e10", "diesel", "e85"] = "gasoline"
    #: Engine displacement in litres (volumetric-efficiency fuel estimate).
    displacement_l: float = 4.0
    #: Volumetric efficiency (0–1.2) for the speed-density fallback when no
    #: MAF / fuel-rate PID is available.
    volumetric_efficiency: float = 0.85
    #: Driveline loss fraction (0–1) between crank and wheels for the
    #: estimated-power channel.
    drivetrain_loss_fraction: float = 0.15
    #: Brake thermal efficiency (0–1) for the fuel-energy power estimate.
    thermal_efficiency: float = 0.30
    #: Rolling tire diameter in metres (optional; speed cross-checks only).
    tire_diameter_m: float | None = None
    #: Driveline, as three separate unitless/SI facts (H07.1). They replace
    #: ``gear_ratios``, which was documented as gearbox × final drive but
    #: compared as rpm per km/h, with an example in the wrong units for a
    #: six-speed. Expected rpm per km/h is *computed* from these
    #: (:meth:`rpm_per_kph`), so the two readings cannot be confused again.
    #: Axle (final-drive) ratio.
    final_drive: float | None = Field(default=None, gt=0.0)
    #: Gearbox ratio of every forward gear, first gear first (e.g. 3.79 for
    #: first). All of them or none: a gear estimate against a partial table
    #: would name the wrong gear whenever the driver is in a missing one.
    gearbox_ratios: tuple[float, ...] = ()
    #: Loaded rolling circumference of the driven tires, metres. Needs no
    #: evidence: a tire swap must save on its own (operator, 2026-11-07), and
    #: each drive measures it from GPS anyway.
    tire_loaded_circumference_m: float | None = Field(default=None, gt=0.0)
    #: Where the gearing (``final_drive``, ``gearbox_ratios``) comes from.
    #: Required once either is set.
    driveline_evidence: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _retired_gear_ratios(cls, data: Any) -> Any:
        if isinstance(data, dict) and "gear_ratios" in data:
            data = dict(data)
            if data.pop("gear_ratios"):
                raise ValueError(
                    "vehicle parameter gear_ratios is retired (it mixed up gearbox x final "
                    "drive with rpm per km/h): use final_drive, gearbox_ratios (one "
                    "unitless ratio per gear) and tire_loaded_circumference_m"
                )
        return data

    @model_validator(mode="before")
    @classmethod
    def _british_tire_key(cls, data: Any) -> Any:
        """Read the pre-2026-10-11 spelling ``tyre_loaded_circumference_m``.

        Registries, machine overlays and every session manifest written before
        the US-English rename carry it; they load as the new field. Both at
        once is refused: one would silently win.
        """
        old = "tyre_loaded_circumference_m"
        if isinstance(data, dict) and old in data:
            data = dict(data)
            value = data.pop(old)
            if "tire_loaded_circumference_m" in data:
                raise ValueError(
                    f"vehicle parameters give both {old} (the old spelling) and "
                    "tire_loaded_circumference_m; keep tire_loaded_circumference_m"
                )
            data["tire_loaded_circumference_m"] = value
        return data

    @model_validator(mode="after")
    def _driveline_has_evidence(self) -> VehicleParameters:
        if (self.final_drive or self.gearbox_ratios) and not self.driveline_evidence:
            raise ValueError(
                "final_drive and gearbox_ratios need driveline_evidence saying how they "
                "were arrived at"
            )
        if any(ratio <= 0 for ratio in self.gearbox_ratios):
            raise ValueError("gearbox_ratios must be positive")
        return self

    def rpm_per_kph(self) -> tuple[float, ...]:
        """Engine rpm per km/h of true road speed in each gear, or ``()``
        unless the gearbox, final drive and tire are all known."""
        if not (self.gearbox_ratios and self.final_drive and self.tire_loaded_circumference_m):
            return ()
        wheel_rpm_per_kph = 1000.0 / 60.0 / self.tire_loaded_circumference_m
        return tuple(g * self.final_drive * wheel_rpm_per_kph for g in self.gearbox_ratios)


class VehicleConfig(_Strict):
    make: str
    model: str
    year: int
    vin: str = ""
    parameters: VehicleParameters = VehicleParameters()
