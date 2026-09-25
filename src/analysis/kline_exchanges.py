"""What a sniffed K-line session said, by module: exchanges, cycles, bits, values.

Works from a session's stored ``kline`` rows, never from the live sniffer, so
any session can be re-read. Four pieces (K01.1-K01.3):

- :func:`wire_frames` re-reads every ``kline:FRAME`` row: direction from the
  service byte (:func:`~src.collection.kline.protocol.is_response_sid`, not
  the direction stored by a sniffer that judged it against a request it may
  have missed) and the module each frame belongs to. A frame recorded since
  K01.1 carries its module (``module_basis = "recorded"``); an older one gets
  it from this session's own StartCommunication frames
  (``"reconstructed"``); before any StartCommunication it is ``unknown``.
- :func:`attribute_modules` names the module of the legacy value rows
  (``DID_1202``, ``RAM_...``) by the ``FRAME`` row stored at the same instant,
  returning a copy with ``<module>:<item>`` channels. Stored rows are never
  rewritten.
- :func:`exchange_report` pairs requests with replies and summarises them per
  module: services, DIDs, NRCs by name, per-DID poll intervals, the DID cycle a
  scan-tool screen repeats versus one-off reads, and how well each frame's
  time is known. :func:`render_markdown` prints it.
- :func:`bit_transitions` lists each bit's changes in a flag word, with times,
  to match against an operator's actions; :func:`pair_value` pairs a candidate
  DID with a reference channel through
  :func:`~src.analysis.compare.compare_channels` and reports the per-sample
  ratio — never an aggregate.

A rate here is always the median interval between consecutive reads of one
DID, never a count over a wall-clock span (``achieved_hz_capture`` read a
14.93 Hz capture as 11.25 Hz that way).
"""

from __future__ import annotations

import json
import statistics
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import numpy as np
import pandas as pd

from src.analysis.compare import ChannelComparison, align_pairs, compare_channels
from src.analysis.pairing import PairingPolicy
from src.collection.kline import protocol as kp
from src.collection.kline.names import FRAME_CHANNEL, LEGACY_DID_PREFIX, is_legacy
from src.collection.kline.profile import KlineProfile

KLINE = "kline"
ModuleBasis = Literal["recorded", "reconstructed", "unknown"]
#: A DID read at least this often in one module's traffic is a cycle member;
#: fewer, a one-off (screen-load or identification reads).
MIN_CYCLE_READS = 3


class KlineReportError(ValueError):
    """The session cannot answer the question, and says which kind of empty."""


# --------------------------------------------------------------------------- frames


@dataclass(frozen=True)
class WireFrame:
    at: pd.Timestamp
    raw: bytes
    kind: Literal["addressed", "length"]
    payload: bytes
    direction: Literal["tester", "ecu"]
    module: int | None
    module_basis: ModuleBasis
    #: ``None``: the sniffer that recorded it did not measure it.
    timing_uncertainty_s: float | None
    #: The direction the sniffer stored, when it differs from the service byte's.
    stored_direction: str | None = None


def _metadata(raw: object) -> dict[str, Any]:
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _kline_rows(frame: pd.DataFrame) -> pd.DataFrame:
    rows = frame[frame["source"] == KLINE]
    return rows.sort_values("timestamp", kind="stable")


def wire_frames(frame: pd.DataFrame) -> list[WireFrame]:
    """Every stored ``kline:FRAME``, re-read, in time order."""
    rows = _kline_rows(frame)
    rows = rows[rows["channel"] == FRAME_CHANNEL]
    out: list[WireFrame] = []
    current: int | None = None  # reconstructed session target
    for timestamp, text, meta_json in zip(
        rows["timestamp"], rows["value_text"], rows["metadata_json"], strict=True
    ):
        meta = _metadata(meta_json)
        raw = bytes.fromhex(str(text))
        kind: Literal["addressed", "length"] = (
            "addressed" if meta.get("kind") == "addressed" else "length"
        )
        payload = kp.decode_addressed(raw).payload if kind == "addressed" else raw[1:-1]
        is_reply = kp.is_response_sid(payload[0])
        direction: Literal["tester", "ecu"] = "ecu" if is_reply else "tester"
        if kind == "addressed":
            addressed = kp.decode_addressed(raw)
            reconstructed: int | None = addressed.source if is_reply else addressed.target
            if payload[0] == kp.SID_START_COMMUNICATION + kp.POSITIVE_OFFSET:
                current = addressed.source
        else:
            reconstructed = current
            if payload[0] == kp.SID_STOP_COMMUNICATION + kp.POSITIVE_OFFSET:
                current = None
        module: int | None
        basis: ModuleBasis
        if "module" in meta:
            address = meta.get("module_address")
            module = int(address) if isinstance(address, int) else None
            basis = "recorded" if module is not None else "unknown"
        else:
            module = reconstructed
            basis = "reconstructed" if module is not None else "unknown"
        uncertainty = meta.get("timing_uncertainty_s")
        stored = meta.get("direction")
        out.append(
            WireFrame(
                at=pd.Timestamp(timestamp),
                raw=raw,
                kind=kind,
                payload=payload,
                direction=direction,
                module=module,
                module_basis=basis,
                timing_uncertainty_s=float(uncertainty) if uncertainty is not None else None,
                stored_direction=stored if stored not in (None, direction) else None,
            )
        )
    return out


