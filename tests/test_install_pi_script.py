"""Offline tests for ``scripts/install_pi.sh`` (S02.4, S02.6).

The installer is run for real, unprivileged, against a scratch filesystem:
``CANARY_INSTALL_ROOT`` puts every system path it writes (``/etc/...``,
``/data/canary``, ``/usr/local/sbin``, ``/var/log/journal``) under
``tmp_path``, and ``CANARY_INSTALL_ALLOW_UNPRIVILEGED=1`` lifts the root check.
Everything that would touch the host — ``systemctl``, ``udevadm``,
``apt-get``, ``chown``, ``visudo``, ``runuser`` and friends — is a shim on
``PATH`` that logs its argv. The checkout is a scratch directory holding a copy
of the real installer and the real files it installs; ``src`` is a symlink to
this repo's, so the GPS-source read goes through the project's own config
loader, as it does on the Pi.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install_pi.sh"
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
EXAMPLE_CONFIG = REPO_ROOT / "canary.pi.example.yaml"

GPS_DEVICE = "/dev/serial/by-id/usb-test-gps-if00-port0"

pytestmark = [
    pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash"),
    pytest.mark.skipif(not VENV_PYTHON.exists(), reason="needs the project .venv"),
]


def _write_exec(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)


_LOG_PRELUDE = 'printf \'%s %s\\n\' "$(basename "$0")" "$*" >>"${SHIM_LOG}"\n'
_LOGGING_SHIM = "#!/usr/bin/env bash\n" + _LOG_PRELUDE + "exit 0\n"

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

# Stands in for scripts/install_deps.py: the venv is not rebuilt here.
_INSTALL_DEPS_STUB = (
    "import os, sys\n"
    "with open(os.environ['SHIM_LOG'], 'a') as log:\n"
    "    log.write('install_deps.py ' + ' '.join(sys.argv[1:]) + '\\n')\n"
)

_HOST_COMMANDS = (
    "systemctl",
    "udevadm",
    "apt-get",
    "apt-cache",
    "chown",
    "visudo",
    "modprobe",
    "systemd-tmpfiles",
    "journalctl",
    "groupadd",
    "useradd",
    "usermod",
    # The canary user/group "exist", so groupadd/useradd are not reached.
    "getent",
    "id",
)

_INSTALLED_UNIT_FILES = (
    "canary.service",
    "canary-mcp.service",
    "ups-shutdown.service",
    "canary-modules.conf",
    "tmpfiles-canary.conf",
    "canary-gpsd@.service",
    "canary-chrony-pps.service",
    "can0.service",
    "70-canable-can0.rules",
    "canary-provision.service",
    "canary-update.path",
    "canary-update.service",
    "canary-firstboot.service",
)


@dataclass
class Installer:
    checkout: Path
    root: Path
    shim_log: Path
    env: dict[str, str]

    @property
    def config(self) -> Path:
        return self.root / "etc" / "canary" / "canary.yaml"

    def run(self, *, check: bool = True, **env: str) -> subprocess.CompletedProcess[str]:
        self.shim_log.write_text("")
        # The installer leaves the sudoers drop-in 0440 and rewrites it on the
        # next run; root writes through that mode, this unprivileged run cannot.
        sudoers = self.root / "etc" / "sudoers.d" / "canary-restart"
        if sudoers.exists():
            sudoers.chmod(0o640)
        result = subprocess.run(
            ["bash", str(self.checkout / "scripts" / "install_pi.sh")],
            capture_output=True,
            text=True,
            env={**self.env, **env},
            timeout=120,
        )
        if check:
            assert result.returncode == 0, result.stdout + result.stderr
        return result

    def calls(self, prefix: str) -> list[str]:
        return [line for line in self.shim_log.read_text().splitlines() if line.startswith(prefix)]

    def restarts(self) -> list[str]:
        return self.calls("systemctl restart")


def _fake_net_dir(tmp_path: Path, **macs: str) -> Path:
    net = tmp_path / "sys-class-net"
    for iface, mac in {"lo": "00:00:00:00:00:00", **macs}.items():
        (net / iface).mkdir(parents=True, exist_ok=True)
        (net / iface / "address").write_text(mac + "\n")
    return net


@pytest.fixture
def installer(tmp_path: Path) -> Installer:
    checkout = tmp_path / "checkout"
    (checkout / "scripts").mkdir(parents=True)
    shutil.copy(INSTALL_SCRIPT, checkout / "scripts" / "install_pi.sh")
    shutil.copy(REPO_ROOT / "scripts" / "update_canary.sh", checkout / "scripts")
    shutil.copy(REPO_ROOT / "scripts" / "data_drive.sh", checkout / "scripts")
    (checkout / "scripts" / "lib").mkdir()
    for lib in ("canary_hostname.sh", "dashboard_build.sh"):
        shutil.copy(REPO_ROOT / "scripts" / "lib" / lib, checkout / "scripts" / "lib")
    (checkout / "scripts" / "install_deps.py").write_text(_INSTALL_DEPS_STUB)
    shutil.copy(EXAMPLE_CONFIG, checkout / "canary.pi.example.yaml")
    (checkout / "systemd").mkdir()
    for name in _INSTALLED_UNIT_FILES:
        shutil.copy(REPO_ROOT / "systemd" / name, checkout / "systemd" / name)
    (checkout / "src").symlink_to(REPO_ROOT / "src")
    # The CANary-HAT revision table and its reader, which pick the HAT's GNSS
    # UART and say which overlay a board loads.
    firmware = checkout / "hardware" / "canary-hat-firmware"
    firmware.mkdir(parents=True)
    for name in ("hat_rev.py", "hat_revisions.json"):
        shutil.copy(REPO_ROOT / "hardware" / "canary-hat-firmware" / name, firmware)
    # A wrapper, not a symlink: the venv is found relative to the interpreter
    # path, so a symlinked copy would run without the venv's site-packages.
    _write_exec(checkout / ".venv" / "bin" / "python", f'#!/bin/sh\nexec "{VENV_PYTHON}" "$@"\n')

    root = tmp_path / "root"
    for rel in (
        "etc/default",
        "etc/udev/rules.d",
        "etc/modules-load.d",
        "etc/tmpfiles.d",
        "etc/sudoers.d",
        "etc/systemd/system",
        "etc/chrony",
        "usr/local/sbin",
    ):
        (root / rel).mkdir(parents=True)
    (root / "etc" / "chrony" / "chrony.conf").write_text(
        "pool 2.debian.pool.ntp.org iburst\nconfdir /etc/chrony/conf.d\n"
    )

    bin_dir = tmp_path / "bin"
    for name in _HOST_COMMANDS:
        _write_exec(bin_dir / name, _LOGGING_SHIM)
    _write_exec(bin_dir / "runuser", _RUNUSER_SHIM)

    shim_log = tmp_path / "shim.log"
    scratch = tmp_path / "tmpdir"
    scratch.mkdir()
    env = {
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "HOME": str(tmp_path),
        "TMPDIR": str(scratch),
        "CANARY_INSTALL_ALLOW_UNPRIVILEGED": "1",
        "CANARY_INSTALL_ROOT": str(root),
        "CANARY_HOME": str(checkout),
        "CANARY_PYTHON": sys.executable,
        "CANARY_GPS_DEVICE": GPS_DEVICE,
        "SHIM_LOG": str(shim_log),
        # Adopting a drive mounts real block devices; the guard (fstab-driven)
        # still runs.
        "CANARY_DATA_DRIVE_ADOPT": "0",
        # Node/pnpm: off here, exercised with a fake download below.
        "CANARY_DASHBOARD_TOOLS": "0",
        # The bundle build: off here, exercised against a scratch dashboard
        # below (src/ is this repo's, and its dist/ is not the test's to write).
        "CANARY_DASHBOARD_BUILD": "0",
        # The laptop's own interfaces must not name the scratch Pi.
        "CANARY_NET_DIR": str(_fake_net_dir(tmp_path, eth0="88:a2:9e:1d:89:74")),
        # Nor its HAT, if it has one: no EEPROM unless a test writes one.
        "CANARY_HAT_PRODUCT_FILE": str(tmp_path / "no-hat" / "product"),
        "CANARY_HAT_PRODUCT_VER_FILE": str(tmp_path / "no-hat" / "product_ver"),
    }
    return Installer(checkout=checkout, root=root, shim_log=shim_log, env=env)


# ---------------------------------------------------------------------------
# S02.4 — restart a service only when the file written for it changed
# ---------------------------------------------------------------------------


def test_first_install_restarts_gpsd_chrony_and_journald(installer: Installer) -> None:
    result = installer.run()

    assert installer.restarts() == [
        "systemctl restart gpsd.socket gpsd.service",
        "systemctl restart chrony",
        "systemctl restart systemd-journald",
    ]
    assert installer.calls("udevadm trigger"), "first install must replay the tty add events"
    assert "unchanged:" not in result.stdout
    gpsd_defaults = (installer.root / "etc" / "default" / "gpsd").read_text()
    assert 'GPSD_OPTIONS="-n"' in gpsd_defaults
    rule = (installer.root / "etc" / "udev" / "rules.d" / "70-canary-gps.rules").read_text()
    assert 'SYMLINK=="serial/by-id/usb-test-gps-if00-port0"' in rule
    for path in (
        installer.root / "etc" / "chrony" / "conf.d" / "canary-gps.conf",
        installer.root / "etc" / "systemd" / "journald.conf.d" / "90-canary-persistent.conf",
        installer.root / "etc" / "systemd" / "system" / "canary-gpsd@.service",
    ):
        assert path.is_file(), path
        assert oct(path.stat().st_mode & 0o777) == "0o644", path


def test_the_daemon_config_is_owned_by_canary_and_the_secrets_stay_root(
    installer: Installer,
) -> None:
    """Editing or migrating canary.yaml must not need root (2026-10-04: the
    channel-rename migration had to run under sudo for one config line). The
    directory too, since rewrites go through temp file + rename. Re-applied on
    every run, so an older install is fixed by the next one."""
    for _ in range(2):
        installer.run()
        chowns = installer.calls("chown")
        config = installer.root / "etc" / "canary"
        assert f"chown canary:canary {config} {config / 'canary.yaml'}" in chowns, chowns
        assert f"chown root:canary {config / 'canary.env'}" in chowns
        assert not any("control.token" in c and "canary:canary" in c for c in chowns)


def test_the_daemon_joins_the_hat_device_groups(installer: Installer) -> None:
    """Rev 2.1's ADC is /dev/spidev0.1, group spi on Raspberry Pi OS; the IMU
    and the K-line lines are i2c and gpio. Nothing else grants them."""
    installer.run()
    usermods = installer.calls("usermod")
    for group in ("i2c", "gpio", "spi"):
        assert f"usermod -aG {group} canary" in usermods, usermods


def test_second_run_on_an_unchanged_system_restarts_nothing(installer: Installer) -> None:
    installer.run()
    result = installer.run()

    assert installer.restarts() == []
    for service in ("gpsd", "chrony", "systemd-journald"):
        assert f"unchanged: {service} not restarted" in result.stdout, result.stdout
    assert not installer.calls("udevadm trigger")
    # daemon-reload stays unconditional: it is cheap and restarts nothing.
    assert installer.calls("systemctl daemon-reload")
    # An unchanged service is still started, which is a no-op if it is running.
    assert "systemctl start gpsd.socket gpsd.service" in installer.calls("systemctl start")


@pytest.mark.parametrize(
    ("rel_path", "service", "restart"),
    [
        ("etc/default/gpsd", "gpsd", "systemctl restart gpsd.socket gpsd.service"),
        (
            "etc/udev/rules.d/70-canary-gps.rules",
            "gpsd",
            "systemctl restart gpsd.socket gpsd.service",
        ),
        (
            "etc/systemd/system/canary-gpsd@.service",
            "gpsd",
            "systemctl restart gpsd.socket gpsd.service",
        ),
        ("etc/chrony/conf.d/canary-gps.conf", "chrony", "systemctl restart chrony"),
        (
            "etc/systemd/journald.conf.d/90-canary-persistent.conf",
            "systemd-journald",
            "systemctl restart systemd-journald",
        ),
    ],
)
def test_a_changed_file_restarts_only_its_own_service(
    installer: Installer, rel_path: str, service: str, restart: str
) -> None:
    installer.run()
    target = installer.root / rel_path
    managed = target.read_text()
    target.write_text(managed + "# an older revision\n")

    result = installer.run()

    assert installer.restarts() == [restart]
    assert target.read_text() == managed, "the managed content is restored"
    assert f"unchanged: {service} not restarted" not in result.stdout
    others = {"gpsd", "chrony", "systemd-journald"} - {service}
    for other in others:
        assert f"unchanged: {other} not restarted" in result.stdout


def test_a_new_gps_device_rewrites_the_rule_and_restarts_gpsd(installer: Installer) -> None:
    installer.run()
    result = installer.run(CANARY_GPS_DEVICE="/dev/serial/by-id/usb-other-gps")

    assert installer.restarts() == ["systemctl restart gpsd.socket gpsd.service"]
    assert installer.calls("udevadm trigger")
    rule = (installer.root / "etc" / "udev" / "rules.d" / "70-canary-gps.rules").read_text()
    assert 'SYMLINK=="serial/by-id/usb-other-gps"' in rule
    assert "unchanged: chrony not restarted" in result.stdout


def test_adding_the_chrony_confdir_include_restarts_chrony(installer: Installer) -> None:
    installer.run()
    chrony_conf = installer.root / "etc" / "chrony" / "chrony.conf"
    chrony_conf.write_text("pool 2.debian.pool.ntp.org iburst\n")

    installer.run()

    assert installer.restarts() == ["systemctl restart chrony"]
    assert "confdir /etc/chrony/conf.d" in chrony_conf.read_text()


# ---------------------------------------------------------------------------
# S02.6 — say only what is true about GPS
# ---------------------------------------------------------------------------

_GPS_HINT = "hardware.gps.source: gpsd"


def test_no_gps_hint_when_the_config_already_says_gpsd(installer: Installer) -> None:
    # First install copies canary.pi.example.yaml, which says `source: gpsd`.
    result = installer.run()

    assert "source: gpsd" in installer.config.read_text()
    assert _GPS_HINT not in result.stdout
    assert "hardware.gps.source" not in result.stdout + result.stderr


def test_gps_hint_when_the_config_says_serial(installer: Installer) -> None:
    installer.config.parent.mkdir(parents=True)
    installer.config.write_text(
        EXAMPLE_CONFIG.read_text().replace("source: gpsd", "source: serial", 1)
    )

    result = installer.run()

    assert "Keeping existing" in result.stdout
    assert _GPS_HINT in result.stdout
    assert "is 'serial'" in result.stdout


def test_gps_hint_when_the_config_cannot_be_read(installer: Installer) -> None:
    installer.config.parent.mkdir(parents=True)
    installer.config.write_text("hardware: [not, a, mapping\n")

    result = installer.run()

    assert f"could not read hardware.gps.source from {installer.config}" in result.stdout
    assert _GPS_HINT in result.stdout


def test_no_gps_lines_when_the_gps_stack_is_skipped(installer: Installer) -> None:
    result = installer.run(CANARY_INSTALL_GPS_STACK="0")

    assert "hardware.gps.source" not in result.stdout
    assert "unchanged: gpsd" not in result.stdout
    assert installer.restarts() == ["systemctl restart systemd-journald"]


# ---------------------------------------------------------------------------
# CANary-HAT: its GNSS is the GPS, and the board overlay is installed
# ---------------------------------------------------------------------------


def _hat_product_file(installer: Installer, product: str = "CANary-HAT rev 2.0") -> Path:
    # /proc/device-tree strings are NUL-terminated.
    path = installer.root / "proc-hat-product"
    path.write_bytes(product.encode() + b"\0")
    return path


def _hat_eeprom(installer: Installer, rev: str) -> dict[str, Any]:
    """The env for a CANary-HAT whose EEPROM carries product_ver (eeprom_settings*.txt)."""
    ver = installer.root / "proc-hat-product_ver"
    ver.write_bytes({"2.0": b"0x0200", "2.1": b"0x0201"}[rev] + b"\0")
    return {
        "CANARY_HAT_PRODUCT_FILE": str(_hat_product_file(installer, f"CANary-HAT rev {rev}")),
        "CANARY_HAT_PRODUCT_VER_FILE": str(ver),
    }


def _gps_rule(installer: Installer) -> str:
    return (installer.root / "etc" / "udev" / "rules.d" / "70-canary-gps.rules").read_text()


def test_a_canary_hat_makes_its_gnss_uart_the_gps(installer: Installer) -> None:
    result = installer.run(
        CANARY_GPS_DEVICE="", CANARY_HAT_PRODUCT_FILE=str(_hat_product_file(installer))
    )

    assert "CANary-HAT detected (rev 2.0); GPS device: /dev/ttyAMA2" in result.stdout
    # A board UART has no by-id symlink, so a SYMLINK match would never fire.
    assert 'KERNEL=="ttyAMA2"' in _gps_rule(installer)
    assert "SYMLINK" not in _gps_rule(installer)


@pytest.mark.parametrize(("rev", "tty"), [("2.0", "ttyAMA2"), ("2.1", "ttyAMA4")])
def test_each_hat_revision_gets_its_own_gnss_uart(installer: Installer, rev: str, tty: str) -> None:
    """Rev 2.1 moved the receiver from uart2 to uart4; a rev 2.0 board is
    still in service, so the EEPROM's product_ver decides."""
    result = installer.run(CANARY_GPS_DEVICE="", **_hat_eeprom(installer, rev))

    assert f"CANary-HAT detected (rev {rev}); GPS device: /dev/{tty}" in result.stdout
    assert f'KERNEL=="{tty}"' in _gps_rule(installer)
    units = installer.root / "etc" / "systemd" / "system"
    baud = (units / f"canary-gpsd@{tty}.service.d" / "canary-baud.conf").read_text()
    assert f"/usr/bin/gpsctl -s 115200 /dev/{tty}" in baud


