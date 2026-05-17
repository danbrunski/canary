#!/usr/bin/env python3
"""Pre-capture go/no-go check for an in-vehicle CANary recording.

Run this on the Pi *before* starting a drive capture. It verifies, with
specific actionable failures, that a recording will actually produce usable
data — catching the failure modes that have silently wasted drive sessions:

  - config doesn't load / wrong storage path
  - not enough free disk for the drive (a 1-hour drive is ~400+ MB)
  - can0 missing / down / wrong bitrate (the manual `ip link` dance was
    skipped, or the Canable re-enumerated and the link dropped)
  - no CAN frames arriving (ignition off, bus not wired, listen-only on a
    bus that needs the adapter to ACK)
  - the ECU does not answer Mode 01 — most often because the adapter is
    still in the Nissan `0xC0` diagnostic (DID) session, which is mutually
    exclusive with Mode 01. This is exactly why the recent corpus came back
    Mode-22-only and blocked the behavior-model refit.

Exit status is 0 only if every CRITICAL check passes, so it composes:

  .venv/bin/python scripts/preflight.py && \\
    .venv/bin/python scripts/record.py --obd-source canable ...

It is read-only on the bus (only standard OBD read requests, the normal
`obd_source: canable` traffic). With --bring-up it will (re)configure can0
via `sudo ip link` using the documented bitrate before checking.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from src.collection.can_collector import bus_factory
from src.collection.can_obd_client import PID_DECODERS, CanObdClient
from src.config import CanaryConfig, CanBusConfig
from src.log import configure_logging
from src.storage.monitor import free_space_mb

# The drive backlog (docs/data-collection-plan.md §1) needs these decoded.
# Missing any of them is a warning, not a hard fail — but Mode 01 returning
# *nothing* is fatal (almost always the 0xC0-session trap).
CORE_DRIVE_PIDS = ("RPM", "SPEED", "COOLANT_TEMP", "THROTTLE_POS", "ENGINE_LOAD")

CommandRunner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    critical: bool = True
    remediation: str | None = None


def _run(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(cmd), capture_output=True, text=True, timeout=15)


def check_config(config_path: Path) -> tuple[CheckResult, CanaryConfig | None]:
    try:
        config = CanaryConfig.from_yaml(config_path)
    except FileNotFoundError:
        return (
            CheckResult(
                "config",
                False,
                f"{config_path} not found",
                remediation=f"cp canary.dev.example.yaml {config_path}",
            ),
            None,
        )
    except Exception as exc:  # noqa: BLE001 — surface any parse/validation error
        return (
            CheckResult(
                "config",
                False,
                f"{type(exc).__name__}: {exc}",
                remediation=f"fix {config_path}",
            ),
            None,
        )
    can = config.hardware.can
    return (
        CheckResult(
            "config",
            True,
            f"{config_path} ok — obd_source={config.collection.obd_source} "
            f"bus={can.interface}:{can.channel}@{can.bitrate} "
            f"storage={config.collection.storage_path}",
        ),
        config,
    )


def check_storage(out_dir: Path, min_free_mb: int) -> CheckResult:
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        probe = out_dir / ".preflight_write_test"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        return CheckResult(
            "storage",
            False,
            f"{out_dir} not writable: {exc}",
            remediation=f"chown/chmod {out_dir} or pick another --out-dir",
        )
    free_mb = free_space_mb(out_dir)
    if free_mb < min_free_mb:
        return CheckResult(
            "storage",
            False,
            f"only {free_mb:.0f} MB free in {out_dir} (need >= {min_free_mb})",
            remediation="free space (prune old sessions) or lower --min-free-mb",
        )
    return CheckResult("storage", True, f"{out_dir} writable, {free_mb:.0f} MB free")


def parse_ip_link(payload: str) -> dict[str, object] | None:
    """Parse `ip -details -json link show dev <chan>` into the first object."""
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if isinstance(data, list) and data:
        first = data[0]
        return first if isinstance(first, dict) else None
    return None


def check_can_link(spec: CanBusConfig, *, runner: CommandRunner = _run) -> CheckResult:
    """Verify a socketcan link exists, is UP, and is at the configured bitrate.

    Non-socketcan interfaces (slcan/virtual dev) have no `ip link` state, so
    this is an informational skip there.
    """
    if spec.interface != "socketcan":
        return CheckResult(
            "can_link",
            True,
            f"interface={spec.interface} (not socketcan) — link state N/A",
            critical=False,
        )
    chan = spec.channel
    up_cmds = (
        f"sudo ip link set {chan} down; "
        f"sudo ip link set {chan} type can bitrate {spec.bitrate}; "
        f"sudo ip link set {chan} up   (or: scripts/canup.sh)"
    )
    try:
        proc = runner(["ip", "-details", "-json", "link", "show", "dev", chan])
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        return CheckResult("can_link", False, f"ip link query failed: {exc}", remediation=up_cmds)
    if proc.returncode != 0:
        return CheckResult(
            "can_link",
            False,
            f"{chan} does not exist (is the Canable plugged in?)",
            remediation=up_cmds,
        )
    info = parse_ip_link(proc.stdout)
    if info is None:
        return CheckResult(
            "can_link", False, f"could not parse `ip link` for {chan}", remediation=up_cmds
        )
    operstate = str(info.get("operstate", "?"))
    flags = info.get("flags", [])
    is_up = operstate == "UP" or (isinstance(flags, list) and "UP" in flags)
    linkinfo = info.get("linkinfo")
    bitrate: int | None = None
    if isinstance(linkinfo, dict):
        info_data = linkinfo.get("info_data")
        if isinstance(info_data, dict):
            raw = info_data.get("bittiming", {})
            if isinstance(raw, dict) and isinstance(raw.get("bitrate"), int):
                bitrate = raw["bitrate"]
    if not is_up:
        return CheckResult(
            "can_link", False, f"{chan} is {operstate} (not UP)", remediation=up_cmds
        )
    if bitrate is not None and bitrate != spec.bitrate:
        return CheckResult(
            "can_link",
            False,
            f"{chan} bitrate {bitrate} != configured {spec.bitrate}",
            remediation=up_cmds,
        )
    br = f"{bitrate}" if bitrate is not None else "unknown"
    return CheckResult("can_link", True, f"{chan} UP, bitrate {br}")


def check_frames(bus: object, *, seconds: float, min_frames: int) -> CheckResult:
    """Count CAN frames over `seconds`. Zero frames ⇒ ignition off / bus dead."""
    deadline = time.monotonic() + seconds
    count = 0
    try:
        while time.monotonic() < deadline:
            msg = bus.recv(timeout=0.2)  # type: ignore[attr-defined]
            if msg is not None:
                count += 1
    except Exception as exc:  # noqa: BLE001 — any bus error here is a hard fail
        return CheckResult(
            "frames",
            False,
            f"bus recv failed: {type(exc).__name__}: {exc}",
            remediation="check wiring / adapter; re-run scripts/canup.sh",
        )
    rate = count / seconds if seconds else 0.0
    if count < min_frames:
        return CheckResult(
            "frames",
            False,
            f"only {count} frames in {seconds:.0f}s ({rate:.0f}/s)",
            remediation="ignition ON? OBD-II port wired? bus at the right bitrate?",
        )
    return CheckResult("frames", True, f"{count} frames in {seconds:.0f}s ({rate:.0f}/s)")


def check_obd(bus: object, *, timeout: float) -> CheckResult:
    """Confirm the ECU answers Mode 01 (and the core drive PIDs are present).

    Empty support ⇒ almost always the adapter is still in the Nissan 0xC0
    DID session (Mode 22), which blocks Mode 01.
    """
    try:
        client = CanObdClient(bus, timeout_seconds=timeout)  # type: ignore[arg-type]
        supported = client.query_supported_mode_01_pids()
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "obd_mode01",
            False,
            f"Mode 01 probe failed: {type(exc).__name__}: {exc}",
            remediation="ECU not answering — see remediation for an empty result",
        )
    if not supported:
        return CheckResult(
            "obd_mode01",
            False,
            "ECU returned no supported Mode 01 PIDs",
            remediation=(
                "engine ON, and NOT in a Mode 22 / 0xC0 DID session "
                "(power-cycle the adapter; don't run a DID sweep first)"
            ),
        )
    names = {PID_DECODERS.get(pid, (None,))[0] for pid in supported}
    missing = [p for p in CORE_DRIVE_PIDS if p not in names]
    detail = f"{len(supported)} Mode 01 PIDs supported"
    if missing:
        return CheckResult(
            "obd_mode01",
            True,
            f"{detail}; core PID(s) missing: {', '.join(missing)}",
            critical=False,
            remediation="degraded coverage — proceed only if expected for this vehicle",
        )
    return CheckResult("obd_mode01", True, f"{detail}; all core drive PIDs present")


def _bring_up_can0(spec: CanBusConfig, runner: CommandRunner) -> None:
    chan = spec.channel
    runner(["sudo", "ip", "link", "set", chan, "down"])
    runner(["sudo", "ip", "link", "set", chan, "type", "can", "bitrate", str(spec.bitrate)])
    runner(["sudo", "ip", "link", "set", chan, "up"])


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="preflight",
        description="Go/no-go check before an in-vehicle CANary capture.",
    )
    p.add_argument("--config", type=Path, default=Path("canary.yaml"))
    p.add_argument("--out-dir", type=Path, default=Path("data/sessions"))
    p.add_argument("--min-free-mb", type=int, default=500)
    p.add_argument("--frames-seconds", type=float, default=5.0)
    p.add_argument("--min-frames", type=int, default=20)
    p.add_argument("--obd-timeout", type=float, default=1.0)
    p.add_argument(
        "--skip-obd",
        action="store_true",
        help="Skip the Mode 01 ECU check (CAN-only passive capture).",
    )
    p.add_argument(
        "--bring-up",
        action="store_true",
        help="If can0 is down/wrong, (re)configure it via `sudo ip link` first.",
    )
    p.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    p.add_argument("--log-level", default="WARNING", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)
    results: list[CheckResult] = []

    cfg_result, config = check_config(args.config)
    results.append(cfg_result)

    if config is not None:
        results.append(check_storage(args.out_dir, args.min_free_mb))
        spec = config.hardware.can

        if args.bring_up and spec.interface == "socketcan":
            link = check_can_link(spec)
            if not link.ok:
                _bring_up_can0(spec, _run)
        results.append(check_can_link(spec))

        # Open the bus once for both the frames and OBD checks — opening a
        # USB Canable twice is unreliable (see can_obd_collector docstring).
        bus = None
        try:
            bus = bus_factory(spec)
            results.append(
                check_frames(bus, seconds=args.frames_seconds, min_frames=args.min_frames)
            )
            obd_source = config.collection.obd_source
            if args.skip_obd or obd_source != "canable":
                results.append(
                    CheckResult(
                        "obd_mode01",
                        True,
                        f"skipped (obd_source={obd_source}, --skip-obd={args.skip_obd})",
                        critical=False,
                    )
                )
            else:
                results.append(check_obd(bus, timeout=args.obd_timeout))
        except Exception as exc:  # noqa: BLE001 — bus open failure is a hard fail
            results.append(
                CheckResult(
                    "bus",
                    False,
                    f"could not open {spec.interface}:{spec.channel}: {type(exc).__name__}: {exc}",
                    remediation="run scripts/canup.sh; confirm the Canable is attached",
                )
            )
        finally:
            if bus is not None:
                try:
                    bus.shutdown()
                except Exception:
                    pass

    failed_critical = [r for r in results if r.critical and not r.ok]
    if args.json:
        print(
            json.dumps({"results": [vars(r) for r in results], "go": not failed_critical}, indent=2)
        )
    else:
        for r in results:
            mark = "PASS" if r.ok else ("FAIL" if r.critical else "WARN")
            print(f"[{mark}] {r.name}: {r.detail}")
            if not r.ok and r.remediation:
                print(f"       → {r.remediation}")
        print()
        if failed_critical:
            print(f"NO-GO — {len(failed_critical)} critical check(s) failed.")
        else:
            print("GO — safe to start the capture.")
    return 1 if failed_critical else 0


if __name__ == "__main__":
    sys.exit(main())