def attribute_modules(frame: pd.DataFrame, profile: KlineProfile) -> pd.DataFrame:
    """A copy of ``frame`` whose legacy K-line value rows name their module.

    A legacy sniffed row (``instrument: sniff``) was stored at the same instant
    as the ``FRAME`` row of the reply it came from, so that frame's module is
    its module. A legacy row CANary read itself came from the ECM, the only
    module the pre-K01.1 reader could address. With neither, ``unknown``.
    """
    out = frame.copy()
    kline = out["source"] == KLINE
    legacy = kline & out["channel"].astype("string").map(is_legacy, na_action="ignore").eq(True)
    if not legacy.any():
        return out
    replies = {wf.at: wf.module for wf in wire_frames(frame) if wf.direction == "ecu"}
    ecm = profile.module_label(profile.ecm_address)
    renamed: dict[Any, str] = {}
    rows = out[legacy]
    for index, channel, meta_json, timestamp in zip(
        rows.index, rows["channel"], rows["metadata_json"], rows["timestamp"], strict=True
    ):
        meta = _metadata(meta_json)
        if meta.get("instrument") != "sniff":
            label = ecm
        else:
            label = profile.module_label(replies.get(pd.Timestamp(timestamp)))
        name = str(channel)
        if name.startswith(LEGACY_DID_PREFIX):
            item = name[len(LEGACY_DID_PREFIX) :]
        else:  # RAM_/MEM_ keep their prefix after the module
            item = name
        renamed[index] = f"{label}:{item}"
    out.loc[list(renamed), "channel"] = list(renamed.values())
    if "source_channel" in out.columns:
        out.loc[list(renamed), "source_channel"] = [f"{KLINE}:{c}" for c in renamed.values()]
    return out


# --------------------------------------------------------------------------- exchanges


@dataclass(frozen=True)
class KlineExchange:
    module: str
    at: pd.Timestamp
    request: bytes
    reply: bytes | None

    @property
    def service(self) -> int:
        return self.request[0]

    @property
    def outcome(self) -> Literal["positive", "negative", "no_reply"]:
        if self.reply is None:
            return "no_reply"
        return "negative" if self.reply[0] == kp.SID_NEGATIVE_RESPONSE else "positive"

    @property
    def nrc(self) -> int | None:
        if self.reply is None or self.reply[0] != kp.SID_NEGATIVE_RESPONSE or len(self.reply) < 3:
            return None
        return self.reply[2]

    @property
    def did(self) -> int | None:
        if self.service == kp.SID_READ_DATA_BY_COMMON_ID and len(self.request) >= 3:
            return int.from_bytes(self.request[1:3], "big")
        return None

    @property
    def identifier(self) -> str:
        """DID for ``22``; the sub-function or local id byte otherwise."""
        if self.did is not None:
            return f"{self.did:04X}"
        return f"{self.request[1]:02X}" if len(self.request) > 1 else ""

    @property
    def value(self) -> bytes | None:
        """A positive ``22`` reply's value bytes."""
        if self.did is None or self.outcome != "positive" or self.reply is None:
            return None
        return bytes(self.reply[3:])


def _answers(request: bytes, reply: bytes) -> bool:
    if reply[0] == kp.SID_NEGATIVE_RESPONSE:
        return len(reply) > 1 and reply[1] == request[0]
    return reply[0] == (request[0] + kp.POSITIVE_OFFSET) & 0xFF