def test_an_unknown_hat_revision_picks_no_gnss_uart(installer: Installer) -> None:
    ver = installer.root / "proc-hat-product_ver"
    ver.write_bytes(b"0x0300\0")
    result = installer.run(
        CANARY_GPS_DEVICE="",
        CANARY_HAT_PRODUCT_FILE=str(_hat_product_file(installer, "CANary-HAT rev 3.0")),
        CANARY_HAT_PRODUCT_VER_FILE=str(ver),
    )

    assert "CANary-HAT of unknown revision" in result.stderr
    assert "CANary-HAT detected" not in result.stdout


def test_an_explicit_gps_device_beats_the_hat(installer: Installer) -> None:
    installer.run(CANARY_HAT_PRODUCT_FILE=str(_hat_product_file(installer)))

    assert 'SYMLINK=="serial/by-id/usb-test-gps-if00-port0"' in _gps_rule(installer)


def test_another_hat_is_not_mistaken_for_a_canary_hat(installer: Installer) -> None:
    result = installer.run(
        CANARY_GPS_DEVICE="",
        CANARY_HAT_PRODUCT_FILE=str(_hat_product_file(installer, "Some Other HAT")),
    )

    assert "CANary-HAT detected" not in result.stdout


# Stands in for dtc: "compiles" by copying the source to the -o path.
_DTC_SHIM = (
    "#!/usr/bin/env bash\n"
    + _LOG_PRELUDE
    + """
out=""; src=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -I|-O) shift 2 ;;
    -*) shift ;;
    *) src="$1"; shift ;;
  esac
done
cp "$src" "$out"
"""
)


