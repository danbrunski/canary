"""The target-phi twin's fixtures: the profile and its ground-truth expectations, stored
sessions, resolved tables and viewers (R30.2).

Moved out of the ``tests/test_*.py`` files that provided them, bodies unchanged; private helpers
got public names."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pandas as pd

from src.analysis.ecu_application import ApplicationModel, load_application_model
from src.analysis.rom_trace import ViewerDefinition, load_viewer_definition
from src.analysis.target_phi import (
    ResolvedTable,
    TwinDefinition,
    evaluate_session,
    load_twin_definition,
    table_from_calibration,
)
from src.paths import REPO_ROOT
from src.signals.afr import load_afr_calibration
from src.yaml_load import load_yaml

REPO = REPO_ROOT


PROFILE = REPO / "data" / "nissan-xterra-2008"


EXPECT_PATH = REPO / "tests" / "fixtures" / "ground_truth" / "target_phi.yaml"


EXPECT: dict[str, Any] = load_yaml(EXPECT_PATH.read_text())


IMAGE_SHA = EXPECT["calibration"]["image_sha256"]


def phi_definition() -> tuple[TwinDefinition, str]:
    return load_twin_definition(PROFILE)


def phi_viewer() -> ViewerDefinition:
    return load_viewer_definition(PROFILE)


def phi_model() -> tuple[ApplicationModel, str]:
    return load_application_model(PROFILE)


def phi_table(definition: TwinDefinition) -> ResolvedTable:
    calibration = load_afr_calibration(PROFILE / "afr_calibration.yaml")
    return table_from_calibration(
        calibration,
        definition,
        viewer=phi_viewer(),
        application_model=phi_model(),
        revision=EXPECT["calibration"]["revision"],
        image_sha256=IMAGE_SHA,
        slot=EXPECT["calibration"]["slot"],
    )


def fixture_frame() -> pd.DataFrame:
    """The two committed slices as one session frame, after their hashes pass."""
    frames = []
    for part in EXPECT["telemetry"]:
        path = REPO / part["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == part["sha256"], part["path"]
        frame = pd.read_parquet(path)
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        frames.append(frame)
    frame = pd.concat(frames, ignore_index=True)
    return frame.sort_values("timestamp", kind="stable").reset_index(drop=True)


def record_lines(result: dict[str, Any]) -> list[str]:
    lines = []
    for record in result["records"]:
        sample = record["sample"]
        stamp = pd.Timestamp(sample["acquired_at"]).strftime("%H:%M:%S.%f")
        word = "cut" if sample["observed_word"] is None else sample["observed_word"]
        lines.append(f"{stamp} {word} {record['state']} {record['reason']}")
    return lines


T0 = pd.Timestamp("2026-10-04T21:00:00Z")


X_SCALE = 0.250859


def phi_row(
    t: float, channel: str, value: float | None, *, text: str | None = None, basis: str = "acquired"
) -> dict[str, Any]:
    stamp = T0 + pd.Timedelta(seconds=t)
    meta: dict[str, Any] = {"round_id": 1}
    if basis == "acquired":
        meta["acquired_at"] = stamp.timestamp()
    return {
        "timestamp": stamp,
        "source": "obd",
        "channel": channel,
        "value_number": value,
        "value_text": text,
        "unit": "",
        "metadata_json": json.dumps(meta),
    }


def phi_session(
    *,
    t: float = 30.0,
    load: int = 100,
    rpm_word: int = 236,
    flag: int = 1,
    word: int | None = 19072,
    load_at: float | None = 0.0,
    flag_at: float = 0.12,
    extra_load_at: float | None = None,
    magic: int = 0xAA5555AA,
    active: int = 0,
    active_after: int | None = None,
    selector: int = 0,
    coolant: float | None = 90.0,
    fuel_status: float = 4.0,
    fuel_status_after: float | None = None,
    rpm_span: float = 50.0,
    target_basis: str = "acquired",
    end: float = 60.0,
) -> pd.DataFrame:
    """One command sample at ``t`` with its tuple (all within 120 ms by
    default), identity reads every 5 s (one lands exactly on ``t = 30``),
    and 1-2 Hz gate channels over ``0..end`` seconds."""
    rows = []
    if word is None:
        rows.append(phi_row(t, "mem:ECM_TARGET_EQUIV_RATIO_SENTINEL", None, text="fuel cut"))
    else:
        rows.append(
            phi_row(t, "mem:ECM_TARGET_EQUIV_RATIO", word * 6.103515625e-05, basis=target_basis)
        )
    if load_at is not None:
        rows.append(phi_row(t + load_at, "mem:ECM_FUEL_TARGET_MAP_X_TRACE", load * X_SCALE))
    if extra_load_at is not None:
        rows.append(phi_row(t + extra_load_at, "mem:ECM_FUEL_TARGET_MAP_X_TRACE", load * X_SCALE))
    rows.append(phi_row(t + 0.06, "mem:ECM_ENGINE_SPEED_RAM", rpm_word * 12.5))
    rows.append(phi_row(t + flag_at, "mem:ECM_PEDAL_ENRICH_FLAGS", float(flag)))
    s = 0.0
    while s <= end:
        slot = active if active_after is None or s <= t else active_after
        rows += [
            phi_row(s, "mem:ECM_UPREV_MAGIC", float(magic)),
            phi_row(s, "mem:ECM_ACTIVE_MAP", float(slot)),
            phi_row(s, "mem:ECM_UPREV_MAP_SELECTOR", float(selector)),
        ]
        s += 5.0
    for k in range(int(end) + 1):
        if coolant is not None:
            rows.append(phi_row(k, "COOLANT_TEMP", coolant))
        for half in (0.0, 0.5):
            status = fuel_status
            if fuel_status_after is not None and k + half > t:
                status = fuel_status_after
            rows.append(phi_row(k + half, "FUEL_STATUS", status))
        rpm = 2950.0 + (rpm_span if k % 2 else 0.0)
        rows.append(phi_row(k + 0.2, "RPM", rpm))
        rows.append(phi_row(k + 0.7, "RPM", rpm))
        rows.append(phi_row(k + 0.3, "MAF", 120.0))
        rows.append(phi_row(k + 0.8, "MAF", 121.0))
    frame = pd.DataFrame(rows)
    return frame.sort_values("timestamp", kind="stable").reset_index(drop=True)


def phi_seconds(frame: pd.DataFrame) -> pd.Series:
    return (frame["timestamp"] - T0).dt.total_seconds()


def _without(frame: pd.DataFrame, channel: str, start: float, end: float) -> pd.DataFrame:
    """``frame`` minus ``channel``'s rows in ``[start, end]`` seconds."""
    seconds = phi_seconds(frame)
    drop = (frame["channel"] == channel) & (seconds >= start) & (seconds <= end)
    return frame[~drop].reset_index(drop=True)


