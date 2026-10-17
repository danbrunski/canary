"""Mandatory authorization gate for future ECU non-volatile writes.

There is deliberately no flash transport in this module.  It owns the token
that a future transport must accept and the only block executor that may call
one, making the operator permission and live-state checks part of the write
path instead of a convention at its callers.
"""

from __future__ import annotations

import hashlib
import math
import numbers
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, TypeVar

from src.collection.hat_revision import HatRevisionError, resolve_hat_revision
from src.config import HardwareConfig, NonvolatileWriteConfig
from src.control.audit import ControlAuditLogger
from src.decode.hat_adc_board import load_hat_adc_board
from src.timefmt import parse_iso_utc
from src.vehicle_state import VehicleState

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_Block = TypeVar("_Block")
_BlockContra = TypeVar("_BlockContra", contravariant=True)


class AuthorizationRefusedError(RuntimeError):
    """A non-volatile write did not satisfy a named interlock."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail

    def __reduce__(self) -> tuple[object, ...]:
        # Rebuilt from its constructor's arguments, so it survives a process
        # boundary (pickle); tests/test_exceptions_pickle.py holds every class to it.
        return (type(self), (self.code, self.detail), self.__dict__)


#: Where the vehicle's supply voltage is read from, in order of preference
#: (H03.2). The HAT's 12 V sense (``sensor:SUPPLY_VOLTAGE``, H03.1) reads with
#: the ECM asleep; ``obd:CONTROL_MODULE_VOLTAGE`` (Mode 01 PID 0x42) needs it
#: awake, which it is at the only state a write may start from
#: (``ignition_only``). The first *fresh* one wins; a stale preferred source
#: never hides a fresh fallback, and a stale fallback never passes.
SUPPLY_VOLTAGE_SOURCES: tuple[tuple[str, str], ...] = (
    ("sensor", "SUPPLY_VOLTAGE"),
    ("obd", "CONTROL_MODULE_VOLTAGE"),
)


@dataclass(frozen=True)
class SupplyVoltageSources:
    """The supply-voltage channels a write may be gated on, in preference
    order, and the ones held back with the reason. Build it with
    :func:`supply_voltage_sources`, which knows when the HAT's 12 V sense is
    not to be trusted; :func:`supply_voltage_from_latest_rows` requires one, so
    no caller can skip that question."""

    order: tuple[tuple[str, str], ...]
    #: ``"<source>:<channel>: <why>"`` for each source held back.
    withheld: tuple[str, ...] = ()


def supply_voltage_sources(hardware: HardwareConfig) -> SupplyVoltageSources:
    """:data:`SUPPLY_VOLTAGE_SOURCES`, less a 12 V sense whose calibration is
    not known.

    Rev 2.1's sense is the ADS8688A's CH0. Until its ``gain_trim`` is measured
    against a meter (open item 23), the divider's 1 % parts alone leave it
    about ±1.6 % out, so an 11.8 V battery can read 12.0 V. That reading is
    still fine for telling parked from bench, but it must not pass a write's
    battery floor. The gate then falls back to the ECM's own reading, which a
    write's ``ignition_only`` state guarantees is awake."""
    sense = ("sensor", "SUPPLY_VOLTAGE")
    others = tuple(s for s in SUPPLY_VOLTAGE_SOURCES if s != sense)
    if not hardware.supply_voltage.enabled:
        return SupplyVoltageSources(SUPPLY_VOLTAGE_SOURCES)
    try:
        revision = resolve_hat_revision(hardware.hat_revision)
    except HatRevisionError as exc:
        return SupplyVoltageSources(others, (f"sensor:SUPPLY_VOLTAGE: {exc}",))
    if revision.supply_adc == "ads8688a":
        board = load_hat_adc_board(hardware.hat_adc.board)
        if board.supply.gain_trim is None:
            return SupplyVoltageSources(
                others,
                (
                    "sensor:SUPPLY_VOLTAGE: rev 2.1's 12 V sense is uncalibrated "
                    f"(supply.gain_trim is null in {hardware.hat_adc.board.name} until it is "
                    "measured against a meter, open item 23)",
                ),
            )
    return SupplyVoltageSources(SUPPLY_VOLTAGE_SOURCES)