def _with_overlay(installer: Installer, *names: str) -> Path:
    firmware = installer.checkout / "hardware" / "canary-hat-firmware"
    for name in names or ("canary-hat-overlay.dts",):
        shutil.copy(REPO_ROOT / "hardware" / "canary-hat-firmware" / name, firmware)
    overlays = installer.root / "boot" / "firmware" / "overlays"
    overlays.mkdir(parents=True)
    bin_dir = Path(installer.env["PATH"].split(":", 1)[0])
    _write_exec(bin_dir / "dtc", _DTC_SHIM)
    return overlays


def test_the_hat_overlay_is_installed_and_asks_for_a_reboot(installer: Installer) -> None:
    overlays = _with_overlay(installer)

    result = installer.run()

    assert (overlays / "canary-hat.dtbo").exists()
    assert "REBOOT REQUIRED: canary-hat.dtbo changed" in result.stdout


def test_an_unchanged_hat_overlay_asks_for_nothing(installer: Installer) -> None:
    _with_overlay(installer)
    installer.run()

    result = installer.run()

    assert "canary-hat.dtbo changed" not in result.stdout


_BOTH_OVERLAYS = ("canary-hat-overlay.dts", "canary-hat-r21-overlay.dts")


def test_every_revisions_overlay_is_installed(installer: Installer) -> None:
    overlays = _with_overlay(installer, *_BOTH_OVERLAYS)

    result = installer.run()

    assert (overlays / "canary-hat.dtbo").exists()
    assert (overlays / "canary-hat-r21.dtbo").exists()
    # No EEPROM says which one loads, so either changing needs a boot.
    assert "REBOOT REQUIRED: canary-hat.dtbo canary-hat-r21.dtbo changed" in result.stdout


