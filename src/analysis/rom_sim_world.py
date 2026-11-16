"""What ``tools/rom_sim.py`` loads and compares, as library code (R30.5).

The simulator CLI used to carry its own loader (``_World``: the kit, the
slot, the base and the candidate), its own ``compare_roms`` call and trace,
and its own copies of the slot source and the learned state, beside the
ROM Editor's (:mod:`src.analysis.rom_editor`). A new kit field or a change
to how a candidate is traced had to be made twice. The tool now keeps
argparse, its argument refusals (the ones that name a flag) and its "never
write inside the checkout" policy; everything it loads and compares is
here, built from the pieces the editor uses:

* :meth:`SimWorld.load` -- the simulator's declared kit (``sim_kit.yaml``,
  :func:`~src.analysis.sim_montecarlo.load_sim_kit`, never the contract's
  default) with that plant's uncertainty declaration, the slot the profile
  declares (``fuel_comp_fit.yaml``'s ``map_slot``) and the
  ``tune_revisions.yaml`` resolver.
* :meth:`SimWorld.load_base` and :meth:`SimWorld.candidate` -- the base
  and the candidate (an E08.1 edit spec or an already edited image), traced
  through :func:`~src.analysis.rom_editor.spec_candidate` and
  :func:`~src.analysis.rom_editor.file_candidate`, the editor's own.
* :meth:`SimWorld.compare` and :meth:`SimWorld.trace` --
  :func:`~src.analysis.rom_editor.compare_images` and the reach it reports.
* :meth:`SimWorld.template` -- the paired scenario,
  :func:`~src.analysis.rom_editor.scenario_template`, the editor's too.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.analysis.fuel_comp_inputs import load_fuel_comp_declaration
from src.analysis.fuel_comp_rom import RevisionImageResolver
from src.analysis.paired_scenarios import LearnedState, PairedScenario, SimKit
from src.analysis.rom_edit_spec import BaseImage, BaseRef, EditSpec, load_edit_spec, resolve_base
from src.analysis.rom_editor import (
    compare_images,
    file_candidate,
    scenario_template,
    spec_candidate,
)
from src.analysis.rom_effects import Candidate, EffectReport, RomEffectRefusalError
from src.analysis.sim_contract import SimulationRefusalError
from src.analysis.sim_montecarlo import MODE, LoadedUncertainty, load_sim_kit
from src.vehicle.tune_revisions import load_tune_revisions


def load_spec(path: Path) -> EditSpec:
    """An E08.1 edit spec file, refused by name (``edit_spec_not_found``) when
    it is not a file."""
    if not Path(path).is_file():
        raise RomEffectRefusalError("edit_spec_not_found", f"{path}")
    return load_edit_spec(path)


@dataclass(frozen=True)
class SimWorld:
    """The kit, the slot and the revision resolver a run loads once."""

    profile: Path
    kit: SimKit
    uncertainty: LoadedUncertainty
    #: The slot the profile's ``fuel_comp_fit.yaml`` declares the ECM runs.
    slot: int
    resolver: RevisionImageResolver

    @classmethod
    def load(cls, profile: Path, *, plant_version: str | None, rom_dirs: list[Path]) -> SimWorld:
        """The simulator's own declared plant (``sim_kit.yaml``, E08.11) with
        that plant's uncertainty declaration, the declared slot, and the
        resolver over ``rom_dirs``. A profile with no ``tune_revisions.yaml``
        is refused (``no_tune_registry``)."""
        loaded = load_sim_kit(profile, plant_version)
        declaration = load_fuel_comp_declaration(profile)
        slot = declaration.program.map_slot
        registry = load_tune_revisions(profile / "tune_revisions.yaml")
        if registry is None:
            raise SimulationRefusalError(
                "no_tune_registry", f"{profile} has no tune_revisions.yaml"
            )
        resolver = RevisionImageResolver(
            registry, loaded.kit.rom_map, declaration.rom, rom_dirs=rom_dirs, slot=slot
        )
        return cls(
            profile=profile,
            kit=loaded.kit,
            uncertainty=loaded.uncertainty,
            slot=slot,
            resolver=resolver,
        )

    def load_base(self, ref: BaseRef, *, spec_dir: Path) -> BaseImage:
        """``ref`` as a base image: a revision through the resolver, a file
        relative to ``spec_dir``."""
        return resolve_base(ref, self.kit.rom_map, spec_dir=spec_dir, resolver=self.resolver)

    def candidate(
        self, base: BaseImage, *, spec: EditSpec | None, candidate_rom: Path | None
    ) -> Candidate:
        """The candidate on ``base``: the edit spec's, else the edited image's,
        traced on the declared slot."""
        if spec is not None:
            return spec_candidate(spec, self.kit, base, slot=self.slot)
        if candidate_rom is None:
            raise SimulationRefusalError(
                "candidate_unstated", "a candidate is an edit spec or an edited image"
            )
        return file_candidate(Path(candidate_rom).expanduser(), self.kit, base, slot=self.slot)

    def compare(self, base: BaseImage, candidate: Candidate, *, in_mode: bool) -> EffectReport:
        """:func:`~src.analysis.rom_editor.compare_images` on the declared slot:
        with ``in_mode`` under the run's contract mode, else the plain reach."""
        return compare_images(
            base.image, candidate.image, self.kit, slot=self.slot, in_mode=in_mode
        )

    def trace(self, base: BaseImage, candidate: Candidate, *, in_mode: bool) -> dict[str, Any]:
        """What :meth:`compare` says the candidate reaches: the outcome, the
        changed nodes and moved lookups, and per output its status, reasons,
        model status and path."""
        plain = self.compare(base, candidate, in_mode=in_mode).to_dict()
        return {
            "mode": MODE if in_mode else None,
            "outcome": plain["outcome"],
            "reason": plain["reason"],
            "changed_nodes": plain["changed_nodes"],
            "moved_lookups": plain["moved_lookups"],
            "outputs": [
                {
                    "output": o["output"],
                    "status": o["status"],
                    "reasons": o["reasons"],
                    "model_status": o["model_status"],
                    "path": o["path"],
                }
                for o in plain["outputs"]
            ],
        }

    def template(
        self,
        base: BaseImage,
        candidate: Candidate,
        *,
        learned: LearnedState,
        candidate_events: tuple[str, ...],
    ) -> PairedScenario:
        """The paired scenario minus its points, on the declared slot."""
        return scenario_template(
            self.kit,
            base.image_sha256,
            candidate.image_sha256,
            slot=self.slot,
            learned=learned,
            candidate_events=candidate_events,
        )


__all__ = ["SimWorld", "load_spec"]
