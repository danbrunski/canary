"""scripts/prepare_image.sh and scripts/canary_firstboot.sh in a scratch root.

An image made from one Pi must not carry that Pi's identity: its control
token would control every copy, its paired phone would control a customer's
Pi, and its WiFi profile would close BLE setup (docs/roadmap/archive/phone-ui.md
P2.4). The first boot of each copy then makes the identity its own.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PREPARE = REPO_ROOT / "scripts" / "prepare_image.sh"
FIRSTBOOT = REPO_ROOT / "scripts" / "canary_firstboot.sh"

HOME_WIFI = "[connection]\nid=canary-home\ntype=wifi\n\n[wifi]\nmode=infrastructure\nssid=Home\n"
AP = "[connection]\nid=canary-ap\ntype=wifi\n\n[wifi]\nmode=ap\nssid=CANary\n"
WIRED = "[connection]\nid=Wired connection 1\ntype=ethernet\n"


def _run(script: Path, root: Path, **env: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "CANARY_INSTALL_ROOT": str(root),
            "CANARY_INSTALL_ALLOW_UNPRIVILEGED": "1",
            **env,
        },
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result


@pytest.fixture
def installed(tmp_path: Path) -> Path:
    """A root as scripts/install_pi.sh leaves it on the reference Pi."""
    root = tmp_path / "root"
    etc = root / "etc"
    (etc / "canary").mkdir(parents=True)
    (etc / "canary" / "control.token").write_text("reference-pi-token\n")
    (etc / "canary" / "canary.env").write_text(
        "CANARY_HOME=/home/canary/canary\nCANARY_DATA_DIR=/data/canary\n"
        "CANARY_CONTROL_TOKEN=reference-pi-token\n"
    )
    control = root / "data" / "canary" / "control"
    control.mkdir(parents=True)
    (control / "paired_devices.json").write_text('{"devices": []}')
    nm = etc / "NetworkManager" / "system-connections"
    nm.mkdir(parents=True)
    (nm / "canary-home.nmconnection").write_text(HOME_WIFI)
    (nm / "canary-wifi-1a2b.nmconnection").write_text(HOME_WIFI.replace("Home", "Starlink"))
    (nm / "canary-ap.nmconnection").write_text(AP)
    (nm / "wired.nmconnection").write_text(WIRED)
    (etc / "hostname").write_text("canary-9f3e\n")
    (etc / "hosts").write_text("127.0.0.1\tlocalhost\n127.0.1.1\tcanary-9f3e\n")
    (etc / "machine-id").write_text("0123456789abcdef\n")
    (etc / "ssh").mkdir()
    (etc / "ssh" / "ssh_host_ed25519_key").write_text("secret")
    return root


def _net(tmp_path: Path, mac: str) -> Path:
    net = tmp_path / "net"
    (net / "eth0").mkdir(parents=True)
    (net / "eth0" / "address").write_text(mac + "\n")
    return net


def test_prepare_strips_the_reference_pis_identity(installed: Path) -> None:
    _run(PREPARE, installed)
    etc = installed / "etc"
    assert not (etc / "canary" / "control.token").exists()
    assert "reference-pi-token" not in (etc / "canary" / "canary.env").read_text()
    assert not (installed / "data/canary/control/paired_devices.json").exists()
    left = sorted(p.name for p in (etc / "NetworkManager/system-connections").iterdir())
    assert left == ["canary-ap.nmconnection", "wired.nmconnection"]
    assert (etc / "hostname").read_text() == "canary\n"
    assert "127.0.1.1\tcanary\n" in (etc / "hosts").read_text()
    assert (etc / "machine-id").read_text() == ""
    assert not list((etc / "ssh").iterdir())
    assert (etc / "canary" / "firstboot").exists()


def test_first_boot_makes_the_identity_this_pis_own(installed: Path, tmp_path: Path) -> None:
    _run(PREPARE, installed)
    _run(FIRSTBOOT, installed, CANARY_NET_DIR=str(_net(tmp_path, "D8:3A:DD:12:AB:CD")))
    etc = installed / "etc"
    token = (etc / "canary" / "control.token").read_text().strip()
    assert len(token) >= 40 and token != "reference-pi-token"
    assert f"CANARY_CONTROL_TOKEN={token}\n" in (etc / "canary" / "canary.env").read_text()
    assert (etc / "hostname").read_text() == "canary-abcd\n"
    assert "127.0.1.1\tcanary-abcd\n" in (etc / "hosts").read_text()
    assert not (etc / "canary" / "firstboot").exists()


def test_two_copies_get_different_tokens(installed: Path, tmp_path: Path) -> None:
    _run(PREPARE, installed)
    copies = []
    for i, mac in enumerate(("d8:3a:dd:00:00:01", "d8:3a:dd:00:00:02")):
        copy = tmp_path / f"copy{i}"
        subprocess.run(["cp", "-a", str(installed), str(copy)], check=True)
        _run(FIRSTBOOT, copy, CANARY_NET_DIR=str(_net(tmp_path / f"n{i}", mac)))
        copies.append((copy / "etc/canary/control.token").read_text())
    assert copies[0] != copies[1]


def test_first_boot_runs_once(installed: Path, tmp_path: Path) -> None:
    _run(PREPARE, installed)
    net = str(_net(tmp_path, "d8:3a:dd:12:ab:cd"))
    _run(FIRSTBOOT, installed, CANARY_NET_DIR=net)
    token = (installed / "etc/canary/control.token").read_text()
    (installed / "etc/hostname").write_text("chosen-name\n")
    _run(FIRSTBOOT, installed, CANARY_NET_DIR=net)
    assert (installed / "etc/canary/control.token").read_text() == token
    assert (installed / "etc/hostname").read_text() == "chosen-name\n"


def test_prepare_refuses_without_an_install(tmp_path: Path) -> None:
    result = subprocess.run(
        ["bash", str(PREPARE)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "CANARY_INSTALL_ROOT": str(tmp_path),
            "CANARY_INSTALL_ALLOW_UNPRIVILEGED": "1",
        },
        timeout=60,
    )
    assert result.returncode == 1
    assert "install_pi.sh first" in result.stderr
