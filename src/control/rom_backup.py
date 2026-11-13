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

**Layout** (R29.13). This module is the job: its lifecycle, the start
preconditions and the mid-read re-check. The recipe schema is
:mod:`src.control.rom_backup_recipe`, the wire protocol (one
:class:`~src.control.rom_backup_wire.MemoryReader` per read, owning its
counters) :mod:`src.control.rom_backup_wire`, and the post-processing that
needs no bus :mod:`src.control.rom_backup_store`. Every name this module
exported before the split is still importable from here.

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
from typing import Any

import can
import yaml

from src.analysis.rom_map import RomMap
from src.collection import private_traffic, tx_gate
from src.control.rom_backup_recipe import (
    ROM_BACKUP_FILE,
    BackupRecipe,
    load_backup_recipe,
    recipe_extent,
)
from src.control.rom_backup_store import BackupStateName, integrity, store_image
from src.control.rom_backup_wire import (
    CLAIM_TTL_SECONDS,
    HOLDER,
    MemoryReader,
    ReadAbortedError,
    ReadCancelledError,
    counters_summary,
)
from src.control.rom_library import RomLibrary
from src.vehicle.platforms import VehicleDataFile
from src.vehicle_state import engine_running_evidence

log = logging.getLogger(__name__)

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


def _engine_running(reading: VehicleReading) -> str | None:
    """Why the engine counts as running, or ``None``.

    :func:`src.vehicle_state.engine_running_evidence`, the definition the
    non-volatile write guard uses too (R29.14): the classifier's verdict,
    **or** a fresh broadcast engine speed at or above the running threshold
    whatever the verdict says. Once OBD polling pauses for the read its last
    ``0`` stays "fresh" for up to ten seconds — exactly the window in which
    the session is entered — while the broadcast already reads 800.
    """
    return engine_running_evidence(reading)


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
        #: The running (or last) read's wire side: its frame tap and counters.
        self._reader: MemoryReader | None = None
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
            self._reader = None
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

    def _midread_problem(self, *, check_vehicle: bool) -> str | None:
        """Why the read must stop now, or ``None``; the reader asks before every
        block and retry. Raises :class:`ReadCancelledError` on a cancel."""
        if self._cancel.is_set():
            raise ReadCancelledError
        foreign_can = None if self._reader is None else self._reader.foreign_can
        if foreign_can is not None:
            return f"another tester transmitted on CAN (id 0x{foreign_can:03X}) during the read"
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
        reader = MemoryReader(
            recipe,
            midread_problem=self._midread_problem,
            update=self._update,
            cancel=self._cancel,
            listen_seconds=self._listen_seconds,
            recheck_seconds=self._recheck_seconds,
        )
        self._reader = reader
        image: bytearray | None = None
        result: BackupStateName = "failed"
        reason: str | None = None
        reread_mismatches: list[str] | None = None
        bus: can.BusABC | None = None
        entered = False
        frames_excluded = 0
        try:
            tx_gate.claim_ecm_session(HOLDER, ttl_seconds=CLAIM_TTL_SECONDS)
            private_traffic.hold(HOLDER, {recipe.request_id, recipe.response_id})
            try:
                bus = self._open_bus()
            except (OSError, can.CanError) as exc:
                raise ReadAbortedError(f"could not open the CAN bus: {exc}") from exc
            client = reader.attach(bus)
            self._update(phase="listening")
            reader.listen(bus)
            problem = self._midread_problem(check_vehicle=True)
            if problem:
                raise ReadAbortedError(problem)
            self._update(phase="entering session")
            entered = True
            for sub in recipe.session.enter_once:
                reader.enter_session(client, sub)
            self._update(phase="reading")
            image = reader.read_all(client, started)
            if recipe.verify.reread_blocks:
                self._update(phase="verifying")
                reread_mismatches = reader.reread(client, image)
            result = "done"
        except ReadCancelledError:
            result = "cancelled"
            reason = (
                "cancelled: the daemon is shutting down"
                if self._shutting_down.is_set()
                else "cancelled by the operator"
            )
            image = None
        except ReadAbortedError as exc:
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
                    reader.exit_session(bus)
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
            **counters_summary(reader.counters),
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

    def _store(
        self,
        image: bytes,
        recipe: BackupRecipe,
        rom_map: RomMap | None,
        summary: dict[str, Any],
        reread_mismatches: list[str],
    ) -> tuple[BackupStateName, str | None]:
        """:func:`src.control.rom_backup_store.store_image`, with this job's
        library, recipe source, ECM calibration ID and state."""
        return store_image(
            image,
            recipe,
            rom_map,
            summary,
            reread_mismatches,
            library=self.library,
            recipe_source=self._recipe_source,
            ecu_calibration_id=self._ecu_calibration_id,
            update=self._update,
        )

    def _integrity(
        self,
        image: bytes,
        recipe: BackupRecipe,
        rom_map: RomMap | None,
        reread_mismatches: list[str],
    ) -> tuple[dict[str, Any], list[str], list[str]]:
        """:func:`src.control.rom_backup_store.integrity` against this job's ECM."""
        return integrity(image, recipe, rom_map, reread_mismatches, self._ecu_calibration_id)

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
