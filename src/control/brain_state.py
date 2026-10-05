"""Where the second brain keeps its state, and what survives a restart (E04.6).

Everything the brain writes lives in one per-vehicle directory beside the
memory ledger (:func:`brain_dir`: ``control/<slug>/brain/`` under
``state_dir`` on the Pi), so it is never in the checkout and never under the
session root that retention, replay and training discovery walk.

What a restart keeps and what it cannot:

* **Memory reopens.** The ledger is the append-only file; reopening re-reads it
  (``src.evidence.ledger``). Jobs reopen from their own log
  (:mod:`src.control.brain_jobs`).
* **Program and calibration identity are compared, not assumed.** The last
  identity the brain ran under is persisted; on reopen it is compared with what
  the profile and the flash history say now (:func:`reconcile_identity`):

  - a different *program* (another program hash, or an image the application
    model does not list) invalidates program-specific twin state — results
    computed under the old program stop being current — and asks for the RAM
    channels to be re-verified, because a different program is a different
    memory map;
  - a *calibration-only* change selects the new calibration's tables and
    invalidates predictions made for the old one, and says explicitly that RAM
    addresses did **not** move: a flash rewrites calibration tables, not the
    program's memory map (AGENTS.md, "RAM channels").

* **Transient ECU state is unknown after a restart, and says so.** Nothing the
  ECM held between two reads (hysteresis state, a map switch in progress) can
  be reconstructed from disk; :func:`recovery_report` names it ``unknown``
  rather than resuming as if it were known.

The identity is derived from tracked profile files and the flash history; this
module writes only its own record of what it last saw.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from src.analysis.ecu_application import (
    ApplicationModel,
    load_application_model,
    program_sha256,
)
from src.analysis.rom_image import sha256_hex
from src.atomic_write import atomic_write_text
from src.config import CanaryConfig
from src.control.rom_library import RomLibrary, RomNotFoundError
from src.evidence.ledger import UnscopedMemoryError, memory_ledger_for
from src.jsonl_store import append_jsonl_line
from src.signals.afr import load_afr_calibration
from src.vehicle.profiles import active_registry, profile_dir

__all__ = [
    "BRAIN_DIRNAME",
    "IDENTITY_FILE",
    "IDENTITY_HISTORY_FILE",
    "EcuIdentity",
    "IdentityChange",
    "brain_dir",
    "classify_change",
    "current_identity",
    "image_program",
    "reconcile_identity",
    "recovery_report",
    "table_selection",
]

BRAIN_DIRNAME = "brain"
IDENTITY_FILE = "identity.json"
IDENTITY_HISTORY_FILE = "identity_history.jsonl"

#: ``known``: the image is the model's program (listed, or its program spans
#: hash to the model's identity). ``other_program``: the image's bytes are
#: available and hash to a different program. ``unverified``: an image hash is
#: recorded but neither listed nor available to hash. ``no_image`` /
#: ``no_revision`` / ``no_model``: nothing to identify it from.
ProgramStatus = Literal[
    "known", "other_program", "unverified", "no_model", "no_revision", "no_image"
]
#: Statuses whose ``program_sha256`` is the running program's actual hash.
_IDENTIFIED: frozenset[str] = frozenset({"known", "other_program"})
IdentityChange = Literal[
    "first_seen",
    "unchanged",
    "calibration_changed",
    "program_changed",
    "program_identified",
    "program_unknown",
]


def brain_dir(config: CanaryConfig) -> Path:
    """The vehicle's brain directory, beside its memory ledger.

    Refuses an unscoped config, as the ledger does: brain state is per vehicle.
    """
    if config.scoped_profile is None:
        raise UnscopedMemoryError(
            "the second brain is per vehicle: scope the config to a profile "
            "(src.vehicle.profiles.scope_config) before opening its state"
        )
    return config.daemon.memory_ledger.parent / BRAIN_DIRNAME


@dataclass(frozen=True)
class EcuIdentity:
    """Which ECM program and calibration the vehicle runs, as the profile and
    flash history say now. ``program_id`` is ``None`` unless the current image
    is one the application model's program was checked on."""

    program_id: str | None
    program_sha256: str | None
    program_status: ProgramStatus
    calibration: str | None
    image_sha256: str | None
    application_model_sha256: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _model(config: CanaryConfig) -> tuple[ApplicationModel, str] | None:
    try:
        return load_application_model(profile_dir(config))
    except FileNotFoundError:
        return None


