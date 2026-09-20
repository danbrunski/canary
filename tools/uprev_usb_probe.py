#!/usr/bin/env python3
"""Enumerate the UpRev/Cipher cable and optionally run its local ping."""

from __future__ import annotations

import argparse

from src.collection.uprev_usb import (
    UpRevUsbError,
    UpRevUsbTransport,
    discover_uprev_usb_devices,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect an UpRev/Cipher USB cable attached to WSL via usbipd."
    )
    parser.add_argument("--serial", help="Select one cable by USB serial number.")
    parser.add_argument(
        "--ping",
        action="store_true",
        help="Claim the cable and run the cable-local 07 -> 06 handshake.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        devices = discover_uprev_usb_devices()
        for device in devices:
            print(
                f"bus={device.bus} address={device.address} "
                f"id={device.vendor_id:04x}:{device.product_id:04x} "
                f"serial={device.serial_number!r} product={device.product!r}"
            )
        if not devices:
            print("no attached UpRev USB cable (expected 0403:ccb8)")
            return 1
        if args.ping:
            with UpRevUsbTransport.connect(serial_number=args.serial) as transport:
                transport.cable_ping()
            print("cable ping: 07 -> 06 OK")
        return 0
    except (UpRevUsbError, TimeoutError) as exc:
        print(f"uprev_usb_probe: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