def pair_exchanges(
    frames: Sequence[WireFrame], profile: KlineProfile
) -> tuple[list[KlineExchange], list[WireFrame]]:
    """Requests with their replies, and the replies whose request was never
    recorded (the sniffer missed it)."""
    exchanges: list[KlineExchange] = []
    orphans: list[WireFrame] = []
    pending: WireFrame | None = None

    def flush(reply: bytes | None) -> None:
        assert pending is not None
        exchanges.append(
            KlineExchange(profile.module_label(pending.module), pending.at, pending.payload, reply)
        )

    for wf in frames:
        if wf.direction == "tester":
            if pending is not None:
                flush(None)
            pending = wf
            continue
        if pending is not None and _answers(pending.payload, wf.payload):
            flush(wf.payload)
            pending = None
        else:
            orphans.append(wf)
    if pending is not None:
        flush(None)
    return exchanges, orphans


def _service_name(service: int) -> str:
    return kp.SERVICE_NAMES.get(service, f"0x{service:02X}")


def _nrc_name(code: int) -> str:
    return kp.NEGATIVE_RESPONSE_CODES.get(code, "unknown")


@dataclass
class DidSummary:
    did: str
    reads: int
    positive: int
    negative: dict[str, int]
    no_reply: int
    first_at: str
    last_at: str
    #: Median interval between consecutive reads, and how many intervals.
    median_interval_s: float | None
    n_intervals: int
    rate_hz: float | None
    width: int | None
    values: dict[str, int]
    binding: str | None
    role: Literal["cycle", "one_off"] = "one_off"


@dataclass
class ModuleSummary:
    module: str
    address: str | None
    module_basis: dict[str, int]
    frames: int
    exchanges: int
    services: dict[str, int]
    nrcs: dict[str, int]
    dids: list[DidSummary]
    cycle: list[str]
    cycle_repetitions: int
    cycle_period_s: float | None
    one_off: list[str]
    other_requests: list[dict[str, Any]]


@dataclass
class TimingSummary:
    frames: int
    recorded: int
    max_s: float | None
    p95_s: float | None
    note: str


@dataclass
class ExchangeReport:
    session: str
    first_at: str | None
    last_at: str | None
    frames: int
    modules: list[ModuleSummary]
    orphan_replies: int
    direction_corrections: int
    timing: TimingSummary
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _iso(ts: pd.Timestamp) -> str:
    return ts.isoformat()


def _cycle(sequence: list[str], members: set[str]) -> tuple[list[str], int]:
    """The repeated order of ``members`` in ``sequence``: split at the most
    read member, take the commonest run, rotate to the first one read."""
    ordered = [d for d in sequence if d in members]
    if not ordered:
        return [], 0
    anchor = Counter(ordered).most_common(1)[0][0]
    runs: list[tuple[str, ...]] = []
    run: list[str] = []
    for did in ordered:
        if did == anchor and run:
            runs.append(tuple(run))
            run = []
        run.append(did)
    if not runs:
        return [anchor], 0
    common, repetitions = Counter(runs).most_common(1)[0]
    first = next(d for d in ordered if d in common)
    start = common.index(first)
    return list(common[start:] + common[:start]), repetitions