def image_program(
    config: CanaryConfig, model: ApplicationModel, image_sha256: str
) -> tuple[ProgramStatus, str | None]:
    """The program an image runs: ``(status, program_sha256)``.

    Listed in the model: ``known``. Otherwise the image's bytes from the W08
    library are hashed over the model's program spans
    (:func:`~src.analysis.ecu_application.program_sha256`): equal is ``known``
    (a new calibration of the same program), different is ``other_program``.
    Without the bytes it is ``unverified`` — never assumed either way."""
    program = model.program
    if program.image(image_sha256) is not None:
        return "known", program.identity.sha256
    library = RomLibrary(
        config.daemon.rom_library_dir,
        profile_dir=profile_dir(config),
        registry=lambda: active_registry(config),
    )
    try:
        image = library.image(image_sha256)
    except RomNotFoundError:
        return "unverified", None
    if sha256_hex(image) != image_sha256:
        return "unverified", None  # the library file is not the bytes it is named for
    actual = program_sha256(image, model)
    return ("known" if actual == program.identity.sha256 else "other_program"), actual


def current_identity(config: CanaryConfig, *, now: datetime) -> EcuIdentity:
    """The identity in force at ``now`` (flash history + application model,
    hashing the library image when the model does not list it). Blocking."""
    registry = active_registry(config)
    revision = registry.at(now) if registry is not None else None
    model = _model(config)
    image = revision.image_sha256 if revision is not None else None
    calibration = revision.revision if revision is not None else None
    if model is None:
        return EcuIdentity(None, None, "no_model", calibration, image, None)
    application, sha = model
    program = application.program
    if revision is None:
        return EcuIdentity(None, None, "no_revision", None, None, sha)
    if image is None:
        return EcuIdentity(None, None, "no_image", calibration, None, sha)
    status, actual = image_program(config, application, image)
    program_id = program.id if status == "known" else None
    return EcuIdentity(program_id, actual, status, calibration, image, sha)


def classify_change(previous: dict[str, Any] | None, current: EcuIdentity) -> IdentityChange:
    """How ``current`` differs from the last identity the brain recorded.

    Programs are compared by their actual hash. ``program_unknown`` only when
    no image can identify the current program."""
    if previous is None:
        return "first_seen"
    before = (
        previous.get("program_sha256") if previous.get("program_status") in _IDENTIFIED else None
    )
    if current.program_status not in _IDENTIFIED:
        if (
            previous.get("program_status") == current.program_status
            and previous.get("image_sha256") == current.image_sha256
        ):
            return "unchanged"
        return "program_unknown"
    if before is None:
        # Unknown before, identified now: as the model's program nothing moved
        # that was ever verified; as another program it is a change.
        return "program_identified" if current.program_status == "known" else "program_changed"
    if before != current.program_sha256:
        return "program_changed"
    if (previous.get("calibration"), previous.get("image_sha256")) != (
        current.calibration,
        current.image_sha256,
    ):
        return "calibration_changed"
    return "unchanged"


def _read_identity(directory: Path) -> dict[str, Any] | None:
    path = directory / IDENTITY_FILE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def reconcile_identity(
    config: CanaryConfig, *, now: datetime | None = None, write: bool = True
) -> dict[str, Any]:
    """Compare the current identity with the recorded one; record a change.

    Returns the record as it stands after this call: ``identity``,
    ``program_since``/``calibration_since`` (when each last changed),
    ``last_change`` (kind, time, the identity before) and ``change`` (what this
    call found). ``write=False`` computes the same answer without recording it
    (the read routes): a reader never moves the brain's clock.
    """
    instant = now or datetime.now(UTC)
    directory = brain_dir(config)
    stored = _read_identity(directory)
    current = current_identity(config, now=instant)
    previous = None if stored is None else stored.get("identity")
    change = classify_change(previous, current)
    if change == "unchanged" and stored is not None:
        return {**stored, "change": "unchanged"}
    stamp = instant.isoformat()
    program_moved = change in (
        "first_seen",
        "program_changed",
        "program_identified",
        "program_unknown",
    )
    record: dict[str, Any] = {
        "identity": current.to_dict(),
        "program_since": stamp
        if program_moved or stored is None
        else stored.get("program_since", stamp),
        "calibration_since": stamp,
        "last_change": {"kind": change, "at": stamp, "previous": previous},
    }
    if write:
        directory.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            directory / IDENTITY_FILE, json.dumps(record, indent=2, sort_keys=True) + "\n"
        )
        append_jsonl_line(
            directory / IDENTITY_HISTORY_FILE,
            record["last_change"] | {"identity": record["identity"]},
        )
    return {**record, "change": change}


