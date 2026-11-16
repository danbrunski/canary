"""The live twin's fake resolver and drive builders (R30.2).

Apart from tests/twin_builders.py because these import src.daemon.twin_live, which pulls in
can: the imports nothing that talks to the ECU tests import tests.twin_builders in a
subprocess and then inspect sys.modules.
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import Executor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from src.analysis.target_phi import ResolvedTable, TwinRefusalError
from src.collection.replay import _metadata, _value
from src.control.twin_identity import IdentitySegment, TwinIdentity
from src.daemon.inference_metrics import InferenceTimingRecorder
from src.daemon.twin_live import LiveTwin, _InlineExecutor
from src.decode.dids import load_did_map
from src.paths import DIDS_YAML
from src.telemetry import TelemetryRecord
from tests.target_phi_builders import EXPECT, IMAGE_SHA, phi_definition, phi_table

CATALOG = load_did_map(DIDS_YAML)


REVISION = EXPECT["calibration"]["revision"]


IDENTITY = TwinIdentity(REVISION, IMAGE_SHA, "registry", False, "known", "program")


class FakeResolver:
    """The shared resolver's contract with injected segments and tables.

    ``segments`` maps the asked instant to segments; ``table`` maps an
    identity to its default-slot table (or raises its refusal). Every other
    slot refuses ``table_unverified``, as the real profile's slots 2-5 do."""

    def __init__(
        self,
        table: ResolvedTable | None = None,
        *,
        segments: Any = None,
        table_for: Any = None,
    ) -> None:
        self.definition, self.definition_sha256 = phi_definition()
        chosen = table or phi_table(self.definition)
        self._segments = segments or (lambda _at: [IdentitySegment(None, None, IDENTITY)])
        self._table_for = table_for or (lambda _identity: chosen)
        self.asked: list[float | None] = []

    def segments(self, session: Path | None, *, first_read: float | None) -> list[Any]:
        self.asked.append(first_read)
        return list(self._segments(first_read))

    def table(self, identity: TwinIdentity, slot: int) -> ResolvedTable:
        if slot != self.definition.map_identity.default_slot.slot:
            raise TwinRefusalError("table_unverified", f"slot {slot} is inferred")
        return self._table_for(identity)


def live_twin(
    table: ResolvedTable | None = None,
    *,
    resolver: FakeResolver | None = None,
    recorder: InferenceTimingRecorder | None = None,
    executor: Executor | None = None,
    session: str = "s",
    capacity: int | None = None,
    recheck_s: float = 60.0,
    by_segment: dict[int, list[dict[str, Any]]] | None = None,
) -> tuple[LiveTwin, list[dict[str, Any]]]:
    """A live twin on a fake resolver, preloaded; returns it and the list its
    default-slot records land in (``by_segment`` splits them per segment)."""
    chosen = resolver or FakeResolver(table)
    out: list[dict[str, Any]] = []

    def sink(segment: int, slot: int, records: list[dict[str, Any]]) -> None:
        if slot == chosen.definition.map_identity.default_slot.slot:
            out.extend(records)
            if by_segment is not None:
                by_segment.setdefault(segment, []).extend(records)

    kwargs: dict[str, Any] = {}
    if capacity is not None:
        kwargs["intake_capacity"] = capacity
    twin = LiveTwin(
        resolver=lambda: chosen,
        recorder=recorder,
        did_map=CATALOG,
        catalog=CATALOG,
        session=session,
        executor=executor or _InlineExecutor(),
        identity_recheck_s=recheck_s,
        on_records=sink,
        **kwargs,
    )
    twin.tick(session=session, vehicle_state=None)  # the worker preloads
    return twin, out


def live_drive(
    twin: LiveTwin,
    records: Iterator[TelemetryRecord] | list[TelemetryRecord],
    *,
    tick_s: float = 1.0,
    session: str = "s",
    flush: bool = True,
) -> None:
    """Records into the intake, one tick per ``tick_s`` of stored time."""
    next_tick: datetime | None = None
    for record in records:
        if next_tick is None:
            next_tick = record.timestamp + timedelta(seconds=tick_s)
        while record.timestamp >= next_tick:
            twin.tick(session=session, vehicle_state="engine_running")
            next_tick += timedelta(seconds=tick_s)
        twin.intake.observe(record)
    twin.tick(session=session, vehicle_state="engine_running")
    if flush:
        twin.flush(session=session)


def lambda_live_records(session: pd.DataFrame) -> Iterator[TelemetryRecord]:
    """The stored polled rows as the collector's records, in stored order."""
    stamps = pd.to_datetime(session["timestamp"], utc=True)
    for (_, row), stamp in zip(session.iterrows(), stamps, strict=True):
        unit = row.get("unit")
        yield TelemetryRecord(
            timestamp=stamp.to_pydatetime(),
            source=str(row["source"]),  # type: ignore[arg-type]
            channel=str(row["channel"]),
            value=_value(row),
            unit="" if unit is None or pd.isna(unit) else str(unit),
            metadata=_metadata(row),
        )
