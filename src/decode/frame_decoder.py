"""Decode raw CAN frames into ``decoded:*`` TelemetryRecords at ingest time.

Session Parquet has always stored raw CAN frame payloads (the ``payload``
binary column), but the ``decoded:*`` rows produced on the live path
(:meth:`src.preprocessing.live.LiveFeatureSource.decode_for_telemetry`) were
never routed to storage — so every retroactive analysis of a stored session
had to hand-decode payload bytes (hex substrings in SQL, one-off scripts).
This module gives the storage sink its own decoder so bound signals are
persisted as queryable rows alongside the raw frames they came from.

Deliberately mirrors :func:`src.preprocessing.decode.decode_can_rows` (the
batch/training decode-at-read path), not a new convention:

* values are the RAW integer from the binding's byte range, NOT the scaled
  engineering value — training's scaler was fit on raw values, and mixing
  scaled and raw rows for the same channel across sessions would corrupt
  feature normalization. Use :func:`src.decode.bindings.apply_scaling` when
  presenting to humans.
* metadata carries the same ``{"can_id", "binding_confidence"}`` keys, so
  stored rows are indistinguishable from recomputed ones and
  ``decode_can_rows`` can safely skip channels that were already persisted.

Confidence gating: storage is forever, so default to persisting only
``confirmed`` bindings (``collection.persist_decoded_confidence`` widens
this). The live/dashboard path keeps decoding every binding regardless —
this filter is about what's worth writing to disk, not what's worth seeing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from src.can_ids import parse_can_channel
from src.decode.bindings import Bindings, Signal, decode_raw, load_bindings
from src.telemetry import TelemetryRecord


@dataclass
class FrameDecoder:
    """Pre-indexed binding decoder for single raw CAN records."""

    _by_can_id: dict[int, list[Signal]] = field(default_factory=dict)

    @classmethod
    def from_bindings(
        cls,
        bindings: Bindings,
        *,
        confidence_levels: frozenset[str] | set[str] | tuple[str, ...] | None = None,
    ) -> FrameDecoder:
        """Index ``bindings`` by CAN ID, optionally filtered by confidence.

        A signal with ``persist: true`` is included whatever the filter says —
        see :attr:`~src.decode.bindings.Signal.persist` for why that is a
        separate question from confidence.
        """
        by_id: dict[int, list[Signal]] = {}
        for sig in bindings.signals:
            if (
                confidence_levels is not None
                and sig.confidence not in confidence_levels
                and not sig.persist
            ):
                continue
            by_id.setdefault(sig.can_id, []).append(sig)
        return cls(_by_can_id=by_id)

    @classmethod
    def from_path(
        cls,
        bindings_path: Path,
        *,
        confidence_levels: frozenset[str] | set[str] | tuple[str, ...] | None = None,
    ) -> FrameDecoder | None:
        """Load + index, or ``None`` when the bindings file is absent.

        Absent bindings is a working configuration (fresh checkout, foreign
        vehicle profile) — the caller just gets no decoded persistence.
        """
        if not bindings_path.exists():
            return None
        return cls.from_bindings(load_bindings(bindings_path), confidence_levels=confidence_levels)

    @property
    def signal_count(self) -> int:
        return sum(len(sigs) for sigs in self._by_can_id.values())

    def decode(self, record: TelemetryRecord) -> tuple[TelemetryRecord, ...]:
        """Decoded rows for one raw CAN record (empty for anything else).

        Skips signals whose byte range overshoots this frame's payload —
        same tolerance as ``decode_can_rows`` (which emits NaN there; a NaN
        row is useless in storage, so here it is simply omitted).
        """
        if record.source != "can" or record.payload is None:
            return ()
        can_id = parse_can_channel(record.channel)
        if can_id is None:
            return ()
        signals = self._by_can_id.get(can_id)
        if not signals:
            return ()
        payload = bytes(record.payload)
        out: list[TelemetryRecord] = []
        for sig in signals:
            raw = decode_raw(sig, payload)
            if raw is None:
                continue
            out.append(
                TelemetryRecord.decoded(
                    record.timestamp,
                    sig.name,
                    float(raw),
                    sig.unit,
                    metadata={"can_id": can_id, "binding_confidence": sig.confidence},
                )
            )
        return tuple(out)


__all__ = ["FrameDecoder"]
