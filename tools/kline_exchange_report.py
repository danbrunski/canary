#!/usr/bin/env python3
"""What a sniffed K-line session said, by module (K01.2, K01.3).

Reads one session's stored ``kline`` rows and writes a Markdown and a JSON
report: request/response pairs by module, services, DIDs, NRCs by name,
per-DID poll intervals, the DID cycle a scan-tool screen repeats versus the
one-off reads at screen load, and how well each frame's time is known
(``src/analysis/kline_exchanges.py`` does the work; this is its command).

Optional, for binding work:

- ``--bits ABS:1303`` lists every bit transition of that flag word, with times,
  to match against what the operator did ("brake at 16:42:55");
- ``--pair ABS:1202=decoded:ECM_RPM --within 100ms`` pairs a candidate DID
  (raw counts) with a reference channel at the same instants through
  ``compare_channels`` and reports the per-sample ratio, never an aggregate.

Sessions recorded before K01.1 store values as ``DID_<hex>``; their module is
recovered from the session's own StartCommunication frames first, so
``--bits``/``--pair`` take the module-scoped name either way.

Example::

    .venv/bin/python -m tools.kline_exchange_report ~/canary-data/sessions/2026-09-24T162507 \\
        --kline-profile data/nissan-xterra-2008/kline.yaml --bits ABS:1303
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from src.analysis.compare import ComparisonRefusedError
from src.analysis.kline_exchanges import (
    KlineReportError,
    attribute_modules,
    bit_transitions,
    exchange_report,
    pair_value,
    render_markdown,
)
from src.collection.kline.profile import load_kline_profile
from src.paths import DATA_DIR
from src.preprocessing.loader import read_session
from src.signals.pairing import PairingPolicy

DEFAULT_OUT_DIR = DATA_DIR / "analyses"


def _pair_arg(value: str) -> tuple[str, str]:
    candidate, sep, reference = value.partition("=")
    if not sep or not candidate.strip() or not reference.strip():
        raise argparse.ArgumentTypeError(
            f"--pair takes CANDIDATE=REFERENCE (ABS:1202=decoded:ECM_RPM), got {value!r}"
        )
    return candidate.strip(), reference.strip()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("session", type=Path, help="A session directory (telemetry-*.parquet).")
    parser.add_argument(
        "--kline-profile",
        type=Path,
        required=True,
        help="The vehicle's K-line profile (data/<vehicle>/kline.yaml): names the modules.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help="Where the .md and .json reports go (default: data/analyses).",
    )
    parser.add_argument(
        "--bits",
        action="append",
        default=[],
        metavar="MODULE:DID",
        help="A flag word whose bit transitions to list (repeatable).",
    )
    parser.add_argument(
        "--pair",
        action="append",
        default=[],
        type=_pair_arg,
        metavar="CANDIDATE=REFERENCE",
        help="Pair kline:<CANDIDATE> with a reference channel spec (repeatable).",
    )
    parser.add_argument(
        "--within",
        default="100ms",
        help="Largest separation two paired observations may have (default 100ms).",
    )
    return parser.parse_args(argv)


def _bits_section(frame: pd.DataFrame, channel: str) -> tuple[list[str], list[dict[str, Any]]]:
    transitions = bit_transitions(frame, channel)
    lines = [f"## Bit transitions: {channel}", ""]
    rows: list[dict[str, Any]] = []
    if not transitions:
        lines += ["The word never changed.", ""]
        return lines, rows
    lines += ["| time | bit | became | word before | word after |", "|---|---|---|---|---|"]
    for t in transitions:
        lines.append(
            f"| {t.at.isoformat()} | 0x{t.mask:04X} | {int(t.became)} | "
            f"{t.word_before:04X} | {t.word_after:04X} |"
        )
        rows.append(
            {
                "at": t.at.isoformat(),
                "mask": t.mask,
                "became": t.became,
                "word_before": t.word_before,
                "word_after": t.word_after,
            }
        )
    return [*lines, ""], rows


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    profile = load_kline_profile(args.kline_profile)
    session = args.session.expanduser()
    frame = attribute_modules(read_session(session), profile)
    try:
        report = exchange_report(frame, profile, session=session.name)
    except KlineReportError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    markdown = render_markdown(report).rstrip("\n").split("\n") + [""]
    payload: dict[str, Any] = report.as_dict()
    payload["bits"] = {}
    for channel in args.bits:
        try:
            lines, rows = _bits_section(frame, channel)
        except KlineReportError as exc:
            lines, rows = [f"## Bit transitions: {channel}", "", f"Refused: {exc}", ""], []
        markdown += lines
        payload["bits"][channel] = rows
    payload["pairs"] = []
    policy = PairingPolicy.within(args.within)
    for candidate, reference in args.pair:
        spec = f"kline:{candidate}"
        try:
            pairing = pair_value(frame, spec, reference, policy=policy)
            text = pairing.describe()
            payload["pairs"].append(
                {
                    "candidate": spec,
                    "reference": reference,
                    "n_pairs": pairing.n_pairs,
                    "ratio_median": pairing.ratio_median,
                    "ratio_min": pairing.ratio_min,
                    "ratio_max": pairing.ratio_max,
                    "ratio_spread_pct": pairing.ratio_spread_pct,
                    "comparison": pairing.comparison.as_dict(),
                }
            )
        except (ComparisonRefusedError, KlineReportError) as exc:
            text = f"refused: {exc}"
            payload["pairs"].append(
                {"candidate": spec, "reference": reference, "refused": str(exc)}
            )
        markdown += [f"## Pairing: {spec} vs {reference} (within {args.within})", "", text, ""]
    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{session.name}-kline-exchanges"
    md_path, json_path = out_dir / f"{stem}.md", out_dir / f"{stem}.json"
    md_path.write_text("\n".join(markdown).rstrip() + "\n", encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    print(md_path)
    print(json_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