@pytest.mark.parametrize(("rev", "loaded"), [("2.0", "canary-hat"), ("2.1", "canary-hat-r21")])
def test_only_the_fitted_boards_overlay_asks_for_a_reboot(
    installer: Installer, rev: str, loaded: str
) -> None:
    """A rev 2.0 Pi pulling rev 2.1's new overlay should not be told to reboot."""
    _with_overlay(installer, *_BOTH_OVERLAYS)

    result = installer.run(**_hat_eeprom(installer, rev))

    assert f"REBOOT REQUIRED: {loaded}.dtbo changed" in result.stdout


def test_no_overlay_dir_means_no_overlay_work(installer: Installer) -> None:
    result = installer.run()

    assert "canary-hat.dtbo" not in result.stdout + result.stderr
    assert not installer.calls("dtc")


def test_a_canary_hat_gets_a_stable_pps_name_but_no_fatal_refclock(
    installer: Installer,
) -> None:
    """chronyd exits when a `refclock PPS` device is missing; on 2026-09-23 that
    took the Pi's time sync down. The name rule stays; the direct refclock may
    not come back."""
    installer.run(CANARY_HAT_PRODUCT_FILE=str(_hat_product_file(installer)))

    chrony = (installer.root / "etc" / "chrony" / "conf.d" / "canary-gps.conf").read_text()
    assert "refclock PPS" not in chrony
    assert "refclock SHM 0 refid GPS" in chrony
    rule = (installer.root / "etc" / "udev" / "rules.d" / "70-canary-pps.rules").read_text()
    # The device's name reads `canary-pps.-1`, so an exact match never fires.
    assert 'ATTR{name}=="canary-pps*", SYMLINK+="pps-canary"' in rule


def _chrony_conf(installer: Installer) -> str:
    return (installer.root / "etc" / "chrony" / "conf.d" / "canary-gps.conf").read_text()


def test_a_canary_hat_offers_chrony_its_pps_only_when_the_device_exists(
    installer: Installer,
) -> None:
    """H03.3, 2026-10-04: chrony reads the PPS directly. The `refclock PPS` line
    is generated at each chrony start only when /dev/pps-canary exists, and
    pulled in by a glob include, which chrony ignores when nothing matches (a
    missing plain include is fatal)."""
    installer.run(CANARY_HAT_PRODUCT_FILE=str(_hat_product_file(installer)))

    chrony = _chrony_conf(installer)
    assert "include /run/canary-chrony-*.conf" in chrony
    assert "refclock SOCK" not in chrony
    units = installer.root / "etc" / "systemd" / "system"
    pre = (units / "chrony.service.d" / "canary-pps.conf").read_text()
    # `+`: chrony.service runs as _chrony under ProtectSystem=strict.
    assert "ExecStartPre=+/bin/sh -c" in pre
    assert "if [ -e /dev/pps-canary ]" in pre
    assert '"refclock PPS /dev/pps-canary refid PPS lock GPS" >/run/canary-chrony-pps.conf' in pre
    rule = (installer.root / "etc" / "udev" / "rules.d" / "70-canary-pps.rules").read_text()
    assert 'ENV{SYSTEMD_WANTS}+="canary-chrony-pps.service"' in rule
    late = (units / "canary-chrony-pps.service").read_text()
    assert "systemctl try-restart chrony.service" in late
    assert 'grep -qs "^refclock PPS /dev/pps-canary" /run/canary-chrony-pps.conf' in late


def test_a_canary_hat_states_its_measured_nmea_lag(installer: Installer) -> None:
    """With `precision 5e-1` chrony could not number the pulses and dropped
    every one. The HAT receiver's NMEA lag measured 62-68 ms at 115200."""
    installer.run(CANARY_HAT_PRODUCT_FILE=str(_hat_product_file(installer)))

    assert "refclock SHM 0 refid GPS precision 1e-1 offset 0.065" in _chrony_conf(installer)
    assert "refclock SHM 0 refid GPS precision 5e-1" not in _chrony_conf(installer)


def test_upgrading_removes_the_gpsd_pps_attachment(installer: Installer) -> None:
    """The 2026-09-23..10-04 path attached /dev/pps-canary to gpsd. gpsd never
    fed chrony from it, and it took SHM unit 0 from the receiver."""
    units = installer.root / "etc" / "systemd" / "system"
    (units / "gpsd.service.d").mkdir(parents=True)
    (units / "canary-gpsd-pps.service").write_text("[Unit]\n")
    (units / "gpsd.service.d" / "canary-reattach-pps.conf").write_text("[Service]\n")

    result = installer.run(CANARY_HAT_PRODUCT_FILE=str(_hat_product_file(installer)))

    assert not (units / "canary-gpsd-pps.service").exists()
    assert not (units / "gpsd.service.d" / "canary-reattach-pps.conf").exists()
    assert "systemctl stop canary-gpsd-pps.service" in installer.calls("systemctl stop")
    assert "systemctl restart chrony" in installer.restarts()
    assert "removed" in result.stdout


def test_without_a_hat_chrony_has_no_pps_source(installer: Installer) -> None:
    installer.run()

    chrony = (installer.root / "etc" / "chrony" / "conf.d" / "canary-gps.conf").read_text()
    assert "refclock PPS" not in chrony
    assert "refclock SOCK" not in chrony
    assert "include /run/canary-chrony" not in chrony
    assert "refclock SHM 0 refid GPS precision 5e-1 offset 0.0" in chrony
    assert not (installer.root / "etc" / "udev" / "rules.d" / "70-canary-pps.rules").exists()
    units = installer.root / "etc" / "systemd" / "system"
    assert not (units / "canary-chrony-pps.service").exists()
    assert not (units / "chrony.service.d" / "canary-pps.conf").exists()