def _until(frame: pd.DataFrame, end: float) -> pd.DataFrame:
    """A capture still recording: nothing after ``end`` seconds yet."""
    return frame[phi_seconds(frame) <= end].reset_index(drop=True)


def hysteresis_table() -> ResolvedTable:
    """Row 0 runs 128 -> 132 over the first load span, so load 0x38 at the
    first rpm breakpoint looks up 129: inside the gate's [129, 130) band."""
    definition, _ = phi_definition()
    base = phi_table(definition)
    return ResolvedTable(
        **{**base.__dict__, "cells": tuple(tuple([128, 132] + [128] * 6) for _ in range(8))}
    )


def phi_evaluate(
    frame: pd.DataFrame, *, table: ResolvedTable | None = None, **kwargs: Any
) -> dict[str, Any]:
    definition, sha = phi_definition()
    return evaluate_session(
        frame, definition, table or phi_table(definition), definition_sha256=sha, **kwargs
    )


#: Every per-sample reason the evaluator can give, each reached below.
#: ``(session kwargs, frame edit, evaluate kwargs, state, reason)``.
NAMED_STATES: list[tuple[dict[str, Any], Any, dict[str, Any], str, str]] = [
    ({"word": None}, None, {}, "unevaluable", "sentinel"),
    ({"load_at": 0.4}, None, {}, "unevaluable", "input_stale:load"),
    ({"flag_at": 0.4}, None, {}, "unevaluable", "input_stale:flag"),
    ({"load_at": -0.2}, None, {}, "unevaluable", "input_stale:tuple_spread"),
    ({"load_at": None}, None, {}, "unevaluable", "input_not_captured:load"),
    ({"target_basis": "round"}, None, {}, "unevaluable", "unsupported_clock"),
    ({"magic": 0}, None, {}, "unevaluable", "map_uninitialised"),
    ({"selector": 2}, None, {}, "unevaluable", "slot_ambiguous"),
    ({"active": 2}, None, {}, "unevaluable", "slot_unresolved"),
    ({"active": 1, "active_after": 2}, None, {}, "unevaluable", "map_switching"),
    ({"t": 58.0, "end": 58.0}, None, {}, "unevaluable", "map_identity_unbracketed"),
    ({"coolant": None}, None, {}, "unevaluable", "gate_missing:coolant"),
    (
        {},
        lambda f: _without(f, "COOLANT_TEMP", 19.0, 41.0),
        {},
        "unevaluable",
        "gate_stale:coolant",
    ),
    ({}, lambda f: _without(f, "FUEL_STATUS", 0.0, 99.0), {}, "unevaluable", "gate_missing:mode"),
    ({}, lambda f: _without(f, "FUEL_STATUS", 24.0, 36.0), {}, "unevaluable", "gate_stale:mode"),
    ({"fuel_status": 3.0}, None, {}, "unevaluable", "mode_unknown"),
    (
        {},
        lambda f: _without(f, "RPM", 27.5, 30.0),
        {},
        "unevaluable",
        "steadiness_unobserved:obd:RPM",
    ),
    (
        {},
        lambda f: _without(f, "MAF", 27.5, 30.0),
        {},
        "unevaluable",
        "steadiness_unobserved:obd:MAF",
    ),
    ({"flag": 0, "load": 10}, None, {}, "unevaluable", "uncovered_region"),
    ({"rpm_word": 600}, None, {}, "unevaluable", "uncovered_region"),
    (
        {"flag": 0, "load": 0x38, "rpm_word": 96},
        None,
        {"table": "hysteresis"},
        "unevaluable",
        "enrichment_gate_state_unknown",
    ),
    ({"coolant": 60.0}, None, {}, "excluded", "cold"),
    ({"fuel_status": 2.0}, None, {}, "excluded", "closed_loop"),
    ({"fuel_status_after": 2.0}, None, {}, "excluded", "mode_transition"),
    ({"rpm_span": 400.0}, None, {}, "excluded", "transient"),
    (
        {"load_at": None, "extra_load_at": -5.0},
        lambda f: _until(f, 30.13),
        {"session_complete": False},
        "pending",
        "awaiting_input:load",
    ),
    (
        {},
        lambda f: _until(f, 30.13),
        {"session_complete": False},
        "pending",
        "awaiting_mode_read",
    ),
    (
        {},
        lambda f: _until(f, 30.13),
        {"session_complete": False},
        "pending",
        "awaiting_identity_read",
    ),
]
