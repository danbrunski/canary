"""Tests for the laptop-facing canaryctl CLI."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import canaryctl  # noqa: E402


def test_base_url_adds_scheme() -> None:
    assert canaryctl._base_url("192.168.0.230:8000") == "http://192.168.0.230:8000"
    assert canaryctl._base_url("http://canary.local:8000/") == "http://canary.local:8000"


def test_capture_start_dispatch_posts_structured_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_request(
        host: str,
        method: str,
        path: str,
        *,
        token: str | None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        captured.update(host=host, method=method, path=path, token=token, body=body)
        return {"ok": True}

    monkeypatch.setattr(canaryctl, "_request_json", fake_request)
    args = canaryctl.parse_args(
        [
            "--host",
            "192.168.0.230:8000",
            "--token",
            "secret",
            "capture",
            "start",
            "--purpose",
            "next drive",
            "--protocol",
            "drive",
            "--field",
            "route=mesa",
        ]
    )

    out = canaryctl._dispatch(args)

    assert out == {"ok": True}
    assert captured["host"] == "http://192.168.0.230:8000"
    assert captured["method"] == "POST"
    assert captured["path"] == "/control/captures"
    assert captured["token"] == "secret"
    assert captured["body"] == {
        "purpose": "next drive",
        "protocol": "drive",
        "vehicle_state": None,
        "fields": {"route": "mesa"},
    }


def test_job_start_dispatch_builds_params(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_request(
        host: str,
        method: str,
        path: str,
        *,
        token: str | None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        captured.update(host=host, method=method, path=path, token=token, body=body)
        return {"job": {"status": "queued"}}

    monkeypatch.setattr(canaryctl, "_request_json", fake_request)
    args = canaryctl.parse_args(
        [
            "--host",
            "http://canary.local:8000",
            "--token",
            "secret",
            "jobs",
            "start",
            "refit_behavior_models",
            "--days",
            "14",
            "--recipe",
            "coolant_warmup",
            "--dry-run",
        ]
    )

    out = canaryctl._dispatch(args)

    assert out == {"job": {"status": "queued"}}
    assert captured["path"] == "/control/jobs"
    assert captured["body"] == {
        "kind": "refit_behavior_models",
        "params": {
            "days": 14,
            "recipes": ["coolant_warmup"],
            "dry_run": True,
        },
    }