def _unit_dir(installer: Installer) -> Path:
    return installer.root / "etc" / "systemd" / "system"


def test_every_gpsd_start_replays_the_receivers_add_event(installer: Installer) -> None:
    """2026-10-03: `apt upgrade` stopped gpsd and started it again. PartOf
    carries a stop to the attach unit but not a start, so GPS stayed dark
    until a reboot."""
    installer.run()

    dropin = (_unit_dir(installer) / "gpsd.service.d" / "canary-reattach.conf").read_text()
    assert f"ExecStartPost=-/usr/bin/udevadm trigger --action=add {GPS_DEVICE}" in dropin
    assert not (_unit_dir(installer) / "gpsd.service.d" / "canary-reattach-pps.conf").exists()


def test_a_canary_hat_reattaches_its_receiver_and_sets_the_gnss_baud(installer: Installer) -> None:
    """At 9600 baud the HAT's MAX-M10M overflowed its transmit buffer and its
    time ran ~14 s late (2026-10-04). It forgets the speed at every power cut,
    so it is set on every attach."""
    installer.run(CANARY_GPS_DEVICE="", CANARY_HAT_PRODUCT_FILE=str(_hat_product_file(installer)))

    units = _unit_dir(installer)
    reattach = (units / "gpsd.service.d" / "canary-reattach.conf").read_text()
    assert "udevadm trigger --action=add /dev/ttyAMA2" in reattach
    assert not (units / "gpsd.service.d" / "canary-reattach-pps.conf").exists()
    baud = (units / "canary-gpsd@ttyAMA2.service.d" / "canary-baud.conf").read_text()
    assert "/usr/bin/gpsctl -s 115200 /dev/ttyAMA2" in baud
    # gpsctl can hang and ignores SIGTERM; an unbounded try never retries.
    assert "/usr/bin/timeout -s KILL 20 /usr/bin/gpsctl" in baud
    assert "ExecStartPost=-" in baud


def test_the_gps_stack_skips_the_gui_clients(installer: Installer) -> None:
    """gpsd-clients depends on GTK and matplotlib on trixie: 384 MB on a fresh
    card for gpspipe alone (2026-10-11). gpsd carries every tool the stack runs."""
    installer.run()

    (install,) = [c for c in installer.calls("apt-get") if " install " in f" {c} "]
    assert "--no-install-recommends" in install
    assert " gpsd " in f" {install} " and " chrony" in install
    assert "gpsd-clients" not in install


def test_a_usb_receiver_gets_no_baud_change(installer: Installer) -> None:
    installer.run()

    assert not list(_unit_dir(installer).glob("canary-gpsd@*.service.d"))


def _attach_dropin(installer: Installer, tty: str) -> str:
    return (_unit_dir(installer) / f"canary-gpsd@{tty}.service.d" / "canary-baud.conf").read_text()


def _exec_start_post_script(dropin: str) -> str:
    """The ``sh -c`` body of the drop-in's ExecStartPost, as systemd reads it:
    a trailing backslash joins the next line with a space."""
    joined = re.sub(r"\\\n", " ", dropin)
    (line,) = [ln for ln in joined.splitlines() if ln.startswith("ExecStartPost=")]
    prefix = "ExecStartPost=-/bin/sh -c '"
    assert line.startswith(prefix) and line.endswith("'"), line
    body = line[len(prefix) : -1]
    # systemd would expand these; the script must not lean on either.
    assert "$" not in body and "%" not in body
    return body


@pytest.mark.parametrize(("rev", "tty"), [("2.0", "ttyAMA2"), ("2.1", "ttyAMA4")])
def test_each_hat_revision_sets_its_receiver_to_10_hz_on_every_attach(
    installer: Installer, rev: str, tty: str
) -> None:
    """H03.4: the MAX-M10M has no backup cell, so a RAM CFG-VALSET of
    CFG-RATE-MEAS 100 ms is lost at every power cut and is sent on every attach,
    on whichever UART the revision wires it to."""
    installer.run(CANARY_GPS_DEVICE="", **_hat_eeprom(installer, rev))

    dropin = _attach_dropin(installer, tty)
    script = _exec_start_post_script(dropin)
    target = f"localhost:2947:/dev/{tty}"
    rate = f"/usr/bin/ubxtool -P 34 -z CFG-RATE-MEAS,100,1 {target}"
    gsv = f"/usr/bin/ubxtool -P 34 -z CFG-MSGOUT-NMEA_ID_GSV_UART1,10,1 {target}"
    assert rate in script and gsv in script
    # Speed first, then GSV to 1 Hz, then the 10 Hz rate: never 10 Hz of the
    # full NMEA set on a link that may still be at the power-up 9600 baud.
    assert script.index("gpsctl -s 115200") < script.index(gsv) < script.index(rate)
    # ubxtool talks to the port through gpsd and, like gpsctl, may hang.
    assert script.count("/usr/bin/timeout -s KILL 20 /usr/bin/ubxtool") == 2
    assert "ExecStartPost=-" in dropin
    assert "100 ms measurement rate" in dropin


def _run_attach_script(installer: Installer, script: str, *, gpsctl_ok: bool) -> list[str]:
    """Run the drop-in's script with gpsctl/ubxtool/timeout/sleep as logging
    shims; returns the commands it ran, in order."""
    shims = installer.root.parent / "attach-shims"
    log = shims / "calls.log"
    shims.mkdir()
    log.write_text("")
    logger = f'#!/bin/sh\necho "$(basename "$0") $*" >>"{log}"\n'
    _write_exec(shims / "gpsctl", logger + ("exit 0\n" if gpsctl_ok else "exit 1\n"))
    _write_exec(shims / "ubxtool", logger + "exit 0\n")
    _write_exec(shims / "timeout", '#!/bin/sh\nshift 3\nexec "$@"\n')
    _write_exec(shims / "sleep", "#!/bin/sh\nexit 0\n")
    result = subprocess.run(
        ["/bin/sh", "-c", script.replace("/usr/bin/", f"{shims}/")],
        capture_output=True,
        text=True,
        env={"PATH": f"{shims}:/usr/bin:/bin"},
        timeout=30,
    )
    assert (result.returncode == 0) == gpsctl_ok, result.stderr
    return log.read_text().splitlines()


def test_the_rate_is_set_only_once_the_baud_is(installer: Installer) -> None:
    """The drop-in's own script, run against shims: 10 Hz goes out only after
    gpsctl moved the port off 9600, and never when it could not."""
    installer.run(CANARY_GPS_DEVICE="", **_hat_eeprom(installer, "2.0"))
    script = _exec_start_post_script(_attach_dropin(installer, "ttyAMA2"))

    calls = _run_attach_script(installer, script, gpsctl_ok=True)
    assert [c.split()[0] for c in calls] == ["gpsctl", "ubxtool", "ubxtool"]
    assert "CFG-MSGOUT-NMEA_ID_GSV_UART1,10,1" in calls[1]
    assert "CFG-RATE-MEAS,100,1" in calls[2]

    shutil.rmtree(installer.root.parent / "attach-shims")
    calls = _run_attach_script(installer, script, gpsctl_ok=False)
    assert calls == ["gpsctl -s 115200 /dev/ttyAMA2"] * 10


