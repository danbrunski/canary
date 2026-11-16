"""The delivery-investigation drive and the hypothesis-tracking world (R30.2).

Moved out of the ``tests/test_*.py`` files that provided them, bodies unchanged; private helpers
got public names."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd
import pytest

from src.analysis.delivery_evidence import mint_delivery_error, mint_fuel_pressure_droop
from src.analysis.delivery_investigation import investigate
from src.analysis.fault_model import FAULT_MODEL_FILE, FaultModel, load_fault_model
from src.analysis.measurement_plan import (
    MEASUREMENT_PLAN_FILE,
    MeasurementPlan,
    load_measurement_plan,
)
from src.analysis.tuning_context import TuningContext
from src.collection.capture_profiles import CaptureProfileCatalog
from src.config import CanaryConfig
from src.evidence import MemoryLedger
from src.vehicle.profiles import profile_dir, resolve_profile_for_tooling, scope_config
from src.yaml_load import load_yaml

PROFILE = "nissan-xterra-2008"


REGIONS: tuple[Literal["high_airflow", "enrichment"], ...] = ("high_airflow", "enrichment")


@dataclass
class Vehicle:
    context: TuningContext
    model: FaultModel
    model_sha: str
    data: Path

    @property
    def floor_conditions(self) -> str:
        assert self.context.lambda_delivery is not None
        return self.context.lambda_delivery.noise_floors["enrichment"].conditions


class Clock:
    def __init__(self, start: datetime) -> None:
        self.t = start

    def at(self, start: datetime) -> None:
        self.t = max(self.t, start)

    def __call__(self) -> datetime:
        self.t += timedelta(seconds=1)
        return self.t


def delivery_mint_all(
    ledger: MemoryLedger, vehicle: Vehicle, session: Path, clock: Clock
) -> tuple[str, str, str]:
    """Delivery, closed-loop droop and enrichment flow loss for one drive."""
    delivery = mint_delivery_error(
        ledger, session, vehicle.context, vehicle.model, vehicle.model_sha,
        vin=None, program_id=None, now=clock(),
    )  # fmt: skip
    high, loss = (
        mint_fuel_pressure_droop(
            ledger,
            session,
            vehicle.context,
            vehicle.model,
            vehicle.model_sha,
            region=region,
            vin=None,
            program_id=None,
            now=clock(),
        )  # fmt: skip
        for region in REGIONS
    )
    return delivery, high, loss


def delivery_synthetic_drive(
    root: Path, name: str, start: datetime, *, lean_pct: float, pressure_drop_pct: float | None
) -> Path:
    """SYNTHETIC: idle, three cruise-then-WOT cycles, idle; Mode 01 at 4 Hz.

    At WOT the ECM commands phi 1.17 open loop and the measured lambda is
    ``lean_pct`` lean of it; pressure (when logged, 100 Hz) falls by
    ``pressure_drop_pct`` at WOT only.
    """
    rng = np.random.default_rng(7)
    segments: list[tuple[float, str]] = [(60.0, "idle")]
    for _ in range(3):
        segments += [(20.0, "cruise"), (10.0, "wot")]
    segments.append((40.0, "idle"))
    rows: list[dict[str, object]] = []
    t0 = start.timestamp()
    t = 0.0
    round_id = 0
    for duration, kind in segments:
        steps = int(duration * 4)
        for k in range(steps):
            stamp = t + k * 0.25
            frac = k / max(steps - 1, 1)
            if kind == "wot":
                rpm, maf, load, status, phi = 3000 + 2000 * frac, 90 + 50 * frac, 95.0, 4.0, 1.17
                lam = (1 / phi) * (1 + lean_pct / 100)
            else:
                rpm, maf, load, status, phi = (
                    (800.0, 5.0, 20.0, 2.0, 1.0)
                    if kind == "idle"
                    else (2000.0, 25.0, 40.0, 2.0, 1.0)
                )
                lam = 1.0 + float(rng.normal(0, 0.003))
            round_id += 1
            meta = json.dumps(
                {"acquired_at": t0 + stamp, "acquired_clock": "kernel", "round_id": round_id,
                 "ts_schema": 2}
            )  # fmt: skip
            for channel, value, unit in (
                ("RPM", rpm, "rpm"),
                ("MAF", maf, "g/s"),
                ("ENGINE_LOAD", load, "%"),
                ("FUEL_STATUS", status, "bitmask"),
                ("COMMANDED_EQUIV_RATIO", phi, "ratio"),
                ("O2_WR_LAMBDA_S1", lam, "ratio"),
            ):
                rows.append(
                    {"t": t0 + stamp, "source": "obd", "channel": channel, "value": value,
                     "unit": unit, "meta": meta}
                )  # fmt: skip
        if pressure_drop_pct is not None:
            for k in range(int(duration * 100)):
                level = 350.0 * (1 - pressure_drop_pct / 100) if kind == "wot" else 350.0
                rows.append(
                    {"t": t0 + t + k * 0.01, "source": "sensor", "channel": "FUEL_PRESSURE",
                     "value": level + float(rng.normal(0, 0.5)), "unit": "kPa", "meta": "{}"}
                )  # fmt: skip
        t += duration
    frame = pd.DataFrame(rows).sort_values("t", kind="stable")
    out = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(frame["t"], unit="s", utc=True),
            "source": frame["source"],
            "channel": frame["channel"],
            "value_number": frame["value"].astype(float),
            "value_text": pd.Series([None] * len(frame), dtype=object),
            "value_bool": pd.Series([None] * len(frame), dtype=object),
            "unit": frame["unit"],
            "payload": [b""] * len(frame),
            "metadata_json": frame["meta"],
        }
    )
    session = root / name
    session.mkdir(parents=True)
    out.to_parquet(session / "telemetry-00000.parquet", index=False)
    return session


PRESSURE = "Log fuel pressure through a full-throttle pull (supercharger-build NA2)."


BUDGET = 8.0


@dataclass
class World:
    vehicle: Vehicle
    plan: MeasurementPlan
    catalog: CaptureProfileCatalog
    ledger: MemoryLedger
    drive_a: tuple[str, str, str]
    drive_b: tuple[str, str, str]
    drive_c: tuple[str, str, str]
    session_a: Path
    session_b: Path
    opened: str


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> World:
    base = CanaryConfig.from_yaml(Path("canary.dev.example.yaml"))
    config = scope_config(base, resolve_profile_for_tooling(base, PROFILE))
    data = profile_dir(config)
    model, sha = load_fault_model(data / FAULT_MODEL_FILE)
    context = TuningContext.from_config(config)
    vehicle = Vehicle(context, model, sha, data)
    plan, _sha = load_measurement_plan(
        data / MEASUREMENT_PLAN_FILE, model, mode01_wot=context.mode01_wot
    )
    catalog = CaptureProfileCatalog.model_validate(
        load_yaml((data / "capture_profiles.yaml").read_text())
    )
    root = tmp_path_factory.mktemp("tracking")
    a = delivery_synthetic_drive(
        root, "_synthetic-wot-lean-2026-09-01T120000", datetime(2026, 9, 1, 12, tzinfo=UTC),
        lean_pct=4.0, pressure_drop_pct=None,
    )  # fmt: skip
    b = delivery_synthetic_drive(
        root, "_synthetic-wot-lean-2026-11-01T120000", datetime(2026, 11, 1, 12, tzinfo=UTC),
        lean_pct=4.0, pressure_drop_pct=0.2,
    )  # fmt: skip
    c = delivery_synthetic_drive(
        root, "_synthetic-wot-on-command-2026-11-08T120000",
        datetime(2026, 11, 8, 12, tzinfo=UTC), lean_pct=0.0, pressure_drop_pct=None,
    )  # fmt: skip
    ledger = MemoryLedger(root / "memory.jsonl", vehicle=PROFILE)
    clock = Clock(datetime(2026, 9, 1, 13, tzinfo=UTC))
    drive_a = delivery_mint_all(ledger, vehicle, a, clock)
    opened = investigate(
        ledger, model, delivery_evidence=drive_a[0], other_evidence=drive_a[1:], now=clock()
    )
    assert opened.investigation is not None
    clock.at(datetime(2026, 11, 1, 13, tzinfo=UTC))
    drive_b = delivery_mint_all(ledger, vehicle, b, clock)
    clock.at(datetime(2026, 11, 8, 13, tzinfo=UTC))
    drive_c = delivery_mint_all(ledger, vehicle, c, clock)
    return World(
        vehicle, plan, catalog, ledger, drive_a, drive_b, drive_c, a, b, opened.investigation
    )


def tracking_rows(session: Path) -> list[tuple[float, str, float]]:
    frame = pd.read_parquet(session)
    t = frame["timestamp"].astype("int64").to_numpy() / 1e9
    specs = (frame["source"] + ":" + frame["channel"]).to_numpy()
    values = frame["value_number"].to_numpy()
    return sorted(zip(t.tolist(), specs.tolist(), values.tolist(), strict=True))
