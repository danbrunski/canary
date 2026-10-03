#!/usr/bin/env python3
"""Disposition report over the stored corpus (Task 1.5, online-training-strategy.md).

Runs the Phase 1 screen — regime gate (segmenter) → data-quality gate
(``src.preprocessing.quality_gate``) → four-tier disposition classifier
(``src.analysis.training_disposition``) — over every session under
``--sessions-root`` and writes a per-channel, per-operating-mode
persistently-outside-nominal report to ``data/analyses/``. This is the
artifact the operator disposes from: quarantined channels are candidates
for a new ``accepted_baselines.yaml`` override (an explicit, reviewed
operator action — never authored by this script).

Pure analysis, zero risk: **no retrain happens here**, nothing is written
back to any model artifact or the training ledger. Phase 1 of the doc is
explicitly "no auto-retrain yet" — this script is that phase's whole
deliverable, run once over the current corpus and its output committed as
a morning-review artifact (the same convention this repo's earlier
overnight reports follow).

    .venv/bin/python scripts/disposition_report.py
    .venv/bin/python scripts/disposition_report.py --all --sessions-root data/sessions
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

try:
    from _cli import (
        add_bindings_arg,
        add_common_args,
        add_log_level_arg,
        add_profile_arg,
        load_config,
        resolve_sessions,
    )
except ImportError:  # loaded as `scripts.disposition_report` — repo root on path, not scripts/
    from scripts._cli import (  # type: ignore[no-redef]
        add_bindings_arg,
        add_common_args,
        add_log_level_arg,
        add_profile_arg,
        load_config,
        resolve_sessions,
    )

from src.analysis.training_disposition import (  # noqa: E402
    ChannelDisposition,
    DispositionSummary,
    classify_dispositions,
)
from src.behavior.accepted_baselines import AcceptedBaselines, load_accepted_baselines  # noqa: E402
from src.features.feature_pipeline import (  # noqa: E402
    candidate_feature_columns,
    disposition_screen_frames,
)
from src.log import configure_logging  # noqa: E402
from src.models.golden_baselines import golden_baselines_path, read_golden_baselines  # noqa: E402
from src.models.training_ledger import DEFAULT_TRAINING_LEDGER_PATH  # noqa: E402
from src.vehicle.profiles import resolve_profile_for_tooling, scope_config  # noqa: E402

log = logging.getLogger(__name__)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="disposition_report",
        description=(
            "Screen the stored session corpus for physically-impossible cells "
            "(Reject) and channels that sit persistently outside this engine's "
            "own baseline (Quarantine / Accept-recenter), writing a per-channel "
            "report the operator disposes from. No retrain, no model writes."
        ),
    )
    parser.add_argument(
        "sessions",
        nargs="*",
        type=Path,
        help="Session directories. Default: all under --sessions-root within --days.",
    )
    parser.add_argument(
        "--sessions-root",
        type=Path,
        default=Path("data/sessions"),
        help="Where to discover sessions when none given explicitly.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Use sessions captured in the last N days. Default 30. --all overrides.",
    )
    parser.add_argument(
        "--all",
        dest="use_all_sessions",
        action="store_true",
        help="Override --days; use every session under --sessions-root.",
    )
    add_common_args(parser)
    add_profile_arg(parser)
    add_bindings_arg(parser)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Markdown output path (default: data/analyses/disposition-report-<timestamp>.md).",
    )
    parser.add_argument(
        "--timestamp",
        default=None,
        help="Filesystem-safe timestamp for the default --out name (default: now, UTC).",
    )
    parser.add_argument(
        "--golden-model-type",
        default=None,
        help=(
            "Screen against this model_type's frozen golden baselines (Task 3.7), when "
            "declared and re-frozen (see `scripts/ledger.py golden`), instead of "
            "recomputing window-local statistics from this scan. Omitted by default -- "
            "window-local is the Phase 1 baseline and the documented fallback anyway "
            "when no golden run exists yet."
        ),
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        default=DEFAULT_TRAINING_LEDGER_PATH,
        help="Training ledger path -- where --golden-model-type's frozen baselines sidecar lives.",
    )
    add_log_level_arg(parser)
    return parser.parse_args(argv)


def build_report_markdown(
    summary: DispositionSummary,
    *,
    generated_at: str,
    sessions: Sequence[Path],
    tail_only: bool = True,
) -> str:
    """Render the disposition summary as the operator-facing markdown report.

    ``tail_only`` restricts the per-group table to groups with any
    Quarantine or Accept-recenter hits — "persistently outside nominal", the
    doc's own phrase for the report's purpose — so a clean channel with 100%
    Accept doesn't clutter the table the operator actually needs to read.
    """
    totals = summary.totals
    lines = [
        f"# Disposition report ({generated_at})",
        "",
        f"{len(sessions)} session(s) screened. Regime gate (segmenter) -> data-quality "
        "gate (physical limits) -> four-tier disposition. No retrain occurred; "
        "this is a pure screen.",
        "",
        "## Totals",
        "",
        "| Tier | Cells |",
        "|---|---:|",
        f"| Reject | {totals['reject']:,} |",
        f"| Accept-recenter | {totals['accept_recenter']:,} |",
        f"| Quarantine | {totals['quarantine']:,} |",
        f"| Accept | {totals['accept']:,} |",
        "",
    ]

    if summary.reject_counts:
        lines += ["## Reject — physically-impossible cells, by channel", ""]
        lines += ["| Channel | Rejected cells |", "|---|---:|"]
        for channel, count in sorted(summary.reject_counts.items(), key=lambda kv: -kv[1]):
            lines.append(f"| `{channel}` | {count:,} |")
        lines.append("")
    else:
        lines += ["## Reject — physically-impossible cells, by channel", "", "None.", ""]

    flagged = [g for g in summary.groups if (g.quarantine or g.accept_recenter)]
    lines += ["## Persistently outside this-engine baseline", ""]
    if not flagged:
        lines += ["No channel sits persistently outside its own baseline in this corpus.", ""]
    else:
        lines += [
            "| Channel | Mode | Accept-recenter | Quarantine | Accept | Baseline band |",
            "|---|---|---:|---:|---:|---|",
        ]
        for group in sorted(flagged, key=lambda g: -(g.quarantine + g.accept_recenter)):
            lines.append(_group_row(group))
        lines.append("")

    lines += [
        "## Next steps",
        "",
        "Quarantined channels are candidates for a reviewed "
        "`accepted_baselines.yaml` override (operator action — see "
        '`docs/roadmap/online-training-strategy.md`\'s "Directional overrides" '
        "section). This report never authors one.",
        "",
    ]
    return "\n".join(lines)


def _group_row(group: ChannelDisposition) -> str:
    band = _band_cell(group)
    mode = group.operating_mode or "(all)"
    return (
        f"| `{group.channel}` | {mode} | {group.accept_recenter:,} | "
        f"{group.quarantine:,} | {group.accept:,} | {band} |"
    )


def _band_cell(group: ChannelDisposition) -> str:
    """Render the "Baseline band" column — a discrete channel's (Task 1.6)
    observed value *set*, or a continuous channel's median/MAD band."""
    if group.is_discrete and group.baseline_values is not None:
        values = ", ".join(_format_number(v) for v in group.baseline_values)
        return f"{{{values}}} (discrete)"
    if group.baseline_low is not None and group.baseline_high is not None:
        return f"[{group.baseline_low:.3g}, {group.baseline_high:.3g}]"
    return "n/a (too few samples)"


