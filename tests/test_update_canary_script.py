"""Offline tests for ``scripts/update_canary.sh`` (the Pi's ``canary-update``).

The script is exercised for real, against a throwaway git upstream and
checkout in ``tmp_path``. Everything it would touch on a Pi is a shim on
``PATH`` that logs its argv to a file and exits with a scripted code:
``systemctl``, ``sudo``, ``curl``, ``pnpm`` and ``runuser``. The checkout's
``scripts/restart_canary.sh`` is the real one (so the capture refusal is the
real refusal), ``scripts/install_pi.sh`` is a logging shim committed to the
fake upstream, and ``.venv/bin/python`` is a shim inside the (gitignored)
venv path.

The script under test is launched from a copy *outside* the checkout, the way
``install_pi.sh`` installs it to ``/usr/local/sbin/canary-update`` — which is
what makes the re-exec behaviour (S02.2) observable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UPDATE_SCRIPT = REPO_ROOT / "scripts" / "update_canary.sh"
RESTART_SCRIPT = REPO_ROOT / "scripts" / "restart_canary.sh"

# Exit codes documented in update_canary.sh usage() and docs/pi-setup.md.
EXIT_CAPTURE = 3
EXIT_DIRTY = 4
EXIT_FETCH = 5
EXIT_ROLLED_BACK = 6
EXIT_ROLLBACK_FAILED = 7
EXIT_LEFT_BROKEN = 8

# A tracked manifest, as on the Pi (the binary beside it is gitignored).
MANIFEST = "models/demo/isolation_forest/model.manifest.json"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        },
    ).stdout.strip()


def _write_exec(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)


# Every shim appends one line per call: ``<name> <argv...>``.
_LOG_PRELUDE = 'printf \'%s %s\\n\' "$(basename "$0")" "$*" >>"${SHIM_LOG}"\n'

_SYSTEMCTL_SHIM = (
    "#!/usr/bin/env bash\n"
    + _LOG_PRELUDE
    + """
if [[ "$1" == "restart" && "$2" == "canary.service" ]]; then
  n=$(cat "${SHIM_STATE}/restarts" 2>/dev/null || echo 0)
  echo $((n + 1)) >"${SHIM_STATE}/restarts"
fi
exit 0
"""
)

_SUDO_SHIM = "#!/usr/bin/env bash\n" + _LOG_PRELUDE + 'exec "$@"\n'

# runuser -u <user> -- <cmd...>
_RUNUSER_SHIM = (
    "#!/usr/bin/env bash\n"
    + _LOG_PRELUDE
    + """
while [[ $# -gt 0 ]]; do
  case "$1" in
    -u) shift 2 ;;
    --) shift; break ;;
    *) break ;;
  esac
done
exec "$@"
"""
)

# curl: the health endpoint. Behaviour is keyed by which *restart phase* we
# are in (0 = before any restart, 1 = after the first, 2 = after the rollback
# restart): SHIM_HEALTH_FAIL_CALLS_<phase> is how many calls fail before one
# succeeds (-1 = never succeeds). Successful calls print SHIM_STATUS_JSON.
_CURL_SHIM = (
    "#!/usr/bin/env bash\n"
    + _LOG_PRELUDE
    + """
phase=$(cat "${SHIM_STATE}/restarts" 2>/dev/null || echo 0)
var="SHIM_HEALTH_FAIL_CALLS_${phase}"
fail_calls="${!var:-0}"
calls_file="${SHIM_STATE}/curl_calls_${phase}"
calls=$(cat "${calls_file}" 2>/dev/null || echo 0)
echo $((calls + 1)) >"${calls_file}"
if [[ "${fail_calls}" -eq -1 || "${calls}" -lt "${fail_calls}" ]]; then
  exit 7
fi
printf '%s' "${SHIM_STATUS_JSON:-{\\}}"
exit 0
"""
)

# pnpm: `pnpm build` writes the dist marker the updater's staleness check reads.
_PNPM_SHIM = (
    "#!/usr/bin/env bash\n"
    + _LOG_PRELUDE
    + """
