"""Evidence-ledger fixtures: records, vehicles, conditions, operators, predictions and
investigations (R30.2).

Moved out of the ``tests/test_*.py`` files that provided them, bodies unchanged; private helpers
got public names."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from src.analysis.noise_floor import floor_reference
from src.evidence import (
    Conditions,
    Evidence,
    Investigation,
    InvestigationCard,
    MemoryLedger,
    NoiseFloorRef,
    Prediction,
    Provenance,
    Quality,
    Quantity,
    Result,
    SourceRef,
    VehicleIdentity,
)
from src.evidence.text import IDENTIFIER
from src.paths import DATA_DIR

T0 = datetime(2026, 9, 20, 18, 0, tzinfo=UTC)


HASH = "a" * 64


def memory_vehicle(profile: str = "truck") -> VehicleIdentity:
    return VehicleIdentity(profile=profile, vin=None, program_id=None, calibration="ZS44A")


def memory_conditions(regime: str = "WOT: throttle>=85, 3000-5000 rpm") -> Conditions:
    return Conditions(regime=regime, vehicle_state="engine_running")


def _tool() -> Provenance:
    return Provenance(
        producer="compare_channels", producer_kind="tool", version="1", code_version="c"
    )


def memory_operator() -> Provenance:
    return Provenance(producer="operator", producer_kind="operator", version="n/a")


def memory_evidence(**overrides: Any) -> Evidence:
    fields: dict[str, Any] = {
        "vehicle": memory_vehicle(),
        "provenance": _tool(),
        "conditions": memory_conditions(),
        "valid_from": T0,
        "valid_to": T0 + timedelta(minutes=5),
        "subject": "compare_channels:2026-09-20T180000:obd:RPM:can:ECM_RPM:WOT",
        "sources": (
            SourceRef(
                session="2026-09-20T180000",
                window_start=T0,
                window_end=T0 + timedelta(minutes=5),
                channels=("obd:RPM", "can:ECM_RPM"),
                n_samples=300,
            ),
        ),
        "input_hashes": {"pairs": HASH},
        "policy": {"pairing": {"max_separation_s": 0.05}},
        "values": {"difference_bias": Quantity(value=0.4, unit="rpm")},
        "quality": Quality(n_observations=300, timestamp_basis="acquired"),
        "result": Result(status="evaluated", residual=Quantity(value=0.4, unit="rpm")),
    }
    fields.update(overrides)
    return Evidence(**fields)


def cards_investigation(**overrides: Any) -> Investigation:
    fields: dict[str, Any] = {
        "vehicle": memory_vehicle(),
        "provenance": memory_operator(),
        "conditions": memory_conditions(),
        "valid_from": T0,
        "title": "RPM channels disagree at WOT",
        "question": "is the broadcast RPM decoded with the wrong scale",
    }
    fields.update(overrides)
    return Investigation(**fields)


def allowed_tokens(card: InvestigationCard, ledger: MemoryLedger) -> set[str]:
    """Every digit-bearing token a card may print: record values (as the
    template formats them), record ids, times, sessions, channels, counts and
    the tools' own detail text — all read from records."""
    allowed: set[str] = set()

    def add(text: str) -> None:
        allowed.update(_tokens(text))

    for entry in ledger.entries():
        record = entry.record
        add(record.id)
        add(f"v{record.provenance.version}")
        for stamp in (record.valid_from, record.valid_to, record.recorded_at):
            if stamp is not None:
                add(stamp.isoformat())
        for name in ("subject", "title", "reason", "supersession_reason"):
            value = getattr(record, name, None)
            if isinstance(value, str):
                add(value)
        conditions = getattr(record, "conditions", None)
        if conditions is not None:
            add(conditions.regime)
        result = getattr(record, "result", None)
        if result is not None:
            add(result.detail)
        for source in getattr(record, "sources", ()):
            add(source.session)
            add(" ".join(source.channels))
            add(str(source.n_samples))
            add(source.window_start.isoformat())
            add(source.window_end.isoformat())
        for label in getattr(record, "labels", {}).values():
            add(label)
    payload = card.model_dump(mode="python")

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if "occurrences" in value:
                add(str(value["occurrences"]))
            if {"value", "unit", "record_id"} <= set(value):
                add(f"{value['value']:.6g}")
                if value.get("uncertainty") is not None:
                    add(f"{value['uncertainty']:.6g}")
                add(value["name"])
            for item in value.values():
                walk(item)
        elif isinstance(value, list | tuple):
            for item in value:
                walk(item)

    walk(payload)
    return allowed


def _tokens(text: str) -> list[str]:
    return [t.strip(".,;:()[]'\"") for t in re.split(r"\s+", text) if t.strip()]


def unresolved_numbers(text: str, allowed: set[str]) -> list[str]:
    return [
        token
        for token in _tokens(text)
        if any(ch.isdigit() for ch in token)
        and token not in allowed
        and not IDENTIFIER.fullmatch(token)
    ]


PROFILE = DATA_DIR / "nissan-xterra-2008"


WOT = "WOT: throttle>=85, 3000-5000 rpm, matched on rpm x MAF voltage, 50 ms grid"


FLASH = datetime(2026, 10, 4, 18, 0, tzinfo=UTC)


def interventions_floor() -> NoiseFloorRef:
    """The tracked profile's measured WOT delivery-error floor."""
    return floor_reference(PROFILE / "noise_floor.yaml", "delivery_error_pct", conditions=WOT)


def interventions_prediction(**overrides: Any) -> Prediction:
    fields: dict[str, Any] = {
        "vehicle": memory_vehicle(),
        "provenance": memory_operator(),
        "conditions": Conditions(regime=WOT, vehicle_state="engine_running"),
        "valid_from": FLASH,
        "investigation": None,
        "intervention": None,
        "quantity": "delivery_error_pct",
        "direction": "decrease",
        "magnitude": Quantity(value=3.0, unit="percent"),
        "noise_floor": interventions_floor(),
    }
    fields.update(overrides)
    return Prediction(**fields)


def retrieval_occurrence(session: str, start: datetime, **overrides: Any) -> Evidence:
    """One evidence record over ``session`` starting at ``start`` (one minute)."""
    fields: dict[str, Any] = {
        "valid_from": start,
        "valid_to": start + timedelta(minutes=1),
        "sources": (
            SourceRef(
                session=session,
                window_start=start,
                window_end=start + timedelta(minutes=1),
                channels=("obd:RPM", "can:ECM_RPM"),
                n_samples=60,
            ),
        ),
    }
    fields.update(overrides)
    return memory_evidence(**fields)
