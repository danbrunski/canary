"""Capture coverage and tuple yield from the rows on disk (roadmap C03.7/C03.9).

The live ledger (``src.collection.capture_lifetime``) knows what the
collector *observed*; only the Parquet knows what was *persisted*, and only
the persisted round identity (``generation``, ``round_id``, ``acquired_at``
— C02's ``ts_schema`` 2) can say whether a tuple actually completed. So the
yield figure is computed here, from the rows, with the same accumulator the
live path uses for cadence and coverage, so a recomputed verdict for a
historic session and a stop-time verdict are the same arithmetic over the
same evidence.

A tuple is **complete** in a round when every required member has at least
one persisted observation tagged with that round and the round's measured
acquisition spread (``round_skew``) is within the profile's declared
``max_tuple_skew_s``. No observation is reused across rounds; nothing is
binned, averaged or interpolated. A round with all members present but too
much skew is ``skew_rejected``; a round missing a member is counted against
that member. Rows with no round identity contribute nothing — an empty
result is ``no_round_evidence``, which is not a zero yield.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from src.analysis.channels import (
    ACQUIRED_AT_COLUMN,
    GENERATION_COLUMN,
    ROUND_ID_COLUMN,
    read_acquisition_frame,
)
from src.collection.capture_lifetime import ChannelAccumulator
from src.collection.capture_quality import (
    TUPLE_CHANNEL,
    CaptureQuality,
    effective_max_tuple_skew_s,
    evaluate_capture_quality,
    profile_requests,
)
from src.collection.obd_scheduler import parse_did_token
from src.storage.capture_manifest import (
    SCOPE_RECOMPUTE,
    normalize_quality_payload,
    read_capture_manifest,
    read_capture_quality,
)
from src.storage.session_files import PARQUET_PATTERN, count_session_rows

log = logging.getLogger(__name__)

OBD_SOURCE = "obd"
DID_PREFIX = "DID_"

SOURCE_ROWS = "persisted_rows"
SOURCE_NO_ROUNDS = "no_round_evidence"


def did_channel_for(name: str) -> str | None:
    """``ECM_MAF_GPS`` -> ``DID_1209`` via the DID catalogue, or ``None``."""

    try:
        return f"{DID_PREFIX}{parse_did_token(name):04X}"
    except ValueError:
        return None


def member_channels(profile: Mapping[str, Any]) -> dict[str, str]:
    """Required-tuple member (profile name) -> persisted transport channel."""
    mode01 = {str(k).upper() for k in (profile.get("mode01") or {})}
    out: dict[str, str] = {}
    for name in profile.get("required_tuple") or ():
        upper = str(name).upper()
        if upper in mode01:
            out[upper] = upper
        else:
            out[upper] = did_channel_for(str(name)) or upper
    return out


def _epoch(value: Any) -> float:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    return float(stamp.timestamp())


def _acquisition_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """A raw ``parquet_row`` frame is placed on the acquisition clock first."""
    return frame if ROUND_ID_COLUMN in frame else read_acquisition_frame(frame)


def _acquisition_seconds(frame: pd.DataFrame) -> pd.Series:
    """Per-row acquisition instant as float epoch seconds.

    ``read_acquisition_frame`` already placed ``timestamp`` on the acquisition
    clock where a row carried one; this just converts to seconds.
    """
    stamps = pd.to_datetime(frame["timestamp"], utc=True)
    # Unit-independent: after a fillna the column may be datetime64[s] rather
    # than [ns], and an int64 view would then be off by a factor of 1e9.
    return (stamps - pd.Timestamp(0, tz="UTC")) / pd.Timedelta(seconds=1)


def channel_lifetimes_from_rows(
    frame: pd.DataFrame,
    channels: list[str],
    *,
    started_at: float,
    ended_at: float,
) -> dict[str, dict[str, Any]]:
    """The live ledger's per-channel numbers, rebuilt from persisted rows."""
    frame = _acquisition_frame(frame)
    elapsed = max(0.0, ended_at - started_at)
    obd = frame[frame["source"] == OBD_SOURCE]
    seconds = _acquisition_seconds(obd)
    upper = obd["channel"].astype(str).str.upper()
    out: dict[str, dict[str, Any]] = {}
    for channel in channels:
        accumulator = ChannelAccumulator(channel)
        mask = upper == channel.upper()
        for value in sorted(seconds[mask].tolist()):
            offset = value - started_at
            if offset < 0 or offset > elapsed:
                continue
            accumulator.observe(offset)
        out[channel] = accumulator.snapshot(elapsed).to_dict()
    return out


