#!/usr/bin/env python3
"""Passive K-line edge capture (run on the Pi): no transmit, pull-up off.

    python3 kline_edges.py [seconds] [out-file]

Holds the TLIN1027 in normal mode (EN, GPIO26, high) with the commander
pull-up (GPIO24) off, so RXD (GPIO15) mirrors the bus and the HAT is invisible
to whatever else is on K-line -- e.g. UpRev's cable while it logs. Records the
kernel timestamp of every RXD edge, so any baud rate, 5-baud address or fast-init
pulse can be decoded afterwards with kline_decode.py.

Claiming GPIO15 as an input re-muxes it away from uart0 (the overlay enables
uart0 at boot), and the kernel leaves it that way after release — measured on
the HAT 2026-09-23. So this script puts the pin back to uart0 RXD (``a4``) on
exit; while it runs, the daemon's K-line collector hears nothing.
Output: a `# start_wall ...` header, then one line per edge,
"<timestamp_ns> <level after the edge>".
"""

import subprocess
import sys
import time

import gpiod
from gpiod.line import Direction, Edge, Value

GPIO_RXD, GPIO_PULLUP, GPIO_EN = 15, 24, 26
#: GPIO15's uart0 function on the Pi 5 (RXD0), restored on exit.
UART0_RXD_FUNCTION = "a4"


def main() -> None:
    dur = float(sys.argv[1]) if len(sys.argv) > 1 else 300
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = sys.argv[2] if len(sys.argv) > 2 else f"kline-edges-{stamp}.txt"
    chip = gpiod.Chip("/dev/gpiochip0")
    ctl = chip.request_lines(
        consumer="kline-sniff",
        config={
            (GPIO_EN, GPIO_PULLUP): gpiod.LineSettings(
                direction=Direction.OUTPUT, output_value=Value.INACTIVE
            )
        },
    )
    rx = chip.request_lines(
        consumer="kline-sniff",
        event_buffer_size=1024,  # the kernel's cap; drained every <= 50 ms
        config={GPIO_RXD: gpiod.LineSettings(edge_detection=Edge.BOTH)},
    )
    n = 0
    try:
        ctl.set_value(GPIO_EN, Value.ACTIVE)
        time.sleep(0.01)
        if rx.wait_edge_events(0):
            rx.read_edge_events()  # discard the mode-change edges
        print(f"capturing {dur:.0f} s -> {out}", flush=True)
        t_end = time.monotonic() + dur
        last_report = time.monotonic()
        with open(out, "w") as f:
            f.write(f"# start_wall {time.time():.6f} start_mono_ns {time.monotonic_ns()}\n")
            while time.monotonic() < t_end:
                if rx.wait_edge_events(0.05):
                    evs = rx.read_edge_events()
                    n += len(evs)
                    rising = gpiod.EdgeEvent.Type.RISING_EDGE
                    f.write(
                        "".join(f"{e.timestamp_ns} {int(e.event_type == rising)}\n" for e in evs)
                    )
                if time.monotonic() - last_report > 10:
                    print(f"  {n} edges so far", flush=True)
                    last_report = time.monotonic()
    except KeyboardInterrupt:
        pass
    finally:
        ctl.set_value(GPIO_EN, Value.INACTIVE)
        ctl.release()
        rx.release()
        subprocess.run(["pinctrl", "set", str(GPIO_RXD), UART0_RXD_FUNCTION], check=False)
        print(f"done: {n} edges -> {out}; GPIO15 back to uart0", flush=True)


if __name__ == "__main__":
    main()