@dataclass(frozen=True)
class SupplyVoltageReading:
    """The supply voltage a write is gated on, and where it came from.

    ``volts`` is ``None`` when no source was fresh, and ``reason`` then says
    why for each source (absent, stale by how much, no numeric value) — the
    refusal quotes it. A bare ``float | None`` could only say "not reported",
    which does not tell the operator whether the 12 V sense is unplugged or
    the ECM stopped answering (H03.2). Build one with
    :func:`supply_voltage_from_latest_rows`, or :meth:`unknown`.
    """

    volts: float | None
    source: str | None
    age_seconds: float | None
    reason: str | None = None

    def __post_init__(self) -> None:
        """An unknown reading names why; a known one is a finite number from a
        named source with a finite, non-negative age. Anything else cannot be
        built, so no caller can hand the guard a half-formed reading (the
        guard re-checks regardless)."""
        if self.volts is None:
            if not self.reason:
                raise ValueError("an unknown supply voltage must carry its reason")
            return
        if isinstance(self.volts, bool) or not math.isfinite(self.volts):
            raise ValueError(f"supply voltage must be a finite number, not {self.volts!r}")
        if not self.source:
            raise ValueError("a supply voltage must name its source")
        age = self.age_seconds
        if age is None or isinstance(age, bool) or not math.isfinite(age) or age < 0:
            raise ValueError(f"a supply voltage needs a finite age >= 0 s, not {age!r}")

    @classmethod
    def unknown(cls, reason: str) -> SupplyVoltageReading:
        return cls(volts=None, source=None, age_seconds=None, reason=reason)

    def to_audit_dict(self) -> dict[str, Any]:
        return {
            "volts": self.volts,
            "source": self.source,
            "age_seconds": None if self.age_seconds is None else round(self.age_seconds, 3),
            "reason": self.reason,
        }


def _row_instant(raw: object) -> datetime | None:
    if isinstance(raw, str):
        try:
            return parse_iso_utc(raw)
        except ValueError:
            return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    return None


def supply_voltage_from_latest_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    now: datetime,
    max_age_seconds: float,
    sources: SupplyVoltageSources,
) -> SupplyVoltageReading:
    """The production supply-voltage reader for :class:`NonvolatileWriteSignals`.

    ``rows`` are latest-per-channel rows (``{source, channel, value,
    timestamp}``, what ``dashboard.readers.read_latest_per_channel`` returns
    for the session being written — pass the writer's live directory, or a
    capture is invisible). Tries ``sources`` in order (from
    :func:`supply_voltage_sources`) and returns the first whose newest value
    is a finite number stamped no more
    than ``max_age_seconds`` before ``now`` (and not after it: a reading from
    the future is a clock fault, not a fresh one). Anything else is an
    unknown reading naming why each source failed, which the guard refuses.
    Pure: the read stays with the caller.
    """
    wanted = set(sources.order)
    newest: dict[tuple[str, str], tuple[datetime | None, object]] = {}
    for row in rows:
        key = (str(row.get("source")), str(row.get("channel")))
        if key not in wanted:
            continue
        stamp = _row_instant(row.get("timestamp"))
        held = newest.get(key)
        if held is None or (stamp is not None and (held[0] is None or stamp > held[0])):
            newest[key] = (stamp, row.get("value"))
    why: list[str] = list(sources.withheld)
    for source, channel in sources.order:
        name = f"{source}:{channel}"
        entry = newest.get((source, channel))
        if entry is None:
            why.append(f"{name} has no reading in the latest rows")
            continue
        stamp, value = entry
        if isinstance(value, bool) or not isinstance(value, numbers.Real):
            why.append(f"{name} has no numeric value ({value!r})")
            continue
        volts = float(value)
        if not math.isfinite(volts):
            why.append(f"{name} is {volts!r}")
            continue
        if stamp is None:
            why.append(f"{name} has no readable timestamp")
            continue
        age = (now - stamp).total_seconds()
        if age < 0:
            why.append(f"{name} is stamped {-age:.1f} s in the future (clock fault)")
            continue
        if age > max_age_seconds:
            why.append(f"{name} is stale ({age:.1f} s old, limit {max_age_seconds:g} s)")
            continue
        return SupplyVoltageReading(volts=volts, source=name, age_seconds=age)
    return SupplyVoltageReading.unknown("no fresh supply voltage: " + "; ".join(why))