def exchange_report(
    frame: pd.DataFrame, profile: KlineProfile, *, session: str = ""
) -> ExchangeReport:
    """Summarise a session's K-line traffic by module. Refuses a session with
    no ``kline`` rows, saying which kind of empty it is."""
    kline = frame[frame["source"] == KLINE] if "source" in frame.columns else frame.iloc[0:0]
    if kline.empty:
        raise KlineReportError(
            f"{session or 'session'}: no kline rows at all — the K-line collector was not "
            "running (hardware.kline.enabled off, or no HAT), not a quiet line"
        )
    frames = wire_frames(frame)
    if not frames:
        raise KlineReportError(
            f"{session or 'session'}: kline rows but no FRAME rows — only values were "
            "stored, so there is no wire traffic to report"
        )
    exchanges, orphans = pair_exchanges(frames, profile)
    corrections = sum(1 for wf in frames if wf.stored_direction is not None)
    modules: list[ModuleSummary] = []
    labels = sorted({profile.module_label(wf.module) for wf in frames})
    for label in labels:
        module_frames = [wf for wf in frames if profile.module_label(wf.module) == label]
        module_exchanges = [e for e in exchanges if e.module == label]
        modules.append(_module_summary(label, module_frames, module_exchanges, profile))
    notes: list[str] = []
    if corrections:
        notes.append(
            f"{corrections} frames were stored with the other direction: the sniffer judged "
            "direction against the pending request, and a reply whose request it missed "
            "read as a request. Direction here is from the service byte."
        )
    if orphans:
        notes.append(
            f"{len(orphans)} replies have no recorded request (the sniffer missed it, e.g. a "
            "host stall before 892e4c9); they count as frames, not exchanges."
        )
    if any(wf.module_basis == "reconstructed" for wf in frames):
        notes.append(
            "Modules were reconstructed from this session's StartCommunication frames (it "
            "predates K01.1). Wake-ups are not stored, so a StartCommunication the sniffer "
            "missed would carry the previous module forward."
        )
    return ExchangeReport(
        session=session,
        first_at=_iso(frames[0].at),
        last_at=_iso(frames[-1].at),
        frames=len(frames),
        modules=modules,
        orphan_replies=len(orphans),
        direction_corrections=corrections,
        timing=_timing(frames),
        notes=notes,
    )


def _timing(frames: Sequence[WireFrame]) -> TimingSummary:
    recorded = [wf.timing_uncertainty_s for wf in frames if wf.timing_uncertainty_s is not None]
    if not recorded:
        return TimingSummary(
            frames=len(frames),
            recorded=0,
            max_s=None,
            p95_s=None,
            note="not recorded: this session's sniffer predates metadata.timing_uncertainty_s "
            "(892e4c9), so frame times are read times with unknown slack",
        )
    values = np.asarray(recorded, dtype=float)
    return TimingSummary(
        frames=len(frames),
        recorded=len(recorded),
        max_s=float(values.max()),
        p95_s=float(np.percentile(values, 95)),
        note="each frame ended no earlier than its stamp minus its timing_uncertainty_s",
    )


def _module_summary(
    label: str,
    frames: Sequence[WireFrame],
    exchanges: Sequence[KlineExchange],
    profile: KlineProfile,
) -> ModuleSummary:
    address = frames[0].module if frames else None
    module = profile.modules.get(address) if address is not None else None
    services = Counter(_service_name(e.service) for e in exchanges)
    nrcs = Counter(
        f"{_service_name(e.service)} {e.identifier}: 0x{e.nrc:02X} {_nrc_name(e.nrc)}"
        for e in exchanges
        if e.nrc is not None
    )
    reads = [e for e in exchanges if e.did is not None]
    by_did: dict[str, list[KlineExchange]] = {}
    for e in reads:
        by_did.setdefault(e.identifier, []).append(e)
    summaries: list[DidSummary] = []
    for did, items in sorted(by_did.items()):
        times = [e.at for e in items]
        intervals = [(b - a).total_seconds() for a, b in zip(times, times[1:], strict=False)]
        median = statistics.median(intervals) if intervals else None
        values = Counter(e.value.hex() for e in items if e.value is not None)
        widths = {len(e.value) for e in items if e.value is not None}
        entry = module.did(int(did, 16)) if module is not None else None
        binding = None
        if entry is not None and entry.name is not None:
            binding = f"{entry.name} ({entry.status})"
        elif entry is not None and entry.flags:
            binding = ", ".join(f"{f.name}=0x{f.mask:04X} ({f.status})" for f in entry.flags)
        summaries.append(
            DidSummary(
                did=did,
                reads=len(items),
                positive=sum(e.outcome == "positive" for e in items),
                negative=dict(
                    Counter(f"0x{e.nrc:02X} {_nrc_name(e.nrc)}" for e in items if e.nrc is not None)
                ),
                no_reply=sum(e.outcome == "no_reply" for e in items),
                first_at=_iso(times[0]),
                last_at=_iso(times[-1]),
                median_interval_s=median,
                n_intervals=len(intervals),
                rate_hz=(1.0 / median) if median else None,
                width=widths.pop() if len(widths) == 1 else None,
                values=dict(values.most_common(8)),
                binding=binding,
            )
        )
    members = {s.did for s in summaries if s.reads >= MIN_CYCLE_READS}
    for s in summaries:
        s.role = "cycle" if s.did in members else "one_off"
    cycle, repetitions = _cycle([e.identifier for e in reads], members)
    period = None
    if cycle:
        anchor_times = [e.at for e in reads if e.identifier == cycle[0]]
        gaps = [
            (b - a).total_seconds() for a, b in zip(anchor_times, anchor_times[1:], strict=False)
        ]
        period = statistics.median(gaps) if gaps else None
    others = Counter(
        (e.request.hex(" "), e.reply.hex(" ") if e.reply is not None else "no reply")
        for e in exchanges
        if e.did is None
    )
    return ModuleSummary(
        module=label,
        address=f"0x{address:02X}" if address is not None else None,
        module_basis=dict(Counter(wf.module_basis for wf in frames)),
        frames=len(frames),
        exchanges=len(exchanges),
        services=dict(services.most_common()),
        nrcs=dict(nrcs.most_common()),
        dids=summaries,
        cycle=cycle,
        cycle_repetitions=repetitions,
        cycle_period_s=period,
        one_off=sorted(s.did for s in summaries if s.role == "one_off"),
        other_requests=[
            {
                "service": _service_name(bytes.fromhex(req.replace(" ", ""))[0]),
                "request": req,
                "reply": rep,
                "count": n,
            }
            for (req, rep), n in others.most_common()
        ],
    )