def count_persisted_tuples(  # noqa: PLR0913
    frame: pd.DataFrame,
    members: Mapping[str, str],
    *,
    max_skew_s: float,
    started_at: float,
    ended_at: float,
    generation_boundary: int | None = None,
) -> dict[str, Any]:
    """Round-level tuple yield from persisted rows (see module docstring)."""
    transport = {channel.upper(): name for name, channel in members.items()}
    if not transport:
        return {"source": "no_required_tuple"}
    frame = _acquisition_frame(frame)
    obd = frame[frame["source"] == OBD_SOURCE].copy()
    obd["_channel"] = obd["channel"].astype(str).str.upper()
    rows = obd[obd["_channel"].isin(transport)]
    tagged = rows.dropna(subset=[GENERATION_COLUMN, ROUND_ID_COLUMN, ACQUIRED_AT_COLUMN])
    if tagged.empty:
        return {
            "source": SOURCE_NO_ROUNDS,
            "reason": (
                "no persisted row carries a round identity (generation/round_id with "
                "acquired_at); rows written before ts_schema 2 cannot be counted as tuples"
            ),
        }
    post_boundary = 0
    if generation_boundary is not None:
        beyond = tagged[GENERATION_COLUMN].astype(int) > int(generation_boundary)
        post_boundary = int(beyond.sum())
        tagged = tagged[~beyond]
    grouped = tagged.groupby([GENERATION_COLUMN, ROUND_ID_COLUMN], sort=True)
    summary = grouped.agg(
        present=("_channel", lambda s: frozenset(s)),
        first=(ACQUIRED_AT_COLUMN, "min"),
        last=(ACQUIRED_AT_COLUMN, "max"),
    )
    required = frozenset(transport)
    misses: dict[str, int] = {name: 0 for name in members}
    complete_times: list[float] = []
    skews: list[float] = []
    skew_rejected = 0
    for present, first, last in zip(
        summary["present"], summary["first"], summary["last"], strict=True
    ):
        absent = required - present
        if absent:
            for channel in absent:
                misses[transport[channel]] += 1
            continue
        skew = float(last) - float(first)
        skews.append(skew)
        if skew > max_skew_s:
            skew_rejected += 1
            continue
        complete_times.append(float(first))
    attempted = int(len(summary))
    complete = len(complete_times)
    accumulator = ChannelAccumulator(TUPLE_CHANNEL)
    elapsed = max(0.0, ended_at - started_at)
    for value in sorted(complete_times):
        accumulator.observe(value - started_at)
    skews_sorted = sorted(skews)
    return {
        "source": SOURCE_ROWS,
        "members": dict(members),
        "scheduled": None,
        "attempted": attempted,
        "complete": complete,
        "persisted": complete,
        "incomplete": attempted - complete,
        "skew_rejected": skew_rejected,
        "member_misses": misses,
        "post_boundary_rounds": post_boundary,
        "skew": {
            "p95_s": None
            if not skews_sorted
            else round(
                skews_sorted[min(len(skews_sorted) - 1, int(0.95 * (len(skews_sorted) - 1)))], 6
            ),
            "max_s": None if not skews_sorted else round(skews_sorted[-1], 6),
            "rounds_with_all_members": len(skews_sorted),
        },
        "lifetime": accumulator.snapshot(elapsed).to_dict(),
    }


def _observed_obd_channels(frame: pd.DataFrame) -> list[str]:
    obd = frame[frame["source"] == OBD_SOURCE]
    names = obd["channel"].astype(str).str.upper().unique().tolist()
    return sorted(name for name in names if not name.startswith(DID_PREFIX))


def _requests_for(profile: Mapping[str, Any], frame: pd.DataFrame) -> list[dict[str, Any]]:
    rows = profile_requests(profile, did_channel_for=did_channel_for)
    mode01 = {str(k).upper() for k in (profile.get("mode01") or {})}
    if "AUTO" in mode01 and profile.get("mode01_enabled", True):
        listed = {row["channel"] for row in rows}
        rows += [
            {"channel": name, "profile_channel": name, "kind": "pid", "capture_selected": True}
            for name in _observed_obd_channels(frame)
            if name not in listed
        ]
    return rows


#: What the score reads of the session, and nothing else (D03.64). The tuple
#: and cadence arithmetic below reads only ``obd`` rows, and of those only the
#: round identity in ``metadata_json``: a 108-minute ``tuning`` capture is 80 %
#: raw CAN frames, 7.7 % ADC samples and 6.7 % GPS, and reading all of them
#: (with the CAN decoded on read) peaked at 9.6 GB, past the Pi's 6 GiB
#: cgroup. The restriction is made once, here, at the parquet read, so no
#: caller can forget it; the two figures that *are* whole-session counts are
#: taken from the footers and a column scan instead (:func:`persisted_quality_inputs`).
SCORED_SOURCE = OBD_SOURCE
SCORED_COLUMNS = ("timestamp", "source", "channel", "metadata_json")


