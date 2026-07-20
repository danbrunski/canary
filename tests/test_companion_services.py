"""Tests for the shared `/control/*` handlers + HTTP client in companion/services.py.

refactoring.md 1.2: these functions are the seam companion/routes.py and
dashboard/routes/control.py both call into. Covers the two things a naive
extraction could get wrong: (1) the urllib -> DaemonClient swap losing error
semantics, and (2) the optional hooks (`finalize`/`serialize`/`on_run`/etc.)
that carry each caller's one divergence not actually being called.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.companion import services


class _FakeDaemonClient:
    """Stand-in for `DaemonClient` capturing calls made against it."""

    instances: list[_FakeDaemonClient] = []

    def __init__(self, host: str, token: str | None, *, timeout: float) -> None:
        self.host = host
        self.token = token
        self.timeout = timeout
        self.calls: list[tuple[str, str, Any]] = []
        _FakeDaemonClient.instances.append(self)

    async def __aenter__(self) -> _FakeDaemonClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def get_json(self, path: str) -> dict[str, Any]:
        self.calls.append(("GET", path, None))
        return {"ok": True, "path": path}

    async def post_json(self, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        self.calls.append(("POST", path, body))
        return {"ok": True, "body": body}

    async def get_bytes(self, path: str) -> bytes:
        self.calls.append(("GET", path, None))
        return b"zip-bytes"


class _FailingDaemonClient(_FakeDaemonClient):
    async def get_json(self, path: str) -> dict[str, Any]:
        raise services.DaemonError("boom", status=404, detail="not found")

    async def get_bytes(self, path: str) -> bytes:
        raise services.DaemonError("boom", status=500, detail="server error")


@pytest.fixture(autouse=True)
def _reset_instances() -> None:
    _FakeDaemonClient.instances.clear()
    yield
    _FakeDaemonClient.instances.clear()


def test_request_json_delegates_to_daemon_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services, "DaemonClient", _FakeDaemonClient)

    result = services._request_json(
        "http://pi.local:8000", "POST", "/control/sessions/x/pin", token="secret", body={"a": 1}
    )

    assert result == {"ok": True, "body": {"a": 1}}
    client = _FakeDaemonClient.instances[0]
    assert client.host == "http://pi.local:8000"
    assert client.token == "secret"
    assert client.calls == [("POST", "/control/sessions/x/pin", {"a": 1})]


def test_request_bytes_delegates_to_daemon_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services, "DaemonClient", _FakeDaemonClient)

    result = services._request_bytes(
        "http://pi.local:8000", "GET", "/control/sessions/x/export", token=None
    )

    assert result == b"zip-bytes"


def test_request_json_wraps_daemon_error_as_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services, "DaemonClient", _FailingDaemonClient)

    with pytest.raises(RuntimeError, match="failed"):
        services._request_json("http://pi.local:8000", "GET", "/control/jobs", token=None)


def test_request_bytes_wraps_daemon_error_as_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(services, "DaemonClient", _FailingDaemonClient)

    with pytest.raises(RuntimeError, match="failed"):
        services._request_bytes(
            "http://pi.local:8000", "GET", "/control/sessions/x/export", token=None
        )


def test_update_session_metadata_response_finalize_hook_is_optional(tmp_path: Path) -> None:
    session = tmp_path / "2026-05-17T120000"
    session.mkdir()

    # Control's shape: no finalize -> no "session" key at all.
    without_hook = services.update_session_metadata_response(
        session.name,
        {"route": "mesa"},
        merge=True,
        resolve=lambda _sid: session,
    )
    assert "session" not in without_hook
    assert without_hook["metadata"]["route"] == "mesa"

    # Companion's shape: a finalize hook adds "session" and can carry any
    # extra work (here, standing in for the library-index reimport).
    calls: list[Path] = []

    def finalize(path: Path) -> dict[str, Any]:
        calls.append(path)
        return {"refreshed": True}

    with_hook = services.update_session_metadata_response(
        session.name,
        {"route": "mesa"},
        merge=True,
        resolve=lambda _sid: session,
        finalize=finalize,
    )
    assert with_hook["session"] == {"refreshed": True}
    assert calls == [session]


def test_pin_session_response_serialize_hook_carries_the_divergence(tmp_path: Path) -> None:
    session = tmp_path / "2026-05-17T120000"
    session.mkdir()
    refresh_calls: list[Path] = []

    def serialize_with_refresh(path: Path) -> dict[str, Any]:
        refresh_calls.append(path)
        return {"session_id": path.name, "reimported": True}

    result = services.pin_session_response(
        session.name,
        reason="good drive",
        extra_metadata={"terrain": "city"},
        resolve=lambda _sid: session,
        serialize=serialize_with_refresh,
    )

    assert refresh_calls == [session]
    assert result["session"] == {"session_id": session.name, "reimported": True}
    assert result["metadata"]["terrain"] == "city"


def test_delete_session_response_guard_and_after_delete_hooks(tmp_path: Path) -> None:
    session = tmp_path / "2026-05-17T120000"
    session.mkdir()

    guard_calls: list[str] = []
    after_calls: list[str] = []
    result = services.delete_session_response(
        session.name,
        force=False,
        resolve=lambda _sid: session,
        guard=guard_calls.append,
        after_delete=after_calls.append,
    )

    assert result == {"session_id": session.name, "deleted": True, "force": False}
    assert guard_calls == [session.name]
    assert after_calls == [session.name]
    assert not session.exists()


def test_training_runs_response_on_run_hook_is_optional(tmp_path: Path) -> None:
    ledger_path = tmp_path / "training_runs.jsonl"

    no_hook = services.training_runs_response(ledger_path)
    assert no_hook == {"runs": [], "count": 0}

    recorded: list[dict[str, Any]] = []
    with_hook = services.training_runs_response(ledger_path, on_run=recorded.append)
    assert with_hook == {"runs": [], "count": 0}
    assert recorded == []
