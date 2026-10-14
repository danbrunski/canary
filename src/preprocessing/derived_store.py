"""The derived session layer (P01.2, P01.3): reduce a session once, reuse it.

A derived frame is one session's :class:`~src.preprocessing.derived.ReducedSession`
on disk (a ``cells-<write>.parquet`` and the ``meta.json`` that names it, so
one rename publishes the pair: :func:`_write`), under a gitignored directory
outside every sessions tree (:data:`DEFAULT_DERIVED_DIR`). Raw parquet stays
the source of truth: a derived frame can always be rebuilt, and is never
built on the Pi (:func:`refuse_on_the_pi`).

**Versioning.** Some inputs shape every channel -- the session's chunk files,
the grid interval, the quality-gate limits and :data:`DERIVED_CODE_VERSION` --
and a change to any of them rebuilds the whole frame. The decode catalogs are
versioned per *decode unit*, the smallest piece of catalog that produces its
own channels:

- ``binding:<NAME>``: one ``bindings.yaml`` signal → ``decoded:<NAME>`` (and
  its ``_SENTINEL`` state channel);
- ``did:<NAME>``: the ``dids.yaml`` entry(ies) named NAME → ``did:<NAME>``;
- ``dbc``: the ``.dbc`` database → every ``dbc:*`` channel;
- ``raw:<source>:<channel>``: a recorded channel no decoder claims.

Each unit's hash is its spec, so editing one binding re-derives that one
channel from a filtered read of the rows it needs, adding a DID adds its
column, and every other channel is read back, not recomputed (P01.3).
Channels are separable because the reduction is (``derived``'s docstring):
a channel's kept cells depend only on its own rows, and the grid's bin edges
are the min/max over each channel's own first/last rows.

A stale frame is detected by hash and rebuilt, never served; the
:class:`DerivedLoad` a load returns says which of built / reused / refreshed
happened and why.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from src.can_ids import parse_can_channel
from src.decode.on_read import on_read_catalog
from src.decode.slices import sentinel_channel
from src.paths import DATA_DIR
from src.preprocessing.decode import on_read_hash
from src.preprocessing.derived import (
    CELL_COLUMNS,
    ReducedSession,
    grid_interval_ns,
    reduce_numeric_session,
)
from src.preprocessing.imu_stream import plan_imu_frame
from src.preprocessing.resampler import ResampleConfig
from src.preprocessing.session_decode import reduce_session
from src.preprocessing.session_reader import SESSION_PART_ROWS
from src.signals.fsm import PHYSICAL_LIMITS
from src.storage.broadcast_storage import refuse_shorter_read_timeout, session_can_storage
from src.storage.session_files import PARQUET_PATTERN, session_files_fingerprint
from src.telemetry import DBC_SOURCE, DECODED_SOURCE, SOURCE_SEP

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_DERIVED_DIR",
    "DERIVED_CODE_VERSION",
    "DERIVED_DIR_ENV",
    "NUMERIC_CODE_VERSION",
    "DecodeInputs",
    "DerivedLoad",
    "DerivedStore",
    "active_store",
    "on_the_pi",
    "refuse_on_the_pi",
    "use_store",
]

#: Bump on any change that alters a derived frame for unchanged inputs (the
#: reduction's rules, the stored form). Every frame of another version is
#: rebuilt on its next load.
#:
#: 1 -- P01.1/P01.2: per-channel bin extremes, typed cell values.
#: 2 -- cells in a per-write file that ``meta.json`` names (:func:`_write`).
DERIVED_CODE_VERSION = 2

#: The ``value_number`` frame's own version, beside :data:`DERIVED_CODE_VERSION`
#: (which it also checks). Bump on a change to that chain alone, so the
#: trainers' frames, whose build is 28 minutes over the corpus, are not
#: rebuilt for it.
#:
#: 1 -- P02.8: ``decoded:`` rows decoded on read with the on-read catalog
#: (versioned by ``on_read_hash``), not taken from stored rows. A session that
#: stored fewer bindings than the catalog now has (every recording before
#: 2026-08-07) gains their channels.
#: 2 -- H02.4: a session's resolved ``imu_frame.json`` adds ``imu:ACCEL_LON/LAT``
#: (``imu_stream``), decimated to ``imu:ACCEL_LON_MAX`` etc. The sidecar's
#: identity (``ImuFramePlan.key``) is versioned beside the chunks, so resolving
#: or re-resolving a session rebuilds its frame.
NUMERIC_CODE_VERSION = 2

#: Gitignored (``data/cache/``) and outside every sessions tree, so training
#: discovery never walks it.
DEFAULT_DERIVED_DIR = DATA_DIR / "cache" / "derived_sessions"

#: A laptop process that sets this reads and writes derived frames there;
#: unset (the daemon, the MCP server, every test that does not ask) it reduces
#: in memory and stores nothing. Child processes inherit it, so a trainer's
#: process pool uses the same store as its parent.
DERIVED_DIR_ENV = "CANARY_DERIVED_DIR"

_DID_SOURCE = "did"
_DID_PREFIX = "DID_"
#: Each write's cells file is ``cells-<pid>-<uuid>.parquet``; ``meta.json``
#: records which one is the frame's (:func:`_write`).
_CELLS_PREFIX = "cells-"
_CELLS_SUFFIX = ".parquet"
#: Version 1's fixed-name cells file, deleted by the first write over it.
_LEGACY_CELLS = "cells.parquet"
#: Subdirectory of a session's frame holding the ``value_number`` chain.
_NUMERIC = "numeric"
_META = "meta.json"


def refuse_on_the_pi() -> None:
    """Derived frames are built on the laptop only (P01 rules).

    The Pi collects; a whole-session reduction there costs the same
    outage the ``nice -19`` pandas run did (AGENTS.md: no analysis on the Pi
    while it collects). Its device tree names the board.
    """
    if on_the_pi():
        raise RuntimeError(
            "derived session frames are never built on the Pi: pull the session "
            "and derive it on the laptop"
        )


def on_the_pi() -> bool:
    """True on a Raspberry Pi (its device tree names the board)."""
    try:
        text = Path("/proc/device-tree/model").read_text(errors="ignore")
    except OSError:
        return False
    return "Raspberry Pi" in text


def active_store() -> DerivedStore | None:
    """The store :data:`DERIVED_DIR_ENV` names, or ``None`` (reduce in memory).

    Always ``None`` on the Pi, whatever the environment says: the daemon's
    trip-end digest and workbench children run the same session readers there,
    and they must keep the bounded no-store path rather than build frames.
    """
    root = os.environ.get(DERIVED_DIR_ENV, "").strip()
    if not root:
        return None
    if on_the_pi():
        log.warning("%s is set on the Pi; derived frames stay off there", DERIVED_DIR_ENV)
        return None
    return DerivedStore(Path(root))


@contextmanager
def use_store(root: Path | None) -> Iterator[None]:
    """Within the block, :func:`active_store` (and every child process) uses
    ``root``; ``None`` turns the store off. Restores the previous setting, so
    an in-process caller (a test running a script's ``main``) leaks nothing."""
    previous = os.environ.get(DERIVED_DIR_ENV)
    if root is None:
        os.environ.pop(DERIVED_DIR_ENV, None)
    else:
        os.environ[DERIVED_DIR_ENV] = str(root)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(DERIVED_DIR_ENV, None)
        else:
            os.environ[DERIVED_DIR_ENV] = previous


@dataclass(frozen=True)
class DecodeInputs:
    """The decode catalogs, as loaded (``None`` = that decoder is off)."""

    bindings: Any = None
    dbc: Any = None
    did_map: Any = None


@dataclass
class DerivedLoad:
    """What a load did: ``built`` (no usable frame), ``reused`` (fresh) or
    ``refreshed`` (some decode units re-derived), and why."""

    session: str
    status: str
    reason: str
    rederived_units: list[str] = field(default_factory=list)
    reused_channels: int = 0
    seconds: float = 0.0


@dataclass(frozen=True)
class _Unit:
    uid: str
    spec_hash: str
    #: Channels this unit owns: its re-derive's output is cut to these.
    owns: frozenset[str] | None
    #: ``owns`` is a prefix test instead (the dbc unit owns every ``dbc:*``).
    owns_prefix: str | None
    filters: list[list[tuple[str, str, Any]]]
    decoders: DecodeInputs

    def claims(self, channel: str) -> bool:
        if self.owns_prefix is not None:
            return channel.startswith(self.owns_prefix)
        return self.owns is not None and channel in self.owns


class DerivedStore:
    """Derived frames under ``root`` (one directory per session)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def frame_dir(self, session_dir: Path) -> Path:
        """Same-named sessions under different roots get different frames."""
        digest = hashlib.sha1(str(Path(session_dir).resolve()).encode()).hexdigest()[:10]
        return self.root / f"{Path(session_dir).name}-{digest}"

    # -- public -------------------------------------------------------------

    def load(
        self,
        session_dir: Path,
        decoders: DecodeInputs,
        resample_config: ResampleConfig,
        *,
        part_rows: int = SESSION_PART_ROWS,
    ) -> tuple[ReducedSession, DerivedLoad]:
        """The session's reduced frame: read back, refreshed, or built."""
        started = time.monotonic()
        session_dir = Path(session_dir)
        interval_ns = grid_interval_ns(resample_config)
        # P02.10: the cells are timeout-independent, but a frame resampled
        # from them with a shorter broadcast class than the session was
        # stored with would read every quiet id stale between re-writes.
        refuse_shorter_read_timeout(session_dir, resample_config.broadcast_max_gap_seconds)
        frame_dir = self.frame_dir(session_dir)
        meta = _read_meta(frame_dir)
        stale = _whole_frame_staleness(meta, session_dir, interval_ns)
        cells: pd.DataFrame | None = None
        if stale is None:
            assert meta is not None
            cells, stale = _read_cells(frame_dir, meta)
        if stale is not None:
            reduced, meta = self._build(session_dir, decoders, resample_config, part_rows)
            report = DerivedLoad(session_dir.name, "built", stale)
        else:
            assert meta is not None and cells is not None
            units = _expected_units(decoders, _raw_pairs(meta))
            recorded = meta["units"]
            changed = [
                u for u in units.values() if recorded.get(u.uid, {}).get("hash") != u.spec_hash
            ]
            gone = sorted(set(recorded) - set(units))
            if not changed and not gone:
                reduced = _reduced_from(session_dir.name, interval_ns, cells, meta)
                report = DerivedLoad(
                    session_dir.name,
                    "reused",
                    "every input hash matches",
                    reused_channels=len(meta["channels"]),
                )
            else:
                reduced, meta, reused = self._refresh(
                    session_dir, cells, meta, units, changed, gone, resample_config, part_rows
                )
                report = DerivedLoad(
                    session_dir.name,
                    "refreshed",
                    "decode units changed: " + ", ".join(sorted(u.uid for u in changed) + gone),
                    rederived_units=sorted(u.uid for u in changed),
                    reused_channels=reused,
                )
            if report.status == "refreshed":
                _write(frame_dir, reduced, meta)
        reduced.broadcast_stale_timeout_seconds = session_can_storage(
            session_dir
        ).stale_timeout_seconds
        report.seconds = time.monotonic() - started
        log.info(
            "derived session frame %s",
            report.status,
            extra={"session": report.session, "reason": report.reason},
        )
        return reduced, report

    def load_numeric(
        self, session_dir: Path, resample_config: ResampleConfig
    ) -> tuple[ReducedSession, DerivedLoad]:
        """The ``value_number`` chain's reduced frame (``resample_and_segment``:
        digest, fingerprints, classification): read back, or built.

        Versioned as a whole: the chunk list, the grid interval (which is also
        the IMU decimation tick), :data:`DERIVED_CODE_VERSION`,
        :data:`NUMERIC_CODE_VERSION`, the on-read catalog its ``decoded:``
        rows are decoded with (P02.8; an edit to one of those bindings
        rebuilds it, naming the cause) and the session's IMU frame
        (``imu_frame.json``, H02.4).
        """
        started = time.monotonic()
        session_dir = Path(session_dir)
        interval_ns = grid_interval_ns(resample_config)
        refuse_shorter_read_timeout(session_dir, resample_config.broadcast_max_gap_seconds)
        frame_dir = self.frame_dir(session_dir) / _NUMERIC
        meta = _read_meta(frame_dir)
        catalog = on_read_catalog()
        decode_hash = on_read_hash(catalog)
        imu_plan = plan_imu_frame(session_dir)
        stale = _numeric_staleness(meta, session_dir, interval_ns, decode_hash, imu_plan.key())
        cells: pd.DataFrame | None = None
        if stale is None:
            assert meta is not None
            cells, stale = _read_cells(frame_dir, meta)
        if stale is None:
            assert meta is not None and cells is not None
            reduced = _reduced_from(session_dir.name, interval_ns, cells, meta)
            report = DerivedLoad(session_dir.name, "reused", "every input hash matches")
        else:
            refuse_on_the_pi()
            reduced = reduce_numeric_session(
                session_dir, resample_config, on_read=catalog, imu_plan=imu_plan
            )
            meta = {
                "kind": _NUMERIC,
                "code_version": DERIVED_CODE_VERSION,
                "numeric_code_version": NUMERIC_CODE_VERSION,
                "on_read_hash": decode_hash,
                "imu_frame": imu_plan.key(),
                "interval_ns": interval_ns,
                "source": _source_entry(session_dir),
                "timestamp_unit": reduced.timestamp_unit,
                "channels": _channel_meta(reduced, reduced.channels),
            }
            _write(frame_dir, reduced, meta)
            report = DerivedLoad(session_dir.name, "built", stale)
        reduced.broadcast_stale_timeout_seconds = session_can_storage(
            session_dir
        ).stale_timeout_seconds
        report.seconds = time.monotonic() - started
        log.info(
            "derived numeric session frame %s",
            report.status,
            extra={"session": report.session, "reason": report.reason},
        )
        return reduced, report

    def status(
        self, session_dir: Path, decoders: DecodeInputs, resample_config: ResampleConfig
    ) -> str | None:
        """``None`` when a load would reuse the frame as is, else why it would not."""
        session_dir = Path(session_dir)
        frame_dir = self.frame_dir(session_dir)
        meta = _read_meta(frame_dir)
        stale = _whole_frame_staleness(meta, session_dir, grid_interval_ns(resample_config))
        if stale is not None:
            return stale
        assert meta is not None
        stale = _cells_staleness(frame_dir, meta)
        if stale is not None:
            return stale
        units = _expected_units(decoders, _raw_pairs(meta))
        recorded = meta["units"]
        changed = sorted(
            u.uid for u in units.values() if recorded.get(u.uid, {}).get("hash") != u.spec_hash
        )
        gone = sorted(set(recorded) - set(units))
        if changed or gone:
            return "decode units changed: " + ", ".join(changed + gone)
        return None

    # -- building -----------------------------------------------------------

    def _build(
        self,
        session_dir: Path,
        decoders: DecodeInputs,
        resample_config: ResampleConfig,
        part_rows: int,
    ) -> tuple[ReducedSession, dict[str, Any]]:
        refuse_on_the_pi()

        raw_pairs: set[tuple[str, str]] = set()
        reduced = reduce_session(
            session_dir,
            decoders.bindings,
            decoders.dbc,
            decoders.did_map,
            resample_config,
            part_rows=part_rows,
            raw_channels=raw_pairs,
        )
        units = _expected_units(decoders, sorted(raw_pairs))
        owner = _owners(reduced.channels, units.values())
        meta = {
            "code_version": DERIVED_CODE_VERSION,
            "interval_ns": reduced.interval_ns,
            "limits_hash": _limits_hash(),
            "source": _source_entry(session_dir),
            "timestamp_unit": reduced.timestamp_unit,
            "raw_channels": sorted([list(pair) for pair in raw_pairs]),
            "units": {
                u.uid: {
                    "hash": u.spec_hash,
                    "channels": sorted(c for c, o in owner.items() if o == u.uid),
                }
                for u in units.values()
            },
            "channels": _channel_meta(reduced, reduced.channels),
        }
        _write(self.frame_dir(session_dir), reduced, meta)
        return reduced, meta

    def _refresh(
        self,
        session_dir: Path,
        cells: pd.DataFrame,
        meta: dict[str, Any],
        units: Mapping[str, _Unit],
        changed: Sequence[_Unit],
        gone: Sequence[str],
        resample_config: ResampleConfig,
        part_rows: int,
    ) -> tuple[ReducedSession, dict[str, Any], int]:
        refuse_on_the_pi()

        dropped: set[str] = set()
        for uid in [*gone, *(u.uid for u in changed)]:
            dropped.update(meta["units"].get(uid, {}).get("channels", []))
        kept = cells.loc[~cells["channel"].isin(sorted(dropped))]
        reused = int(kept["channel"].nunique())

        fresh = _combined_rederive(changed)
        pieces = [kept]
        new_channels: dict[str, str] = {}
        timestamp_unit = str(meta["timestamp_unit"])
        rederived: ReducedSession | None = None
        if fresh is not None:
            filters, decoders = fresh
            rederived = reduce_session(
                session_dir,
                decoders.bindings,
                decoders.dbc,
                decoders.did_map,
                resample_config,
                part_rows=part_rows,
                filters=filters,
            )
            owner = _owners(rederived.channels, changed, strict=False)
            wanted = sorted(owner)
            pieces.append(rederived.cells.loc[rederived.cells["channel"].isin(wanted)])
            new_channels = owner
            timestamp_unit = _finer(timestamp_unit, rederived.timestamp_unit)

        merged = pd.concat(pieces, ignore_index=True)
        merged = merged.sort_values(["channel", "timestamp_ns"], kind="stable")
        merged = merged.reset_index(drop=True)
        channel_meta = {c: m for c, m in meta["channels"].items() if c not in dropped}
        if rederived is not None:
            channel_meta.update(_channel_meta(rederived, sorted(new_channels)))
        unit_meta = {uid: m for uid, m in meta["units"].items() if uid not in set(gone)}
        for unit in changed:
            unit_meta[unit.uid] = {
                "hash": unit.spec_hash,
                "channels": sorted(c for c, o in new_channels.items() if o == unit.uid),
            }
        meta = {
            **meta,
            "timestamp_unit": timestamp_unit,
            "units": unit_meta,
            "channels": channel_meta,
        }
        return _reduced_from(session_dir.name, int(meta["interval_ns"]), merged, meta), meta, reused


# ---------------------------------------------------------------------------
# decode units
# ---------------------------------------------------------------------------


def _expected_units(decoders: DecodeInputs, raw_pairs: Iterable[Sequence[str]]) -> dict[str, _Unit]:
    """The decode units that produce this session's channels under ``decoders``.

    Mirrors ``session_decode.decode_long_frame``: raw CAN rows never become channels; a
    binding (or DID entry) makes its channel from the CAN (or ``DID_<hex>``)
    rows it reads *and* from any stored rows of that channel; every other
    recorded channel passes through as itself. That chain reads sessions as
    recorded (``decoded="as_recorded"``), so a stored ``decoded`` row of a
    session recorded before P02.8 reaches it and loses to the recomputed row;
    a unit's filter reads those stored rows too, or a refresh would not equal
    a rebuild. A session recorded after P02.8 has none, and the clause reads
    nothing.
    """
    pairs = [(str(source), str(channel)) for source, channel in raw_pairs]
    present = {f"{source}{SOURCE_SEP}{channel}" for source, channel in pairs}
    can_spellings: dict[int, list[str]] = {}
    for source, channel in pairs:
        if source == "can":
            can_id = parse_can_channel(channel)
            if can_id is not None:
                can_spellings.setdefault(can_id, []).append(channel)
    units: dict[str, _Unit] = {}
    claimed: set[str] = {f"can{SOURCE_SEP}{ch}" for s, ch in pairs if s == "can"}

    bindings = decoders.bindings
    if bindings is not None:
        for signal in bindings.signals:
            names = [signal.name, sentinel_channel(signal.name)]
            channels = {f"{DECODED_SOURCE}{SOURCE_SEP}{n}" for n in names}
            spellings = can_spellings.get(signal.can_id, [])
            if not spellings and not channels & present:
                continue
            filters: list[list[tuple[str, str, Any]]] = [
                [("source", "==", DECODED_SOURCE), ("channel", "in", names)]
            ]
            if spellings:
                filters.append([("source", "==", "can"), ("channel", "in", sorted(spellings))])
            uid = f"binding:{signal.name}"
            units[uid] = _Unit(
                uid,
                _hash(signal.model_dump(mode="json")),
                frozenset(channels),
                None,
                filters,
                DecodeInputs(bindings=bindings.model_copy(update={"signals": [signal]})),
            )
            claimed |= channels

    dbc = decoders.dbc
    if dbc is not None:
        dbc_ids = {message.frame_id for message in dbc.messages}
        spellings = sorted(ch for can_id in dbc_ids for ch in can_spellings.get(can_id, []))
        stored = any(source == DBC_SOURCE for source, _ in pairs)
        if spellings or stored:
            filters = [[("source", "==", DBC_SOURCE)]]
            if spellings:
                filters.append([("source", "==", "can"), ("channel", "in", spellings)])
            units["dbc"] = _Unit(
                "dbc",
                _hash(dbc.as_dbc_string()),
                None,
                f"{DBC_SOURCE}{SOURCE_SEP}",
                filters,
                DecodeInputs(dbc=dbc),
            )
            claimed |= {p for p in present if p.startswith(f"{DBC_SOURCE}{SOURCE_SEP}")}

    did_map = decoders.did_map
    if did_map is not None:
        raw_did = [
            channel
            for source, channel in pairs
            if channel.startswith(_DID_PREFIX) and source != "can"
        ]
        # One unit per DID id, which is how the decoder indexes the catalog
        # (``did_entries_by_hex``: last entry per id wins). ``DidMap`` refuses
        # a repeated id or name when loaded; a map built with ``model_copy``
        # skips that validator, so the same refusal is repeated here.
        by_name: dict[str, list[Any]] = {}
        ids: dict[int, str] = {}
        for entry in did_map.dids:
            if entry.did in ids or entry.name in by_name:
                raise ValueError(
                    f"DID catalog declares 0x{entry.did:04X} / {entry.name!r} twice; the "
                    "decoder keeps only the last entry per id, so a per-entry version "
                    "could not match it"
                )
            ids[entry.did] = entry.name
            by_name[entry.name] = [entry]
        for name, entries in sorted(by_name.items()):
            hexes = {f"{entry.did:04X}" for entry in entries}
            spellings = sorted({ch for ch in raw_did if ch[len(_DID_PREFIX) :].upper() in hexes})
            channel = f"{_DID_SOURCE}{SOURCE_SEP}{name}"
            if not spellings and channel not in present:
                continue
            filters = [[("source", "==", _DID_SOURCE), ("channel", "==", name)]]
            if spellings:
                filters.append([("channel", "in", spellings)])
            uid = f"did:{name}"
            units[uid] = _Unit(
                uid,
                _hash([entry.model_dump(mode="json") for entry in entries]),
                frozenset({channel}),
                None,
                filters,
                DecodeInputs(did_map=did_map.model_copy(update={"dids": entries})),
            )
            claimed.add(channel)

    for source, channel in pairs:
        key = f"{source}{SOURCE_SEP}{channel}"
        if key in claimed:
            continue
        uid = f"raw:{key}"
        units[uid] = _Unit(
            uid,
            _hash({"source": source, "channel": channel}),
            frozenset({key}),
            None,
            [[("source", "==", source), ("channel", "==", channel)]],
            DecodeInputs(),
        )
    return units


def _owners(
    channels: Iterable[str], units: Iterable[_Unit], *, strict: bool = True
) -> dict[str, str]:
    """``channel -> unit id``; a channel no unit claims is a bug (strict)."""
    units = list(units)
    out: dict[str, str] = {}
    for channel in channels:
        owners = [u.uid for u in units if u.claims(channel)]
        if len(owners) > 1:
            raise RuntimeError(f"channel {channel!r} is claimed by several decode units: {owners}")
        if owners:
            out[channel] = owners[0]
        elif strict:
            raise RuntimeError(f"channel {channel!r} came from no known decode unit")
    return out


def _combined_rederive(
    changed: Sequence[_Unit],
) -> tuple[list[list[tuple[str, str, Any]]], DecodeInputs] | None:
    """One filtered pass that re-derives every changed unit at once (their
    channels are separable, so decoding them together changes none of them)."""
    if not changed:
        return None
    filters: list[list[tuple[str, str, Any]]] = []
    signals: list[Any] = []
    bindings_doc: Any = None
    dids: list[Any] = []
    did_doc: Any = None
    dbc: Any = None
    for unit in changed:
        filters.extend(unit.filters)
        if unit.decoders.bindings is not None:
            bindings_doc = unit.decoders.bindings
            signals.extend(unit.decoders.bindings.signals)
        if unit.decoders.did_map is not None:
            did_doc = unit.decoders.did_map
            dids.extend(unit.decoders.did_map.dids)
        if unit.decoders.dbc is not None:
            dbc = unit.decoders.dbc
    bindings = bindings_doc.model_copy(update={"signals": signals}) if bindings_doc else None
    did_map = did_doc.model_copy(update={"dids": dids}) if did_doc else None
    return filters, DecodeInputs(bindings=bindings, dbc=dbc, did_map=did_map)


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------


def _whole_frame_staleness(
    meta: Mapping[str, Any] | None, session_dir: Path, interval_ns: int
) -> str | None:
    if meta is None:
        return "no derived frame yet"
    if meta.get("code_version") != DERIVED_CODE_VERSION:
        return f"derived code version {meta.get('code_version')} != {DERIVED_CODE_VERSION}"
    if meta.get("interval_ns") != interval_ns:
        return f"grid interval {meta.get('interval_ns')} ns != {interval_ns} ns"
    if meta.get("limits_hash") != _limits_hash():
        return "quality-gate limits changed"
    if meta.get("source") != _source_entry(session_dir):
        return "the session's chunk files changed"
    return None


def _numeric_staleness(
    meta: Mapping[str, Any] | None,
    session_dir: Path,
    interval_ns: int,
    decode_hash: str,
    imu_key: str,
) -> str | None:
    if meta is None:
        return "no derived frame yet"
    if meta.get("kind") != _NUMERIC:
        return "not a numeric-chain frame"
    if meta.get("code_version") != DERIVED_CODE_VERSION:
        return f"derived code version {meta.get('code_version')} != {DERIVED_CODE_VERSION}"
    if meta.get("numeric_code_version") != NUMERIC_CODE_VERSION:
        return f"numeric code version {meta.get('numeric_code_version')} != {NUMERIC_CODE_VERSION}"
    if meta.get("on_read_hash") != decode_hash:
        return "the on-read decode catalog changed (a confirmed or persist: true binding)"
    if meta.get("imu_frame") != imu_key:
        return "the session's IMU frame (imu_frame.json) changed"
    if meta.get("interval_ns") != interval_ns:
        return f"grid interval {meta.get('interval_ns')} ns != {interval_ns} ns"
    if meta.get("source") != _source_entry(session_dir):
        return "the session's chunk files changed"
    return None


def _source_entry(session_dir: Path) -> dict[str, Any]:
    entry = session_files_fingerprint(session_dir)
    # The chunk list is what matters; the directory's own path is the frame
    # directory's key already.
    return {"files": entry.get("files", []), "pattern": PARQUET_PATTERN}


def _limits_hash() -> str:
    return _hash({name: [limit.min, limit.max] for name, limit in sorted(PHYSICAL_LIMITS.items())})


def _hash(payload: Any) -> str:
    text = payload if isinstance(payload, str) else json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def _raw_pairs(meta: Mapping[str, Any]) -> list[tuple[str, str]]:
    return [(str(s), str(c)) for s, c in meta.get("raw_channels", [])]


def _channel_meta(reduced: ReducedSession, channels: Iterable[str]) -> dict[str, Any]:
    return {
        channel: {
            "non_numeric": channel in reduced.non_numeric,
            "rejects_object": int(reduced.rejects_object.get(channel, 0)),
            "rejects_float": int(reduced.rejects_float.get(channel, 0)),
        }
        for channel in channels
    }


def _reduced_from(
    session: str, interval_ns: int, cells: pd.DataFrame, meta: Mapping[str, Any]
) -> ReducedSession:
    channels = meta["channels"]
    cells = cells[list(CELL_COLUMNS)].copy()
    cells["vtext"] = cells["vtext"].astype(object).where(cells["vtext"].notna(), None)
    return ReducedSession(
        session=session,
        interval_ns=interval_ns,
        cells=cells.reset_index(drop=True),
        non_numeric=frozenset(c for c, m in channels.items() if m["non_numeric"]),
        timestamp_unit=str(meta["timestamp_unit"]),
        rejects_object={c: int(m["rejects_object"]) for c, m in channels.items()},
        rejects_float={c: int(m["rejects_float"]) for c, m in channels.items()},
    )


def _read_meta(frame_dir: Path) -> dict[str, Any] | None:
    try:
        meta = json.loads((frame_dir / _META).read_text())
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def _cells_path(frame_dir: Path, meta: Mapping[str, Any]) -> Path | None:
    """The cells file ``meta`` names, or ``None`` if it names none (or a path
    that is not one of this directory's cells files)."""
    name = meta.get("cells")
    if (
        not isinstance(name, str)
        or Path(name).name != name
        or not name.startswith(_CELLS_PREFIX)
        or not name.endswith(_CELLS_SUFFIX)
    ):
        return None
    return frame_dir / name


def _cells_staleness(frame_dir: Path, meta: Mapping[str, Any]) -> str | None:
    path = _cells_path(frame_dir, meta)
    if path is None:
        return "meta.json names no cells file"
    if not path.exists():
        return _cells_gone(path)
    return None


def _cells_gone(path: Path) -> str:
    return f"cells file {path.name} is gone (superseded by a concurrent write)"


def _read_cells(frame_dir: Path, meta: Mapping[str, Any]) -> tuple[pd.DataFrame | None, str | None]:
    """The cells ``meta`` describes, else why not.

    Reads exactly the file ``meta`` names, so a frame is always one write's
    meta with that same write's cells. A writer deletes the file it
    superseded right after publishing, so a reader that read the old meta
    first can find its file gone; that is a stale frame (rebuilt), never a
    reason to read whatever file is there instead.
    """
    stale = _cells_staleness(frame_dir, meta)
    if stale is not None:
        return None, stale
    path = _cells_path(frame_dir, meta)
    assert path is not None
    try:
        return pd.read_parquet(path), None
    except FileNotFoundError:
        return None, _cells_gone(path)


def _write(frame_dir: Path, reduced: ReducedSession, meta: Mapping[str, Any]) -> None:
    """Publish ``reduced`` and ``meta`` together, with one rename.

    The cells go to a file named for this write, and ``meta.json``, which names
    that file, is renamed into place last. Version 1 gave cells and meta fixed
    names and a rename each, so two writers with different catalogs could
    leave one's meta over the other's cells, and the next load served the mix
    as "every input hash matches" (P01 verification, 2026-09-26). Now a meta
    can only describe the cells it names, and a crash at any point leaves the
    previous pair intact.

    Other cells files are deleted after the publish: the one the old meta
    named, and any a crashed or racing writer left unpublished. A racing
    writer whose file this removes publishes a meta naming a missing file,
    which the next load rebuilds (:func:`_read_cells`). That costs a rebuild,
    never a wrong frame.
    """
    refuse_on_the_pi()
    frame_dir.mkdir(parents=True, exist_ok=True)
    cells = reduced.cells.copy()
    cells["vtext"] = cells["vtext"].astype("string")
    # Unique per writer: two processes deriving the same session (a trainer
    # pool and an evaluation run) never share a file, temporary or published.
    token = f"{os.getpid()}-{uuid.uuid4().hex}"
    name = f"{_CELLS_PREFIX}{token}{_CELLS_SUFFIX}"
    # Written under a dot name first, so the sweep below never deletes a file
    # another writer is still writing.
    tmp_cells = frame_dir / f".{name}.tmp"
    cells.to_parquet(tmp_cells, index=False)
    os.replace(tmp_cells, frame_dir / name)
    tmp_meta = frame_dir / f".{_META}.{token}.tmp"
    tmp_meta.write_text(json.dumps({**meta, "cells": name}, indent=1, sort_keys=True))
    os.replace(tmp_meta, frame_dir / _META)
    for old in frame_dir.glob(f"{_CELLS_PREFIX}*{_CELLS_SUFFIX}"):
        if old.name != name:
            old.unlink(missing_ok=True)
    (frame_dir / _LEGACY_CELLS).unlink(missing_ok=True)


_UNIT_ORDER = {"s": 0, "ms": 1, "us": 2, "ns": 3}


def _finer(a: str, b: str) -> str:
    return a if _UNIT_ORDER.get(a, 3) >= _UNIT_ORDER.get(b, 3) else b
