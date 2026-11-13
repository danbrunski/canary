"""Every exception class defined under ``src/`` survives a pickle round trip.

The refusal classes carry ``(reason, message)`` (or another required
signature) and call ``super().__init__`` with one formatted string, so the
default pickle (``cls(*self.args)``) rebuilt them with one argument and
failed: a refusal raised in the workbench's spawn child broke its pool and
surfaced as an opaque 503 (E09.2c verifier, 2026-10-16). Each such class now
has a ``__reduce__`` from its constructor's arguments. This test imports every
``src`` module, constructs every exception class it defines and round-trips
it, so a new class that cannot cross a process boundary fails here.

R24.1 / R29.3: the refusal classes now share two bases (``ReasonedRefusal``,
``StatusError``) whose ``__reduce__`` rebuilds from ``__new__`` and the
instance dict, so a subclass with a constructor signature of its own (an extra
keyword-only field) round-trips without writing one. The tests at the end
enumerate every ``src`` subclass of either base by reflection and hold each to
the round trip with its fields equal, and refuse a subclass that writes a
``__reduce__`` of its own.
"""

from __future__ import annotations

import copy
import importlib
import inspect
import pickle
import pkgutil
from pathlib import Path
from typing import Any

import pytest

import src
from src.control.status_error import StatusError
from src.refusal import ReasonedRefusal

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
        "src.analysis.refit._RecipeSkipError": (("no rows",), {"candidate": ("a", "candidate")}),
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
        "src.control.tuning_mode.TuningStorageSwitchError": ((500, "storage did not switch"), {}),
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


# -- the two refusal bases (R24.1, R29.3) ----------------------------------------


def _subclasses(base: type[BaseException]) -> list[type[BaseException]]:
    """Every subclass of ``base`` defined under ``src/``, found by reflection.

    ``CLASSES`` above has imported every ``src`` module, so the subclass tree is
    complete; a class defined anywhere else (this file's toys) is not a product class.
    """
    found: dict[str, type[BaseException]] = {}
    stack = list(base.__subclasses__())
    while stack:
        cls = stack.pop()
        stack.extend(cls.__subclasses__())
        if cls.__module__.startswith("src."):
            found[f"{cls.__module__}.{cls.__qualname__}"] = cls
    return [found[k] for k in sorted(found)]


REASONED = _subclasses(ReasonedRefusal)
STATUS = _subclasses(StatusError)


def test_the_bases_reach_the_classes_that_were_hand_written() -> None:
    reasoned = {c.__name__ for c in REASONED}
    assert {
        "AirChargeRefusalError",
        "ApplicationModelRefusalError",
        "AxisPairingRefusedError",
        "CommandChangeRefusalError",
        "ContextRefusalError",
        "EditSpecRefusalError",
        "FuelCompRomRefusalError",
        "FuelPlantRefusalError",
        "KnockOnRefusalError",
        "LoadAxisUnavailableError",
        "RestoreReproductionRefusalError",
        "RomEffectRefusalError",
        "SimDesignRefusalError",
        "SimulationRefusalError",
        "TimingLookupRefusalError",
        "TripEndDigestUnavailableError",
        "TwinRefusalError",
        "TwinRunnerRefusalError",
        "_RecipeSkipError",
    } <= reasoned
    assert {c.__name__ for c in STATUS} == {
        "BrainJobRefusedError",
        "BrainReadError",
        "ProfileCaptureError",
        "TuningStorageSwitchError",
        "WorkbenchRefusedError",
    }
    # Every one is in the whole-``src`` enumeration, so it is also round-tripped there.
    assert set(REASONED + STATUS) <= set(CLASSES)


@pytest.mark.parametrize("cls", REASONED + STATUS, ids=lambda c: f"{c.__module__}.{c.__qualname__}")
def test_a_refusal_subclass_has_no_pickle_rule_of_its_own(cls: type[BaseException]) -> None:
    """The base's ``__reduce__`` is the contract: a subclass that writes its own is
    a second copy of the rule this module exists to hold in one place."""
    base = ReasonedRefusal if issubclass(cls, ReasonedRefusal) else StatusError
    assert cls.__reduce__ is base.__reduce__, f"{cls.__qualname__} defines __reduce__"


@pytest.mark.parametrize("cls", REASONED + STATUS, ids=lambda c: f"{c.__module__}.{c.__qualname__}")
def test_a_refusal_subclass_keeps_its_fields_through_pickle_and_copy(
    cls: type[BaseException],
) -> None:
    exc = _instance(cls, _construct_typed())
    for back in (pickle.loads(pickle.dumps(exc)), copy.copy(exc), copy.deepcopy(exc)):
        assert type(back) is cls
        assert back.args == exc.args
        assert str(back) == str(exc)
        assert vars(back) == vars(exc)
        if isinstance(exc, ReasonedRefusal):
            assert isinstance(back, ReasonedRefusal)
            assert (back.reason, back.detail, back.message) == (exc.reason, exc.detail, exc.message)
        else:
            assert isinstance(exc, StatusError) and isinstance(back, StatusError)
            assert (back.status, back.detail) == (exc.status, exc.detail)


def test_a_reasoned_refusal_reads_as_reason_then_detail_and_keeps_both() -> None:
    exc = ReasonedRefusal("no_floor", "the floor was measured under WOT")
    assert str(exc) == "no_floor: the floor was measured under WOT"
    assert (exc.reason, exc.detail, exc.message) == (
        "no_floor",
        "the floor was measured under WOT",
        "the floor was measured under WOT",
    )
    back = pickle.loads(pickle.dumps(exc))
    assert (back.reason, back.detail, str(back)) == (exc.reason, exc.detail, str(exc))


def test_the_str_of_each_hand_written_class_is_what_it_was() -> None:
    """``str(exc)`` is read by logs, sidecars and tests: three classes never used
    the ``reason: detail`` form."""
    from src.analysis.refit import _RecipeSkipError
    from src.analysis.twin_runner import TwinRunnerRefusalError
    from src.control.trip_end import TripEndDigestUnavailableError
    from src.signals.afr import LoadAxisUnavailableError

    assert str(TwinRunnerRefusalError("code", "text")) == "code: text"
    assert str(TripEndDigestUnavailableError("code", "text")) == "text"
    assert str(LoadAxisUnavailableError("code text")) == "code text"
    assert str(_RecipeSkipError("code text")) == "code text"


class _ToyRefusal(ReasonedRefusal):
    """A subclass whose constructor is not ``(reason, detail)``: the trap."""

    def __init__(self, code: str, text: str, *, extra: object, also: int = 3) -> None:
        super().__init__(code, text)
        self.extra = extra
        self.also = also


class _ToyStatus(StatusError[dict[str, object]]):
    def __init__(self, detail: dict[str, object], *, status: int = 418, hint: str) -> None:
        super().__init__(status, detail)
        self.hint = hint


def test_a_subclass_with_a_constructor_of_its_own_round_trips_without_a_reduce() -> None:
    refusal = _ToyRefusal("code", "text", extra=("a", 1), also=9)
    back = pickle.loads(pickle.dumps(refusal))
    assert type(back) is _ToyRefusal
    assert (back.reason, back.detail, back.extra, back.also) == ("code", "text", ("a", 1), 9)
    assert str(back) == "code: text"
    status = _ToyStatus({"message": "m"}, status=409, hint="h")
    back_status = pickle.loads(pickle.dumps(status))
    assert type(back_status) is _ToyStatus
    assert (back_status.status, back_status.detail, back_status.hint) == (
        409,
        {"message": "m"},
        "h",
    )
    assert str(back_status) == "m"
