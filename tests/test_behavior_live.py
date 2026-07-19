"""Live behavior overlay: state object, daemon capture, and the endpoint.

Covers the Models▸Behavior "you are here" feed end to end —
:class:`BehaviorLiveState` bookkeeping, the daemon's ``_observe_behavior``
deviation/severity/mode-gating math, and ``GET /models/behavior/live``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src.config import CanaryConfig
from src.daemon import _observe_behavior
from src.dashboard.api import create_app
from src.dashboard.behavior_live import (
    TRAIL_MAXLEN,
    BehaviorLiveState,
    BehaviorObservation,
)
from src.decode.behavior_models import (
    Axis1D,
    BehaviorModel1D,
    BehaviorModels,
    DeviationConfig,
    PolynomialFit,
)


def _cfg_yaml(storage: Path) -> str:
    return f"""
vehicle: {{make: Nissan, model: Xterra, year: 2008, vin: ""}}
hardware:
  elm327_port: /dev/ttyUSB0
  can: {{interface: virtual, channel: vcan0, bitrate: 500000}}
collection:
  obd_poll_rate_hz: 1
  storage_path: {storage}
  session_retention_days: 30
  storage_warning_mb: 500
preprocessing:
  resample_hz: 1
  rolling_window_seconds: 30
  operating_modes: {{idle: {{rpm_max: 900, speed_max: 0}}}}
models:
  isolation_forest: {{enabled: true, n_estimators: 100, contamination: 0.01}}
  lstm_autoencoder: {{enabled: false, sequence_length: 60, latent_dim: 16, threshold_k: 3.0}}