def read_scored_rows(session_dir: Path) -> pd.DataFrame:
    """The ``obd`` rows of ``session_dir`` on the acquisition clock: the only
    rows (and only the stored columns) the quality score reads.

    ``decoded="as_recorded"``: nothing the score uses is a decoded row, so
    nothing is decoded on read. Raises ``FileNotFoundError`` as the session
    reader does when no chunk is readable.
    """
    return read_acquisition_frame(
        session_dir,
        decoded="as_recorded",
        filters=[("source", "==", SCORED_SOURCE)],
        columns=SCORED_COLUMNS,
    )


#: What :func:`last_row_seconds_outside_scored` reads of every chunk.
_LAST_ROW_COLUMNS = ("timestamp", "source", "metadata_json")


def last_row_seconds_outside_scored(session_dir: Path) -> float | None:
    """The latest acquisition instant among the rows the score does **not**
    read (every source but :data:`SCORED_SOURCE`), or ``None`` when there are
    none.

    The interval's end, when nobody recorded one, is the last row of *any*
    source (a capture that ended on a CAN frame ended there), so the rows the
    score leaves unread still have to be asked. Asked of each chunk's
    ``timestamp``, ``source`` and ``metadata_json`` columns in Arrow, one chunk
    at a time: a row whose metadata cannot carry an ``acquired_at`` is on the
    stored clock and is never turned into a Python object, and the few that
    can (K-line exchanges) go through the same acquisition clock
    :func:`read_acquisition_frame` puts on every row.
    """
    import pyarrow.compute as pyarrow_compute
    import pyarrow.parquet as pq

    # The kernels (``not_equal``, ``match_substring``, ...) are generated when
    # pyarrow.compute is imported, so the stubs do not declare them.
    pc: Any = pyarrow_compute

    latest_stored: pd.Timestamp | None = None
    latest_stamped: float | None = None
    for chunk in sorted(session_dir.glob(PARQUET_PATTERN)):
        try:
            if chunk.stat().st_size == 0:
                continue
            with pq.ParquetFile(chunk) as handle:  # type: ignore[no-untyped-call, unused-ignore]
                names = handle.schema_arrow.names
                wanted = [name for name in _LAST_ROW_COLUMNS if name in names]
                table = handle.read(columns=wanted)
        except (OSError, ValueError):
            continue  # unreadable chunks are skipped by the session reader too
        if "timestamp" not in table.column_names or "source" not in table.column_names:
            continue
        table = table.filter(pc.fill_null(pc.not_equal(table["source"], SCORED_SOURCE), True))
        if table.num_rows == 0:
            continue
        plain, maybe = table, table.slice(0, 0)
        if "metadata_json" in table.column_names:
            metadata = table["metadata_json"]
            # The proof in ``channels._parse_candidates``: no key substring and
            # no backslash (a JSON escape) means no ``acquired_at`` is spelled.
            stamped = pc.fill_null(
                pc.or_(
                    pc.match_substring(metadata, ACQUIRED_AT_COLUMN),
                    pc.match_substring(metadata, "\\"),
                ),
                False,
            )
            plain, maybe = table.filter(pc.invert(stamped)), table.filter(stamped)
        if plain.num_rows:
            newest = pd.to_datetime(pc.max(plain["timestamp"]).as_py(), utc=True)
            latest_stored = newest if latest_stored is None else max(latest_stored, newest)
        if maybe.num_rows:
            on_clock = read_acquisition_frame(maybe.to_pandas())
            newest_seconds = float(_acquisition_seconds(on_clock).max())
            latest_stamped = (
                newest_seconds if latest_stamped is None else max(latest_stamped, newest_seconds)
            )
    seconds = [] if latest_stored is None else [_stored_seconds(latest_stored)]
    return max([*seconds, *([] if latest_stamped is None else [latest_stamped])], default=None)


def _stored_seconds(stamp: pd.Timestamp) -> float:
    """One stored instant as the acquisition-clock seconds every row's is."""
    return float(_acquisition_seconds(pd.DataFrame({"timestamp": pd.Series([stamp])})).iloc[0])