def _fmt_s(value: float | None, digits: int = 3) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def render_markdown(report: ExchangeReport) -> str:
    lines = [
        f"# K-line exchanges: {report.session}",
        "",
        "Generated by `tools/kline_exchange_report.py` from the session's stored "
        "`kline:FRAME` rows (`src/analysis/kline_exchanges.py`).",
        "",
        f"- Frames: {report.frames}, {report.first_at} to {report.last_at}",
        f"- Replies with no recorded request: {report.orphan_replies}",
        f"- Frames whose stored direction was corrected from the service byte: "
        f"{report.direction_corrections}",
        f"- Timing uncertainty: {report.timing.note}"
        + (
            f" (max {_fmt_s(report.timing.max_s, 4)} s, p95 {_fmt_s(report.timing.p95_s, 4)} s "
            f"over {report.timing.recorded} of {report.timing.frames} frames)"
            if report.timing.recorded
            else ""
        ),
        "",
    ]
    for note in report.notes:
        lines.append(f"> {note}")
        lines.append("")
    for module in report.modules:
        lines += [
            f"## Module {module.module}" + (f" ({module.address})" if module.address else ""),
            "",
            f"- Frames {module.frames}, exchanges {module.exchanges}; module basis "
            + ", ".join(f"{k} {v}" for k, v in sorted(module.module_basis.items())),
            "- Services: " + ", ".join(f"{k} ×{v}" for k, v in module.services.items()),
        ]
        if module.cycle:
            lines.append(
                f"- Screen cycle ({len(module.cycle)} DIDs, {module.cycle_repetitions} exact "
                f"repetitions, median period {_fmt_s(module.cycle_period_s, 2)} s): "
                + " → ".join(module.cycle)
            )
        if module.one_off:
            lines.append("- One-off reads: " + ", ".join(module.one_off))
        if module.nrcs:
            lines.append("- NRCs: " + "; ".join(f"{k} ×{v}" for k, v in module.nrcs.items()))
        lines.append("")
        if module.dids:
            lines += [
                "| DID | role | reads | positive | refused | no reply | median interval s "
                "(n) | width | values (top) | binding |",
                "|---|---|---|---|---|---|---|---|---|---|",
            ]
            for d in module.dids:
                refused = ", ".join(f"{k} ×{v}" for k, v in d.negative.items()) or "—"
                values = ", ".join(f"`{k}` ×{v}" for k, v in list(d.values.items())[:4]) or "—"
                lines.append(
                    f"| 0x{d.did} | {d.role} | {d.reads} | {d.positive} | {refused} | "
                    f"{d.no_reply} | {_fmt_s(d.median_interval_s, 2)} ({d.n_intervals}) | "
                    f"{d.width if d.width is not None else '—'} | {values} | "
                    f"{d.binding or '—'} |"
                )
            lines.append("")
        if module.other_requests:
            lines += ["| service | request | reply | count |", "|---|---|---|---|"]
            for other in module.other_requests:
                lines.append(
                    f"| {other['service']} | `{other['request']}` | `{other['reply']}` | "
                    f"{other['count']} |"
                )
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- bits