if [[ "$1" == "build" ]]; then
  mkdir -p dist
  echo "built" >dist/index.html
fi
exit "${SHIM_PNPM_EXIT:-0}"
"""
)

# .venv/bin/python inside the checkout: `-m pip check` / `-m pip install`.
_VENV_PYTHON_SHIM = (
    "#!/usr/bin/env bash\n"
    + 'printf \'%s %s\\n\' "venv-python" "$*" >>"${SHIM_LOG}"\n'
    + """
if [[ "$*" == *"pip check"* ]]; then
  exit "${SHIM_PIP_CHECK_EXIT:-0}"
fi
exit 0
"""
)

# The checkout's own install_pi.sh (committed to the fake upstream).
_INSTALL_PI_SHIM = "#!/usr/bin/env bash\n" + _LOG_PRELUDE + 'exit "${SHIM_INSTALL_PI_EXIT:-0}"\n'


@dataclass
class Harness:
    tmp: Path
    upstream: Path
    checkout: Path
    bin_dir: Path
    sbin_script: Path
    shim_log: Path
    shim_state: Path
    cgroup_dir: Path
    env: dict[str, str] = field(default_factory=dict)

    # -- authoring upstream commits ------------------------------------------
    def commit_upstream(self, message: str, **files: str) -> str:
        for rel, content in files.items():
            path = self.upstream / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            _git(self.upstream, "add", rel)
        _git(self.upstream, "commit", "-q", "--allow-empty", "-m", message)
        return _git(self.upstream, "rev-parse", "--short", "HEAD")

    def head(self) -> str:
        return _git(self.checkout, "rev-parse", "--short", "HEAD")

    # -- running -------------------------------------------------------------
    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        self.shim_log.write_text("")
        for stale in self.shim_state.glob("*"):
            stale.unlink()
        return subprocess.run(
            ["bash", str(self.sbin_script), *args],
            capture_output=True,
            text=True,
            env={**self.env, **env},
            timeout=60,
        )

    def log_lines(self) -> list[str]:
        return [line for line in self.shim_log.read_text().splitlines() if line]

    def calls(self, prefix: str) -> list[str]:
        return [line for line in self.log_lines() if line.startswith(prefix)]

    def canary_restarts(self) -> int:
        return len(self.calls("systemctl restart canary.service"))

    def health_polls(self, phase: int) -> int:
        path = self.shim_state / f"curl_calls_{phase}"
        return int(path.read_text()) if path.exists() else 0

    def build_dist(self) -> None:
        dist = self.checkout / "src" / "dashboard" / "static" / "dist"
        dist.mkdir(parents=True, exist_ok=True)
        (dist / "index.html").write_text("built")


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    (upstream / ".gitignore").write_text("dist/\n.venv/\n")
    (upstream / "scripts").mkdir()
    shutil.copy(UPDATE_SCRIPT, upstream / "scripts" / "update_canary.sh")
    shutil.copy(RESTART_SCRIPT, upstream / "scripts" / "restart_canary.sh")
    _write_exec(upstream / "scripts" / "install_pi.sh", _INSTALL_PI_SHIM)
    (upstream / "src" / "dashboard" / "static").mkdir(parents=True)
    (upstream / "src" / "dashboard" / "static" / "index.html").write_text("<html/>\n")
    (upstream / "src" / "main.py").write_text("print('hi')\n")
    (upstream / "pyproject.toml").write_text("[project]\nname='x'\n")
    (upstream / "systemd").mkdir()
    (upstream / "systemd" / "canary.service").write_text("[Unit]\n")
    (upstream / "docs").mkdir()
    (upstream / "docs" / "readme.md").write_text("docs\n")
    (upstream / MANIFEST).parent.mkdir(parents=True)
    (upstream / MANIFEST).write_text('{"v": "git"}\n')
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-q", "-m", "initial")
    # A pull from a non-bare upstream is fine as long as we never push to it.
    _git(upstream, "config", "receive.denyCurrentBranch", "ignore")

    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "-q", str(upstream), str(checkout))
    _write_exec(checkout / ".venv" / "bin" / "python", _VENV_PYTHON_SHIM)

    bin_dir = tmp_path / "bin"
    _write_exec(bin_dir / "systemctl", _SYSTEMCTL_SHIM)
    _write_exec(bin_dir / "sudo", _SUDO_SHIM)
    _write_exec(bin_dir / "runuser", _RUNUSER_SHIM)
    _write_exec(bin_dir / "curl", _CURL_SHIM)
    _write_exec(bin_dir / "pnpm", _PNPM_SHIM)
    # restart_canary.sh parses /status with `python3`; pin it to this interpreter.
    (bin_dir / "python3").symlink_to(sys.executable)

    sbin = tmp_path / "sbin"
    sbin.mkdir()
    shutil.copy(UPDATE_SCRIPT, sbin / "canary-update")

    shim_log = tmp_path / "shim.log"
    shim_state = tmp_path / "state"
    shim_state.mkdir()
    cgroup_dir = tmp_path / "cgroup"

    env = {
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "HOME": str(tmp_path),
        "CANARY_UPDATE_ALLOW_UNPRIVILEGED": "1",
        "CANARY_HOME": str(checkout),
        "CANARY_ENV": str(tmp_path / "no-such-canary.env"),
        "CANARY_USER": "tester",
        "CANARY_CGROUP_DIR": str(cgroup_dir),
        "CANARY_HEALTH_WAIT_SECONDS": "3",
        "CANARY_HEALTH_POLL_SECONDS": "0.05",
        "CANARY_STATUS_URL": "http://shim/status",
        "SHIM_LOG": str(shim_log),
        "SHIM_STATE": str(shim_state),
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    h = Harness(
        tmp=tmp_path,
        upstream=upstream,
        checkout=checkout,
        bin_dir=bin_dir,
        sbin_script=sbin / "canary-update",
        shim_log=shim_log,
        shim_state=shim_state,
        cgroup_dir=cgroup_dir,
        env=env,
    )
    h.build_dist()
    return h


def _summary(stdout: str) -> str:
    assert "=== canary-update summary ===" in stdout, stdout
    return stdout.split("=== canary-update summary ===", 1)[1]


# ---------------------------------------------------------------------------
# S02.1 — today's behaviour
# ---------------------------------------------------------------------------


def test_refuses_while_a_capture_is_active(harness: Harness) -> None:
    before = harness.head()
    harness.commit_upstream("docs", **{"docs/readme.md": "new docs\n"})

    status = '{"capture_profile": {"active": true, "name": "tuning", "session_dir": "/d/x"}}'
    result = harness.run(SHIM_STATUS_JSON=status)

    assert result.returncode == EXIT_CAPTURE, result.stdout + result.stderr
    assert "refusing: capture 'tuning' is active" in result.stderr
    assert harness.head() == before, "refusal must happen before the fetch"
    assert harness.canary_restarts() == 0
    assert "result: refused: capture or ECU backup active (exit 3)" in _summary(result.stdout)


def test_refuses_while_an_ecu_backup_is_running(harness: Harness) -> None:
    """W08.7: a backup holding the ECM (``rom_backup`` claim) refuses like a
    capture, before the fetch, under the same exit code."""
    before = harness.head()
    harness.commit_upstream("docs", **{"docs/readme.md": "new docs\n"})

    status = '{"passive": {"enabled": false, "ecm_session_claims": {"rom_backup": 29.5}}}'
    result = harness.run(SHIM_STATUS_JSON=status)

    assert result.returncode == EXIT_CAPTURE, result.stdout + result.stderr
    assert "refusing: an ECU backup (ROM read) is running" in result.stderr
    assert harness.head() == before, "refusal must happen before the fetch"
    assert harness.canary_restarts() == 0
    assert "result: refused: capture or ECU backup active (exit 3)" in _summary(result.stdout)


def test_refuses_a_checkout_with_tracked_local_changes(harness: Harness) -> None:
    before = harness.head()
    harness.commit_upstream("docs", **{"docs/readme.md": "new docs\n"})
    (harness.checkout / "src" / "main.py").write_text("print('local edit')\n")

    result = harness.run()

    assert result.returncode == EXIT_DIRTY, result.stdout + result.stderr
    assert "tracked local changes" in result.stderr
    assert "src/main.py" in result.stderr
    assert harness.head() == before
    assert harness.canary_restarts() == 0
    assert "result: refused: tracked local changes (exit 4)" in _summary(result.stdout)


def test_local_model_changes_do_not_block_and_are_kept(harness: Harness) -> None:
    # 2026-09-24: a retrain on the Pi rewrote a tracked manifest and every
    # update refused with exit 4.
    before = harness.head()
    after = harness.commit_upstream("docs", **{"docs/readme.md": "new docs\n"})
    (harness.checkout / MANIFEST).write_text('{"v": "pi"}\n')

    result = harness.run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert harness.head() == after != before
    assert (harness.checkout / MANIFEST).read_text() == '{"v": "pi"}\n'


def test_pull_never_changes_the_pis_models(harness: Harness) -> None:
    # The binary does not travel by git, so a pulled manifest would describe a
    # binary the Pi does not have. Both the dirty and the clean case.
    other = "models/demo/sequence/model.manifest.json"
    after = harness.commit_upstream(
        "models", **{MANIFEST: '{"v": "laptop"}\n', other: '{"v": "new"}\n'}
    )

    result = harness.run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert harness.head() == after
    assert (harness.checkout / MANIFEST).read_text() == '{"v": "git"}\n'
    assert not (harness.checkout / other).exists()


def test_local_model_edit_survives_an_upstream_change_to_the_same_file(
    harness: Harness,
) -> None:
    after = harness.commit_upstream("models", **{MANIFEST: '{"v": "laptop"}\n'})
    (harness.checkout / MANIFEST).write_text('{"v": "pi"}\n')

    result = harness.run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert harness.head() == after
    assert (harness.checkout / MANIFEST).read_text() == '{"v": "pi"}\n'


def test_model_changes_do_not_hide_other_local_changes(harness: Harness) -> None:
    (harness.checkout / MANIFEST).write_text('{"v": "pi"}\n')
    (harness.checkout / "src" / "main.py").write_text("print('local edit')\n")

    result = harness.run()

    assert result.returncode == EXIT_DIRTY, result.stdout + result.stderr
    assert "src/main.py" in result.stderr
    assert MANIFEST not in result.stderr


def test_prints_the_source_line_in_drive_md_form(harness: Harness) -> None:
    before = harness.head()
    after = harness.commit_upstream("docs", **{"docs/readme.md": "new docs\n"})

    result = harness.run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"\nsource: {before} -> {after}\n" in result.stdout
    assert harness.head() == after


def test_health_loop_waits_for_the_nth_curl_success(harness: Harness) -> None:
    harness.commit_upstream("docs", **{"docs/readme.md": "new docs\n"})

    result = harness.run(SHIM_HEALTH_FAIL_CALLS_1="3")

    assert result.returncode == 0, result.stdout + result.stderr
    assert harness.health_polls(phase=1) == 4, "three failures then the answer"
    assert "health: ok after" in _summary(result.stdout)


def test_health_loop_gives_up_after_the_wait(harness: Harness) -> None:
    harness.commit_upstream("docs", **{"docs/readme.md": "new docs\n"})

    result = harness.run("--no-rollback", SHIM_HEALTH_FAIL_CALLS_1="-1")

    assert result.returncode == EXIT_LEFT_BROKEN, result.stdout + result.stderr
    assert "did not answer at http://shim/status within 3 s" in result.stderr
    assert harness.health_polls(phase=1) >= 2


def test_warns_when_the_memory_cgroup_file_is_absent(harness: Harness) -> None:
    result = harness.run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "memory.max does not exist" in result.stderr
    assert "NOT enforced" in result.stderr
    assert "memory limits: NOT enforced" in _summary(result.stdout)


def test_reports_the_memory_limits_when_enforced(harness: Harness) -> None:
    harness.cgroup_dir.mkdir()
    (harness.cgroup_dir / "memory.max").write_text("6442450944\n")
    (harness.cgroup_dir / "memory.swap.max").write_text("0\n")

    result = harness.run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "NOT enforced" not in result.stderr
    assert "memory limits: enforced (memory.max 6442450944, memory.swap.max 0)" in _summary(
        result.stdout
    )


def test_fetch_failure_changes_nothing(harness: Harness) -> None:
    # Diverge: a local commit the upstream does not have makes --ff-only fail.
    (harness.checkout / "docs" / "readme.md").write_text("local\n")
    _git(harness.checkout, "commit", "-q", "-am", "local")
    before = harness.head()
    harness.commit_upstream("docs", **{"docs/readme.md": "upstream\n"})

    result = harness.run()

    assert result.returncode == EXIT_FETCH, result.stdout + result.stderr
    assert harness.head() == before
    assert harness.canary_restarts() == 0
    assert "result: fetch failed (exit 5)" in _summary(result.stdout)


# ---------------------------------------------------------------------------
# S02.2 — re-exec the new updater
# ---------------------------------------------------------------------------


def test_reexecs_the_new_updater_when_the_update_changes_it(harness: Harness) -> None:
    new_script = UPDATE_SCRIPT.read_text().replace(
        'echo "CANary update complete (${after})."',
        'echo "CANary update complete (${after}) [NEW UPDATER]."',
    )
    assert "[NEW UPDATER]" in new_script
    before = harness.head()
    after = harness.commit_upstream("updater", **{"scripts/update_canary.sh": new_script})

    result = harness.run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "scripts/update_canary.sh changed" in result.stdout
    assert "re-executing the new copy" in result.stdout
    assert f"CANary update complete ({after}) [NEW UPDATER]." in result.stdout
    # The new copy did not fetch again, and the fetch-time source line was
    # printed once (the summary repeats it by design).
    log, _summary_text = result.stdout.split("=== canary-update summary ===", 1)
    assert log.count(f"\nsource: {before} -> {after}\n") == 1
    assert len(harness.calls("runuser -u tester -- git")) == 1, harness.log_lines()
    assert f"re-executed from {after}; fetch already done ({before} -> {after})" in result.stdout
    # The updater is one of the files install_pi.sh installs.
    assert harness.calls("install_pi.sh")
    assert harness.canary_restarts() == 1


def test_reexec_marker_prevents_a_second_reexec(harness: Harness) -> None:
    before = harness.head()
    after = harness.commit_upstream(
        "updater", **{"scripts/update_canary.sh": UPDATE_SCRIPT.read_text() + "# changed\n"}
    )
    _git(harness.checkout, "pull", "-q", "--ff-only")
    assert harness.head() == after

    result = harness.run(CANARY_UPDATE_REEXEC=after, CANARY_UPDATE_REEXEC_BEFORE=before)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "re-executing the new copy" not in result.stdout
    assert f"re-executed from {after}" in result.stdout
    assert not harness.calls("runuser -u tester -- git"), "no fetch under the marker"
    assert "install_pi.sh: ran (scripts/update_canary.sh changed)" in _summary(result.stdout)
    assert harness.canary_restarts() == 1


# ---------------------------------------------------------------------------
# S02.3 — skip the work the diff does not touch
# ---------------------------------------------------------------------------


def test_docs_only_update_skips_build_pip_and_install(harness: Harness) -> None:
    harness.commit_upstream("docs", **{"docs/readme.md": "new docs\n"})

    result = harness.run()

    assert result.returncode == 0, result.stdout + result.stderr
    summary = _summary(result.stdout)
    assert "dashboard build: skipped (no dashboard source change; dist/ is current)" in summary
    assert "pip install: skipped (no dependency change; pip check passes)" in summary
    assert "install_pi.sh: skipped (no installer, systemd or installed-file change)" in summary
    assert not harness.calls("pnpm")
    assert not harness.calls("venv-python -m pip install")
    assert not harness.calls("install_pi.sh")
    assert harness.canary_restarts() == 1
    assert harness.calls("systemctl restart canary-mcp.service")


def test_dashboard_only_update_builds_only_the_dashboard(harness: Harness) -> None:
    harness.commit_upstream("ui", **{"src/dashboard/static/index.html": "<html>v2</html>\n"})

    result = harness.run()

    assert result.returncode == 0, result.stdout + result.stderr
    summary = _summary(result.stdout)
    assert "dashboard build: ran (src/dashboard/static changed)" in summary
    assert "pip install: skipped" in summary
    assert "install_pi.sh: skipped" in summary
    assert harness.calls("pnpm install --frozen-lockfile")
    assert harness.calls("pnpm build")
    assert not harness.calls("install_pi.sh")


def test_pyproject_change_runs_pip_only(harness: Harness) -> None:
    harness.commit_upstream("deps", **{"pyproject.toml": "[project]\nname='x'\nversion='2'\n"})

    result = harness.run()

    assert result.returncode == 0, result.stdout + result.stderr
    summary = _summary(result.stdout)
    assert "pip install: ran (pyproject.toml changed)" in summary
    assert "dashboard build: skipped" in summary
    assert "install_pi.sh: skipped" in summary
    pip_calls = harness.calls("venv-python -m pip install -e")
    assert pip_calls and str(harness.checkout) in pip_calls[0]
    assert not harness.calls("pnpm")


def test_failing_pip_check_runs_pip_without_a_diff(harness: Harness) -> None:
    result = harness.run(SHIM_PIP_CHECK_EXIT="1")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "pip install: ran (pip check failed)" in _summary(result.stdout)
    assert harness.calls("venv-python -m pip install -e")


def test_missing_dist_rebuilds_even_with_no_diff(harness: Harness) -> None:
    shutil.rmtree(harness.checkout / "src" / "dashboard" / "static" / "dist")
    before = harness.head()

    result = harness.run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"already at {before}" in result.stdout
    summary = _summary(result.stdout)
    assert "dashboard build: ran (dist/ missing)" in summary
    assert harness.calls("pnpm build")
    assert f"result: already at {before}; restarted (exit 0)" in summary
    assert harness.canary_restarts() == 1


def test_stale_dist_rebuilds_even_with_no_diff(harness: Harness) -> None:
    dist_index = harness.checkout / "src" / "dashboard" / "static" / "dist" / "index.html"
    source = harness.checkout / "src" / "dashboard" / "static" / "index.html"
    os.utime(dist_index, (1_000_000, 1_000_000))
    os.utime(source, (2_000_000, 2_000_000))

    result = harness.run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "dashboard build: ran (dist/ older than src/dashboard/static/index.html)" in _summary(
        result.stdout
    )
    assert harness.calls("pnpm build")


def test_systemd_change_runs_install_pi_which_covers_pip(harness: Harness) -> None:
    harness.commit_upstream("unit", **{"systemd/canary.service": "[Unit]\nDescription=x\n"})

    result = harness.run()

    assert result.returncode == 0, result.stdout + result.stderr
    summary = _summary(result.stdout)
    assert "install_pi.sh: ran (systemd/canary.service changed)" in summary
    assert "pip install: ran (by install_pi.sh)" in summary
    assert harness.calls("install_pi.sh")
    assert not harness.calls("venv-python -m pip install")


@pytest.mark.parametrize(
    "path",
    [
        "hardware/canary-hat-firmware/canary-hat-overlay.dts",
        "hardware/canary-hat-firmware/canary-hat-r21-overlay.dts",
        "hardware/canary-hat-firmware/hat_revisions.json",
    ],
)
def test_hat_overlay_change_runs_install_pi(harness: Harness, path: str) -> None:
    # install_pi.sh compiles every revision's overlay into /boot/firmware and
    # picks the HAT's GNSS UART from the revision table; a pull alone would
    # leave the Pi booting the old overlay or gpsd on the old UART.
    harness.commit_upstream("overlay", **{path: "/dts-v1/;\n/plugin/;\n"})

    result = harness.run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"install_pi.sh: ran ({path} changed)" in _summary(result.stdout)


def test_full_forces_every_step(harness: Harness) -> None:
    result = harness.run("--full")

    assert result.returncode == 0, result.stdout + result.stderr
    summary = _summary(result.stdout)
    assert "dashboard build: ran (--full)" in summary
    assert "pip install: ran (by install_pi.sh)" in summary
    assert "install_pi.sh: ran (--full)" in summary
    assert harness.calls("pnpm build")
    assert harness.calls("install_pi.sh")
    assert harness.canary_restarts() == 1


def test_every_skip_is_printed_once_per_step(harness: Harness) -> None:
    result = harness.run()

    summary = _summary(result.stdout)
    for step in ("dashboard build:", "pip install:", "install_pi.sh:"):
        assert summary.count(step) == 1, summary


# ---------------------------------------------------------------------------
# S02.5 — rollback
# ---------------------------------------------------------------------------


def test_failed_health_rolls_back_to_before_and_is_healthy(harness: Harness) -> None:
    before = harness.head()
    after = harness.commit_upstream(
        "ui", **{"src/dashboard/static/index.html": "<html>v2</html>\n"}
    )

    result = harness.run(SHIM_HEALTH_FAIL_CALLS_1="-1", SHIM_HEALTH_FAIL_CALLS_2="0")

    assert result.returncode == EXIT_ROLLED_BACK, result.stdout + result.stderr
    assert harness.head() == before, "checkout reset to the recorded <before>"
    assert harness.calls(f"runuser -u tester -- git -C {harness.checkout} reset --hard {before}")
    assert harness.canary_restarts() == 2
    summary = _summary(result.stdout)
    assert f"source: {before} -> {after}" in summary
    assert "dashboard build: ran (src/dashboard/static changed)" in summary
    # The rollback redid the step the reverse diff needs.
    assert "rollback dashboard build: ran (src/dashboard/static changed)" in summary
    assert "health: FAILED" in summary
    assert f"rollback: rolled back to {before}; healthy after" in summary
    assert f"result: update failed; rolled back to {before} (exit 6)" in summary
    assert len(harness.calls("pnpm build")) == 2


def test_rollback_keeps_the_pis_models(harness: Harness) -> None:
    before = harness.head()
    harness.commit_upstream("models", **{MANIFEST: '{"v": "laptop"}\n'})
    (harness.checkout / MANIFEST).write_text('{"v": "pi"}\n')

    result = harness.run(SHIM_HEALTH_FAIL_CALLS_1="-1", SHIM_HEALTH_FAIL_CALLS_2="0")

    assert result.returncode == EXIT_ROLLED_BACK, result.stdout + result.stderr
    assert harness.head() == before
    assert (harness.checkout / MANIFEST).read_text() == '{"v": "pi"}\n'


def test_rollback_that_also_fails_says_so_with_its_own_code(harness: Harness) -> None:
    before = harness.head()
    harness.commit_upstream("docs", **{"docs/readme.md": "new docs\n"})

    result = harness.run(SHIM_HEALTH_FAIL_CALLS_1="-1", SHIM_HEALTH_FAIL_CALLS_2="-1")

    assert result.returncode == EXIT_ROLLBACK_FAILED, result.stdout + result.stderr
    assert harness.head() == before
    assert harness.canary_restarts() == 2
    summary = _summary(result.stdout)
    assert f"rollback: rolled back to {before}; still no answer" in summary
    assert "result: update failed and rollback failed (exit 7)" in summary
    assert "journalctl -u canary" in result.stderr


def test_no_rollback_leaves_the_failed_version_in_place(harness: Harness) -> None:
    after = harness.commit_upstream("docs", **{"docs/readme.md": "new docs\n"})

    result = harness.run("--no-rollback", SHIM_HEALTH_FAIL_CALLS_1="-1")

    assert result.returncode == EXIT_LEFT_BROKEN, result.stdout + result.stderr
    assert harness.head() == after
    assert harness.canary_restarts() == 1
    assert not [c for c in harness.calls("runuser") if "reset --hard" in c]
    summary = _summary(result.stdout)
    assert f"rollback: skipped (--no-rollback); {after} left in place" in summary
    assert "result: update failed; left in place for debugging (exit 8)" in summary


def test_failed_restart_with_nothing_older_does_not_reset(harness: Harness) -> None:
    before = harness.head()

    result = harness.run(SHIM_HEALTH_FAIL_CALLS_1="-1")

    assert result.returncode == EXIT_LEFT_BROKEN, result.stdout + result.stderr
    assert harness.head() == before
    assert not [c for c in harness.calls("runuser") if "reset --hard" in c]
    assert f"rollback: nothing to roll back to (already at {before})" in _summary(result.stdout)


def test_failed_deploy_step_rolls_back_before_restarting(harness: Harness) -> None:
    before = harness.head()
    harness.commit_upstream("ui", **{"src/dashboard/static/index.html": "<html>v2</html>\n"})

    # The forward build fails; the rollback build (phase 2 of the same shim)
    # must succeed, so key the failure off the log rather than a fixed exit.
    pnpm = harness.bin_dir / "pnpm"
    _write_exec(
        pnpm,
        "#!/usr/bin/env bash\n"
        + _LOG_PRELUDE
        + """