@dataclass(frozen=True)
class NonvolatileWriteSignals:
    """Fresh inputs sampled at an authorization/block boundary."""

    vehicle_state: VehicleState
    vehicle_speed_kph: float | None
    vehicle_speed_freshness_seconds: float | None
    #: A reading, not a number: an unknown voltage carries its reason, and the
    #: guard re-checks the age itself (H03.2).
    battery_voltage: SupplyVoltageReading
    ups_soc_percent: float | None
    ups_runtime_seconds: float | None

    def to_audit_dict(self) -> dict[str, Any]:
        return {
            "vehicle_state": self.vehicle_state.to_dict(),
            "vehicle_speed_kph": self.vehicle_speed_kph,
            "vehicle_speed_freshness_seconds": self.vehicle_speed_freshness_seconds,
            "battery_voltage": self.battery_voltage.to_audit_dict(),
            "ups_soc_percent": self.ups_soc_percent,
            "ups_runtime_seconds": self.ups_runtime_seconds,
        }


SignalReader = Callable[[], NonvolatileWriteSignals]


@dataclass(frozen=True)
class NonvolatileWriteRequest:
    """One operator-present request naming the exact image and target ECU."""

    image_sha256: str
    ecu: str
    operator_confirmed_image_sha256: str
    operator_confirmed_ecu: str
    expected_duration_seconds: float
    read_signals: SignalReader = field(repr=False, compare=False)


@dataclass(frozen=True)
class Authorization:
    """Opaque, short-lived, single-use permission for exactly one operation."""

    image_sha256: str
    ecu: str
    expires_monotonic: float
    _token: str = field(repr=False, compare=False)
    _guard: NonvolatileWriteGuard = field(repr=False, compare=False)


class AuthorizedBlockWriter(Protocol[_BlockContra]):
    """A future transport must require the authorization in its signature."""

    def __call__(self, authorization: Authorization, index: int, block: _BlockContra) -> None: ...