def test_the_gnss_rate_override_keeps_gsv_at_1_hz(installer: Installer) -> None:
    installer.run(
        CANARY_GPS_DEVICE="", CANARY_HAT_GPS_RATE_MS="200", **_hat_eeprom(installer, "2.1")
    )

    script = _exec_start_post_script(_attach_dropin(installer, "ttyAMA4"))
    assert "CFG-RATE-MEAS,200,1" in script
    assert "CFG-MSGOUT-NMEA_ID_GSV_UART1,5,1" in script


@pytest.mark.parametrize(
    ("env", "refusal"),
    [
        # At 9600 even 1 Hz overflowed the M10 (2026-10-04); H03.4's floor.
        ({"CANARY_HAT_GPS_BAUD": "9600"}, "cannot carry a 100 ms GNSS rate"),
        ({"CANARY_HAT_GPS_BAUD": "115200;reboot"}, "is not a baud rate"),
        ({"CANARY_HAT_GPS_RATE_MS": "10"}, "is not a 25-1000 ms measurement period"),
        ({"CANARY_HAT_GPS_RATE_MS": "0.1"}, "is not a 25-1000 ms measurement period"),
    ],
)
def test_a_gnss_setting_the_link_cannot_carry_is_refused(
    installer: Installer, env: dict[str, str], refusal: str
) -> None:
    result = installer.run(
        check=False, CANARY_GPS_DEVICE="", **_hat_eeprom(installer, "2.0"), **env
    )

    assert result.returncode != 0
    assert "REFUSING" in result.stderr and refusal in result.stderr
    assert not list(_unit_dir(installer).glob("canary-gpsd@*.service.d"))


def test_1_hz_at_9600_baud_is_still_allowed(installer: Installer) -> None:
    installer.run(
        CANARY_GPS_DEVICE="",
        CANARY_HAT_GPS_BAUD="9600",
        CANARY_HAT_GPS_RATE_MS="1000",
        **_hat_eeprom(installer, "2.0"),
    )

    script = _exec_start_post_script(_attach_dropin(installer, "ttyAMA2"))
    assert "CFG-RATE-MEAS,1000,1" in script
    assert "CFG-MSGOUT-NMEA_ID_GSV_UART1,1,1" in script


def test_fitting_a_hat_later_restarts_chrony_once(installer: Installer) -> None:
    installer.run()
    hat = str(_hat_product_file(installer))
    installer.run(CANARY_HAT_PRODUCT_FILE=hat)
    assert "systemctl restart chrony" in installer.restarts()
    installer.run(CANARY_HAT_PRODUCT_FILE=hat)
    assert "systemctl restart chrony" not in installer.restarts()


def _cmdline(installer: Installer, text: str) -> Path:
    path = installer.root / "cmdline.txt"
    path.write_text(text + "\n")
    return path


def _serial_dir(installer: Installer, serial0_target: str) -> Path:
    dev = installer.root / "fake-dev"
    dev.mkdir(exist_ok=True)
    (dev / "serial0").symlink_to(serial0_target)
    return dev


def test_a_console_on_uart0_is_refused_with_a_hat(installer: Installer) -> None:
    result = installer.run(
        check=False,
        CANARY_HAT_PRODUCT_FILE=str(_hat_product_file(installer)),
        CANARY_CMDLINE=str(_cmdline(installer, "console=ttyAMA0,115200 root=PARTUUID=1 rootwait")),
    )
    assert result.returncode == 1
    assert "REFUSING: console=ttyAMA0 is uart0" in result.stderr


def test_serial0_is_refused_only_when_it_points_at_uart0(installer: Installer) -> None:
    hat = str(_hat_product_file(installer))
    cmdline = str(_cmdline(installer, "console=serial0,115200 console=tty1 root=PARTUUID=1"))
    refused = installer.run(
        check=False,
        CANARY_HAT_PRODUCT_FILE=hat,
        CANARY_CMDLINE=cmdline,
        CANARY_SERIAL_DEV_DIR=str(_serial_dir(installer, "ttyAMA0")),
    )
    assert refused.returncode == 1 and "console=serial0 is uart0" in refused.stderr


def test_the_pi5_default_console_passes(installer: Installer) -> None:
    """What the HAT Pi actually has: serial0 -> ttyAMA10, the debug UART."""
    result = installer.run(
        CANARY_HAT_PRODUCT_FILE=str(_hat_product_file(installer)),
        CANARY_CMDLINE=str(
            _cmdline(installer, "console=serial0,115200 console=tty1 root=PARTUUID=1")
        ),
        CANARY_SERIAL_DEV_DIR=str(_serial_dir(installer, "ttyAMA10")),
    )
    assert "REFUSING" not in result.stderr


def test_without_a_hat_the_console_is_not_the_installers_business(installer: Installer) -> None:
    installer.run(CANARY_CMDLINE=str(_cmdline(installer, "console=ttyAMA0,115200 root=PARTUUID=1")))


def _boot_config(installer: Installer, text: str) -> Path:
    path = installer.root / "config.txt"
    path.write_text(text)
    return path


def test_a_hat_gets_the_arm_i2c_bus_once(installer: Installer) -> None:
    """A fresh image has no /dev/i2c-1, so the HAT's IMU, barometer and 12 V
    sense were all dark on a new board (2026-10-11). The line goes under its
    own [all]: the stock file ends inside a [pi5] section."""
    config = _boot_config(installer, "#dtparam=i2c_arm=on\n[pi5]\ndtoverlay=nospi10\n")
    hat = str(_hat_product_file(installer))

    result = installer.run(CANARY_HAT_PRODUCT_FILE=hat, CANARY_BOOT_CONFIG=str(config))
    assert config.read_text().endswith("[all]\n" + _I2C_COMMENT + "dtparam=i2c_arm=on\n")
    assert "dtparam=i2c_arm=on was added" in result.stdout
    assert list(installer.root.glob("config.txt.canary-*"))

    again = installer.run(CANARY_HAT_PRODUCT_FILE=hat, CANARY_BOOT_CONFIG=str(config))
    assert config.read_text().count("\ndtparam=i2c_arm=on") == 1
    assert "dtparam=i2c_arm=on was added" not in again.stdout


_I2C_COMMENT = (
    "# Added by canary scripts/install_pi.sh: the CANary-HAT sensors are on /dev/i2c-1.\n"
)


def test_an_i2c_bus_already_on_is_left_alone(installer: Installer) -> None:
    text = "dtparam=audio=on,i2c_arm=on\n"
    config = _boot_config(installer, text)
    installer.run(
        CANARY_HAT_PRODUCT_FILE=str(_hat_product_file(installer)), CANARY_BOOT_CONFIG=str(config)
    )
    assert config.read_text() == text


