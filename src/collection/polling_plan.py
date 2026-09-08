"""Resolved polling-plan and transport-demand accounting (roadmap C09).

The plan is downstream of aliases and selection: one member is one unique
``(service, ECU request id, identifier)``.  Consumers therefore cannot count
profile names, scheduler rows, requests, frames and decoded rows as though they
were interchangeable units.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class PlanMember:
    kind: str
    service: int
    request_id: int
    identifier: int
    channel: str
    profile_channel: str | None
    rate_hz: float
    priority: bool = False
    required: bool = False
    response_payload_bytes: int | None = None
    decoded_rows_per_read: int = 1

    @property
    def key(self) -> tuple[int, int, int]:
        return self.service, self.request_id, self.identifier

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "service": f"0x{self.service:02X}",
            "request_id": f"0x{self.request_id:X}",
            "identifier": f"0x{self.identifier:04X}",
            "channel": self.channel,
            "profile_channel": self.profile_channel,
            "rate_hz": round(self.rate_hz, 6),
            "configured_rate_hz": round(self.rate_hz, 6),
            "priority": self.priority,
            "required": self.required,
            "response_payload_bytes": self.response_payload_bytes,
            "decoded_rows_per_read": self.decoded_rows_per_read,
            "capture_selected": True,
        }


def _response_frames(body_bytes: int) -> int:
    if body_bytes <= 7:
        return 1
    return 1 + math.ceil(max(0, body_bytes - 6) / 7)


def _capacity(member: PlanMember, pid_batch: int, did_batch: int) -> int:
    if member.kind == "pid" and member.service == 0x01:
        return max(1, pid_batch)
    if member.kind == "did" and member.service == 0x22 and member.response_payload_bytes:
        return max(1, did_batch)
    return 1


def _layers(members: list[PlanMember]) -> Iterable[tuple[float, list[PlanMember]]]:
    rates = sorted({member.rate_hz for member in members if member.rate_hz > 0}, reverse=True)
    for index, rate in enumerate(rates):
        lower = rates[index + 1] if index + 1 < len(rates) else 0.0
        yield rate - lower, [member for member in members if member.rate_hz >= rate]


def _batched_demands(
    members: list[PlanMember], pid_batch: int, did_batch: int
) -> tuple[float, float]:
    requests = 0.0
    frames = 0.0
    groups: dict[tuple[int, int, str, int], list[PlanMember]] = {}
    for member in members:
        groups.setdefault(
            (
                member.service,
                member.request_id,
                member.kind,
                _capacity(member, pid_batch, did_batch),
            ),
            [],
        ).append(member)
    for group in groups.values():
        capacity = _capacity(group[0], pid_batch, did_batch)
        for layer_rate, active in _layers(group):
            chunks = [active[i : i + capacity] for i in range(0, len(active), capacity)]
            requests += layer_rate * len(chunks)
            for chunk in chunks:
                body = 1 + sum(
                    (2 if member.kind == "did" else 1) + (member.response_payload_bytes or 0)
                    for member in chunk
                )
                frames += layer_rate * (1 + _response_frames(body))
    return requests, frames


@dataclass(frozen=True)
class ResolvedPollingPlan:
    members: tuple[PlanMember, ...]
    aliases_deduplicated: int
    bus_budget_requests_per_second: float
    pid_batch_max: int
    did_batch_max: int
    session_control_requests_per_second: float = 0.0
    discovery_requests_per_second: float = 0.0

    @classmethod
    def build(
        cls,
        members: Iterable[PlanMember],
        *,
        bus_budget_requests_per_second: float,
        pid_batch_max: int,
        did_batch_max: int,
        session_control_requests_per_second: float = 0.0,
        discovery_requests_per_second: float = 0.0,
    ) -> ResolvedPollingPlan:
        unique: dict[tuple[int, int, int], PlanMember] = {}
        seen = 0
        for member in members:
            seen += 1
            previous = unique.get(member.key)
            if previous is None:
                unique[member.key] = member
            else:
                unique[member.key] = replace(
                    previous,
                    rate_hz=max(previous.rate_hz, member.rate_hz),
                    priority=previous.priority or member.priority,
                    required=previous.required or member.required,
                )
        resolved = tuple(unique.values())
        return cls(
            members=resolved,
            aliases_deduplicated=seen - len(resolved),
            bus_budget_requests_per_second=bus_budget_requests_per_second,
            pid_batch_max=max(1, pid_batch_max),
            did_batch_max=max(1, did_batch_max),
            session_control_requests_per_second=max(0.0, session_control_requests_per_second),
            discovery_requests_per_second=max(0.0, discovery_requests_per_second),
        )

    def to_dict(self) -> dict[str, Any]:
        members = list(self.members)
        unknown_widths = [
            member.channel for member in members if member.response_payload_bytes is None
        ]
        fallback_requests = sum(member.rate_hz for member in members)
        fallback_frames = sum(
            member.rate_hz
            * (
                1
                + _response_frames(
                    1 + (2 if member.kind == "did" else 1) + (member.response_payload_bytes or 0)
                )
            )
            for member in members
        )
        batched_requests, batched_frames = _batched_demands(
            members, self.pid_batch_max, self.did_batch_max
        )
        overhead = self.session_control_requests_per_second + self.discovery_requests_per_second
        priority_fallback = sum(member.rate_hz for member in members if member.priority)
        required_rates = [member.rate_hz for member in members if member.required]
        return {
            "members": [member.to_dict() for member in members],
            "member_count": len(members),
            "aliases_deduplicated": self.aliases_deduplicated,
            "channel_reads_per_second": round(sum(member.rate_hz for member in members), 6),
            "decoded_rows_per_second": round(
                sum(member.rate_hz * member.decoded_rows_per_read for member in members), 6
            ),
            "complete_tuples_per_second": round(min(required_rates), 6) if required_rates else None,
            "diagnostic_requests_per_second": {
                "best_case_batched": round(batched_requests + overhead, 6),
                "single_request_fallback": round(fallback_requests + overhead, 6),
                "session_control": round(self.session_control_requests_per_second, 6),
                "discovery": round(self.discovery_requests_per_second, 6),
            },
            "raw_frames_per_second": {
                "best_case_batched": round(batched_frames + 2 * overhead, 6),
                "single_request_fallback": round(fallback_frames + 2 * overhead, 6),
                "estimate_complete": not unknown_widths,
                "unknown_response_width_channels": unknown_widths,
            },
            "priority_requests_per_second_fallback": round(priority_fallback, 6),
            "bus_budget_requests_per_second": round(self.bus_budget_requests_per_second, 6),
            "priority_feasible": priority_fallback <= self.bus_budget_requests_per_second,
            "best_case_feasible": batched_requests + overhead
            <= self.bus_budget_requests_per_second,
            "fallback_feasible": fallback_requests + overhead
            <= self.bus_budget_requests_per_second,
            "batch_max": {"mode01": self.pid_batch_max, "mode22": self.did_batch_max},
        }


__all__ = ["PlanMember", "ResolvedPollingPlan"]