def table_selection(config: CanaryConfig, identity: EcuIdentity) -> dict[str, Any]:
    """Which table source the twin would read for the current calibration."""
    if identity.calibration is None or identity.image_sha256 is None:
        return {
            "source": None,
            "reason": "calibration_unknown",
            "detail": "the flash history names no image in force now",
        }
    image = config.daemon.rom_library_dir / f"{identity.image_sha256}.bin"
    if image.exists():
        return {"source": "rom_library", "revision": identity.calibration, "reason": None}
    try:
        calibration = load_afr_calibration(config.daemon.afr_calibration)
    except (FileNotFoundError, ValueError) as exc:
        return {"source": None, "reason": "table_missing", "detail": str(exc)}
    target = calibration.target_map(identity.calibration)
    if target is None or target.raw_values is None:
        return {
            "source": None,
            "reason": "table_missing",
            "detail": f"no library image and no raw target table for {identity.calibration!r}",
        }
    return {"source": "afr_calibration", "revision": identity.calibration, "reason": None}


def _ram_addresses(record: dict[str, Any]) -> dict[str, Any]:
    kind = record["last_change"]["kind"]
    status = record["identity"]["program_status"]
    if status == "other_program" or (status == "known" and kind == "program_changed"):
        return {
            "status": "reverify",
            "detail": (
                "a different program is a different memory map: re-verify the mem: "
                "channels (python -m tools.verify_memory_channels) before trusting them"
            ),
        }
    if status != "known":
        return {
            "status": "unverified",
            "detail": (
                f"program {status}: no image is available to hash, so the program (and so "
                "the memory map) is unknown. Add the image to the ROM library, or verify "
                "with python -m tools.verify_memory_channels before trusting mem: channels."
            ),
        }
    return {
        "status": "unchanged",
        "detail": (
            "the program is the decoded one; a calibration flash rewrites tables, not the "
            "program's RAM addresses"
        ),
    }


def recovery_report(
    config: CanaryConfig,
    *,
    now: datetime | None = None,
    process_started_at: datetime | None = None,
    write: bool = False,
) -> dict[str, Any]:
    """The brain's standing after (re)opening: identity, what a change
    invalidated, which tables are selected, memory, and the transient state it
    cannot know. One function serves the HTTP route, MCP and the scheduler."""
    instant = now or datetime.now(UTC)
    record = reconcile_identity(config, now=instant, write=write)
    identity = EcuIdentity(**record["identity"])
    ledger = memory_ledger_for(config)
    entries = ledger.entries() if ledger.path.exists() else []
    last = record["last_change"]
    return {
        "vehicle": config.scoped_profile,
        "identity": record["identity"],
        "change": record["change"],
        "last_change": last,
        "program_specific_state": {
            "valid_since": record["program_since"],
            "program_id": identity.program_id,
            "valid": identity.program_status == "known",
            "detail": (
                "twin results computed under another program are no longer current"
                if identity.program_status == "known"
                else f"program {identity.program_status}: no program-specific twin state is valid"
            ),
        },
        "predictions": {
            "calibration": identity.calibration,
            "valid_since": record["calibration_since"],
            "detail": "predictions for an earlier calibration were invalidated when it changed",
        },
        "tables": table_selection(config, identity),
        "ram_addresses": _ram_addresses(record),
        "transient": {
            "state": "unknown",
            "reason": "not_reconstructible",
            "since": None if process_started_at is None else process_started_at.isoformat(),
            "detail": (
                "transient ECU state (hysteresis, a map switch in progress) is not stored "
                "and cannot be rebuilt after a restart; the live twin's stream (E04.1) starts "
                "empty, and samples whose lookback reaches before its first read are "
                "unevaluable by name"
            ),
        },
        "memory": {
            "ledger": str(ledger.path),
            "records": len(entries),
            "current": sum(1 for e in entries if e.current),
        },
    }