@dataclass(frozen=True)
class BitTransition:
    at: pd.Timestamp
    mask: int
    became: bool
    word_before: int
    word_after: int


def bit_transitions(frame: pd.DataFrame, channel: str) -> list[BitTransition]:
    """Every change of every bit of the flag word ``kline:<channel>``
    (``ABS:1303``), in time order, one entry per bit per change."""
    rows = _kline_rows(frame)
    rows = rows[rows["channel"] == channel]
    if rows.empty:
        raise KlineReportError(
            f"kline:{channel}: no rows — the word was never read in this session "
            "(or is stored under a legacy name; see attribute_modules)"
        )
    out: list[BitTransition] = []
    previous: int | None = None
    for timestamp, text in zip(rows["timestamp"], rows["value_text"], strict=True):
        word = int(str(text), 16)
        if previous is not None and word != previous:
            changed = word ^ previous
            bit = 1
            while bit <= changed:
                if changed & bit:
                    out.append(
                        BitTransition(
                            pd.Timestamp(timestamp), bit, bool(word & bit), previous, word
                        )
                    )
                bit <<= 1
        previous = word
    return out


def transitions_between(
    transitions: Iterable[BitTransition], start: pd.Timestamp | str, end: pd.Timestamp | str
) -> dict[int, list[BitTransition]]:
    """Transitions inside ``[start, end]``, by bit: which bits an operator
    action in that window moved."""
    lo, hi = pd.Timestamp(start), pd.Timestamp(end)
    out: dict[int, list[BitTransition]] = {}
    for t in transitions:
        if lo <= t.at <= hi:
            out.setdefault(t.mask, []).append(t)
    return out


# --------------------------------------------------------------------------- values


@dataclass(frozen=True)
class ValuePairing:
    """A candidate DID against a reference, at the same instants."""

    comparison: ChannelComparison
    n_pairs: int
    #: ``reference / candidate`` for every pair whose candidate is non-zero.
    ratio_median: float
    ratio_min: float
    ratio_max: float
    #: Standard deviation over median, percent.
    ratio_spread_pct: float
    candidate_min: float
    candidate_max: float

    def describe(self) -> str:
        return (
            f"{self.comparison.a} vs {self.comparison.b}: {self.n_pairs} pairs, per-sample "
            f"ratio median {self.ratio_median:.5g} (range {self.ratio_min:.5g}-"
            f"{self.ratio_max:.5g}, spread {self.ratio_spread_pct:.2f} %), candidate "
            f"{self.candidate_min:g}-{self.candidate_max:g}; {self.comparison.describe()}"
        )


def pair_value(
    frame: pd.DataFrame,
    candidate: str,
    reference: str,
    *,
    policy: PairingPolicy,
    min_pairs: int = 10,
) -> ValuePairing:
    """Pair ``candidate`` (a ``kline:<module>:<hex>`` spec, raw counts) with
    ``reference`` (any channel spec in engineering units) through
    :func:`compare_channels`, and give the per-sample ratio beside its fit.
    The verdict alone misleads in both directions (AGENTS.md), so both."""
    comparison = compare_channels(frame, candidate, reference, policy=policy, min_pairs=min_pairs)
    pairs = align_pairs(frame, candidate, reference, policy=policy).pairs
    usable = pairs[pairs["a"] != 0]
    if usable.empty:
        raise KlineReportError(f"{candidate}: every paired sample is zero; no ratio exists")
    ratio = (usable["b"] / usable["a"]).to_numpy(dtype=float)
    median = float(np.median(ratio))
    return ValuePairing(
        comparison=comparison,
        n_pairs=int(len(usable)),
        ratio_median=median,
        ratio_min=float(ratio.min()),
        ratio_max=float(ratio.max()),
        ratio_spread_pct=float(np.std(ratio) / median * 100.0),
        candidate_min=float(usable["a"].min()),
        candidate_max=float(usable["a"].max()),
    )


__all__ = [
    "MIN_CYCLE_READS",
    "BitTransition",
    "DidSummary",
    "ExchangeReport",
    "KlineExchange",
    "KlineReportError",
    "ModuleSummary",
    "ValuePairing",
    "WireFrame",
    "attribute_modules",
    "bit_transitions",
    "exchange_report",
    "pair_exchanges",
    "pair_value",
    "render_markdown",
    "transitions_between",
    "wire_frames",
]
