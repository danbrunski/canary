"""Fuel-rail pulsation and the gauge step check, from a full-rate capture.

Run on the laptop against a pulled session (never on the Pi while it
collects)::

    .venv/bin/python -m tools.analyze_analog_pulsation \\
        ~/canary-data/sessions/nissan-xterra-2008/_capture-fuel-pulsation-<ts>

The session must be recorded with the ``fuel-pulsation`` capture profile and
firmware/usb-adc v2, so every sample is stored with its sequence number. For
the step check, hold at least two steady speeds 600+ rpm apart for ~10 s each
(idle and a steady 2,000 rpm in neutral will do). Method and verdicts:
:mod:`src.analysis.analog_pulsation`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.analysis.analog_pulsation import (
    PulsationRefusedError,
    analyse,
    format_report,
    full_rate_series,
)
from src.preprocessing.loader import read_session

#: rpm sources tried in order unless --rpm names one: Mode 01, then Mode 22.
RPM_CANDIDATES = (("obd", "RPM"), ("obd", "ECM_RPM"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("session", type=Path, help="a pulled session directory")
    parser.add_argument("--channel", default="FUEL_PRESSURE", help="sensor channel")
    parser.add_argument(
        "--rpm", default=None, help="rpm as source:channel (default: obd:RPM, then obd:ECM_RPM)"
    )
    args = parser.parse_args(argv)

    # The pulsation reads ``value`` and each sample's ``metadata_json``
    # (``adc_seq``); nothing reads ``payload`` or ``unit`` (P02.2).
    frame = read_session(
        args.session,
        filters=[("source", "in", ["sensor", "obd"])],
        columns=["value_number", "value_text", "value_bool", "metadata_json"],
    )
    try:
        series = full_rate_series(frame, args.channel)
    except PulsationRefusedError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    candidates = [tuple(args.rpm.split(":", 1))] if args.rpm else [list(c) for c in RPM_CANDIDATES]
    for source, channel in candidates:
        rpm = frame[(frame["source"] == source) & (frame["channel"] == channel)]
        if not rpm.empty:
            break
    else:
        print("refused: no rpm channel in this session (see --rpm)", file=sys.stderr)
        return 2
    print(f"rpm from {source}:{channel}")
    print(format_report(analyse(series, rpm[["timestamp", "value"]])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