def test_without_a_hat_the_i2c_bus_is_not_touched(installer: Installer) -> None:
    config = _boot_config(installer, "[pi5]\ndtoverlay=nospi10\n")
    installer.run(CANARY_BOOT_CONFIG=str(config))
    assert "i2c_arm" not in config.read_text()


# ---------------------------------------------------------------------------
# Data drive (2026-10-10): the services require the mount that holds /data
# ---------------------------------------------------------------------------


def _dropins(installer: Installer) -> list[Path]:
    unit_dir = installer.root / "etc" / "systemd" / "system"
    return [unit_dir / f"{u}.service.d" / "data-drive.conf" for u in ("canary", "canary-mcp")]


def test_a_data_drive_in_fstab_makes_both_services_require_it(installer: Installer) -> None:
    (installer.root / "etc" / "fstab").write_text(
        "PARTUUID=c0579362-02  /  ext4  defaults,noatime  0  1\n"
        "UUID=59c3d070-69dc-45fb-aadc-195135cbc0b2 /data ext4 defaults,noatime,nofail 0 2\n"
    )
    installer.run()
    for dropin in _dropins(installer):
        assert "RequiresMountsFor=/data\n" in dropin.read_text()


def test_no_data_drive_no_guard_and_removing_the_entry_removes_it(
    installer: Installer,
) -> None:
    fstab = installer.root / "etc" / "fstab"
    root_line = "PARTUUID=c0579362-02  /  ext4  defaults,noatime  0  1\n"
    fstab.write_text(root_line)
    installer.run()
    assert not any(d.exists() for d in _dropins(installer))

    fstab.write_text(root_line + "UUID=x /data ext4 defaults,nofail 0 2\n")
    installer.run()
    assert all(d.exists() for d in _dropins(installer))

    fstab.write_text(root_line)
    installer.run()
    assert not any(d.exists() for d in _dropins(installer))


# ---------------------------------------------------------------------------
# Hostname (2026-10-11): canary-<MAC suffix>, so two CANaries can share a LAN
# ---------------------------------------------------------------------------


def _hostname(installer: Installer, name: str | None) -> Path:
    path = installer.root / "etc" / "hostname"
    if name is not None:
        path.write_text(name + "\n")
    (installer.root / "etc" / "hosts").write_text(
        f"127.0.0.1\tlocalhost\n127.0.1.1\t{name or 'raspberrypi'}\n"
    )
    return path


def test_a_first_install_names_the_pi_from_its_mac(installer: Installer) -> None:
    """Two Pis answering as canary.local raced for the name on one LAN."""
    hostname = _hostname(installer, "canary")
    result = installer.run()

    assert hostname.read_text() == "canary-8974\n"
    hosts = (installer.root / "etc" / "hosts").read_text()
    assert "127.0.1.1\tcanary-8974\n" in hosts and "127.0.0.1\tlocalhost" in hosts
    assert "This Pi answers as canary-8974.local" in result.stdout


def test_a_rerun_never_renames(installer: Installer) -> None:
    """canary-update re-runs the installer; the dev Pi stays `canary`."""
    hostname = _hostname(installer, "canary")
    installer.config.parent.mkdir(parents=True, exist_ok=True)
    installer.config.write_text("existing: config\n")

    installer.run()
    assert hostname.read_text() == "canary\n"


def test_a_chosen_hostname_is_kept(installer: Installer) -> None:
    hostname = _hostname(installer, "truckpi")
    installer.run()
    assert hostname.read_text() == "truckpi\n"


def test_without_eth0_another_interface_names_it(installer: Installer) -> None:
    hostname = _hostname(installer, "raspberrypi")
    net = _fake_net_dir(installer.root.parent / "other", wlan0="D8:3A:DD:00:AB:CD")
    installer.run(CANARY_NET_DIR=str(net))
    assert hostname.read_text() == "canary-abcd\n"


def test_an_explicit_hostname_wins_and_is_validated(installer: Installer) -> None:
    hostname = _hostname(installer, "truckpi")
    installer.run(CANARY_HOSTNAME="canary-garage")
    assert hostname.read_text() == "canary-garage\n"

    refused = installer.run(check=False, CANARY_HOSTNAME="Bad_Name")
    assert refused.returncode == 1 and "REFUSING hostname" in refused.stderr


def test_can0_comes_up_on_every_boot(installer: Installer) -> None:
    """can0.service lived only in deploy_truck.sh, so a docs-installed Pi lost
    its bus at the first reboot (2026-10-11)."""
    installer.run()

    unit = installer.root / "etc" / "systemd" / "system" / "can0.service"
    assert "BindsTo=sys-subsystem-net-devices-can0.device" in unit.read_text()
    rule = installer.root / "etc" / "udev" / "rules.d" / "70-canable-can0.rules"
    assert 'ENV{SYSTEMD_WANTS}+="can0.service"' in rule.read_text()
    assert "systemctl enable can0.service" in installer.calls("systemctl enable")
    assert not [c for c in installer.calls("systemctl start") if "can0" in c]


def test_phone_setup_update_and_first_boot_units_are_installed(installer: Installer) -> None:
    """docs/roadmap/phone-ui.md: BLE WiFi setup (P1.1), update from the
    dashboard (P3.1) and an imaged Pi's first boot (P3.3) each need their unit
    on the Pi, not only in the checkout."""
    installer.run()

    units = installer.root / "etc" / "systemd" / "system"
    for name in (
        "canary-provision.service",
        "canary-update.path",
        "canary-update.service",
        "canary-firstboot.service",
    ):
        assert (units / name).read_text() == (REPO_ROOT / "systemd" / name).read_text()
    enabled = installer.calls("systemctl enable")
    assert "systemctl enable canary-provision.service" in enabled
    assert "systemctl enable canary-firstboot.service" in enabled
    # Live now, not at the next boot: an update request from the dashboard
    # must be served on the Pi that was just installed.
    assert "systemctl enable --now canary-update.path" in enabled
    # The oneshot is started only by its path unit.
    assert not [c for c in enabled if c.endswith("canary-update.service")]


def _fake_curl(installer: Installer, version: str) -> Path:
    """A curl that "downloads" a pnpm reporting ``version``."""
    bin_dir = installer.root.parent / "fake-curl-bin"
    bin_dir.mkdir(exist_ok=True)
    _write_exec(
        bin_dir / "curl",
        "#!/bin/sh\n"
        'while [ "$1" != "-o" ]; do shift; done\n'
        f"printf '#!/bin/sh\\necho {version}\\n' > \"$2\"\n",
    )
    return bin_dir


