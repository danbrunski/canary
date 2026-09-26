"""Decode raw CAN frames into ``decoded:*`` TelemetryRecords, one frame at a time.

The live path's decoder: :class:`src.preprocessing.live.LiveFeatureSource`
holds a :class:`FrameDecoder` over *every* binding and publishes exactly these
records to the dashboard stream and its own current-value dict (R04.4). Until
P02.8 (operator decision 2026-09-26) the storage sink had one too and stored
its records beside the raw frames; nothing stores ``decoded`` rows now. The
session readers decode the stored frames on read instead
(:func:`src.preprocessing.decode.decode_on_read`), by this module's rule, so a
record the dashboard showed live and the row a reader decodes later agree:

* values are the RAW integer from the binding's byte range, NOT the scaled
  engineering value — training's scaler was fit on raw values, and mixing
  scaled and raw rows for the same channel across sessions would corrupt
  feature normalization. Use :func:`src.decode.bindings.apply_scaling` when
  presenting to humans.
* metadata carries ``{"can_id", "binding_confidence"}``, the keys the batch
  decode (:func:`src.preprocessing.decode.decode_can_rows`) writes too.
* the unit is :attr:`~src.decode.bindings.Signal.display_unit` (``""`` for
  an ``"unknown"`` binding), the same string the batch decode writes.

Sentinels (A04.6): a binding's declared ``sentinel_raw`` word is a state, not
a number. It decodes to a ``decoded:<NAME>_SENTINEL`` row whose value is the
binding's ``sentinel_meaning`` text, and to *no* ``decoded:<NAME>`` row, so
the word never reaches a scaler, a mean or a model input. "Decoded, and the
sensor said it has nothing" stays distinguishable from "not decoded". Every
other raw value decodes exactly as before.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from src.can_ids import parse_can_channel
from src.decode.bindings import Bindings, Signal, decode_raw, load_bindings
from src.decode.slices import sentinel_channel
from src.telemetry import TelemetryRecord


@dataclass
class FrameDecoder:
    """Pre-indexed binding decoder for single raw CAN records."""

    _by_can_id: dict[int, list[Signal]] = field(default_factory=dict)

    @classmethod
    def from_bindings(cls, bindings: Bindings) -> FrameDecoder:
        """Index every signal of ``bindings`` by CAN ID (the live path decodes
        every binding, whatever its confidence)."""
        by_id: dict[int, list[Signal]] = {}
        for sig in bindings.signals:
            by_id.setdefault(sig.can_id, []).append(sig)
        return cls(_by_can_id=by_id)

    @classmethod
    def from_path(cls, bindings_path: Path) -> FrameDecoder | None:
        """Load + index, or ``None`` when the bindings file is absent.

        Absent bindings is a working configuration (fresh checkout, foreign
        vehicle profile) — the caller just gets no decoded records.
        """
        if not bindings_path.exists():
            return None
        return cls.from_bindings(load_bindings(bindings_path))

    @property
    def signal_count(self) -> int:
        return sum(len(sigs) for sigs in self._by_can_id.values())

    def decode(self, record: TelemetryRecord) -> tuple[TelemetryRecord, ...]:
        """Decoded rows for one raw CAN record (empty for anything else).

        Skips signals whose byte range overshoots this frame's payload —
        same tolerance as ``decode_can_rows`` (which emits NaN there; a NaN
        record is useless, so here it is simply omitted, as
        ``decode_on_read`` omits the row).
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
            if sig.is_sentinel(raw):
                out.append(sentinel_record(record, sig, can_id))
                continue
            out.append(
                TelemetryRecord.decoded(
                    record.timestamp,
                    sig.name,
                    float(raw),
                    sig.display_unit,
                    metadata={"can_id": can_id, "binding_confidence": sig.confidence},
                )
            )
        return tuple(out)


def sentinel_record(record: TelemetryRecord, signal: Signal, can_id: int) -> TelemetryRecord:
    """The ``decoded:<NAME>_SENTINEL`` state row for ``signal``'s sentinel word."""
    return TelemetryRecord.decoded(
        record.timestamp,
        sentinel_channel(signal.name),
        signal.sentinel_meaning or "sentinel",
        "",
        metadata={"can_id": can_id, "binding_confidence": signal.confidence},
    )


__all__ = ["FrameDecoder", "sentinel_record"]