if [[ "$1" == "build" ]]; then
  n=$(grep -c '^pnpm build' "${SHIM_LOG}")
  if [[ "${n}" -eq 1 ]]; then exit 1; fi
  mkdir -p dist && echo built >dist/index.html
fi
exit 0
""",
    )

    result = harness.run()

    assert result.returncode == EXIT_ROLLED_BACK, result.stdout + result.stderr
    assert harness.head() == before
    assert harness.canary_restarts() == 1, "no restart of code whose build failed"
    summary = _summary(result.stdout)
    assert "restart: not attempted (deploy step failed, exit 1)" in summary
    assert f"rollback: rolled back to {before}; healthy after" in summary


# ---------------------------------------------------------------------------
# S02.7 — summary and exit codes
# ---------------------------------------------------------------------------


def test_summary_block_names_every_outcome(harness: Harness) -> None:
    before = harness.head()
    after = harness.commit_upstream("docs", **{"docs/readme.md": "new docs\n"})
    harness.cgroup_dir.mkdir()
    (harness.cgroup_dir / "memory.max").write_text("max\n")

    result = harness.run()

    assert result.returncode == 0, result.stdout + result.stderr
    summary = _summary(result.stdout)
    lines = [line for line in summary.splitlines() if line]
    assert lines[0] == f"source: {before} -> {after}"
    assert lines[1].startswith("dashboard build: ")
    assert lines[2].startswith("pip install: ")
    assert lines[3].startswith("install_pi.sh: ")
    assert lines[4] == "restart: ok"
    assert lines[5].startswith("health: ok after ") and lines[5].endswith(" s")
    assert lines[6].startswith("memory limits: enforced (memory.max max")
    assert lines[7] == "rollback: none"
    assert lines[8] == "result: updated (exit 0)"
    assert lines[9].startswith("total: ") and lines[9].endswith(" s")


def test_usage_documents_the_exit_codes(harness: Harness) -> None:
    result = harness.run("--help")

    assert result.returncode == 0
    for code, phrase in [
        (3, "capture or ECU backup is active"),
        (4, "tracked local changes"),
        (5, "fetch or fast-forward failed"),
        (6, "rolled back"),
        (7, "rollback did not come up"),
        (8, "left in place"),
    ]:
        assert f"  {code}  " in result.stdout and phrase in result.stdout, result.stdout


def test_unknown_argument_is_a_usage_error(harness: Harness) -> None:
    result = harness.run("--bogus")

    assert result.returncode == 2
    assert "unknown argument: --bogus" in result.stderr


def test_docs_describe_the_same_exit_codes() -> None:
    doc = (REPO_ROOT / "docs" / "pi-setup.md").read_text()
    for code in range(3, 9):
        assert f"| {code} |" in doc, f"exit code {code} missing from docs/pi-setup.md"
