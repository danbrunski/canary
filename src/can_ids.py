"""Shared CAN identifier parsing helpers."""

from __future__ import annotations


def parse_can_channel(channel: str | int) -> int | None:
    """Parse a CAN channel string (``"0x551"`` / ``"551"``) into an int."""
    if isinstance(channel, int):
        return channel
    try:
        return int(channel, 16)
    except (ValueError, TypeError):
        return None


def parse_standard_can_id(value: str | int) -> int:
    """Parse and validate an 11-bit CAN arbitration ID."""
    parsed = parse_can_channel(value)
    if parsed is None:
        raise ValueError(f"CAN arbitration ID must be hex, got {value!r}")
    if not 0 <= parsed <= 0x7FF:
        raise ValueError(f"CAN arbitration ID must be 11-bit, got 0x{parsed:X}")
    return parsed


__all__ = ["parse_can_channel", "parse_standard_can_id"]
