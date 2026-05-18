"""OBD capability + coverage report (Task 6.3).

"28 channels" is only honest if the operator can see *which* 28, which
public PIDs the ECU supports but CANary doesn't decode, and which it polls
but never hears back from. This builds that report from the typed J1979
registry (Task 6.1) plus a set of observed/supported PIDs — honest by
construction (Risk R7): an undecoded-but-supported PID is a first-class
row, never silently dropped.

The dashboard derives the "observed" set from the newest session's
telemetry channels (no live ECU query needed, so it works off-vehicle
and on the Pi without owning the diagnostic bus), and may additionally
pass an explicit ECU-reported supported bitmap and a backed-off set when
the live collector has them.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from src.collection.obd_registry import (
    MODE01_REGISTRY,
    PidCoverageStatus,
    classify_pid,
)
from src.telemetry import channel_of

__all__ = [
    "CoverageReport",
    "PidCoverageRow",
    "build_coverage_report",
    "channels_to_pids",
]

#: Reverse index: canonical channel name → Mode 01 PID number.
_NAME_TO_PID: dict[str, int] = {d.channel: pid for pid, d in MODE01_REGISTRY.items()}


def channels_to_pids(channels: set[str]) -> set[int]:
    """Map observed wide-column / channel names back to Mode 01 PID numbers.

    Accepts both bare names (``RPM``) and source-prefixed ones (``obd:RPM``).
    Names not in the registry (decoded-CAN signals, derived channels) are
    ignored — this is strictly the public Mode 01 view.
    """
    pids: set[int] = set()
    for raw in channels:
        name = channel_of(raw)
        pid = _NAME_TO_PID.get(name)
        if pid is not None:
            pids.add(pid)
    return pids


@dataclass(frozen=True)
class PidCoverageRow:
    mode: int
    pid: int
    pid_hex: str
    status: PidCoverageStatus
    channel: str | None
    display_name: str | None
    category: str | None
    unit: str | None
    poll_tier: str | None
    responded_recently: bool
    backed_off: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CoverageReport:
    """The operator-facing proof behind the channel count."""

    supported_count: int
    decoded_count: int
    supported_not_decoded_count: int
    responded_count: int
    backed_off_count: int
    rows: list[PidCoverageRow]
    #: Public, registry-known Mode 01 PIDs the ECU supports but CANary has
    #: no decoder for — the honest gap to close, surfaced explicitly.
    undecoded_supported: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "supported_count": self.supported_count,
            "decoded_count": self.decoded_count,
            "supported_not_decoded_count": self.supported_not_decoded_count,
            "responded_count": self.responded_count,
            "backed_off_count": self.backed_off_count,
            "undecoded_supported": self.undecoded_supported,
            "rows": [r.to_dict() for r in self.rows],
        }


def build_coverage_report(
    supported_pids: set[int],
    *,
    responded_pids: set[int] | None = None,
    backed_off_pids: set[int] | None = None,
) -> CoverageReport:
    """Classify every registry Mode 01 PID against what the ECU supports.

    ``supported_pids`` — the ECU's claimed/observed Mode 01 PID numbers.
    ``responded_pids`` — PIDs that produced a value recently (defaults to
    ``supported_pids`` when the caller only has the observed set).
    ``backed_off_pids`` — PIDs the streaming collector has put on
    short-timeout backoff after repeated non-response.

    Rows cover the whole registry so the UI can show supported-but-not-
    decoded and unsupported rows, not just the live channels.
    """
    responded = responded_pids if responded_pids is not None else set(supported_pids)
    backed_off = backed_off_pids or set()

    rows: list[PidCoverageRow] = []
    decoded = supported_not_decoded = 0
    undecoded_supported: list[str] = []
    for pid in sorted(MODE01_REGISTRY):
        definition = MODE01_REGISTRY[pid]
        is_supported = pid in supported_pids
        coverage = classify_pid(0x01, pid, supported=is_supported)
        if coverage.status == "decoded":
            decoded += 1
        elif coverage.status == "supported_not_decoded":
            supported_not_decoded += 1
            undecoded_supported.append(definition.channel)
        rows.append(
            PidCoverageRow(
                mode=0x01,
                pid=pid,
                pid_hex=f"0x{pid:02X}",
                status=coverage.status,
                channel=coverage.channel,
                display_name=coverage.display_name,
                category=coverage.category,
                unit=coverage.unit,
                poll_tier=definition.poll_tier,
                responded_recently=pid in responded,
                backed_off=pid in backed_off,
            )
        )

    return CoverageReport(
        supported_count=len(supported_pids),
        decoded_count=decoded,
        supported_not_decoded_count=supported_not_decoded,
        responded_count=len(responded & supported_pids),
        backed_off_count=len(backed_off),
        rows=rows,
        undecoded_supported=sorted(undecoded_supported),
    )
