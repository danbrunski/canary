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
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

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
    (checkout / "scripts" / "install_deps.py").write_text(_INSTALL_DEPS_STUB)
    shutil.copy(EXAMPLE_CONFIG, checkout / "canary.pi.example.yaml")
    (checkout / "systemd").mkdir()
    for name in _INSTALLED_UNIT_FILES:
        shutil.copy(REPO_ROOT / "systemd" / name, checkout / "systemd" / name)
    (checkout / "src").symlink_to(REPO_ROOT / "src")
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
        # The laptop's own interfaces must not name the scratch Pi.
        "CANARY_NET_DIR": str(_fake_net_dir(tmp_path, eth0="88:a2:9e:1d:89:74")),
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


def _gps_rule(installer: Installer) -> str:
    return (installer.root / "etc" / "udev" / "rules.d" / "70-canary-gps.rules").read_text()


def test_a_canary_hat_makes_its_gnss_uart_the_gps(installer: Installer) -> None:
    result = installer.run(
        CANARY_GPS_DEVICE="", CANARY_HAT_PRODUCT_FILE=str(_hat_product_file(installer))
    )

    assert "CANary-HAT detected; GPS device: /dev/ttyAMA2" in result.stdout
    # A board UART has no by-id symlink, so a SYMLINK match would never fire.
    assert 'KERNEL=="ttyAMA2"' in _gps_rule(installer)
    assert "SYMLINK" not in _gps_rule(installer)


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


def _with_overlay(installer: Installer) -> Path:
    firmware = installer.checkout / "hardware" / "canary-hat" / "firmware"
    firmware.mkdir(parents=True)
    shutil.copy(
        REPO_ROOT / "hardware" / "canary-hat" / "firmware" / "canary-hat-overlay.dts", firmware
    )
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
