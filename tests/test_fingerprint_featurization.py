"""R05.5 — fingerprints are computed on the frame the models serve on.

``compute_fingerprint`` used to hand-chain a subset of the feature pipeline
(anomaly signals, committees, deviations) and skipped the AFR and
instantaneous channels, so its deviation statistics described a frame no model
scores. It now runs ``featurize_wide_frame(...).operating_frame`` and the
fingerprint version moved, so every 0.1.0 sidecar is recomputed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from src.analysis import fingerprint
from src.config import CanaryConfig
from src.preprocessing import feature_pipeline
from src.storage.parquet_writer import write_records_to_parquet
from src.storage.session_products import FINGERPRINT_SCHEMA_VERSION, SessionFingerprint
from src.telemetry import TelemetryRecord
from tests.configs import example_config


def _idle_session(root: Path) -> Path:
    base = 1_779_400_000.0
    records: list[TelemetryRecord] = []
    for i in range(40):
        t = base + i
        records += [
            TelemetryRecord.obd(t, "RPM", 800.0, "rpm"),
            TelemetryRecord.obd(t, "SPEED", 0.0, "km/h"),
            TelemetryRecord.obd(t, "THROTTLE_POS", 0.0, "%"),
            TelemetryRecord.obd(t, "COOLANT_TEMP", 90.0, "C"),
            TelemetryRecord.obd(t, "MAF", 4.0, "g/s"),
            TelemetryRecord.obd(t, "SHORT_FUEL_TRIM_1", 1.0, "%"),
            TelemetryRecord.obd(t, "SHORT_FUEL_TRIM_2", -1.0, "%"),
        ]
    session = root / "2026-05-20T130000"
    write_records_to_parquet(records, session)
    return session


def test_fingerprint_runs_the_models_featurization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    real = feature_pipeline.featurize_wide_frame

    def spy(wide: pd.DataFrame, config: CanaryConfig, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return real(wide, config, **kwargs)

    monkeypatch.setattr(fingerprint, "featurize_wide_frame", spy)
    config = example_config(Path("canary.example.yaml"))
    stats = fingerprint.compute_fingerprint(_idle_session(tmp_path), config)
    assert len(calls) == 1
    assert "behavior_models" in calls[0]
    # The anomaly signals come from the pipeline, as before.
    assert stats["bank_balance_mean:stft"] == pytest.approx(2.0)


def test_the_version_moved_so_old_sidecars_are_recomputed() -> None:
    assert fingerprint.FINGERPRINT_VERSION == "0.2.0"
    old = SessionFingerprint(
        schema_version=FINGERPRINT_SCHEMA_VERSION,
        fingerprint_version="0.1.0",
        computed_at="2026-07-20T00:00:00Z",
        session_id="2026-05-18T021421",
        stats={},
    )
    assert fingerprint.is_stale(old)
