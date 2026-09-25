"""Mandatory authorization gate for future ECU non-volatile writes.

There is deliberately no flash transport in this module.  It owns the token
that a future transport must accept and the only block executor that may call
one, making the operator permission and live-state checks part of the write
path instead of a convention at its callers.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from src.config import NonvolatileWriteConfig
from src.control.audit import ControlAuditLogger
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


@dataclass(frozen=True)
class NonvolatileWriteSignals:
    """Fresh inputs sampled at an authorization/block boundary."""

    vehicle_state: VehicleState
    vehicle_speed_kph: float | None
    vehicle_speed_freshness_seconds: float | None
    battery_voltage: float | None
    ups_soc_percent: float | None
    ups_runtime_seconds: float | None

    def to_audit_dict(self) -> dict[str, Any]:
        return {
            "vehicle_state": self.vehicle_state.to_dict(),
            "vehicle_speed_kph": self.vehicle_speed_kph,
            "vehicle_speed_freshness_seconds": self.vehicle_speed_freshness_seconds,
            "battery_voltage": self.battery_voltage,
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
        if signals.battery_voltage is None:
            raise AuthorizationRefusedError("battery_unknown", "battery voltage is not reported")
        if signals.battery_voltage < self.policy.min_battery_voltage:
            raise AuthorizationRefusedError(
                "battery_low",
                f"battery voltage {signals.battery_voltage:.2f} V is below "
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
    "authorize_nonvolatile_write",
    "execute_authorized_blocks",
    "sha256_image",
]