dashboard: {{host: 127.0.0.1, port: 8000}}
"""


@pytest.fixture
def config(tmp_path: Path) -> CanaryConfig:
    storage = tmp_path / "storage"
    storage.mkdir()
    cfg_path = tmp_path / "canary.yaml"
    cfg_path.write_text(_cfg_yaml(storage))
    return CanaryConfig.from_yaml(cfg_path)


def _obs(name: str, dev: float, sev: str) -> BehaviorObservation:
    return BehaviorObservation(
        name=name,
        kind="surface_1d",
        x=10.0,
        y=None,
        actual=50.0,
        expected=50.0 - dev,
        deviation=dev,
        severity=sev,
        mode_match=True,
    )


# ---------------------------------------------------------------------------
# BehaviorLiveState
# ---------------------------------------------------------------------------


def test_state_tracks_latest_and_appends_trail() -> None:
    state = BehaviorLiveState()
    state.observe("t0", "idle", [_obs("warmup", 1.0, "ok")])
    state.observe("t1", "cruise", [_obs("warmup", 6.0, "warning")])

    snap = state.snapshot()
    assert snap["observed"] == 2
    assert snap["timestamp"] == "t1"
    assert snap["operating_mode"] == "cruise"
    model = snap["models"]["warmup"]
    assert model["deviation"] == 6.0
    assert model["severity"] == "warning"
    assert [p["deviation"] for p in model["series"]] == [1.0, 6.0]


def test_trail_is_bounded() -> None:
    state = BehaviorLiveState(trail_maxlen=3)
    for i in range(10):
        state.observe(f"t{i}", "idle", [_obs("warmup", float(i), "ok")])
    series = state.snapshot()["models"]["warmup"]["series"]
    assert len(series) == 3
    assert [p["deviation"] for p in series] == [7.0, 8.0, 9.0]


def test_default_trail_maxlen_is_used() -> None:
    state = BehaviorLiveState()
    for i in range(TRAIL_MAXLEN + 50):
        state.observe(f"t{i}", None, [_obs("m", float(i), "ok")])
    assert len(state.snapshot()["models"]["m"]["series"]) == TRAIL_MAXLEN


def test_snapshot_include_series_false_omits_trail_but_keeps_latest() -> None:
    state = BehaviorLiveState()
    state.observe("t0", "idle", [_obs("warmup", 1.0, "ok")])
    state.observe("t1", "cruise", [_obs("warmup", 6.0, "warning")])

    snap = state.snapshot(include_series=False)
    model = snap["models"]["warmup"]
    assert "series" not in model
    assert model["deviation"] == 6.0
    assert model["severity"] == "warning"
    assert snap["observed"] == 2  # top-level fields unaffected


def test_snapshot_include_series_true_is_the_default() -> None:
    state = BehaviorLiveState()
    state.observe("t0", "idle", [_obs("warmup", 1.0, "ok")])
    assert "series" in state.snapshot()["models"]["warmup"]
    assert "series" in state.snapshot(include_series=True)["models"]["warmup"]


# ---------------------------------------------------------------------------
# daemon._observe_behavior
# ---------------------------------------------------------------------------


def _poly_model(operating_mode: str | None = None) -> BehaviorModel1D:
    # y = 25 + 0.15*x  → at x=100, expected = 40.0
    return BehaviorModel1D(
        name="coolant_warmup",
        kind="surface_1d",
        output="obd:COOLANT_TEMP",
        output_unit="degC",
        axis=Axis1D(name="t", channel="obd:RUN_TIME", domain=(0.0, 600.0)),
        operating_mode=operating_mode,
        fit=PolynomialFit(
            kind="poly",
            coefficients=[25.0, 0.15],
            sample_count=100,
            r_squared=0.9,
            fitted_at="now",
        ),
        deviation=DeviationConfig(kind="absolute", warning_band=5.0, critical_band=15.0),
    )


def _source(model: BehaviorModel1D, point: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(
        behavior_models=BehaviorModels(schema_version=1, models=[model]),
        latest_operating_point=point,
    )


def test_observe_behavior_1d_grades_deviation() -> None:
    state = BehaviorLiveState()
    point = {
        "timestamp": pd.Timestamp("2026-05-17T00:00:00Z"),
        "operating_mode": "idle",
        "obd:RUN_TIME": 100.0,
        "obd:COOLANT_TEMP": 50.0,  # expected 40 → Δ +10 → warning (5..15)
    }
    _observe_behavior(_source(_poly_model(), point), state)

    m = state.snapshot()["models"]["coolant_warmup"]
    assert m["x"] == 100.0
    assert m["y"] is None
    assert m["actual"] == 50.0
    assert m["expected"] == pytest.approx(40.0)
    assert m["deviation"] == pytest.approx(10.0)
    assert m["severity"] == "warning"
    assert m["mode_match"] is True


def test_observe_behavior_mode_gated_does_not_speak() -> None:
    state = BehaviorLiveState()
    point = {
        "timestamp": pd.Timestamp("2026-05-17T00:00:00Z"),
        "operating_mode": "cruise",  # model only evaluates in "idle"
        "obd:RUN_TIME": 100.0,
        "obd:COOLANT_TEMP": 50.0,
    }
    _observe_behavior(_source(_poly_model(operating_mode="idle"), point), state)

    m = state.snapshot()["models"]["coolant_warmup"]
    assert m["expected"] is None
    assert m["deviation"] is None
    assert m["mode_match"] is False


def test_observe_behavior_noop_without_models_or_point() -> None:
    state = BehaviorLiveState()
    _observe_behavior(SimpleNamespace(behavior_models=None, latest_operating_point={}), state)
    _observe_behavior(
        SimpleNamespace(
            behavior_models=BehaviorModels(schema_version=1, models=[_poly_model()]),
            latest_operating_point=None,
        ),
        state,
    )
    assert state.snapshot()["observed"] == 0


# ---------------------------------------------------------------------------
# GET /models/behavior/live
# ---------------------------------------------------------------------------


def test_endpoint_empty_when_no_state(config: CanaryConfig, tmp_path: Path) -> None:
    app = create_app(config, anomalies_dir=tmp_path / "anom")
    r = TestClient(app).get("/models/behavior/live")
    assert r.status_code == 200
    assert r.json() == {
        "observed": 0,
        "timestamp": None,
        "operating_mode": None,
        "models": {},
    }


def test_endpoint_serves_snapshot(config: CanaryConfig, tmp_path: Path) -> None:
    state = BehaviorLiveState()
    state.observe("t0", "idle", [_obs("warmup", 2.0, "ok")])
    app = create_app(config, anomalies_dir=tmp_path / "anom", behavior_live=state)
    r = TestClient(app).get("/models/behavior/live")
    assert r.status_code == 200
    body = r.json()
    assert body["operating_mode"] == "idle"
    assert body["models"]["warmup"]["deviation"] == 2.0
    assert "series" in body["models"]["warmup"]  # default: series included


def test_endpoint_series_false_omits_trail(config: CanaryConfig, tmp_path: Path) -> None:
    state = BehaviorLiveState()
    state.observe("t0", "idle", [_obs("warmup", 2.0, "ok")])
    app = create_app(config, anomalies_dir=tmp_path / "anom", behavior_live=state)
    r = TestClient(app).get("/models/behavior/live", params={"series": "false"})
    assert r.status_code == 200
    body = r.json()
    assert body["models"]["warmup"]["deviation"] == 2.0
    assert "series" not in body["models"]["warmup"]
