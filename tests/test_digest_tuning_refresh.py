"""R05.16 — a stored digest's stale tuning section is recomputed, and only it.

Nothing re-reads a digest once it is written (A06.5), so a tuning section
computed by the 1 s last-cell delivery engine (schema 1) stayed on disk beside
the acquisition-paired one (schema 2). The refresh recomputes that section in
the digest's bounded child; the rest of the digest (whose anomaly window would
end at "now" on a whole recompute) is kept as written at trip end.
"""

from __future__ import annotations

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.analysis.digest import stale_tuning_version
from src.analysis.tuning_digest import TUNING_SCHEMA_VERSION
from src.config import CanaryConfig
from src.control import trip_end
from src.dashboard.api import create_app
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord
from tests.configs import isolated_config

SESSION_ID = "2026-09-21T223734"
CURRENT = {"schema_version": TUNING_SCHEMA_VERSION, "available": True, "delivery": {"n": 15}}


def _stored(tuning: Any) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "session_id": SESSION_ID,
        "anomaly_counts_by_source": {"lstm_autoencoder": 3},
        "trip": {"distance_km": 12.5},
        "tuning": tuning,
    }


@pytest.fixture(autouse=True)
def caps(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record the child's address-space cap instead of applying it. These tests
    run the child's work in-process (a thread, or inline), where a real
    ``setrlimit`` would cap the pytest worker: it then could not start a
    thread, and every later test on that worker hung or failed."""
    recorded: list[int] = []
    monkeypatch.setattr(trip_end, "limit_address_space", lambda n: recorded.append(n) or (n, n))
    return recorded


@pytest.fixture
def config(tmp_path: Path) -> CanaryConfig:
    return isolated_config(tmp_path)


@pytest.fixture
def session(config: CanaryConfig) -> Path:
    session = config.collection.storage_path / SESSION_ID
    records = [TelemetryRecord.obd(1_790_000_000.0 + i, "RPM", 800.0, "rpm") for i in range(5)]
    write_records_to_parquet(records, session, max_rows=1_000)
    return session


def _write(session: Path, payload: dict[str, Any]) -> None:
    (session / "digest.json").write_text(json.dumps(payload))


def _read(session: Path) -> dict[str, Any]:
    return json.loads((session / "digest.json").read_text())


@pytest.mark.parametrize(
    ("tuning", "expected"),
    [
        ({"available": True, "delivery": {"n": 16}}, 1),  # no schema_version: the 1 s engine
        ({"schema_version": 1, "available": True}, 1),
        (CURRENT, None),
        (None, None),  # an ordinary drive has no tuning section
        ({"available": False, "kind": "failed", "error": "boom"}, None),  # a failure, not stale
    ],
)
def test_which_sections_are_stale(tuning: Any, expected: int | None) -> None:
    assert stale_tuning_version(_stored(tuning)) == expected


def test_a_stale_section_is_recomputed_and_nothing_else_moves(
    config: CanaryConfig, session: Path, monkeypatch: pytest.MonkeyPatch, caps: list[int]
) -> None:
    monkeypatch.setattr(trip_end, "tuning_section", lambda _session, _config: dict(CURRENT))
    stored = _stored({"available": True, "delivery": {"n": 16}})
    _write(session, stored)

    with ThreadPoolExecutor(max_workers=1) as pool:
        served, note = asyncio.run(
            trip_end.refresh_stale_tuning_off_loop(session, config, stored, executor=pool)
        )

    assert note is None
    # The section is computed under the digest child's own ceiling.
    assert caps == [trip_end.digest_address_space_bytes(config)]
    assert served == _read(session)
    assert served["tuning"] == CURRENT
    assert served["tuning_refreshed"]["from_schema_version"] == 1
    assert served["tuning_refreshed"]["to_schema_version"] == TUNING_SCHEMA_VERSION
    for key in ("anomaly_counts_by_source", "trip", "session_id", "schema_version"):
        assert served[key] == stored[key]
    # Once current, a later read is served as stored, with no child.
    again, again_note = asyncio.run(trip_end.refresh_stale_tuning_off_loop(session, config, served))
    assert (again, again_note) == (served, None)


def test_a_recording_capture_defers_the_refresh_and_says_so(
    config: CanaryConfig, session: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from src.collection import tuning_profile

    monkeypatch.setattr(tuning_profile, "is_active", lambda: True)
    monkeypatch.setattr(tuning_profile, "active_profile", lambda: SimpleNamespace(name="tuning"))
    stored = _stored({"available": True})
    _write(session, stored)

    served, note = asyncio.run(trip_end.refresh_stale_tuning_off_loop(session, config, stored))

    assert served == stored == _read(session)
    assert note is not None
    assert (note["status"], note["reason"], note["profile"]) == (
        "deferred",
        "capture_active",
        "tuning",
    )
    assert note["stored_schema_version"] == 1


def test_a_failed_child_leaves_the_stored_digest_and_names_why(
    config: CanaryConfig, session: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def slow(_session: Path, _config: CanaryConfig) -> dict[str, Any]:
        time.sleep(1.0)
        return dict(CURRENT)

    monkeypatch.setattr(trip_end, "tuning_section", slow)
    stored = _stored({"available": True})
    _write(session, stored)

    with ThreadPoolExecutor(max_workers=1) as pool:
        served, note = asyncio.run(
            trip_end.refresh_stale_tuning_off_loop(
                session, config, stored, executor=pool, timeout=0.05
            )
        )

    assert served == stored == _read(session)
    assert note is not None
    assert (note["status"], note["reason"]) == ("failed", "child_timed_out")


def test_the_digest_route_serves_the_refreshed_section(
    config: CanaryConfig, session: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End to end through ``GET /control/sessions/{id}/digest``. The child is
    run in-process here so the patched section reaches it; the bounded spawn
    child itself is the trip-end digest's, already tested."""

    async def inline(call: Any, *_args: Any, **_kwargs: Any) -> Any:
        return call()

    monkeypatch.setattr(trip_end, "_in_digest_child", inline)
    monkeypatch.setattr(trip_end, "tuning_section", lambda _session, _config: dict(CURRENT))
    _write(session, _stored({"available": True, "delivery": {"n": 16}}))

    client = TestClient(
        create_app(config, anomalies_dir=tmp_path / "anomalies", control_token="secret")
    )
    response = client.get(
        f"/control/sessions/{SESSION_ID}/digest", headers={"Authorization": "Bearer secret"}
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["tuning"] == CURRENT
    assert "tuning_refresh" not in body
    assert body["tuning_refreshed"]["from_schema_version"] == 1
    assert _read(session)["tuning"] == CURRENT
