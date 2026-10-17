"""Every exception class defined under ``src/`` survives a pickle round trip.

The refusal classes carry ``(reason, message)`` (or another required
signature) and call ``super().__init__`` with one formatted string, so the
default pickle (``cls(*self.args)``) rebuilt them with one argument and
failed: a refusal raised in the workbench's spawn child broke its pool and
surfaced as an opaque 503 (E09.2c verifier, 2026-10-16). Each such class now
has a ``__reduce__`` from its constructor's arguments. This test imports every
``src`` module, constructs every exception class it defines and round-trips
it, so a new class that cannot cross a process boundary fails here.
"""

from __future__ import annotations

import importlib
import inspect
import pickle
import pkgutil
from pathlib import Path
from typing import Any

import pytest

import src

#: Modules that need an optional dependency absent from the environment (CI
#: has no torch): their classes are checked wherever the dependency exists.
_OPTIONAL = ("torch",)


def _exception_classes() -> list[type[BaseException]]:
    found: dict[str, type[BaseException]] = {}
    for info in pkgutil.walk_packages(src.__path__, "src."):
        try:
            module = importlib.import_module(info.name)
        except ModuleNotFoundError as exc:
            if exc.name and exc.name.split(".")[0] in _OPTIONAL:
                continue
            raise
        for name, obj in vars(module).items():
            if (
                inspect.isclass(obj)
                and issubclass(obj, BaseException)
                and obj.__module__ == module.__name__
            ):
                found[f"{obj.__module__}.{name}"] = obj
    return [found[k] for k in sorted(found)]


def _construct_typed() -> dict[str, Any]:
    """Arguments for the classes whose constructor needs more than strings."""
    from src.analysis.noise_floor import FloorRefusal
    from src.provisioning.improv import ImprovError

    return {
        "src.analysis.detector_evaluation_inputs.EvaluationRefusedError": (
            (["first", "second"],),
            {"exit_code": 3},
        ),
        "src.analysis.llm_assist.cards.EvidenceResolutionError": ((["a", "b"],), {}),
        "src.analysis.noise_floor.FloorRefusedError": (
            (FloorRefusal("obd:RPM", "no_floor", "no_floor_measured", "idle"),),
            {},
        ),
        "src.analysis.tuning.InputsNotCapturedError": (
            ("never read",),
            {"kind": "not_captured", "missing": ("obd:RPM",)},
        ),
        "src.collection.kline.protocol.NegativeResponseError": ((0x22, 0x31), {}),
        "src.collection.uprev_session.UpRevNegativeResponseError": (
            (0x22, 0x31, b"\x7f\x22\x31"),
            {},
        ),
        "src.control.brain_jobs.BrainJobRefusedError": ((409, {"message": "busy"}), {}),
        "src.control.brain_reads.BrainReadError": ((503, {"message": "down"}), {}),
        "src.control.profile_capture.ProfileCaptureError": ((409, {"message": "busy"}), {}),
        "src.control.workbench_jobs.WorkbenchRefusedError": ((409, {"message": "busy"}), {}),
        "src.models.sequence_autoencoder_training.CalibrationCoverageError": ((["v"],), {}),
        "src.provisioning.improv.ImprovPacketError": (
            (ImprovError.INVALID_RPC_PACKET, "bad packet"),
            {},
        ),
        "src.signals.pairing.InputNotCapturedError": (("never read",), {"spec": "obd:RPM"}),
        "src.storage.broadcast_storage.ChangeOnlyStorageError": (
            ("change-only",),
            {"reader": "reader", "sessions": ["s1", "s2"]},
        ),
        "src.vehicle.tune_revisions.RegistryConflictError": (
            ("rev", Path("a.yaml"), Path("b.yaml"), ("image_sha256",)),
            {},
        ),
    }


def _instance(cls: type[BaseException], typed: dict[str, Any]) -> BaseException:
    key = f"{cls.__module__}.{cls.__name__}"
    if key in typed:
        args, kwargs = typed[key]
        return cls(*args, **kwargs)
    params = list(inspect.signature(cls.__init__).parameters.values())[1:]
    required = [
        p
        for p in params
        if p.default is p.empty and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    keywords = [p for p in params if p.default is p.empty and p.kind is p.KEYWORD_ONLY]
    args = [f"{p.name}_value" for p in required]
    if not args and any(p.kind is p.VAR_POSITIONAL for p in params):
        args = ["message_value"]  # a plain ``Exception(*args)`` constructor: one message
    try:
        return cls(*args, **{p.name: f"{p.name}_value" for p in keywords})
    except Exception as exc:  # pragma: no cover - names the class to add
        pytest.fail(f"{key}: cannot build it from strings ({exc!r}); add it to _construct_typed")


CLASSES = _exception_classes()


def test_the_enumeration_reaches_the_refusal_classes() -> None:
    names = {c.__name__ for c in CLASSES}
    assert {
        "FuelPlantRefusalError",
        "ApplicationModelRefusalError",
        "SimulationRefusalError",
        "TwinRefusalError",
        "TwinRunnerRefusalError",
        "RomEffectRefusalError",
    } <= names
    assert len(CLASSES) > 100


@pytest.mark.parametrize("cls", CLASSES, ids=lambda c: f"{c.__module__}.{c.__name__}")
def test_every_exception_class_round_trips_through_pickle(cls: type[BaseException]) -> None:
    exc = _instance(cls, _construct_typed())
    back = pickle.loads(pickle.dumps(exc))
    assert type(back) is cls
    assert back.args == exc.args
    assert str(back) == str(exc)
    assert getattr(back, "reason", None) == getattr(exc, "reason", None)
    assert vars(back) == vars(exc)