def _pinned_pnpm() -> str:
    """The version package.json's packageManager pins (the fixture's src/ is the repo's)."""
    text = (REPO_ROOT / "src" / "dashboard" / "static" / "package.json").read_text()
    match = re.search(r'"packageManager": *"pnpm@([0-9.]+)"', text)
    assert match is not None
    return match.group(1)


def test_the_pinned_pnpm_is_installed_so_canary_update_can_build(
    installer: Installer,
) -> None:
    """A fresh Pi's first canary-update failed for want of pnpm (2026-10-11)."""
    version = _pinned_pnpm()
    path = f"{_fake_curl(installer, version)}:{installer.env['PATH']}"
    result = installer.run(CANARY_DASHBOARD_TOOLS="1", PATH=path)

    pnpm = installer.root / "usr" / "local" / "bin" / "pnpm"
    assert f"Installed pnpm {version}" in result.stdout
    reported = subprocess.run([pnpm, "--version"], capture_output=True, text=True).stdout
    assert reported == f"{version}\n"
    # No npm: Debian's is 362 packages on trixie.
    assert not [c for c in installer.calls("apt-get") if " npm" in c]

    again = installer.run(CANARY_DASHBOARD_TOOLS="1", PATH=path)
    assert "Installed pnpm" not in again.stdout


def test_a_pnpm_reporting_the_wrong_version_is_refused(installer: Installer) -> None:
    path = f"{_fake_curl(installer, '0.0.1')}:{installer.env['PATH']}"
    result = installer.run(CANARY_DASHBOARD_TOOLS="1", PATH=path)

    assert not (installer.root / "usr" / "local" / "bin" / "pnpm").exists()
    assert f"does not report {_pinned_pnpm()}" in result.stderr


# ---------------------------------------------------------------------------
# Dashboard bundle
# ---------------------------------------------------------------------------

# pnpm: `pnpm build` writes dist/index.html in its working directory, unless
# SHIM_PNPM_NO_DIST=1 (a build that "succeeds" and leaves nothing to serve).
_PNPM_SHIM = (
    "#!/usr/bin/env bash\n"
    + _LOG_PRELUDE
    + """
if [[ "$1" == "build" && "${SHIM_PNPM_NO_DIST:-0}" != "1" ]]; then
  mkdir -p dist
  echo built >dist/index.html
fi
exit "${SHIM_PNPM_EXIT:-0}"
"""
)


def _scratch_dashboard(installer: Installer) -> Path:
    """Give the checkout a dashboard of its own, so the build writes no dist/
    into this repo, and a pnpm shim on PATH. Returns the dashboard directory."""
    src = installer.checkout / "src"
    src.unlink()
    src.mkdir()
    for child in (REPO_ROOT / "src").iterdir():
        if child.name != "dashboard":
            (src / child.name).symlink_to(child)
    static = src / "dashboard" / "static"
    static.mkdir(parents=True)
    shutil.copy(REPO_ROOT / "src" / "dashboard" / "static" / "package.json", static)
    (static / "index.html").write_text("<html/>\n")
    _write_exec(Path(installer.env["PATH"].split(":", 1)[0]) / "pnpm", _PNPM_SHIM)
    return static


def test_a_fresh_checkout_gets_its_dashboard_built(installer: Installer) -> None:
    """install_pi.sh left dist/ to canary-update, so a Pi it set up served no
    dashboard until the operator ran canary-update (2026-11-06)."""
    static = _scratch_dashboard(installer)

    result = installer.run(CANARY_DASHBOARD_BUILD="1")

    assert "Building the dashboard (dist/ missing)" in result.stdout
    assert (static / "dist" / "index.html").read_text() == "built\n"
    assert installer.calls("pnpm") == ["pnpm install --frozen-lockfile", "pnpm build"]
    # As the user that owns the checkout, the way canary-update builds.
    (build,) = [c for c in installer.calls("runuser") if "pnpm build" in c]
    assert build.startswith("runuser -u canary -- bash -c")
    assert "DASHBOARD NOT BUILT" not in result.stdout
    # The shimmed systemctl says canary is running, and the daemon mounts dist/
    # only if it existed at startup.
    assert "RESTART NEEDED: canary started before the dashboard existed" in result.stdout


def test_no_restart_hint_when_canary_is_not_running(installer: Installer) -> None:
    _scratch_dashboard(installer)
    _write_exec(
        Path(installer.env["PATH"].split(":", 1)[0]) / "systemctl",
        "#!/usr/bin/env bash\n" + _LOG_PRELUDE + '[[ "$1" == is-active ]] && exit 3\nexit 0\n',
    )

    result = installer.run(CANARY_DASHBOARD_BUILD="1")

    assert "Building the dashboard (dist/ missing)" in result.stdout
    assert "RESTART NEEDED" not in result.stdout


def test_a_current_dashboard_is_not_rebuilt(installer: Installer) -> None:
    """canary-update builds before it runs install_pi.sh; the installer must
    not build the same bundle a second time."""
    _scratch_dashboard(installer)
    installer.run(CANARY_DASHBOARD_BUILD="1")

    again = installer.run(CANARY_DASHBOARD_BUILD="1")

    assert "unchanged: dashboard dist/ is current, not rebuilt" in again.stdout
    assert not installer.calls("pnpm")
    assert "RESTART NEEDED" not in again.stdout


def test_a_source_newer_than_the_bundle_rebuilds_it(installer: Installer) -> None:
    static = _scratch_dashboard(installer)
    installer.run(CANARY_DASHBOARD_BUILD="1")
    later = (static / "dist" / "index.html").stat().st_mtime + 10
    os.utime(static / "index.html", (later, later))

    again = installer.run(CANARY_DASHBOARD_BUILD="1")

    assert (
        "Building the dashboard (dist/ older than src/dashboard/static/index.html)" in again.stdout
    )
    assert installer.calls("pnpm build")
    # dist/ existed when canary started, so it serves the new bundle as is.
    assert "RESTART NEEDED" not in again.stdout


@pytest.mark.parametrize(
    ("failure", "stderr"),
    [
        ({"SHIM_PNPM_EXIT": "1"}, "the dashboard build failed"),
        ({"SHIM_PNPM_NO_DIST": "1"}, "wrote no src/dashboard/static/dist/index.html"),
    ],
    ids=["pnpm-fails", "no-dist"],
)
def test_a_failed_dashboard_build_is_reported_but_the_install_finishes(
    installer: Installer, failure: dict[str, str], stderr: str
) -> None:
    """The daemon, API and MCP server work without the bundle."""
    _scratch_dashboard(installer)
    installer.env.update(failure)

    result = installer.run(CANARY_DASHBOARD_BUILD="1")

    assert stderr in result.stderr
    assert "CANary Pi install complete." in result.stdout
    assert "systemctl enable canary.service" in installer.calls("systemctl enable")
    assert "DASHBOARD NOT BUILT" in result.stdout
    assert "RESTART NEEDED" not in result.stdout