def persisted_quality_inputs(
    session_dir: Path,
    *,
    profile: Mapping[str, Any],
    started_at: float,
    ended_at: float | None,
    generation_boundary: int | None = None,
) -> dict[str, Any]:
    """Everything the evaluator needs from the rows, in one read.

    Returns ``requests``, ``lifetime_channels``, ``tuples``, ``rows_on_disk``
    and the ``ended_at`` actually used (the last row when none was given —
    then a missing tail is undetectable, and the caller says so).

    Reads only the rows the score is made of (:func:`read_scored_rows`). Two
    figures are counts over the **whole** session and would be silently wrong
    if taken from that read, so they are not:

    * ``rows_on_disk`` is the chunks' footer row count — every persisted row,
      compared against the writer's ``rows_written`` for the
      ``persistence_loss`` clause (a short count invents a loss). It used to
      be ``len`` of a decode-on-read frame, which counted the *decoded* rows
      synthesised from the CAN frames as if they were on disk (12.7 M against
      9.2 M stored on ``_capture-ltft-gate-2026-11-08T201601``), so a real
      shortfall could hide behind them.
    * the ``ended_at`` fallback, the last row of *any* source
      (:func:`last_row_seconds_outside_scored`), asked only when none was given.
    """
    rows_on_disk = count_session_rows(session_dir)
    try:
        if rows_on_disk is None:
            raise FileNotFoundError(f"no readable telemetry chunks in {session_dir}")
        frame = read_scored_rows(session_dir)
    except FileNotFoundError:
        return {
            "requests": profile_requests(profile, did_channel_for=did_channel_for),
            "lifetime_channels": {},
            "tuples": {"source": "unavailable", "reason": "no telemetry rows persisted"},
            "rows_on_disk": 0,
            "ended_at": ended_at if ended_at is not None else started_at,
            "ended_at_source": "given" if ended_at is not None else "no_rows",
        }
    ended_source = "given"
    if ended_at is None:
        ended_source = "last_row"
        instants = [last_row_seconds_outside_scored(session_dir)]
        if len(frame):
            instants.append(float(_acquisition_seconds(frame).max()))
        known = [value for value in instants if value is not None]
        ended_at = max(known) if known else started_at
    requests = _requests_for(profile, frame)
    channels = [str(row["channel"]) for row in requests]
    return {
        "requests": requests,
        "lifetime_channels": channel_lifetimes_from_rows(
            frame, channels, started_at=started_at, ended_at=ended_at
        ),
        "tuples": count_persisted_tuples(
            frame,
            member_channels(profile),
            max_skew_s=effective_max_tuple_skew_s(profile),
            started_at=started_at,
            ended_at=ended_at,
            generation_boundary=generation_boundary,
        ),
        "rows_on_disk": rows_on_disk,
        "ended_at": ended_at,
        "ended_at_source": ended_source,
    }


