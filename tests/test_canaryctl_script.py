"""Tests for the laptop-facing canaryctl CLI."""

from __future__ import annotations

import base64
import json
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


def test_session_pin_dispatch_posts_reason_and_metadata(
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
        return {"session": {"pinned": True}}

    monkeypatch.setattr(canaryctl, "_request_json", fake_request)
    args = canaryctl.parse_args(
        [
            "--host",
            "canary.local:8000",
            "--token",
            "secret",
            "sessions",
            "pin",
            "2026-05-17T120000",
            "--reason",
            "good drive",
            "--field",
            "route=mesa",
        ]
    )

    out = canaryctl._dispatch(args)

    assert out == {"session": {"pinned": True}}
    assert captured["method"] == "POST"
    assert captured["path"] == "/control/sessions/2026-05-17T120000/pin"
    assert captured["body"] == {
        "reason": "good drive",
        "metadata": {"route": "mesa"},
    }


def test_model_upload_dispatch_encodes_artifact_and_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_path = tmp_path / "model.joblib"
    manifest_path = tmp_path / "model.manifest.json"
    artifact_path.write_bytes(b"model bytes")
    manifest_path.write_text(json.dumps({"version": "test"}))
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
        return {"model": {"artifact_bytes": 11}}

    monkeypatch.setattr(canaryctl, "_request_json", fake_request)
    args = canaryctl.parse_args(
        [
            "--host",
            "canary.local:8000",
            "--token",
            "secret",
            "models",
            "upload",
            "isolation_forest",
            "--artifact",
            str(artifact_path),
            "--manifest",
            str(manifest_path),
        ]
    )

    out = canaryctl._dispatch(args)

    assert out == {"model": {"artifact_bytes": 11}}
    assert captured["method"] == "POST"
    assert captured["path"] == "/control/models/isolation_forest/artifact"
    assert captured["body"] == {
        "artifact_b64": base64.b64encode(b"model bytes").decode("ascii"),
        "manifest": {"version": "test"},
    }


def test_load_env_file_parses_dotenv(tmp_path: Path) -> None:
    f = tmp_path / "env"
    f.write_text(
        "# comment\n"
        "\n"
        "CANARY_HOST=http://10.0.0.5:8000\n"
        "export CANARY_TOKEN='tok en'\n"
        'CANARY_URL="http://quoted:8000"\n'
        "BARE\n"
        "  SPACED = value \n"
    )
    parsed = canaryctl._load_env_file(f)
    assert parsed["CANARY_HOST"] == "http://10.0.0.5:8000"
    assert parsed["CANARY_TOKEN"] == "tok en"
    assert parsed["CANARY_URL"] == "http://quoted:8000"
    assert parsed["SPACED"] == "value"
    assert "BARE" not in parsed
    assert canaryctl._load_env_file(tmp_path / "missing") == {}


def test_resolve_default_env_beats_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CANARY_HOST", "http://from-env:8000")
    file_env = {"CANARY_HOST": "http://from-file:8000"}
    assert canaryctl._resolve_default(file_env, "CANARY_HOST") == "http://from-env:8000"
    monkeypatch.delenv("CANARY_HOST", raising=False)
    assert canaryctl._resolve_default(file_env, "CANARY_HOST") == "http://from-file:8000"
    assert canaryctl._resolve_default({}, "CANARY_HOST") is None


def test_parse_args_falls_back_to_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("CANARY_HOST", "CANARY_URL", "CANARY_TOKEN", "CANARY_CONTROL_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    env_file = tmp_path / "env"
    env_file.write_text("CANARY_HOST=http://pi:8000\nCANARY_TOKEN=secrettok\n")
    monkeypatch.setenv("CANARY_ENV_FILE", str(env_file))

    args = canaryctl.parse_args(["status"])
    assert args.host == "http://pi:8000"
    # Legacy CANARY_TOKEN still resolves so older env files keep working.
    assert args.token == "secrettok"

    # A real env var still wins over the file.
    monkeypatch.setenv("CANARY_HOST", "http://override:8000")
    assert canaryctl.parse_args(["status"]).host == "http://override:8000"

    # An explicit flag still wins over both.
    assert canaryctl.parse_args(["--host", "http://flag:8000", "status"]).host == (
        "http://flag:8000"
    )


def test_parse_args_prefers_control_token_over_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``CANARY_CONTROL_TOKEN`` is the canonical name; legacy ``CANARY_TOKEN`` loses.

    Same precedence within either source (env or file) — file value of
    ``CANARY_CONTROL_TOKEN`` wins over file value of ``CANARY_TOKEN``,
    and env var of ``CANARY_CONTROL_TOKEN`` wins over env var of
    ``CANARY_TOKEN``. Operators following the post-2026-05-19 install
    instructions get the new name; older operators see no regression.
    """
    for var in ("CANARY_HOST", "CANARY_URL", "CANARY_TOKEN", "CANARY_CONTROL_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    env_file = tmp_path / "env"
    env_file.write_text(
        "CANARY_HOST=http://pi:8000\nCANARY_TOKEN=legacy\nCANARY_CONTROL_TOKEN=canonical\n"
    )
    monkeypatch.setenv("CANARY_ENV_FILE", str(env_file))
    assert canaryctl.parse_args(["status"]).token == "canonical"

    # And env var: control wins over legacy regardless of order.
    monkeypatch.setenv("CANARY_TOKEN", "legacy-env")
    monkeypatch.setenv("CANARY_CONTROL_TOKEN", "canonical-env")
    assert canaryctl.parse_args(["status"]).token == "canonical-env"
