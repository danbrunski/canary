"""R29.7: ``POST /control/tuning-mode`` arms the poll profile *and* isolates storage.

The two halves must not disagree: if the writer cannot switch sessions, the poll
profile goes back to what it was, so a drive is never logged into the corpus
under a mode that says it is quarantined. That transaction lived inside the
route's ``try``; it is :func:`src.control.tuning_mode.set_tuning_mode` now, and
this file proves its rollback, with and without the route around it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.collection import tuning_profile
from src.config import CanaryConfig
from src.control.status_error import StatusError
from src.control.tuning_mode import TuningStorageSwitchError, set_tuning_mode
from src.dashboard.api import create_app
from tests.configs import isolated_config

AUTH = {"Authorization": "Bearer secret"}
FAILED = "failed to switch tuning session storage; tuning mode unchanged"
NORMAL_ROOT = Path("/sessions/2026-07-27T120000")


class _Writer:
    """The two calls the route makes on the storage writer, recording them."""

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.calls: list[tuple[str, str | None]] = []
        self.paths = SimpleNamespace(root=NORMAL_ROOT)

    async def enter_tuning_mode(self, tuning_id: str) -> Path:
        self.calls.append(("enter", tuning_id))
        if self.fail_on == "enter":
            raise OSError("disk full")
        root = Path("/sessions") / f"_tuning-{tuning_id}"
        self.paths = SimpleNamespace(root=root)
        return root

    async def exit_tuning_mode(self) -> Path:
        self.calls.append(("exit", None))
        if self.fail_on == "exit":
            raise OSError("disk full")
        self.paths = SimpleNamespace(root=NORMAL_ROOT)
        return NORMAL_ROOT


@pytest.fixture(autouse=True)
def _poll_profile_reset() -> Iterator[None]:
    tuning_profile.set_active(False, source="test")
    yield
    tuning_profile.set_active(False, source="test")


@pytest.fixture
def config(tmp_path: Path) -> CanaryConfig:
    return isolated_config(tmp_path, "example")


def _client(config: CanaryConfig, tmp_path: Path, writer: _Writer | None) -> TestClient:
    app = create_app(
        config,
        anomalies_dir=tmp_path / "an",
        control_token="secret",
        storage_writer=writer,  # type: ignore[arg-type]
    )
    return TestClient(app)


def _post(client: TestClient, enabled: bool, **extra: Any) -> Any:
    return client.post("/control/tuning-mode", json={"enabled": enabled, **extra}, headers=AUTH)


# -- through the route: what a client sees ------------------------------------


def test_arming_isolates_storage_and_reports_the_new_directory(
    config: CanaryConfig, tmp_path: Path
) -> None:
    writer = _Writer()
    response = _post(_client(config, tmp_path, writer), True)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["previous_active"] is False
    assert body["tuning_mode"]["active"] is True
    assert body["output_session_dir"] == str(writer.paths.root)
    assert writer.paths.root.name.startswith("_tuning-")
    assert [name for name, _ in writer.calls] == ["enter"]


def test_disarming_returns_storage_to_a_plain_session(config: CanaryConfig, tmp_path: Path) -> None:
    writer = _Writer()
    client = _client(config, tmp_path, writer)
    _post(client, True)
    body = _post(client, False).json()
    assert body["previous_active"] is True
    assert body["tuning_mode"]["active"] is False
    assert body["output_session_dir"] == str(NORMAL_ROOT)
    assert [name for name, _ in writer.calls] == ["enter", "exit"]


def test_a_repeated_transition_leaves_storage_alone(config: CanaryConfig, tmp_path: Path) -> None:
    writer = _Writer()
    client = _client(config, tmp_path, writer)
    body = _post(client, False).json()
    assert "output_session_dir" not in body and writer.calls == []
    _post(client, True)
    again = _post(client, True).json()
    assert "output_session_dir" not in again
    assert [name for name, _ in writer.calls] == ["enter"]


def test_without_a_writer_the_mode_still_arms_with_no_directory(
    config: CanaryConfig, tmp_path: Path
) -> None:
    body = _post(_client(config, tmp_path, None), True).json()
    assert body["tuning_mode"]["active"] is True
    assert "output_session_dir" not in body


def test_a_failed_arm_is_a_500_and_the_poll_profile_goes_back(
    config: CanaryConfig, tmp_path: Path
) -> None:
    writer = _Writer(fail_on="enter")
    response = _post(_client(config, tmp_path, writer), True)
    assert (response.status_code, response.json()) == (500, {"detail": FAILED})
    assert tuning_profile.is_active() is False
    assert writer.paths.root == NORMAL_ROOT


def test_a_failed_disarm_is_a_500_and_the_mode_stays_armed(
    config: CanaryConfig, tmp_path: Path
) -> None:
    writer = _Writer()
    client = _client(config, tmp_path, writer)
    assert _post(client, True).status_code == 200
    writer.fail_on = "exit"
    response = _post(client, False)
    assert (response.status_code, response.json()) == (500, {"detail": FAILED})
    assert tuning_profile.is_active() is True


# -- the transaction itself: no app, no HTTP -----------------------------------


async def _arm(writer: _Writer | None, **kwargs: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {"enabled": True, "channels": None, "rate_hz": None, **kwargs}
    return await set_tuning_mode(writer, **arguments)  # type: ignore[arg-type]


async def test_arming_moves_storage_and_reports_what_it_replaced() -> None:
    writer = _Writer()
    payload = await _arm(writer)
    assert payload["previous_active"] is False
    assert payload["tuning_mode"]["active"] is True
    assert payload["output_session_dir"] == str(writer.paths.root)
    assert tuning_profile.is_active() is True


async def test_a_writer_that_cannot_enter_puts_the_poll_profile_back() -> None:
    writer = _Writer(fail_on="enter")
    with pytest.raises(TuningStorageSwitchError) as raised:
        await _arm(writer)
    assert (raised.value.status, raised.value.detail) == (500, FAILED)
    assert isinstance(raised.value, StatusError)
    assert isinstance(raised.value.__cause__, OSError)
    assert tuning_profile.is_active() is False
    assert writer.calls[0][0] == "enter"


async def test_a_writer_that_cannot_exit_leaves_the_mode_armed() -> None:
    writer = _Writer()
    await _arm(writer)
    writer.fail_on = "exit"
    with pytest.raises(TuningStorageSwitchError):
        await _arm(writer, enabled=False)
    assert tuning_profile.is_active() is True
    assert writer.paths.root.name.startswith("_tuning-")  # storage and mode still agree


async def test_a_repeat_arm_with_other_channels_is_no_transition_and_touches_no_storage() -> None:
    """Only a change of the flag moves storage, so a writer that would fail is not asked."""
    writer = _Writer()
    await _arm(writer, channels=["RPM", "MAF"])
    snapshot = tuning_profile.snapshot()
    writer.fail_on = "enter"
    again = await _arm(writer, channels=["RPM"])
    assert "output_session_dir" not in again
    assert [name for name, _ in writer.calls] == ["enter"]
    assert tuning_profile.snapshot()["channels"] != snapshot["channels"]
    assert tuning_profile.snapshot()["active"] is True


async def test_the_failure_is_logged_with_its_cause(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("ERROR"), pytest.raises(TuningStorageSwitchError):
        await _arm(_Writer(fail_on="enter"))
    record = next(r for r in caplog.records if "storage isolation failed" in r.getMessage())
    assert record.exc_info is not None and record.exc_info[0] is OSError


async def test_no_writer_still_arms_and_reports_no_directory() -> None:
    payload = await _arm(None)
    assert payload["tuning_mode"]["active"] is True
    assert "output_session_dir" not in payload