class NonvolatileWriteGuard:
    """Mint and consume authorizations; re-check interlocks between blocks."""

    def __init__(
        self,
        *,
        policy: NonvolatileWriteConfig,
        audit: ControlAuditLogger,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.policy = policy
        self.audit = audit
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._readers: dict[str, SignalReader] = {}
        self._expected_durations: dict[str, float] = {}
        self._consumed: set[str] = set()

    def authorize(self, request: NonvolatileWriteRequest) -> Authorization:
        image_sha256 = request.image_sha256.strip().lower()
        payload: dict[str, Any] = {"image_sha256": image_sha256, "ecu": request.ecu}
        try:
            image_sha256 = _normalise_sha256(request.image_sha256)
            if _normalise_sha256(request.operator_confirmed_image_sha256) != image_sha256:
                raise AuthorizationRefusedError(
                    "permission_image_mismatch",
                    "operator confirmation does not name this image hash",
                )
            if request.operator_confirmed_ecu != request.ecu:
                raise AuthorizationRefusedError(
                    "permission_ecu_mismatch",
                    "operator confirmation does not name this ECU",
                )
            if request.expected_duration_seconds <= 0:
                raise AuthorizationRefusedError(
                    "duration_invalid", "expected write duration must be greater than zero"
                )
            signals = request.read_signals()
            payload["signals"] = signals.to_audit_dict()
            self._check_signals(signals, request.expected_duration_seconds)
        except AuthorizationRefusedError as exc:
            payload.update({"reason_code": exc.code, "reason": exc.detail})
            self.audit.record(
                action="nonvolatile_write.authorize", result="refused", payload=payload
            )
            raise

        token = secrets.token_urlsafe(32)
        expires = self._monotonic() + self.policy.authorization_ttl_seconds
        with self._lock:
            self._readers[token] = request.read_signals
            self._expected_durations[token] = request.expected_duration_seconds
        self.audit.record(
            action="nonvolatile_write.authorize",
            result="granted",
            payload={**payload, "ttl_seconds": self.policy.authorization_ttl_seconds},
        )
        return Authorization(
            image_sha256=image_sha256,
            ecu=request.ecu,
            expires_monotonic=expires,
            _token=token,
            _guard=self,
        )

    def begin(self, authorization: Authorization, *, image_sha256: str, ecu: str) -> None:
        """Consume once and check the state immediately before block zero."""
        token = self._validate_binding(authorization, image_sha256=image_sha256, ecu=ecu)
        with self._lock:
            if token in self._consumed:
                self._refuse_use(authorization, "already_used", "authorization is single-use")
            self._consumed.add(token)
        self._recheck(authorization, stage="before_first_block")

    def before_next_block(self, authorization: Authorization, *, block_index: int) -> None:
        """Abort before a later block if any live interlock changed."""
        if authorization._guard is not self or authorization._token not in self._consumed:
            self._refuse_use(
                authorization,
                "operation_not_started",
                "authorization has not started this operation",
            )
        self._recheck(authorization, stage=f"before_block_{block_index}")

    def complete(self, authorization: Authorization, *, block_count: int) -> None:
        self.audit.record(
            action="nonvolatile_write.complete",
            result="ok",
            payload={
                "image_sha256": authorization.image_sha256,
                "ecu": authorization.ecu,
                "block_count": block_count,
            },
        )

    def _validate_binding(
        self, authorization: Authorization, *, image_sha256: str, ecu: str
    ) -> str:
        if authorization._guard is not self:
            self._refuse_use(authorization, "wrong_guard", "authorization belongs to another guard")
        if self._monotonic() > authorization.expires_monotonic:
            self._refuse_use(authorization, "expired", "authorization has expired")
        if _normalise_sha256(image_sha256) != authorization.image_sha256:
            self._refuse_use(
                authorization, "image_mismatch", "authorization is bound to a different image"
            )
        if ecu != authorization.ecu:
            self._refuse_use(authorization, "ecu_mismatch", "authorization is bound to another ECU")
        return authorization._token

    def _recheck(self, authorization: Authorization, *, stage: str) -> None:
        reader = self._readers[authorization._token]
        signals = reader()
        try:
            self._check_signals(signals, self._expected_durations[authorization._token])
        except AuthorizationRefusedError as exc:
            self.audit.record(
                action="nonvolatile_write.abort",
                result="refused",
                payload={
                    "image_sha256": authorization.image_sha256,
                    "ecu": authorization.ecu,
                    "stage": stage,
                    "reason_code": exc.code,
                    "reason": exc.detail,
                    "signals": signals.to_audit_dict(),
                },
            )
            raise

    def _check_signals(
        self, signals: NonvolatileWriteSignals, expected_duration_seconds: float
    ) -> None:
        state = signals.vehicle_state
        if state.obd_freshness_seconds is None or (
            state.obd_freshness_seconds > self.policy.max_signal_age_seconds
        ):
            raise AuthorizationRefusedError(
                "engine_state_unknown",
                "cannot tell whether the engine is running from fresh OBD data",
            )
        if state.state == "engine_running":
            raise AuthorizationRefusedError("engine_running", "engine is running")
        if state.state != "ignition_only":
            raise AuthorizationRefusedError(
                "engine_state_not_ignition_only",
                f"vehicle state is {state.state}; key-on/engine-off is required",
            )
        if state.rpm != 0:
            raise AuthorizationRefusedError("rpm_nonzero", f"engine RPM is {state.rpm!r}, not zero")
        if (
            signals.vehicle_speed_freshness_seconds is None
            or signals.vehicle_speed_freshness_seconds > self.policy.max_signal_age_seconds
        ):
            raise AuthorizationRefusedError(
                "speed_unknown",
                "cannot tell whether the vehicle is stationary from fresh speed data",
            )
        if signals.vehicle_speed_kph is None:
            raise AuthorizationRefusedError("speed_unknown", "vehicle speed is not reported")
        if abs(signals.vehicle_speed_kph) > self.policy.stationary_speed_max_kph:
            raise AuthorizationRefusedError(
                "vehicle_moving", f"vehicle speed is {signals.vehicle_speed_kph:.2f} km/h"
            )
        battery = signals.battery_voltage
        if battery.volts is None or not math.isfinite(battery.volts):
            raise AuthorizationRefusedError(
                "battery_unknown",
                f"battery voltage unknown: {battery.reason or 'not reported'}",
            )
        # Re-checked here, not trusted from the reader: whatever built the
        # reading, a stale or undated voltage never passes.
        age = battery.age_seconds
        # ``not (0 <= age <= max)`` rather than two ``<``/``>`` tests: every
        # comparison with NaN is False, so the negated form refuses it.
        if (
            age is None
            or not math.isfinite(age)
            or not (0.0 <= age <= self.policy.max_signal_age_seconds)
        ):
            raise AuthorizationRefusedError(
                "battery_stale",
                f"battery voltage from {battery.source or 'an unnamed source'} is not fresh "
                f"(age {battery.age_seconds!r} s, limit "
                f"{self.policy.max_signal_age_seconds:g} s)",
            )
        if battery.volts < self.policy.min_battery_voltage:
            raise AuthorizationRefusedError(
                "battery_low",
                f"battery voltage {battery.volts:.2f} V ({battery.source}) is below "
                f"{self.policy.min_battery_voltage:.2f} V",
            )
        if signals.ups_soc_percent is None:
            raise AuthorizationRefusedError(
                "ups_soc_unknown", "UPS state of charge is not reported"
            )
        if signals.ups_soc_percent < self.policy.min_ups_soc_percent:
            raise AuthorizationRefusedError(
                "ups_soc_low",
                f"UPS state of charge {signals.ups_soc_percent:.1f}% is below "
                f"{self.policy.min_ups_soc_percent:.1f}%",
            )
        if signals.ups_runtime_seconds is None:
            raise AuthorizationRefusedError("ups_runtime_unknown", "UPS runtime is not reported")
        required_runtime = expected_duration_seconds + self.policy.ups_runtime_margin_seconds
        if signals.ups_runtime_seconds < required_runtime:
            raise AuthorizationRefusedError(
                "ups_runtime_short",
                f"UPS runtime {signals.ups_runtime_seconds:.0f}s is below the required "
                f"{required_runtime:.0f}s",
            )

    def _refuse_use(self, authorization: Authorization, code: str, detail: str) -> None:
        self.audit.record(
            action="nonvolatile_write.authorization_use",
            result="refused",
            payload={
                "image_sha256": authorization.image_sha256,
                "ecu": authorization.ecu,
                "reason_code": code,
                "reason": detail,
            },
        )
        raise AuthorizationRefusedError(code, detail)


def authorize_nonvolatile_write(
    request: NonvolatileWriteRequest,
    *,
    policy: NonvolatileWriteConfig,
    audit: ControlAuditLogger,
    monotonic: Callable[[], float] = time.monotonic,
) -> Authorization:
    """Authorize one exact image/ECU operation after all live checks pass."""
    return NonvolatileWriteGuard(policy=policy, audit=audit, monotonic=monotonic).authorize(request)


def execute_authorized_blocks(
    authorization: Authorization,
    *,
    image_sha256: str,
    ecu: str,
    blocks: Iterable[_Block],
    write_block: AuthorizedBlockWriter[_Block],
) -> int:
    """Guarded skeleton for a future transport; performs no bus I/O itself."""
    guard = authorization._guard
    guard.begin(authorization, image_sha256=image_sha256, ecu=ecu)
    count = 0
    for index, block in enumerate(blocks):
        if index:
            guard.before_next_block(authorization, block_index=index)
        write_block(authorization, index, block)
        count += 1
    guard.complete(authorization, block_count=count)
    return count


def sha256_image(image: bytes) -> str:
    """Return the exact binding value shown in the operator confirmation."""
    return hashlib.sha256(image).hexdigest()


def _normalise_sha256(value: str) -> str:
    normalised = value.strip().lower()
    if not _SHA256_RE.fullmatch(normalised):
        raise AuthorizationRefusedError("invalid_image_hash", "image SHA-256 must be 64 hex digits")
    return normalised


__all__ = [
    "Authorization",
    "AuthorizationRefusedError",
    "AuthorizedBlockWriter",
    "NonvolatileWriteGuard",
    "NonvolatileWriteRequest",
    "NonvolatileWriteSignals",
    "SUPPLY_VOLTAGE_SOURCES",
    "SupplyVoltageSources",
    "SupplyVoltageReading",
    "authorize_nonvolatile_write",
    "execute_authorized_blocks",
    "sha256_image",
    "supply_voltage_from_latest_rows",
    "supply_voltage_sources",
]
