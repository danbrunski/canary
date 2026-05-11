"""Shared diagnostic-analysis constants and low-level helpers."""

from __future__ import annotations

DEFAULT_DIAG_IDS: frozenset[int] = frozenset({0x7DF, *range(0x7E0, 0x7F0)})


def parse_diag_ids(raw: str) -> set[int]:
    """Parse comma-separated hex CAN IDs used for diagnostic traffic."""
    return {int(s, 16) for s in raw.split(",") if s.strip()}


def body_as_big_endian_int(body: bytes) -> int:
    """Decode a UDS response body as CANary's current raw DID integer."""
    return int.from_bytes(body, "big")


def body_as_scalar_int(body: bytes) -> int | None:
    """Decode a simple scalar DID body, or None for ambiguous packed payloads."""
    if not 1 <= len(body) <= 2:
        return None
    return body_as_big_endian_int(body)