def _format_number(value: float) -> str:
    return f"{value:g}" if value != int(value) else str(int(value))


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)
    config = load_config(args)

    scoped = (
        scope_config(config, resolve_profile_for_tooling(config, args.profile))
        if config.profiles.enabled
        else config
    )
    sessions_root = args.sessions_root or scoped.collection.storage_path
    sessions = resolve_sessions(args, sessions_root)
    if not sessions:
        print(
            f"error: no sessions found under {sessions_root} within --days {args.days}. "
            "Pass session paths directly or use --all.",
            file=sys.stderr,
        )
        return 1

    overrides: AcceptedBaselines | None = None
    if scoped.daemon.accepted_baselines.exists():
        overrides = load_accepted_baselines(scoped.daemon.accepted_baselines)

    framed_parts, reject_counts = disposition_screen_frames(
        sessions, scoped, bindings_path=args.bindings
    )
    if not framed_parts:
        print("error: no session in the selection was readable", file=sys.stderr)
        return 1

    import pandas as pd

    combined = pd.concat(framed_parts, ignore_index=True)
    # Screen the same channel set training would actually consume — drops
    # the dbc:*/decoded:* duplicate decode of the same wire signal (see
    # candidate_feature_columns), GPS position/quality, and monotonic
    # counters, so the report doesn't double-flag one physical channel
    # under two names or spend rows on channels no model ever trains on.
    channels = candidate_feature_columns(combined)
    golden_baselines = None
    if args.golden_model_type:
        golden_baselines = read_golden_baselines(
            golden_baselines_path(args.ledger, args.golden_model_type)
        )
        if golden_baselines is None:
            print(
                f"note: no frozen golden baselines for {args.golden_model_type!r} -- "
                "falling back to window-local screening (see `scripts/ledger.py golden`)",
                file=sys.stderr,
            )
    summary = classify_dispositions(
        combined,
        reject_counts=reject_counts,
        channels=channels,
        overrides=overrides,
        golden_baselines=golden_baselines,
    )

    timestamp = args.timestamp or datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    report = build_report_markdown(summary, generated_at=timestamp, sessions=sessions)
    print(report)

    out_path = args.out or Path("data/analyses") / f"disposition-report-{timestamp}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report)
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
