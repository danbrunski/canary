"""R30.13: the stdlib duplicates of the daemon-HTTP helpers must agree.

``scripts/canaryctl.py`` stays free of ``src`` imports (``src.control`` costs a
second of pandas on every call) and ``scripts/install_deps.py`` /
``scripts/venv_launch.py`` run before any project import, so each keeps its own
copy of a few small functions. These tests are what keeps the copies equal.
Before this file ``canaryctl`` honored ``CANARY_CONTROL_TOKEN_FILE`` and
``passive_client`` did not, so two tools could authenticate differently against
one Pi.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from scripts import canaryctl, install_deps, venv_launch
from src.companion.services import _base_url as companion_base_url
from src.control import passive_client


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for var in ("CANARY_TOKEN", "CANARY_CONTROL_TOKEN", "CANARY_CONTROL_TOKEN_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CANARY_ENV_FILE", str(tmp_path / "no-env-file"))


def _canaryctl_token() -> str | None:
    token: str | None = canaryctl.parse_args(["status"]).token
    return token


def test_token_file_override_is_honored_by_both_resolvers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "control.token"
    token_file.write_text("  file-token\n")
    monkeypatch.setenv("CANARY_CONTROL_TOKEN_FILE", str(token_file))
    assert passive_client.resolve_token() == "file-token"
    assert _canaryctl_token() == "file-token"


def test_the_environment_token_beats_the_file_in_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "control.token"
    token_file.write_text("file-token\n")
    monkeypatch.setenv("CANARY_CONTROL_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("CANARY_CONTROL_TOKEN", " env-token ")
    assert passive_client.resolve_token() == "env-token"
    assert _canaryctl_token() == "env-token"


def test_an_absent_or_empty_token_file_gives_no_token_in_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CANARY_CONTROL_TOKEN_FILE", str(tmp_path / "absent"))
    assert passive_client.resolve_token() is None
    assert _canaryctl_token() is None
    empty = tmp_path / "empty.token"
    empty.write_text("\n")
    monkeypatch.setenv("CANARY_CONTROL_TOKEN_FILE", str(empty))
    assert passive_client.resolve_token() is None
    assert _canaryctl_token() is None


def test_the_default_token_path_is_the_same_in_both() -> None:
    assert passive_client.DEFAULT_TOKEN_PATH == canaryctl.DEFAULT_TOKEN_FILE


@pytest.mark.parametrize(
    "host",
    [
        "192.168.0.230:8000",
        "canary.local",
        "http://canary.local:8000/",
        "https://pi:8000",
        "pi:8000//",
    ],
)
def test_base_url_is_the_same_in_canaryctl_and_the_companion(host: str) -> None:
    assert canaryctl._base_url(host) == companion_base_url(host)


@pytest.mark.parametrize("platform", ["linux", "win32", "darwin"])
def test_venv_python_is_the_same_in_install_deps_and_venv_launch(
    platform: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    assert install_deps.venv_python(tmp_path) == venv_launch.venv_python(tmp_path)


def test_a_whitespace_only_environment_token_falls_through_to_the_file_in_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "control.token"
    token_file.write_text("file-token\n")
    monkeypatch.setenv("CANARY_CONTROL_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("CANARY_CONTROL_TOKEN", "   ")
    assert passive_client.resolve_token() == "file-token"
    assert _canaryctl_token() == "file-token"


def test_the_whole_precedence_table_is_the_same_in_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """env var > alias > env-file var > env-file alias > token file."""
    token_file = tmp_path / "control.token"
    token_file.write_text("token-file\n")
    env_file = tmp_path / "env"
    monkeypatch.setenv("CANARY_CONTROL_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("CANARY_ENV_FILE", str(env_file))

    def both() -> tuple[str | None, str | None]:
        return passive_client.resolve_token(), _canaryctl_token()

    env_file.write_text("# nothing here\n")
    assert both() == ("token-file", "token-file")
    env_file.write_text("export CANARY_TOKEN='file-alias'\n")
    assert both() == ("file-alias", "file-alias")
    env_file.write_text("export CANARY_TOKEN='file-alias'\nCANARY_CONTROL_TOKEN=\"file-control\"\n")
    assert both() == ("file-control", "file-control")
    monkeypatch.setenv("CANARY_TOKEN", "env-alias")
    assert both() == ("env-alias", "env-alias")
    monkeypatch.setenv("CANARY_CONTROL_TOKEN", "env-control")
    assert both() == ("env-control", "env-control")


def test_the_env_file_parser_reads_the_same_pairs_in_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / "env"
    env_file.write_text(
        "# comment\n\nexport A=1\nB = \"two\"\nC='three'\nD=\nnoequals\n=novalue\nE=a=b\n"
        'F="unterminated\n  G =  spaced  \n'
    )
    env_file.chmod(0o600)
    monkeypatch.setenv("CANARY_ENV_FILE", str(env_file))
    assert passive_client._env_file_values() == canaryctl._load_env_file(env_file)
    assert passive_client._env_file_values()["E"] == "a=b"
    monkeypatch.setenv("CANARY_ENV_FILE", str(tmp_path / "absent"))
    assert passive_client._env_file_values() == {}
