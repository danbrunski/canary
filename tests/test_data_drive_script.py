"""scripts/data_drive.sh's prompt (operator, 2026-10-10): install_pi.sh asks
where /data should live, a drive by default when one is fitted, the SD card
otherwise; a drive turned down is not offered again; an adopted drive is kept
without asking. The probe and the actions are stubbed (CANDIDATES, DRY_RUN):
these tests never touch a block device."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "data_drive.sh"
NVME = "setup\t/dev/sda\tP300SERIAL\t476.9G usb M.2 P300 512GB, blank: will be formatted\n"
LABELLED = "adopt\t/dev/sdb1\tSSD2\t1T usb Other SSD, labelled canary-data\n"


def _run(root: Path, candidates: str, answer: str, *args: str) -> subprocess.CompletedProcess[str]:
    probe = root / "candidates.txt"
    probe.write_text(candidates)
    return subprocess.run(
        ["bash", str(SCRIPT), "choose", *args],
        input=answer,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "CANARY_INSTALL_ROOT": str(root),
            "CANARY_DATA_DRIVE_CANDIDATES": str(probe),
            "CANARY_DATA_DRIVE_DRY_RUN": "1",
        },
        timeout=30,
        check=False,
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "etc" / "canary").mkdir(parents=True)
    (tmp_path / "etc" / "fstab").write_text("PARTUUID=x-02 / ext4 defaults 0 1\n")
    return tmp_path


def test_enter_picks_the_drive(root: Path) -> None:
    result = _run(root, NVME, "\n")
    assert "1) /dev/sda: 476.9G usb M.2 P300 512GB, blank: will be formatted" in result.stdout
    assert "2) the SD card" in result.stdout
    assert "would setup /dev/sda" in result.stdout


def test_a_labelled_drive_is_offered_first_and_adopted(root: Path) -> None:
    result = _run(root, LABELLED + NVME, "1\n")
    assert "would adopt /dev/sdb1" in result.stdout


def test_turning_a_drive_down_is_remembered(root: Path) -> None:
    result = _run(root, NVME, "2\n")
    assert "stays on the SD card" in result.stdout
    assert (root / "etc" / "canary" / "data-drive.choice").read_text() == "sd P300SERIAL\n"
    again = _run(root, NVME, "")
    assert "Where should" not in again.stdout  # not asked again
    assert "no data drive to offer" in again.stdout
    asked = _run(root, NVME, "\n", "--ask")
    assert "would setup /dev/sda" in asked.stdout


def test_no_drive_no_question(root: Path) -> None:
    result = _run(root, "", "")
    assert "Where should" not in result.stdout
    assert "stays on the SD card" in result.stdout


def test_an_adopted_drive_is_kept_without_asking(root: Path) -> None:
    (root / "etc" / "fstab").write_text(
        "PARTUUID=x-02 / ext4 defaults 0 1\nUUID=abc /data ext4 defaults,nofail 0 2\n"
    )
    result = _run(root, NVME, "")
    assert "Where should" not in result.stdout
    assert "keeping it" in result.stdout


def test_a_choice_out_of_range_changes_nothing(root: Path) -> None:
    result = _run(root, NVME, "7\n")
    assert result.returncode != 0
    assert "nothing changed" in result.stderr
    assert not (root / "etc" / "canary" / "data-drive.choice").exists()
