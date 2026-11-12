"""No tracked shell script restarts the ``canary`` daemon except ``restart_canary.sh``.

Capture state is ephemeral: a restarted daemon comes back on the ``default``
profile and the rest of the drive is recorded without the capture. On
2026-09-21 a deploy restarted the daemon four minutes into another session's
tuning drive and nothing reported it. ``scripts/restart_canary.sh`` refuses
while a capture or ECU backup is active; every other script goes through it.
``scripts/deploy_truck.sh`` was the one that did not (R30.1).

``canary-mcp`` and ``canary-provision`` are separate services and may be
restarted directly. ``install_pi.sh``'s ``restart_if_changed`` helper restarts
whatever it is given with a bare ``systemctl restart``, so a call that hands
it the daemon is caught here too.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GUARDED = "scripts/restart_canary.sh"

# `systemctl [opts] restart|try-restart|reload-or-restart ... canary[.service]`
# where `canary` is a whole unit name: `canary-mcp` and `canary-provision` do
# not match (a `-` or word character may not follow). Case-sensitive, so the
# sudoers `${SYSTEMCTL} restart canary` allowance in install_pi.sh is not a
# restart.
DIRECT_RESTART = re.compile(
    r"\bsystemctl\b[^\n]*?\brestart\s+(?:[^\n]*\s)?canary(?:\.service)?(?![\w.-])"
)
# The installer's helper wraps `systemctl restart "$@"`.
HELPER_RESTART = re.compile(
    r"\brestart_if_changed\s+\S+\s+(?:[^\n]*\s)?canary(?:\.service)?(?![\w.-])"
)


def tracked_shell_scripts() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "*.sh"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [line for line in out.splitlines() if line]


def restarts_of_daemon(text: str) -> list[str]:
    """Lines of ``text`` that restart the ``canary`` service, comments excluded."""
    return [
        line.strip()
        for line in text.splitlines()
        if not line.lstrip().startswith("#")
        and (DIRECT_RESTART.search(line) or HELPER_RESTART.search(line))
    ]


def test_only_restart_canary_sh_restarts_the_daemon():
    offenders = {}
    for rel in tracked_shell_scripts():
        if rel == GUARDED:
            continue
        hits = restarts_of_daemon((REPO_ROOT / rel).read_text())
        if hits:
            offenders[rel] = hits
    assert not offenders, (
        "A bare `systemctl restart canary` ends a running capture without a "
        "word (2026-09-21: a deploy killed another session's tuning drive four "
        f"minutes in). Restart the daemon through {GUARDED}, which refuses "
        f"while a capture or ECU backup is active: {offenders}"
    )


def test_the_guarded_script_does_restart_the_daemon():
    # The scan above is only meaningful if the pattern recognises the one
    # legitimate restart.
    assert restarts_of_daemon((REPO_ROOT / GUARDED).read_text())


def test_the_pattern_separates_the_daemon_from_its_sibling_services():
    daemon = [
        "systemctl restart canary.service",
        "sudo systemctl restart canary",
        "systemctl --no-block restart canary.service || true",
        "systemctl try-restart canary",
        "systemctl restart canary-mcp.service canary.service",
        "restart_if_changed canary canary.service",
    ]
    siblings = [
        "systemctl restart canary-mcp.service",
        "systemctl restart canary-provision.service",
        'systemctl restart "$@"',
        "systemctl restart gpsd.socket gpsd.service",
        "restart_if_changed gpsd gpsd.socket gpsd.service",
        "${SYSTEMCTL} restart canary.service",
        "# systemctl restart canary.service",
    ]
    for line in daemon:
        assert restarts_of_daemon(line), line
    for line in siblings:
        assert not restarts_of_daemon(line), line


def test_the_scan_catches_the_old_deploy_truck():
    # `git show` of the script as it was before R30.1 routed it through
    # restart_canary.sh: the line the guard exists for.
    old = "step '4/4'\nsystemctl enable canary.service\nsystemctl restart canary.service\n"
    assert restarts_of_daemon(old) == ["systemctl restart canary.service"]