def recompute_capture_quality(  # noqa: PLR0913
    session_dir: Path,
    *,
    lifetime: Mapping[str, Any] | None = None,
    losses: Mapping[str, Any] | None = None,
    persistence: Mapping[str, Any] | None = None,
    scheduler: Mapping[str, Any] | None = None,
    scope: str = SCOPE_RECOMPUTE,
    ended_at: float | None = None,
    batching_start: Mapping[str, Any] | None = None,
    allow_event_loop: bool = False,
) -> CaptureQuality:
    """Score a capture session from what is on disk.

    ``lifetime`` (the live ledger at stop) wins over the rows for cadence and
    coverage when given — it saw observations the writer may have lost, which
    is exactly the difference ``persistence`` reports. Without it, the
    interval comes from the manifest's start and either ``ended_at``, the
    previous v2 sidecar's recorded end, or the last persisted row. Loss
    counters are taken from ``losses`` or the previous v2 sidecar; a session
    with neither reports them as unknown. ``batching_start`` is the arm-time
    batching baseline (``capture_quality.batching_baseline``); the scheduler's
    cumulative batching counters are reported as their delta against it.

    Refuses to run on a thread whose event loop is running: this reads every
    persisted row back, and inline on the daemon's loop it froze HTTP and
    collection for ~60 s per call (2026-09-17, 38,600 records dropped). Async
    callers use ``src.control.capture_finalize.recompute_quality_off_loop``.
    ``allow_event_loop`` is for offline tools whose loop has nothing
    latency-sensitive left on it; the daemon never passes it.
    """
    if not allow_event_loop:
        _refuse_on_running_loop()
    manifest = read_capture_manifest(session_dir)
    if manifest is None:
        raise ValueError(f"{session_dir} is not a capture session (no capture_profile.json)")
    profile = {**manifest.profile_snapshot, "name": manifest.profile}
    previous = read_capture_quality(session_dir)
    previous_v2 = (
        previous if previous is not None and int(previous.get("schema_version") or 1) >= 2 else None
    )
    started_at = _epoch(manifest.started_at)
    recorded_start = ((previous_v2 or {}).get("interval") or {}).get("started_wall")
    if recorded_start is not None:
        # Start and end must come from one clock: the sidecar recorded both
        # from the ledger, whereas the manifest stamp is a separate read.
        started_at = float(recorded_start)
    if lifetime is not None and lifetime.get("started_wall"):
        # The ledger's offsets are measured from its own arm instant; put the
        # rows on the same origin so a tuple at t=0.3 s is 0.3 s in both.
        started_at = float(lifetime["started_wall"])
    if ended_at is None and lifetime is not None and lifetime.get("elapsed_s") is not None:
        ended_at = started_at + float(lifetime["elapsed_s"])
    if ended_at is None and previous_v2 is not None:
        recorded = (previous_v2.get("interval") or {}).get("ended_wall")
        if recorded is not None:
            ended_at = float(recorded)
    generation_boundary = None
    if lifetime is not None:
        generation_boundary = _boundary_from(lifetime)
    elif previous_v2 is not None:
        generation_boundary = (previous_v2.get("persistence") or {}).get("generation_boundary")
    inputs = persisted_quality_inputs(
        session_dir,
        profile=profile,
        started_at=started_at,
        ended_at=ended_at,
        generation_boundary=generation_boundary,
    )
    effective_lifetime = _lifetime_for(
        lifetime, previous_v2, inputs, started_at=started_at, ended_at=float(inputs["ended_at"])
    )
    if losses is None and previous_v2 is not None:
        losses = previous_v2.get("losses")
    if persistence is None and previous_v2 is not None:
        persistence = previous_v2.get("persistence")
    persistence_block = dict(persistence or {})
    persistence_block.setdefault("rows_on_disk", inputs["rows_on_disk"])
    persistence_block["rows_on_disk_at_recompute"] = inputs["rows_on_disk"]
    quality = evaluate_capture_quality(
        profile=profile,
        scheduler=scheduler,
        lifetime=effective_lifetime,
        tuples=inputs["tuples"],
        losses=losses,
        persistence=persistence_block,
        requests=(
            list(manifest.resolved_polling_plan.get("members") or [])
            if manifest.resolved_polling_plan is not None
            else inputs["requests"]
        ),
        scope=scope,
        batching_start=batching_start,
    )
    notes = list(quality.notes)
    if inputs["ended_at_source"] == "last_row":
        notes.append(
            "interval end taken from the last persisted row (no recorded stop): a missing "
            "tail cannot be detected from the rows alone."
        )
    if losses is None:
        notes.append("loss counters unknown: this session recorded no capture epochs.")
    return CaptureQuality(**{**quality.__dict__, "notes": notes})


def _refuse_on_running_loop() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError(
        "recompute_capture_quality must not run on an event loop: it held the daemon's "
        "loop ~60 s per call on 2026-09-17 (38,600 records dropped). Await "
        "src.control.capture_finalize.recompute_quality_off_loop instead."
    )


def _boundary_from(lifetime: Mapping[str, Any]) -> int | None:
    value = lifetime.get("generation_boundary")
    return None if value is None else int(value)


def _lifetime_for(
    lifetime: Mapping[str, Any] | None,
    previous_v2: Mapping[str, Any] | None,
    inputs: Mapping[str, Any],
    *,
    started_at: float,
    ended_at: float,
) -> dict[str, Any]:
    if lifetime is not None:
        return dict(lifetime)
    interval = (previous_v2 or {}).get("interval") or {}
    elapsed = max(0.0, ended_at - started_at)
    return {
        "source": SOURCE_ROWS,
        "started_wall": started_at,
        "ended_wall": ended_at,
        "elapsed_s": elapsed,
        "pause_windows": list(interval.get("pause_windows") or []),
        "pause_reasons": list(interval.get("pause_reasons") or []),
        "generation_at_start": interval.get("generation_at_start"),
        "channels": dict(inputs["lifetime_channels"]),
    }


def read_quality_with_scope(session_dir: Path) -> dict[str, Any] | None:
    """The persisted verdict with its scope explicit (v1 -> trailing_snapshot)."""
    payload = read_capture_quality(session_dir)
    return None if payload is None else normalize_quality_payload(payload)


def epoch_seconds(value: datetime) -> float:
    return _epoch(value)


__all__ = [
    "SOURCE_NO_ROUNDS",
    "SOURCE_ROWS",
    "channel_lifetimes_from_rows",
    "count_persisted_tuples",
    "did_channel_for",
    "member_channels",
    "persisted_quality_inputs",
    "read_quality_with_scope",
    "read_scored_rows",
    "recompute_capture_quality",
]
