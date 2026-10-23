"""Back up the ECU's flash into the ROM library, read-only (W08.7).

The read recipe is vehicle data (``rom_backup.yaml``, the profile's own or its
platform's: :func:`src.vehicle.platforms.vehicle_data_file`): the request and
response ids, the DiagnosticSessionControl sequence (sub-functions sent once,
before each read, and after the read), the address range and block size, and
the pacing, with the capture that established them as provenance. The image is
the recipe's range; a ROM map is not needed to read one, since a backup is how
a vehicle without a map gets the image to build one from. This module holds no
vehicle constants. It sends only DiagnosticSessionControl
(0x10) and ReadMemoryByAddress (0x23), through
:meth:`src.collection.can_obd_client.CanObdClient.query_raw`, whose probe-only
allowlist cannot build a write.

**Refusals**, checked at start and re-checked during the read, each naming its
cause: ``vehicle_state`` other than ``ignition_only`` (a programming-type
session with the engine running is not something to find out about); no fresh
broadcast engine speed to watch while OBD polling is paused; passive armed;
another tester heard recently (K-line ``foreign_tester``, a ``foreign`` claim,
or a frame on the request ids on CAN during the read); the ECM session held by
anything else; a capture window or profile capture active (the read pauses all
polling for ~10 minutes, the reason ``scripts/restart_canary.sh`` refuses
too); another backup running. An engine start mid-read aborts it.

**A key-off mid-read.** While the job holds the ECM, ``vehicle_state`` reads
``passive``, so the re-check cannot see the key turned off. The ECM's own
broadcast frames can: the recipe's ``ecm_broadcast_ids`` are filtered onto
the read's bus, and a block that fails on silence says whether they went
quiet too ("the ECM went silent (key off?)"), were still arriving (powered,
not answering), or were never heard.

**Bus ownership.** The job claims the ECM session through
:func:`src.collection.tx_gate.claim_ecm_session` (holder ``rom_backup``), so
the OBD loop pauses Mode 01, Mode 22 and RAM polling exactly as it does for a
foreign tester, and transmits as that holder. Every exit path — success,
failure, cancel, an unexpected exception — sends the recipe's exit sequence
(when a session was entered), closes its bus, releases its private-traffic
hold and releases the claim, in a ``finally``. The claim carries a TTL renewed
every block, so a wedged reader cannot hold the ECM past it. Afterwards the
job waits (bounded) for an OBD observation newer than the release, as the
evidence that normal polling resumed.

**Telemetry.** The read's frames are excluded from raw capture while it runs
(:mod:`src.collection.private_traffic`); one start event and one end event,
with the summary, go to the control-event log.

**Integrity.** The finished image must pass the vehicle's checksums, carry the
calibration ID the ECM reports, and match a re-read of blocks spread across
the image. A check that ran and disagreed keeps the image, marked unverified
with the reason, and the job ends ``failed``. A check with nothing to check
against (no ROM map describes the image, so no checksums; the ECM never
reported its calibration ID) also keeps it unverified with the reason, but the
job ends ``done``: that is absence of evidence, not evidence against the read.
Without a map the calibration ID is looked for in the image as the ASCII the
ECM reports. An unverified image is never stored as good.

**Size.** The recipe's ``extent`` names vector tables the image's own program
holds (:mod:`src.analysis.rom_extent`). Once the read has their bytes it reads
them, offline, and stops with the reason when one points past the range: the
flash is larger than the recipe says, and the rest could only produce a short
image. The finished image is read again with its call targets; ``larger`` is a
failure, and without a map ``no_sign`` is still unchecked (a sign, not proof).
Nothing is ever read beyond the recipe's range.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import can
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.analysis.rom_extent import ExtentReading, ExtentSpec, early_extent, read_extent
from src.analysis.rom_image import (
    checksum_verdict,
    map_applies,
    read_calibration_id,
    sha256_hex,
)
from src.analysis.rom_map import RomMap
from src.collection import private_traffic, tx_gate
from src.collection.can_obd_client import CanObdClient, RawReply
from src.collection.isotp import DEFAULT_REQUEST_ID, nrc_name
from src.control.rom_library import RomLibrary
from src.operating import ENGINE_RUNNING_MIN_RPM
from src.vehicle.platforms import VehicleDataFile

log = logging.getLogger(__name__)

#: File name of the recipe in a vehicle profile or platform directory.
ROM_BACKUP_FILE = "rom_backup.yaml"
#: The ECM-session holder name, and the private-traffic owner.
HOLDER: tx_gate.EcmSessionHolder = "rom_backup"
#: DiagnosticSessionControl and ReadMemoryByAddress.
SESSION_SERVICE = 0x10
MEMORY_READ_SERVICE = 0x23
#: The claim's TTL, renewed every block: a reader wedged in bus I/O cannot keep
#: the poll loop paused for longer than this.
CLAIM_TTL_SECONDS = 30.0
#: How often the read re-checks vehicle state and captures.
RECHECK_SECONDS = 0.5
#: A tester heard on K-line this recently refuses the start.
FOREIGN_TESTER_QUIET_SECONDS = 120.0
#: After claiming, listen this long for another tester on CAN before sending.
PRE_READ_LISTEN_SECONDS = 0.5
#: How long to wait for polling to resume after the read.
POLLING_RESUME_TIMEOUT_SECONDS = 60.0
#: On daemon shutdown, how long to wait for a cancelled read to send its exit
#: sequence and release the ECM. Bounded: an in-flight attempt plus the exit.
SHUTDOWN_JOIN_SECONDS = 5.0

BackupStateName = Literal["idle", "refused", "running", "done", "failed", "cancelled"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RecipeSession(_Strict):
    """DiagnosticSessionControl sub-functions, in order."""

    enter_once: list[int] = Field(default_factory=list)
    before_each_read: list[int] = Field(default_factory=list)
    #: Sent after the read, on every exit path that entered a session: back to
    #: the session CANary's normal polling expects.
    exit: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def _bytes(self) -> RecipeSession:
        for value in (*self.enter_once, *self.before_each_read, *self.exit):
            if not 0 <= value <= 0xFF:
                raise ValueError(f"session sub-function 0x{value:X} is not a byte")
        return self


class RecipeRead(_Strict):
    start: int = Field(ge=0)
    #: Inclusive.
    end: int = Field(ge=0)
    #: Bytes per ReadMemoryByAddress; the last read is shorter when the range
    #: is not a multiple.
    block_size: int = Field(ge=1, le=0xFFE)

    @model_validator(mode="after")
    def _range(self) -> RecipeRead:
        if self.end < self.start or self.end > 0xFFFFFFFF:
            raise ValueError("read range must run start..end within 32-bit addresses")
        return self

    @property
    def size(self) -> int:
        """Bytes in the image: the range, which starts at 0
        (:func:`load_backup_recipe`)."""
        return self.end + 1 - self.start

    def blocks(self) -> list[tuple[int, int]]:
        """``(address, length)`` for every read, in address order."""
        return [
            (address, min(self.block_size, self.end + 1 - address))
            for address in range(self.start, self.end + 1, self.block_size)
        ]


class RecipePacing(_Strict):
    min_cycle_seconds: float = Field(ge=0)
    request_timeout_seconds: float = Field(gt=0, le=10)
    retries_per_block: int = Field(ge=0, le=10)


class RecipeVerify(_Strict):
    reread_blocks: int = Field(ge=0)


class RecipeProvenance(_Strict):
    capture: str = Field(min_length=1)
    evidence: str = Field(min_length=1)


class BackupRecipe(_Strict):
    schema_version: Literal[1]
    request_id: int = Field(ge=0, le=0x7FF)
    response_id: int = Field(ge=0, le=0x7FF)
    session: RecipeSession
    read: RecipeRead
    pacing: RecipePacing
    verify: RecipeVerify
    provenance: RecipeProvenance
    #: Frames the ECM broadcasts by itself whenever it is powered, at least
    #: one of them faster than ``pacing.request_timeout_seconds``. Empty: a
    #: timeout is reported without saying whether the ECM went silent.
    ecm_broadcast_ids: list[int] = Field(default_factory=list)
    #: What the image says about its own size, read offline from the bytes
    #: (:mod:`src.analysis.rom_extent`). ``None``: the size goes unchecked.
    extent: ExtentSpec | None = None

    @model_validator(mode="after")
    def _broadcast_ids(self) -> BackupRecipe:
        diagnostic = {self.request_id, self.response_id, DEFAULT_REQUEST_ID}
        for value in self.ecm_broadcast_ids:
            if not 0 <= value <= 0x7FF:
                raise ValueError(f"ecm_broadcast_ids 0x{value:X} is not an 11-bit CAN id")
            if value in diagnostic:
                raise ValueError(
                    f"ecm_broadcast_ids 0x{value:03X} is a diagnostic id; list frames the ECM "
                    "broadcasts by itself"
                )
        return self


def load_backup_recipe(path: Path, rom_map: RomMap | None) -> BackupRecipe:
    """The recipe at ``path``, refused when it would not produce a whole image.

    The range must start at 0, so that image offsets are flash addresses. With
    a map it must also cover the map's image exactly; without one, the range is
    the image.
    """
    recipe = BackupRecipe.model_validate(yaml.safe_load(path.read_text()))
    span = recipe.read.size
    if rom_map is not None and (recipe.read.start != 0 or span != rom_map.image_size):
        raise ValueError(
            f"{ROM_BACKUP_FILE} reads 0x{recipe.read.start:06X}-0x{recipe.read.end:06X} "
            f"({span:,} bytes); the image is 0x000000-0x{rom_map.image_size - 1:06X}"
        )
    if recipe.read.start != 0:
        raise ValueError(
            f"{ROM_BACKUP_FILE} reads from 0x{recipe.read.start:06X}; an image starts at "
            "0x000000, so that its offsets are flash addresses"
        )
    extent = recipe.extent
    if extent is not None and extent.tables_end > span:
        raise ValueError(
            f"{ROM_BACKUP_FILE} extent: a vector table runs to 0x{extent.tables_end:06X}, past "
            f"the {span:,} bytes the recipe reads"
        )
    if extent is not None and extent.ram_from <= recipe.read.end:
        raise ValueError(
            f"{ROM_BACKUP_FILE} extent: ram_from 0x{extent.ram_from:08X} is inside the read "
            f"range, which ends at 0x{recipe.read.end:06X}"
        )
    return recipe


def recipe_extent(recipe_file: Callable[[], VehicleDataFile]) -> ExtentSpec | None:
    """The vehicle recipe's ``extent``, or ``None`` when no recipe loads.

    For the library, which reads every image's extent: a missing or broken
    recipe is the backup card's refusal to name, not a reason a listing fails.
    """
    try:
        return load_backup_recipe(recipe_file().path, None).extent
    except (FileNotFoundError, ValueError, yaml.YAMLError):
        return None


@dataclass(frozen=True)
class VehicleReading:
    """What the job needs from the vehicle-state derivation, adapted by the caller.

    ``broadcast_rpm`` is the only engine-start evidence while OBD polling is
    paused for the read: fresh broadcast engine speed, or ``None`` when it is
    absent or stale (``watch_unavailable`` then says why, when known).
    ``obd_freshness_seconds`` is the age of the newest OBD observation; a
    value younger than the time since release proves polling resumed.
    """

    state: str
    rpm: float | None = None
    broadcast_rpm: float | None = None
    obd_freshness_seconds: float | None = None
    watch_unavailable: str | None = None


class BackupRefusedError(RuntimeError):
    """A start refused by a precondition; the message names every cause."""


class _AbortError(Exception):
    """End the read as failed, with a reason."""


class _CancelledError(Exception):
    pass


@dataclass(frozen=True)
class _Failure:
    """One failed request: what to report, and whether the ECM said nothing."""

    cause: str
    #: Sent, and neither a reply nor a negative response came back in time.
    no_answer: bool


def _engine_running(reading: VehicleReading) -> str | None:
    """Why the engine counts as running, or ``None``.

    The classifier's verdict, **or** a fresh broadcast engine speed at or above
    the running threshold whatever the verdict says. The classifier prefers a
    fresh OBD rpm, and once OBD polling pauses for the read its last ``0`` stays
    "fresh" for up to ten seconds — exactly the window in which the session is
    entered — while the broadcast already reads 800.
    """
    if reading.broadcast_rpm is not None and reading.broadcast_rpm >= ENGINE_RUNNING_MIN_RPM:
        return (
            f"broadcast engine speed {reading.broadcast_rpm:.0f} rpm >= "
            f"{ENGINE_RUNNING_MIN_RPM:.0f} (vehicle_state {reading.state})"
        )
    if reading.state == "engine_running":
        rpm = "" if reading.rpm is None else f", rpm {reading.rpm:.0f}"
        return f"vehicle_state engine_running{rpm}"
    return None


def _now_iso(wall: float) -> str:
    return datetime.fromtimestamp(wall, UTC).isoformat()


def _idle_state() -> dict[str, Any]:
    return {
        "state": "idle",
        "reason": None,
        "phase": None,
        "blocks_done": 0,
        "blocks_total": 0,
        "bytes": 0,
        "bytes_total": 0,
        "elapsed_s": 0.0,
        "eta_s": None,
        "retries": 0,
        "requests": 0,
        "timeouts": 0,
        "send_errors": 0,
        "negative_responses": {},
        "started_at": None,
        "ended_at": None,
        #: True once the image is in the library; ``sha256`` is set only then.
        "stored": False,
        "sha256": None,
        "verified": None,
        "integrity": None,
        "revision_match": None,
        "polling_resumed_s": None,
        "polling_resume_note": None,
    }


class RomBackupJob:
    """One ECU backup at a time, run on a worker thread.

    Everything that touches the daemon is injected, so the job runs against a
    fake ECU on a virtual bus in tests:

    * ``recipe_file`` finds the recipe (profile, then platform), raising
      ``FileNotFoundError`` that names where it looked;
    * ``open_bus`` opens a bus handle of its own (SocketCAN serves several
      per interface, as ``/control/obd_request`` relies on);
    * ``vehicle`` returns a :class:`VehicleReading`;
    * ``capture_active`` names an active capture window or profile, or ``None``;
    * ``ecu_calibration_id`` is the id the ECM last reported, or ``None``;
    * ``publish`` records the start and end events.
    """

    def __init__(
        self,
        *,
        recipe_file: Callable[[], VehicleDataFile],
        library: RomLibrary,
        open_bus: Callable[[], can.BusABC],
        vehicle: Callable[[], VehicleReading],
        capture_active: Callable[[], str | None],
        ecu_calibration_id: Callable[[], str | None],
        obd_source: str = "canable",
        publish: Callable[[str, str, dict[str, Any]], None] | None = None,
        resume_timeout_seconds: float = POLLING_RESUME_TIMEOUT_SECONDS,
        pre_read_listen_seconds: float = PRE_READ_LISTEN_SECONDS,
        recheck_seconds: float = RECHECK_SECONDS,
    ) -> None:
        self._recipe_file = recipe_file
        self.library = library
        self._open_bus = open_bus
        self._vehicle = vehicle
        self._capture_active = capture_active
        self._ecu_calibration_id = ecu_calibration_id
        self._obd_source = obd_source
        self._publish = publish
        self._resume_timeout = resume_timeout_seconds
        self._listen_seconds = pre_read_listen_seconds
        self._recheck_seconds = recheck_seconds
        self._lock = threading.Lock()
        self._state: dict[str, Any] = _idle_state()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._shutting_down = threading.Event()
        self._foreign_can: int | None = None
        self._watched_ids: frozenset[int] = frozenset()
        self._ecm_ids: frozenset[int] = frozenset()
        self._ecm_heard_at: float | None = None
        self._started_mono = 0.0
        #: Where the running read's recipe came from, for its records.
        self._recipe_source = ROM_BACKUP_FILE

    # -- public -------------------------------------------------------------

    def running(self) -> bool:
        """A read in progress, or a finished one still releasing and waiting
        for polling to resume. ``state`` turns ``running`` under the lock in
        :meth:`start`, before the worker exists, so two starts cannot race."""
        thread = self._thread
        return self._state["state"] == "running" or (thread is not None and thread.is_alive())

    def state(self) -> dict[str, Any]:
        """The job's state; while not running, the live preconditions too."""
        with self._lock:
            payload = dict(self._state)
            payload["negative_responses"] = dict(self._state["negative_responses"])
        if payload["state"] != "running":
            checks = self.preconditions()
            payload["preconditions"] = checks
            blocked = [c["detail"] for c in checks if not c["ok"]]
            payload["can_start"] = not blocked
            payload["blocked_by"] = "; ".join(blocked) or None
        else:
            payload["preconditions"] = None
            payload["can_start"] = False
            payload["blocked_by"] = "a backup is running"
        return payload

    def _definitions(self) -> tuple[BackupRecipe, VehicleDataFile, RomMap | None]:
        """The recipe, where it came from, and the vehicle's map if it has one.

        ``FileNotFoundError``: no recipe. ``ValueError``: an invalid recipe,
        platform or map; a broken map is refused, only a missing one is fine.
        """
        source = self._recipe_file()
        rom_map = self.library.rom_map_or_none()
        return load_backup_recipe(source.path, rom_map), source, rom_map

    def preconditions(self) -> list[dict[str, Any]]:
        """Every start precondition, each ``{id, ok, detail}``; all are evaluated."""
        checks: list[dict[str, Any]] = []

        def check(name: str, problem: str | None, fine: str) -> None:
            checks.append({"id": name, "ok": problem is None, "detail": problem or fine})

        check(
            "backup",
            "another backup is running" if self.running() else None,
            "no backup running",
        )
        try:
            recipe, source, rom_map = self._definitions()
            unmapped = "" if rom_map is not None else "; no ROM map, so checksums go unchecked"
            check("recipe", None, f"{source}: {len(recipe.read.blocks()):,} reads{unmapped}")
        except FileNotFoundError as exc:
            check("recipe", f"no backup recipe for this vehicle: {exc}", "")
        except (ValueError, yaml.YAMLError) as exc:
            check("recipe", f"the backup recipe, its platform or the ROM map is invalid: {exc}", "")
        check(
            "obd_source",
            None
            if self._obd_source != "elm327"
            else (
                "collection.obd_source is elm327: its adapter polls the ECM on its own, and a "
                "session claim does not pause it"
            ),
            f"collection.obd_source {self._obd_source}",
        )
        try:
            reading = self._vehicle()
        except Exception as exc:  # noqa: BLE001 - reported as a refusal, never a 500
            reading = None
            check("vehicle_state", f"vehicle state could not be read: {exc}", "")
        if reading is not None:
            state_problem = _engine_running(reading)
            if state_problem is None and reading.state != "ignition_only":
                rpm = "" if reading.rpm is None else f", rpm {reading.rpm:.0f}"
                state_problem = (
                    f"vehicle_state is {reading.state}{rpm}; the read needs key on, engine off "
                    "(ignition_only)"
                )
            check("vehicle_state", state_problem, "ignition_only (key on, engine off)")
            check(
                "engine_watch",
                None
                if reading.broadcast_rpm is not None
                else (
                    "no fresh broadcast engine speed"
                    + (f" ({reading.watch_unavailable})" if reading.watch_unavailable else "")
                    + ": OBD polling pauses for the read, so an engine start could not be seen"
                ),
                "broadcast engine speed is fresh",
            )
        gate = tx_gate.snapshot()
        check(
            "passive",
            f"passive mode is armed (source {gate['source']})" if gate["enabled"] else None,
            "passive mode is off",
        )
        heard = gate.get("foreign_tester")
        foreign_problem = None
        if "foreign" in gate["ecm_session_claims"]:
            foreign_problem = "another tester holds the ECM session (foreign claim)"
        elif heard is not None and heard["seconds_since_heard"] < FOREIGN_TESTER_QUIET_SECONDS:
            foreign_problem = (
                f"another tester (K-line {heard['tester_address'] or '?'} -> "
                f"{heard['module_address'] or '?'}) was heard {heard['seconds_since_heard']:.0f} s "
                f"ago; wait until it has been quiet {FOREIGN_TESTER_QUIET_SECONDS:.0f} s"
            )
        check("foreign_tester", foreign_problem, "no other tester heard recently")
        others = sorted(h for h in gate["ecm_session_claims"] if h not in ("foreign", HOLDER))
        check(
            "ecm_session",
            f"the ECM session is held by {', '.join(others)}" if others else None,
            "the ECM session is free",
        )
        try:
            capture = self._capture_active()
        except Exception as exc:  # noqa: BLE001 - reported as a refusal
            capture = f"capture state could not be read: {exc}"
        check(
            "capture",
            None
            if capture is None
            else (f"{capture}; the read pauses all polling for about 10 minutes"),
            "no capture active",
        )
        return checks

    def start(self) -> dict[str, Any]:
        """Start the read, or raise :class:`BackupRefusedError` naming every cause."""
        with self._lock:
            if self._shutting_down.is_set():
                raise BackupRefusedError("the daemon is shutting down")
            if self.running():
                raise BackupRefusedError("another backup is running")
            checks = self.preconditions()
            failed = [c["detail"] for c in checks if not c["ok"]]
            if failed:
                reason = "; ".join(failed)
                self._state = {**_idle_state(), "state": "refused", "reason": reason}
                raise BackupRefusedError(reason)
            recipe, source, rom_map = self._definitions()
            self._recipe_source = str(source)
            blocks = recipe.read.blocks()
            wall = time.time()
            self._cancel.clear()
            self._foreign_can = None
            self._ecm_heard_at = None
            self._state = {
                **_idle_state(),
                "state": "running",
                "phase": "starting",
                "blocks_total": len(blocks),
                "bytes_total": recipe.read.size,
                "started_at": _now_iso(wall),
            }
            worker = threading.Thread(
                target=self._run,
                args=(recipe, rom_map, wall),
                name="rom-backup",
                daemon=True,
            )
            self._thread = worker
        self._emit(
            "rom_backup.start",
            "ok",
            {
                "blocks_total": len(blocks),
                "range": [f"0x{recipe.read.start:06X}", f"0x{recipe.read.end:06X}"],
                "block_size": recipe.read.block_size,
                "request_id": f"0x{recipe.request_id:03X}",
                "response_id": f"0x{recipe.response_id:03X}",
                "recipe": self._recipe_source,
                "rom_map": rom_map is not None,
            },
        )
        worker.start()
        return self.state()

    def cancel(self) -> dict[str, Any]:
        """Ask a running read to stop. ``BackupRefusedError`` when none runs."""
        with self._lock:
            reading = self._state["state"] == "running"
        if not reading:
            raise BackupRefusedError("no backup is running")
        self._cancel.set()
        return self.state()

    def shutdown(self, timeout: float = SHUTDOWN_JOIN_SECONDS) -> bool:
        """Daemon shutdown: cancel a running read and wait (bounded) for it to
        send the recipe's exit sequence, release the ECM and record its end
        event. The polling-resume wait is skipped: polling is ending too. No
        new read starts afterwards. True when no worker is left running."""
        self._shutting_down.set()
        self._cancel.set()
        return self.wait(timeout)

    def wait(self, timeout: float | None = None) -> bool:
        """Join the worker (tests). True when it has finished."""
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    # -- worker -------------------------------------------------------------

    def _update(self, **fields: Any) -> None:
        with self._lock:
            self._state.update(fields)

    def _emit(self, action: str, result: str, payload: dict[str, Any]) -> None:
        if self._publish is None:
            return
        try:
            self._publish(action, result, payload)
        except Exception:  # noqa: BLE001 - an event sink must not fail the read
            log.exception("could not publish %s", action)

    def _tap(self, message: can.Message) -> None:
        """Every frame the read's own bus receives: the ECM still broadcasting,
        or another tester on CAN?

        This socket never hears its own transmissions, and the daemon's poll
        loop sends nothing while the claim stands, so a frame on a request id
        is another tester.
        """
        arbitration_id = int(message.arbitration_id)
        if arbitration_id in self._ecm_ids:
            self._ecm_heard_at = time.monotonic()
        elif self._foreign_can is None and arbitration_id in self._watched_ids:
            self._foreign_can = arbitration_id

    def _midread_problem(self, *, check_vehicle: bool) -> str | None:
        if self._cancel.is_set():
            raise _CancelledError
        if self._foreign_can is not None:
            return (
                f"another tester transmitted on CAN (id 0x{self._foreign_can:03X}) during the read"
            )
        gate = tx_gate.snapshot()
        if gate["enabled"]:
            return f"passive mode was armed during the read (source {gate['source']})"
        others = sorted(h for h in gate["ecm_session_claims"] if h != HOLDER)
        if others:
            return f"the ECM session was taken by {', '.join(others)} during the read"
        heard = gate.get("foreign_tester")
        if (
            heard is not None
            and heard["seconds_since_heard"] < time.monotonic() - self._started_mono
        ):
            tester = heard["tester_address"] or "?"
            return f"another tester was heard on K-line ({tester}) during the read"
        if not check_vehicle:
            return None
        capture = self._capture_active()
        if capture is not None:
            return f"{capture} (started during the read)"
        reading = self._vehicle()
        running = _engine_running(reading)
        if running is not None:
            return f"the engine started during the read ({running})"
        if reading.state not in ("ignition_only", "passive"):
            return f"vehicle_state became {reading.state} during the read"
        if reading.broadcast_rpm is None:
            return (
                "lost the fresh broadcast engine speed during the read, so an engine start could "
                "not be seen"
            )
        return None

    def _run(self, recipe: BackupRecipe, rom_map: RomMap | None, started_wall: float) -> None:
        started = time.monotonic()
        self._started_mono = started
        counters: dict[str, Any] = {
            "requests": 0,
            "retries": 0,
            "timeouts": 0,
            "send_errors": 0,
            "nrcs": {},
        }
        image: bytearray | None = None
        result: BackupStateName = "failed"
        reason: str | None = None
        reread_mismatches: list[str] | None = None
        bus: can.BusABC | None = None
        entered = False
        frames_excluded = 0
        self._watched_ids = frozenset({recipe.request_id, DEFAULT_REQUEST_ID})
        self._ecm_ids = frozenset(recipe.ecm_broadcast_ids)
        try:
            tx_gate.claim_ecm_session(HOLDER, ttl_seconds=CLAIM_TTL_SECONDS)
            private_traffic.hold(HOLDER, {recipe.request_id, recipe.response_id})
            try:
                bus = self._open_bus()
            except (OSError, can.CanError) as exc:
                raise _AbortError(f"could not open the CAN bus: {exc}") from exc
            try:
                bus.set_filters(
                    [
                        {"can_id": i, "can_mask": 0x7FF, "extended": False}
                        for i in sorted({recipe.response_id, *self._watched_ids, *self._ecm_ids})
                    ]
                )
            except (NotImplementedError, can.CanError, OSError):
                log.debug("CAN filters unavailable on this interface; filtering in software")
            client = CanObdClient(
                bus,
                request_id=recipe.request_id,
                response_id_min=recipe.response_id,
                response_id_max=recipe.response_id,
                timeout_seconds=recipe.pacing.request_timeout_seconds,
                on_frame=self._tap,
                parse_error_owner="rom-backup",
                tx_holder=HOLDER,
            )
            self._update(phase="listening")
            self._listen(bus)
            problem = self._midread_problem(check_vehicle=True)
            if problem:
                raise _AbortError(problem)
            self._update(phase="entering session")
            entered = True
            for sub in recipe.session.enter_once:
                self._enter_session(client, recipe, sub, counters)
            self._update(phase="reading")
            image = self._read_all(client, recipe, counters, started)
            if recipe.verify.reread_blocks:
                self._update(phase="verifying")
                reread_mismatches = self._reread(client, recipe, image, counters)
            result = "done"
        except _CancelledError:
            result = "cancelled"
            reason = (
                "cancelled: the daemon is shutting down"
                if self._shutting_down.is_set()
                else "cancelled by the operator"
            )
            image = None
        except _AbortError as exc:
            result, reason = "failed", str(exc)
            image = None
        except Exception as exc:  # noqa: BLE001 - every exit path still releases the bus
            log.exception("ROM backup failed unexpectedly")
            result, reason = "failed", f"unexpected error: {type(exc).__name__}: {exc}"
            image = None
        finally:
            if bus is not None:
                if entered:
                    self._update(phase="exiting session")
                    self._exit_session(bus, recipe)
                try:
                    bus.shutdown()
                except Exception:  # noqa: BLE001 - the release below must still run
                    log.exception("closing the ROM backup bus failed")
            frames_excluded = private_traffic.release(HOLDER)
            tx_gate.release_ecm_session(HOLDER)
        released = time.monotonic()
        ended_wall = time.time()
        summary: dict[str, Any] = {
            "started_at": _now_iso(started_wall),
            "ended_at": _now_iso(ended_wall),
            "duration_s": round(released - started, 2),
            "requests": counters["requests"],
            "retries": counters["retries"],
            "timeouts": counters["timeouts"],
            "send_errors": counters["send_errors"],
            "negative_responses": dict(counters["nrcs"]),
            "frames_excluded": frames_excluded,
        }
        resumed: float | None = None
        note: str | None = None
        try:
            if result == "done" and image is not None:
                result, reason = self._store(
                    bytes(image), recipe, rom_map, summary, reread_mismatches or []
                )
        except Exception as exc:  # noqa: BLE001 - the job must end, never stay "running"
            log.exception("ROM backup: storing the image failed")
            result = "failed"
            reason = f"the read finished but storing it failed: {type(exc).__name__}: {exc}"
        finally:
            self._update(
                state=result,
                reason=reason,
                phase="waiting for polling to resume",
                ended_at=summary["ended_at"],
                elapsed_s=summary["duration_s"],
                eta_s=0.0 if result == "done" else None,
            )
            try:
                resumed, note = self._wait_for_polling(released)
            except Exception as exc:  # noqa: BLE001 - reported, never raised
                note = f"the polling-resume check failed: {type(exc).__name__}: {exc}"
            self._update(phase=None, polling_resumed_s=resumed, polling_resume_note=note)
            with self._lock:
                final = dict(self._state)
            self._emit(
                "rom_backup.end",
                result,
                {
                    **summary,
                    "state": result,
                    "reason": reason,
                    "stored": final["stored"],
                    "sha256": final["sha256"],
                    "verified": final["verified"],
                    "revision_match": final["revision_match"],
                    "blocks_done": final["blocks_done"],
                    "blocks_total": final["blocks_total"],
                    "polling_resumed_s": resumed,
                    "polling_resume_note": note,
                },
            )

    def _listen(self, bus: can.BusABC) -> None:
        """Before the first request: is another tester already talking on CAN?"""
        deadline = time.monotonic() + self._listen_seconds
        while time.monotonic() < deadline and self._foreign_can is None:
            try:
                message = bus.recv(timeout=min(0.05, max(0.0, deadline - time.monotonic())))
            except (ValueError, IndexError):
                continue
            if message is not None:
                self._tap(message)

    def _send(
        self,
        client: CanObdClient,
        recipe: BackupRecipe,
        service: int,
        body: bytes,
        counters: dict[str, Any],
    ) -> RawReply:
        reply = client.query_raw(service, body, request_id=recipe.request_id)
        if not reply.sent and reply.send_error is not None:
            # The bus refused the frame (a full TX queue, bus-off): a failed
            # attempt, retried like a timeout and named as what it is.
            counters["send_errors"] += 1
            self._update(send_errors=counters["send_errors"])
            return reply
        if not reply.sent:
            owner = tx_gate.ecm_session_owner()
            cause = (
                "passive mode is armed"
                if tx_gate.passive_armed()
                else f"the ECM session is held by {owner}"
            )
            raise _AbortError(f"transmit suppressed by the gate: {cause}")
        counters["requests"] += 1
        if reply.negative_response_code is not None:
            code = f"0x{reply.negative_response_code:02X}"
            counters["nrcs"][code] = counters["nrcs"].get(code, 0) + 1
        elif not reply.positive:
            counters["timeouts"] += 1
        self._update(
            requests=counters["requests"],
            timeouts=counters["timeouts"],
            negative_responses=dict(counters["nrcs"]),
        )
        return reply

    @staticmethod
    def _cause(what: str, reply: RawReply, timeout: float) -> _Failure:
        if reply.send_error is not None:
            return _Failure(f"{what}: CAN send failed ({reply.send_error})", no_answer=False)
        if reply.negative_response_code is not None:
            code = reply.negative_response_code
            return _Failure(
                f"{what}: negative response 0x{code:02X} ({nrc_name(code)})", no_answer=False
            )
        return _Failure(f"{what}: no response within {timeout:g} s", no_answer=True)

    def _explain(self, detail: str, last: _Failure | None, timeout: float) -> str:
        """``detail``, with what the ECM's broadcasts say about a silent failure.

        Nothing heard on any of them for a whole request timeout is an ECM
        that is off, not one ignoring the request: at least one broadcasts
        faster than that.
        """
        if last is None or not last.no_answer or not self._ecm_ids:
            return detail
        names = ", ".join(f"0x{i:03X}" for i in sorted(self._ecm_ids))
        heard = self._ecm_heard_at
        if heard is None:
            return (
                f"{detail}; the ECM's broadcasts ({names}) were never heard during the read, so "
                "a key-off cannot be told apart from an ECM that stopped answering"
            )
        quiet = time.monotonic() - heard
        if quiet >= timeout:
            return (
                f"the ECM went silent (key off?): none of its broadcasts ({names}) heard for "
                f"{quiet:.1f} s; {detail}"
            )
        return (
            f"{detail}; the ECM was still broadcasting (heard {quiet:.2f} s before this "
            "failure), so it is powered but not answering"
        )

    def _session_step(
        self, client: CanObdClient, recipe: BackupRecipe, sub: int, counters: dict[str, Any]
    ) -> _Failure | None:
        reply = self._send(client, recipe, SESSION_SERVICE, bytes([sub]), counters)
        if reply.positive:
            return None
        return self._cause(f"session 0x{sub:02X}", reply, recipe.pacing.request_timeout_seconds)

    def _enter_session(
        self, client: CanObdClient, recipe: BackupRecipe, sub: int, counters: dict[str, Any]
    ) -> None:
        last: _Failure | None = None
        for attempt in range(recipe.pacing.retries_per_block + 1):
            if attempt:
                counters["retries"] += 1
                self._update(retries=counters["retries"])
            problem = self._midread_problem(check_vehicle=False)
            if problem:
                raise _AbortError(problem)
            last = self._session_step(client, recipe, sub, counters)
            if last is None:
                return
        detail = f"could not enter the session: {last.cause if last else None}"
        raise _AbortError(self._explain(detail, last, recipe.pacing.request_timeout_seconds))

    def _read_block(
        self,
        client: CanObdClient,
        recipe: BackupRecipe,
        address: int,
        length: int,
        counters: dict[str, Any],
    ) -> bytes:
        last: _Failure | None = None
        timeout = recipe.pacing.request_timeout_seconds
        for attempt in range(recipe.pacing.retries_per_block + 1):
            if attempt:
                counters["retries"] += 1
                self._update(retries=counters["retries"])
                problem = self._midread_problem(check_vehicle=False)
                if problem:
                    raise _AbortError(problem)
            last = None
            for sub in recipe.session.before_each_read:
                last = self._session_step(client, recipe, sub, counters)
                if last is not None:
                    break
            if last is not None:
                continue
            body = address.to_bytes(4, "big") + length.to_bytes(2, "big")
            reply = self._send(client, recipe, MEMORY_READ_SERVICE, body, counters)
            if reply.payload is not None and len(reply.payload) == length:
                return reply.payload
            if reply.payload is not None:
                last = _Failure(
                    f"read: {len(reply.payload)} bytes back for a {length}-byte read",
                    no_answer=False,
                )
            else:
                last = self._cause("read", reply, timeout)
        detail = (
            f"read at 0x{address:06X} ({length} bytes) failed after "
            f"{recipe.pacing.retries_per_block + 1} attempts; last: {last.cause if last else None}"
        )
        raise _AbortError(self._explain(detail, last, timeout))

    def _read_all(
        self,
        client: CanObdClient,
        recipe: BackupRecipe,
        counters: dict[str, Any],
        started: float,
    ) -> bytearray:
        blocks = recipe.read.blocks()
        image = bytearray(recipe.read.size)
        next_check = 0.0
        last_cycle = 0.0
        done_bytes = 0
        # The vector tables are read once their bytes are in (minutes before
        # the end): an image that points past the range stops the read there.
        extent_pending = recipe.extent is not None
        for index, (address, length) in enumerate(blocks):
            now = time.monotonic()
            problem = self._midread_problem(check_vehicle=now >= next_check)
            if now >= next_check:
                next_check = now + self._recheck_seconds
            if problem:
                raise _AbortError(problem)
            tx_gate.claim_ecm_session(HOLDER, ttl_seconds=CLAIM_TTL_SECONDS)
            wait = last_cycle + recipe.pacing.min_cycle_seconds - time.monotonic()
            if wait > 0:
                self._cancel.wait(wait)  # a cancel or shutdown ends the pause
            last_cycle = time.monotonic()
            data = self._read_block(client, recipe, address, length, counters)
            offset = address - recipe.read.start
            image[offset : offset + length] = data
            done_bytes += length
            done = index + 1
            if extent_pending and recipe.extent is not None:
                if done_bytes >= recipe.extent.tables_end:
                    extent_pending = False
                    self._check_extent_early(image, recipe, done, len(blocks))
            elapsed = time.monotonic() - started
            rate = done / elapsed if elapsed > 0 else 0.0
            self._update(
                blocks_done=done,
                bytes=done_bytes,
                elapsed_s=round(elapsed, 2),
                eta_s=round((len(blocks) - done) / rate, 1) if rate > 0 else None,
            )
        return image

    @staticmethod
    def _check_extent_early(image: bytearray, recipe: BackupRecipe, done: int, total: int) -> None:
        """Abort when the vector tables already read point past the range.

        Offline: the bytes are the ones just read. A read of the rest could
        only produce an image known to be short, so it stops here and says
        what the recipe needs.
        """
        spec = recipe.extent
        if spec is None:
            return
        reading = early_extent(bytes(image[: spec.tables_end]), spec, image_size=len(image))
        if reading.verdict != "larger":
            return
        raise _AbortError(
            f"stopped at block {done:,} of {total:,}: {reading.summary()}. The recipe reads "
            f"0x{recipe.read.start:06X}-0x{recipe.read.end:06X}; this ECM needs a "
            f"{ROM_BACKUP_FILE} in its own profile with the flash's real size. Nothing was stored"
        )

    def _reread(
        self,
        client: CanObdClient,
        recipe: BackupRecipe,
        image: bytearray,
        counters: dict[str, Any],
    ) -> list[str]:
        """Re-read blocks spread across the image; the addresses that differ."""
        blocks = recipe.read.blocks()
        count = min(recipe.verify.reread_blocks, len(blocks))
        if count == 1:
            picks = [0]
        else:
            picks = sorted({round(i * (len(blocks) - 1) / (count - 1)) for i in range(count)})
        mismatches = []
        for number, index in enumerate(picks, start=1):
            problem = self._midread_problem(check_vehicle=True)
            if problem:
                raise _AbortError(problem)
            tx_gate.claim_ecm_session(HOLDER, ttl_seconds=CLAIM_TTL_SECONDS)
            address, length = blocks[index]
            data = self._read_block(client, recipe, address, length, counters)
            offset = address - recipe.read.start
            if bytes(image[offset : offset + length]) != data:
                mismatches.append(f"0x{address:06X}")
            self._update(phase=f"verifying ({number}/{len(picks)})")
        return mismatches

    def _exit_session(self, bus: can.BusABC, recipe: BackupRecipe) -> None:
        """Best effort, one try each: the claim is released whatever happens."""
        if not recipe.session.exit:
            return
        client = CanObdClient(
            bus,
            request_id=recipe.request_id,
            response_id_min=recipe.response_id,
            response_id_max=recipe.response_id,
            timeout_seconds=recipe.pacing.request_timeout_seconds,
            parse_error_owner="rom-backup",
            tx_holder=HOLDER,
        )
        for sub in recipe.session.exit:
            try:
                reply = client.query_raw(
                    SESSION_SERVICE, bytes([sub]), request_id=recipe.request_id
                )
            except Exception:  # noqa: BLE001 - exit is best effort
                log.exception("ROM backup: session exit 0x%02X failed", sub)
                continue
            if not reply.positive:
                log.warning(
                    "ROM backup: session exit 0x%02X not acknowledged (sent=%s, nrc=%s, "
                    "send_error=%s)",
                    sub,
                    reply.sent,
                    reply.negative_response_code,
                    reply.send_error,
                )

    def _store(
        self,
        image: bytes,
        recipe: BackupRecipe,
        rom_map: RomMap | None,
        summary: dict[str, Any],
        reread_mismatches: list[str],
    ) -> tuple[BackupStateName, str | None]:
        """Add the image to the library, then report its integrity.

        Unverified is kept, never hidden: ``failed`` when a check disagreed,
        ``done`` with the reason when a check had nothing to check against.
        Nothing that can raise runs before
        :meth:`RomLibrary.add_image`: the integrity checks are wrapped (a
        failure there is itself a reason the image is unverified) and the
        revision match cannot raise (a broken flash history reads ``unknown``
        with its reason), so a bad registry or a callback error can no longer
        lose a ten-minute read.
        """
        unchecked: list[str] = []
        try:
            integrity, failures, unchecked = self._integrity(
                image, recipe, rom_map, reread_mismatches
            )
        except Exception as exc:  # noqa: BLE001 - recorded as the reason, never raised
            log.exception("ROM backup: integrity check failed")
            integrity = None
            failures = [f"the integrity check failed: {type(exc).__name__}: {exc}"]
        problems = failures + unchecked
        verified = not problems
        sha = sha256_hex(image)
        match = self.library.revision_match(sha)
        read_meta = {
            **summary,
            "blocks": len(recipe.read.blocks()),
            "block_size": recipe.read.block_size,
            "range": [f"0x{recipe.read.start:06X}", f"0x{recipe.read.end:06X}"],
            "request_id": f"0x{recipe.request_id:03X}",
            "response_id": f"0x{recipe.response_id:03X}",
            "recipe": self._recipe_source,
            "verified": verified,
            "unverified_reason": None if verified else "; ".join(problems),
            "integrity": integrity,
            "revision_match": match,
        }
        try:
            self.library.add_image(image, source="ecu_read", read_meta=read_meta)
        except Exception as exc:  # noqa: BLE001 - named in the state; nothing was stored
            log.exception("could not store the ECU read")
            self._update(stored=False, sha256=None, verified=verified, integrity=integrity)
            return "failed", (
                f"the read finished but the image could not be stored "
                f"({type(exc).__name__}: {exc}); nothing was kept"
            )
        self._update(
            stored=True,
            sha256=sha,
            verified=verified,
            integrity=integrity,
            revision_match=match,
        )
        if verified:
            return "done", None
        return ("failed" if failures else "done"), "unverified: " + "; ".join(problems)

    def _integrity(
        self,
        image: bytes,
        recipe: BackupRecipe,
        rom_map: RomMap | None,
        reread_mismatches: list[str],
    ) -> tuple[dict[str, Any], list[str], list[str]]:
        """Checksums, calibration ID against the ECM's, and the re-read.

        Returns the payload, the *failures* (a check ran and disagreed) and
        the *unchecked* (a check had nothing to check against).
        """
        failures: list[str] = []
        unchecked: list[str] = []
        calibration_id = read_calibration_id(image, rom_map)
        no_map = map_applies(calibration_id, rom_map)
        checksum = None
        if rom_map is None or no_map is not None:
            unchecked.append(f"{no_map}: checksums not checked")
        else:
            checksum = checksum_verdict(image, rom_map.checksums)
            if not checksum["ok"]:
                failures.append(checksum["reason"] or "checksum mismatch")
        extent = read_extent(image, recipe.extent) if recipe.extent is not None else None
        if extent is not None and extent.verdict == "larger":
            failures.append(extent.summary())
        elif rom_map is None:
            unchecked.append(self._size_caveat(recipe, extent))
        # Blank is as good as unreported: an empty string is found anywhere.
        ecu_id = (self._ecu_calibration_id() or "").strip() or None
        offset: int | None = None
        if rom_map is not None:
            # The map says where the ID lives, whether or not it describes this
            # calibration: a different ID there is the image disagreeing with
            # the ECM, which is exactly what makes the map not apply.
            offset = rom_map.calibration_id.offset
        elif ecu_id is not None and ecu_id.isascii() and ecu_id.isprintable():
            # Nothing says where the ID lives: look for the ECM's own as ASCII.
            found = image.find(ecu_id.encode("ascii"))
            if found >= 0:
                offset, calibration_id = found, ecu_id
        if ecu_id is None:
            unchecked.append(
                "the ECM has not reported its calibration ID (/status calibration), so the "
                f"image's {calibration_id or 'calibration ID'} could not be checked against it"
            )
        elif rom_map is not None and ecu_id != calibration_id:
            failures.append(f"image calibration ID {calibration_id} != ECM-reported {ecu_id}")
        elif rom_map is None and offset is None:
            unchecked.append(
                f"the ECM reports calibration ID {ecu_id}, and the image does not hold it as ASCII"
            )
        if reread_mismatches:
            failures.append(
                f"{len(reread_mismatches)} re-read block(s) differ: {', '.join(reread_mismatches)}"
            )
        integrity = {
            "checksum": checksum,
            "calibration_id": {
                "image": calibration_id,
                "ecu": ecu_id,
                "ok": ecu_id is not None and ecu_id == calibration_id,
                "offset": None if offset is None else f"0x{offset:06X}",
            },
            "reread": {
                "blocks": min(recipe.verify.reread_blocks, len(recipe.read.blocks())),
                "mismatches": reread_mismatches,
            },
            "extent": None if extent is None else extent.to_dict(),
        }
        return integrity, failures, unchecked

    @staticmethod
    def _size_caveat(recipe: BackupRecipe, extent: ExtentReading | None) -> str:
        """Without a map, what (if anything) checked the image's size."""
        size = f"{recipe.read.size:,}"
        if extent is not None and extent.verdict == "no_sign":
            return (
                f"with no map, only the image's own pointers check its size: {extent.summary()}; "
                f"a sign, not proof, that the recipe's {size} bytes are the whole flash"
            )
        caveat = f"with no map, nothing confirms the recipe's {size} bytes are the whole flash"
        return caveat if extent is None else f"{caveat} ({extent.summary()})"

    def _wait_for_polling(self, released: float) -> tuple[float | None, str]:
        """Seconds until an OBD observation newer than the release, or why none came.

        A daemon shutdown ends the wait at once, on entry or part-way: the
        wait sleeps on the shutdown event, not ``time.sleep``, so a stop during
        the up-to-60 s wait still records ``rom_backup.end`` and does not burn
        the daemon's stop budget.
        """
        deadline = released + self._resume_timeout
        while True:
            if self._shutting_down.is_set():
                return None, "not checked: the daemon is shutting down"
            now = time.monotonic()
            try:
                reading = self._vehicle()
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                return None, f"vehicle state could not be read: {exc}"
            age = reading.obd_freshness_seconds
            if age is not None and age < now - released:
                return round(now - released, 1), "an OBD reading arrived after the release"
            if now >= deadline:
                return None, (
                    f"no OBD reading within {self._resume_timeout:.0f} s of the release; check "
                    "/status obd_freshness_seconds"
                )
            self._shutting_down.wait(min(1.0, max(0.05, deadline - now)))


__all__ = [
    "CLAIM_TTL_SECONDS",
    "FOREIGN_TESTER_QUIET_SECONDS",
    "HOLDER",
    "POLLING_RESUME_TIMEOUT_SECONDS",
    "ROM_BACKUP_FILE",
    "SHUTDOWN_JOIN_SECONDS",
    "BackupRecipe",
    "BackupRefusedError",
    "RomBackupJob",
    "VehicleReading",
    "load_backup_recipe",
    "recipe_extent",
]
