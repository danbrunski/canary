"""One reader over the three storage conventions a session uses.

The same physical quantity is stored three different ways, and each one bit at
least once during the 2026-08 tuning campaign:

* ``obd:`` — ``value_number``, already in engineering units.
* ``did:`` — the Mode-22 response payload as **hex text in ``value_text``**,
  with ``value_number`` empty. Analysis that reached for ``value_number``
  silently got nothing.
* ``decoded:`` — ``value_number`` holding the **raw integer**, deliberately
  unscaled so training's scaler stays comparable across sessions (see
  :mod:`src.decode.frame_decoder`). ``decoded:ECM_RPM`` reads ~9700 at
  1200 rpm; taking it at face value looks like a factor-of-8 bug and was
  first diagnosed as one.
* ``can:`` — raw frame bytes in ``payload``, needing a binding to slice.
* ``uprev:`` — imported UpRev K-line logs (``tools/import_uprev_log.py``),
  stored numeric and already in engineering units like ``obd:``. Kept as its
  own namespace because it is a *different reader and conversion* (another
  logger, another clock), not necessarily a different sensor: UpRev's AFR and
  the ECM's own λ (PID 0x24) are one A/F sensor converted two ways -- UpRev
  skips the ECM's barometric gain (B06.7) -- and their 3.8 % disagreement is
  exactly what must not be blurred by filing both under one name.
* ``mem:`` — service-0x23 (ReadMemoryByAddress) channels, numeric and
  already in engineering units. Its own namespace rather than ``obd:``
  because the trust differs: a DID number is a contract the ECU keeps across
  calibrations, while a RAM address is an artifact of one ROM build, and a
  reader has to be able to tell which kind of channel it is holding. The
  companion ``mem:<NAME>_SENTINEL`` channel carries the declared
  no-measurement states (fuel cut, for the commanded-phi word) as a sparse
  indicator: a row exists exactly where the ECU reported one, so "commanding
  nothing" is a value you can pair against rather than a gap in the data.
* ``gps:`` — the GPS receiver's own fields (``speed``, ``heading``,
  ``altitude``, …), numeric and in engineering units. Also a separate
  instrument, and the only road-speed reference in a Mode 22-only tuning
  capture, where no Mode 01 PID is being polled at all.

This module is the single place that knows all four, so callers ask for
``obd:RPM`` or ``did:ECM_AF_SENSOR_V_B1`` and get numbers they can compare.

Scaling always routes through :func:`src.decode.bindings.apply_scaling` and
:func:`src.decode.dids.apply_scaling` rather than re-implementing the
arithmetic — an unpinned ``scale: null`` binding must keep yielding raw
counts with ``unit="counts"``, not a silently invented engineering value.

See ``docs/roadmap/archive/tuning-analysis-toolkit.md`` Phase 1.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, get_args

import numpy as np
import pandas as pd

from src.collection.kline.names import is_legacy
from src.decode import bindings as bindings_decode
from src.decode import dids as dids_decode
from src.decode.on_read import can_channel_spellings
from src.paths import BINDINGS_YAML, DIDS_YAML
from src.preprocessing.loader import DecodedRows, filter_rows, read_session
from src.signals.afr import AfrCalibration
from src.storage.broadcast_storage import (
    FRAME_ATTR,
    ChangeOnlyStorageError,
    change_only_refusal,
    change_only_timeout,
)
from src.storage.capture_manifest import read_capture_manifest
from src.telemetry import DECODED_SOURCE, SOURCE_SEP

#: Namespaces a spec may name. ``did``/``can`` are decode routes rather than
#: literal ``source`` column values — a DID reading is stored with
#: ``source="obd"`` and channel ``DID_1225``.
SpecSource = Literal[
    "obd", "did", "decoded", "can", "uprev", "gps", "imu", "mem", "sensor", "kline"
]
#: The same names as a tuple, for validation and messages. Derived from the
#: Literal so the two cannot drift (they had: ``imu`` was a captured source
#: for months before a spec could name it).
SPEC_SOURCES: tuple[SpecSource, ...] = get_args(SpecSource)

_RAW_UNIT = "counts"


class ChannelSpecError(ValueError):
    """A spec that cannot be resolved against the loaded catalogs."""


@dataclass(frozen=True)
class ChannelSpec:
    """A parsed ``source:NAME`` request."""

    source: SpecSource
    name: str
    text: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.text


def parse_spec(text: str) -> ChannelSpec:
    """Parse ``"obd:RPM"`` / ``"did:0x1225"`` / ``"can:ECM_RPM"``.

    A bare name with no namespace is read as ``obd:`` — that is the namespace
    the rest of the codebase already defaults to (``channel_satisfied`` in
    capture_profiles does the same), so the shorthand does not surprise.
    """
    raw = text.strip()
    if not raw:
        raise ChannelSpecError("empty channel spec")
    source, sep, name = raw.partition(":")
    if not sep:
        source, name = "obd", raw
    source = source.strip().lower()
    name = name.strip()
    if not name:
        raise ChannelSpecError(f"channel spec {text!r} has no channel name")
    if source not in SPEC_SOURCES:
        raise ChannelSpecError(
            f"unknown namespace {source!r} in {text!r}; expected {'/'.join(SPEC_SOURCES)}"
        )
    return ChannelSpec(source=source, name=name, text=raw)


@dataclass
class _Catalogs:
    """Lazily-loaded binding/DID catalogs, indexed for lookup by spec."""

    bindings_path: Path
    dids_path: Path
    _bindings: bindings_decode.Bindings | None = None
    _did_map: dids_decode.DidMap | None = None

    @property
    def bindings(self) -> bindings_decode.Bindings:
        if self._bindings is None:
            if not self.bindings_path.exists():
                raise ChannelSpecError(f"bindings file not found: {self.bindings_path}")
            self._bindings = bindings_decode.load_bindings(self.bindings_path)
        return self._bindings

    @property
    def did_map(self) -> dids_decode.DidMap:
        if self._did_map is None:
            if not self.dids_path.exists():
                raise ChannelSpecError(f"DID file not found: {self.dids_path}")
            self._did_map = dids_decode.load_did_map(self.dids_path)
        return self._did_map

    def signal(self, name: str) -> bindings_decode.Signal:
        wanted = name.upper()
        for signal in self.bindings.signals:
            if signal.name.upper() == wanted:
                return signal
        raise ChannelSpecError(f"no binding named {name!r} in {self.bindings_path}")

    def did(self, name: str) -> dids_decode.DidEntry:
        """Resolve a DID by name (``ECM_AF_SENSOR_V_B1``) or id (``0x1225``)."""
        wanted = name.upper().removeprefix("DID_")
        for entry in self.did_map.dids:
            if entry.name.upper() == wanted:
                return entry
        try:
            did_id = int(wanted, 16)
        except ValueError as exc:
            raise ChannelSpecError(f"no DID named {name!r} in {self.dids_path}") from exc
        for entry in self.did_map.dids:
            if entry.did == did_id:
                return entry
        raise ChannelSpecError(f"no DID 0x{did_id:04X} in {self.dids_path}")


#: Column added by :func:`read_channels` when ``acquired_time=True``; values
#: are :data:`src.signals.pairing.TimestampBasis`.
TIMESTAMP_BASIS_COLUMN = "timestamp_basis"
#: Round identity columns :func:`read_acquisition_frame` adds from
#: ``metadata_json`` (``docs/capture-rounds.md``). Nullable: legacy rows and
#: broadcast frames carry neither.
GENERATION_COLUMN = "generation"
ROUND_ID_COLUMN = "round_id"
ACQUIRED_AT_COLUMN = "acquired_at"
#: Per-round skew columns from :func:`round_skew`.
ROUND_SKEW_COLUMNS = ("generation", "round_id", "n", "first_acquired_at", "skew_s")
#: ``DataFrame.attrs`` key on :func:`read_channels`' output (P02.10): spec ->
#: seconds its rows stay valid after they were stored, for every broadcast spec
#: (``decoded:``/``can:``) read from a session that stores CAN frames
#: change-only. There a row is stored only when the frame's payload changes,
#: and an unchanged id is re-written before this many seconds, so the value at
#: an instant is the last stored row before it, valid this long. Absent (or
#: empty) for a session stored every frame, whose rows are read as they were.
#: :func:`align_long` and :func:`src.analysis.compare.align_pairs` read it, so
#: callers never apply the rule themselves.
AS_OF_ATTR = "as_of_valid_seconds"
#: Spec namespaces decoded from broadcast frames.
_BROADCAST_SPEC_SOURCES = frozenset({"decoded", "can"})


#: The ``metadata_json`` keys this module reads. Parsed together, once.
_ROUND_IDENTITY_KEYS = ("acquired_at", "round_id", "generation")


def _metadata_payload(raw: object) -> dict[str, object] | None:
    """One row's ``metadata_json`` as a mapping, or ``None``.

    Every "no usable object here" case collapses to ``None``: a missing value
    (``None``/``NaN``), a non-string, text that is not JSON, and JSON that is
    not an object (``"3"``, ``"[1]"``, ``"null"`` — those used to arrive as an
    ``AttributeError`` from ``dict.get`` and are still not readings).
    """
    if not isinstance(raw, str):
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _payload_number(payload: dict[str, object], key: str) -> float | None:
    """``payload[key]`` as a float, or ``None`` unless it is a real number.

    ``bool`` is rejected deliberately: it is an ``int`` subclass, so a stray
    ``"acquired_at": true`` would otherwise read as 1.0 — 1970, and a plausible
    enough number to survive.
    """
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


@dataclass(frozen=True)
class _MetadataFields:
    """``metadata_json``'s round identity as columns, parsed once per row.

    Six ``Series.map`` passes used to re-run ``json.loads`` over the whole
    column — 21.1 M parses for a 3.5 M-row capture, 115 s of a 122 s
    ``recompute_capture_quality`` (measured 2026-09-17). One parse fills all
    three columns and every consumer reads them from here.

    Float, not ``Int64``, because that is what the JSON gave: the public
    ``generation``/``round_id`` columns do the integer cast themselves.
    """

    acquired_at: pd.Series
    round_id: pd.Series
    generation: pd.Series


def _parse_candidates(raw: pd.Series) -> np.ndarray:
    """Row positions whose text could possibly carry a round-identity key.

    A JSON object can name ``acquired_at`` without containing that literal
    substring only by escaping a character (``{"acquired_a\\u0074": 1}``), and
    every JSON escape starts with a backslash. So "no key substring **and** no
    backslash" is a proof the keys are absent rather than a guess about how the
    writer formats things, and anything that fails the proof — including
    non-strings and unparseable text — is handed to the parser as before.

    This is the whole of the win: 94 % of a capture's rows are broadcast and
    decoded frames whose metadata is ``{"can_id": …, "dlc": …}``, and parsing
    them yielded nothing three times over.
    """
    text = raw.astype("string")
    candidate = text.str.contains("\\", regex=False)
    for key in _ROUND_IDENTITY_KEYS:
        candidate |= text.str.contains(key, regex=False)
    return candidate.fillna(True).to_numpy(dtype=bool)


def _metadata_fields(frame: pd.DataFrame) -> _MetadataFields | None:
    """Parse ``metadata_json`` once, into one column per round-identity key.

    ``None`` when the frame carries no ``metadata_json`` column at all — the
    single place that fact is decided, so "unknown" timestamp basis and
    all-null identity columns cannot disagree about it.
    """
    if "metadata_json" not in frame:
        return None
    raw = frame["metadata_json"]
    values = raw.to_numpy()
    columns = {key: np.full(len(values), np.nan) for key in _ROUND_IDENTITY_KEYS}
    for position in np.flatnonzero(_parse_candidates(raw)):
        payload = _metadata_payload(values[position])
        if payload is None:
            continue
        for key, column in columns.items():
            number = _payload_number(payload, key)
            if number is not None:
                column[position] = number
    return _MetadataFields(
        acquired_at=pd.Series(columns["acquired_at"], index=frame.index, dtype="float64"),
        round_id=pd.Series(columns["round_id"], index=frame.index, dtype="float64"),
        generation=pd.Series(columns["generation"], index=frame.index, dtype="float64"),
    )


def _round_stamped_sessions(frame: pd.DataFrame, session: Path | pd.DataFrame) -> set[str]:
    """Session paths whose capture manifest says OBD rows carry round stamps.

    Only manifest evidence counts. A stamp shared by several OBD channels is
    *not* evidence on its own: a batched Mode 01 response legitimately stamps
    every PID it carried with the one decode instant, and that is
    indistinguishable from a round stamp without ``acquired_at``.
    """
    candidates: set[str] = set()
    if isinstance(session, Path):
        candidates.add(str(session))
    if "session_path" in frame:
        candidates.update(str(path) for path in frame["session_path"].dropna().unique())
    round_stamped: set[str] = set()
    for path in candidates:
        manifest = read_capture_manifest(Path(path))
        if manifest is not None and bool(manifest.profile_snapshot.get("round_timestamps")):
            round_stamped.add(path)
    return round_stamped


def _timestamp_basis(
    frame: pd.DataFrame, session: Path | pd.DataFrame, fields: _MetadataFields | None
) -> pd.Series:
    """Per-row :data:`~src.signals.pairing.TimestampBasis`.

    ``acquired`` whenever ``acquired_at`` is present, whatever else the row
    or manifest says. ``round`` needs either the manifest's
    ``round_timestamps`` or a ``round_id`` on the row itself (C02.6) — with
    no ``acquired_at`` beside it, a round-tagged stamp is a round stamp.

    ``fields`` is :func:`_metadata_fields`' already-parsed columns; ``None``
    means the frame has no ``metadata_json`` to judge by.
    """
    if fields is None:
        return pd.Series("unknown", index=frame.index, dtype="string")
    acquired = fields.acquired_at.notna()
    basis = pd.Series("stored", index=frame.index, dtype="string")
    basis = basis.mask(fields.round_id.notna(), "round")
    round_stamped = _round_stamped_sessions(frame, session)
    if round_stamped:
        # Only polled (OBD/DID) rows are ever round-stamped; broadcast frames
        # keep their arrival stamp whatever the profile says.
        in_round_session = (
            frame["session_path"].astype("string").isin(round_stamped)
            if "session_path" in frame
            else pd.Series(True, index=frame.index)
        )
        basis = basis.mask((frame["source"] == "obd") & in_round_session, "round")
    return basis.mask(acquired, "acquired")


def _long_frame(
    session: Path | pd.DataFrame,
    *,
    acquired_time: bool = False,
    filters: Any = None,
    columns: Sequence[str] | None = None,
    narrow: _Narrowing | None = None,
) -> pd.DataFrame:
    """The stored rows a reader takes its specs from, on the clock it asked for.

    ``narrow`` (P02.19) keeps only the rows its specs' readers can select
    *before* the acquisition clock is put on them. The clock is row-local, so
    the kept rows are exactly what the whole frame would give them, and a
    call costs the rows it reads: a caller that hands one frame to
    :func:`read_channels` spec by spec parses each spec's ``metadata_json``,
    never the whole frame's once per spec.
    """
    shared = _SHARED_READ.get()
    if shared is not None and shared.serves(session, filters, columns):
        # Already parsed once for the whole shared read.
        return shared.rows(filters, acquired_time=acquired_time)
    if isinstance(session, pd.DataFrame):
        frame = session if narrow is None else narrow(session)
        if frame is session:
            # Never the caller's frame itself: the clock rewrites ``timestamp``.
            frame = session.copy()
    else:
        frame = read_session(Path(session), filters=filters, columns=columns)
        if narrow is not None:
            frame = narrow(frame)
    if not acquired_time:
        return frame
    return _on_acquisition_clock(frame, session)[0]


#: Keeps a stored frame's rows that some spec's reader can select (P02.19).
_Narrowing = Callable[[pd.DataFrame], pd.DataFrame]


class _RowKeys:
    """A frame's ``source`` and ``channel`` for :func:`_spec_rows`, with the
    case-folded and upper-cased channel built once, on first use, rather than
    once per spec."""

    def __init__(self, frame: pd.DataFrame) -> None:
        self.source = frame["source"]
        self.channel = frame["channel"].astype("string")
        self._folded: pd.Series | None = None
        self._upper: pd.Series | None = None

    @property
    def folded(self) -> pd.Series:
        if self._folded is None:
            self._folded = self.channel.str.casefold()
        return self._folded

    @property
    def upper(self) -> pd.Series:
        if self._upper is None:
            self._upper = self.channel.str.upper()
        return self._upper


def _spec_rows(keys: _RowKeys, spec: ChannelSpec, catalogs: _Catalogs) -> pd.Series:
    """Every row that ``spec``'s reader can select (P02.19).

    Never fewer than the reader's own selection, compared the way the reader
    compares it, so a frame narrowed to these rows reads exactly the same:
    ``_numeric_series``/``_kline_series`` casefold the channel, ``_can_series``
    upper-cases it, and ``_did_series`` takes the hex name from any source (it
    must see a K-line module's rows to say that is the only kind it found).
    ``tests/test_channels_raw_read.py`` holds every source kind to that.
    """
    source, channel = keys.source, keys.channel
    if spec.source == "obd" and spec.name.upper() == "COMMANDED_AFR":
        keep = (source == "obd") & (channel == _EQUIV_RATIO_CHANNEL)
    elif spec.source == "mem":
        keep = (source == "obd") & (channel == f"mem:{spec.name}")
    elif spec.source == "did":
        keep = channel == f"DID_{catalogs.did(spec.name).did:04X}"
    elif spec.source == "decoded":
        keep = (source == DECODED_SOURCE) & (channel == catalogs.signal(spec.name).name)
    elif spec.source == "can":
        keep = (source == "can") & (
            keys.upper == f"0x{catalogs.signal(spec.name).can_id:03X}".upper()
        )
    elif spec.source in _CASE_FOLDED_SOURCES:
        keep = (source == spec.source) & (keys.folded == spec.name.casefold())
    else:  # pragma: no cover - parse_spec admits no other source
        raise ChannelSpecError(f"no row selection for {spec.text!r}")
    return keep.fillna(False).astype(bool)


def _narrowing(parsed: Sequence[ChannelSpec], catalogs: _Catalogs) -> _Narrowing:
    """:data:`_Narrowing` for ``parsed``: the union of their :func:`_spec_rows`,
    in the frame's order and index, with its ``attrs`` (the change-only
    storage mode :func:`_as_of_validity` reads). The frame itself when every
    row is kept, so a read that is already exactly its specs' rows is not
    copied again."""

    def narrow(frame: pd.DataFrame) -> pd.DataFrame:
        keys = _RowKeys(frame)
        keep = np.zeros(len(frame), dtype=bool)
        for spec in parsed:
            keep |= _spec_rows(keys, spec, catalogs).to_numpy(dtype=bool)
        if keep.all():
            return frame
        out = frame.loc[keep].copy()
        out.attrs = dict(frame.attrs)
        return out

    return narrow


#: The stored columns every :func:`read_channels` reader takes its rows from
#: (P02.2): the sort key, the three value fields a reader selects on, and the
#: unit. ``payload`` is added for a ``can:`` spec (the loader reads it for a
#: ``decoded:`` one itself) and ``metadata_json`` for the acquisition clock.
_SPEC_COLUMNS = ("timestamp", "source", "channel", "value_number", "value_text", "unit")


def _read_columns(parsed: Sequence[ChannelSpec], *, acquired_time: bool) -> list[str]:
    """The stored columns ``parsed`` read: what the readers below use, and no
    more (``payload`` is a fifth of a session's bytes, and only ``can:`` reads
    it)."""
    columns = list(_SPEC_COLUMNS)
    if any(spec.source == "can" for spec in parsed):
        columns.append("payload")
    if acquired_time:
        columns.append("metadata_json")
    return columns


#: Sources whose readers match the channel case-insensitively (see
#: :func:`_numeric_series`): a parquet filter cannot, so these narrow by source.
_CASE_FOLDED_SOURCES = frozenset({"obd", "uprev", "gps", "imu", "sensor", "kline"})


def _raw_read_filters(parsed: Sequence[ChannelSpec], catalogs: _Catalogs) -> list[list[Any]]:
    """The parquet rows ``parsed`` can read, as a pyarrow DNF (P02.2).

    Each spec maps to the raw rows its reader selects, never to fewer, so a
    filtered read returns exactly what the whole session would. The readers
    still select exactly afterwards. ``decoded:`` asks for its own rows, which
    ``read_session`` decodes from just their frames (P02.8). ``can:`` asks for
    its binding's id in every spelling a recorder era used. A DID reads its
    hex name from any source, so the reader can still say when the only rows
    were some K-line module's.
    """
    terms: list[list[Any]] = []
    for spec in parsed:
        if spec.source == "obd" and spec.name.upper() == "COMMANDED_AFR":
            terms.append([("source", "==", "obd"), ("channel", "==", _EQUIV_RATIO_CHANNEL)])
        elif spec.source == "mem":
            terms.append([("source", "==", "obd"), ("channel", "==", f"mem:{spec.name}")])
        elif spec.source == "did":
            terms.append([("channel", "==", f"DID_{catalogs.did(spec.name).did:04X}")])
        elif spec.source == "decoded":
            name = catalogs.signal(spec.name).name
            terms.append([("source", "==", DECODED_SOURCE), ("channel", "==", name)])
        elif spec.source == "can":
            spellings = can_channel_spellings([catalogs.signal(spec.name).can_id])
            terms.append([("source", "==", "can"), ("channel", "in", spellings)])
        elif spec.source in _CASE_FOLDED_SOURCES:
            terms.append([("source", "==", spec.source)])
        else:  # pragma: no cover - parse_spec admits no other source
            raise ChannelSpecError(f"no raw-read filter for {spec.text!r}")
    unique: list[list[Any]] = []
    for term in terms:
        if term not in unique:
            unique.append(term)
    return unique


def raw_read_filters(
    specs: Sequence[str],
    *,
    bindings_path: Path = BINDINGS_YAML,
    dids_path: Path = DIDS_YAML,
) -> list[list[Any]]:
    """:func:`_raw_read_filters` for a caller that reads a session once and
    hands the frame to :func:`read_channels` spec by spec.

    A spec that does not resolve contributes no rows: its own
    :func:`read_channels` call then refuses it exactly as an unfiltered read
    would, so one bad name cannot fail the shared read.
    """
    catalogs = _Catalogs(bindings_path=bindings_path, dids_path=dids_path)
    terms: list[list[Any]] = []
    for text in specs:
        try:
            parsed = parse_spec(text)
            spec_terms = _raw_read_filters([parsed], catalogs)
        except (ChannelSpecError, KeyError, ValueError):
            continue
        terms.extend(term for term in spec_terms if term not in terms)
    return terms


def channel_read_columns(specs: Iterable[str] = (), *, acquired_time: bool) -> list[str]:
    """The stored columns :func:`read_channels` reads for ``specs`` (P02.2):
    for a caller that reads a session itself and hands the frame to
    :func:`read_channels` or a pairing built on it. Without ``specs`` it is the
    set for any spec but ``can:``, which also needs ``payload``."""
    return _read_columns(_parsed_specs(specs), acquired_time=acquired_time)


def _parsed_specs(specs: Iterable[str]) -> list[ChannelSpec]:
    """The specs of ``specs`` that parse; a bad one is left to its own read."""
    parsed: list[ChannelSpec] = []
    for text in specs:
        try:
            parsed.append(parse_spec(text))
        except ChannelSpecError:
            continue
    return parsed


def read_spec_rows(
    session: Path,
    specs: Sequence[str],
    *,
    acquired_time: bool,
    bindings_path: Path = BINDINGS_YAML,
    dids_path: Path = DIDS_YAML,
) -> pd.DataFrame:
    """The session's raw rows that ``specs`` read, read once, for a caller
    that then hands the frame to :func:`read_channels` spec by spec.

    Only the rows (:func:`raw_read_filters`) and the stored columns those
    specs' readers use with ``acquired_time`` are read (P02.2). Inside
    :func:`shared_session_read` the rows come from its one read. Specs that
    resolve to no rows at all read the whole session, as an unfiltered read
    would: each one's own :func:`read_channels` call then refuses it.
    """
    filters = raw_read_filters(specs, bindings_path=bindings_path, dids_path=dids_path) or None
    columns = _read_columns(_parsed_specs(specs), acquired_time=acquired_time)
    shared = _SHARED_READ.get()
    if shared is not None and shared.serves(session, filters, columns):
        return shared.rows(filters, acquired_time=False)
    return read_session(session, filters=filters, columns=columns)


def _predicate_key(predicate: Sequence[Any]) -> tuple[Any, ...]:
    column, op, operand = predicate
    if isinstance(operand, (list, tuple, set, frozenset)):
        operand = (
            tuple(sorted(operand)) if isinstance(operand, (set, frozenset)) else tuple(operand)
        )
    return (column, op, operand)


def _covers(shared: Sequence[Sequence[Any]], filters: Any) -> bool:
    """Is every row ``filters`` passes in a read with ``shared``'s filters?

    Decided syntactically, so it can say no needlessly but never yes wrongly:
    each of ``filters``' conjunctions must contain every predicate of one of
    ``shared``'s. Only ``source`` and ``channel`` predicates are served, the
    only columns :func:`raw_read_filters` filters on (a ``timestamp`` one would
    be judged against the acquisition clock of a shared acquired frame).
    """
    if filters is None:
        return False
    conjunctions = (
        [list(c) for c in filters] if filters and isinstance(filters[0], list) else [list(filters)]
    )
    held = [{_predicate_key(p) for p in conjunction} for conjunction in shared]
    for conjunction in conjunctions:
        if any(predicate[0] not in ("source", "channel") for predicate in conjunction):
            return False
        keys = {_predicate_key(p) for p in conjunction}
        if not any(conj <= keys for conj in held):
            return False
    return True


class SharedSessionRead:
    """One session's rows for every :func:`read_channels` call that asks for
    a subset of them (P02.2), read once on first use.

    Each served call gets exactly the rows its own filtered read would have
    returned (``loader.filter_rows`` over this read's rows, which contain
    them), on the stored or the acquisition clock. A call this read cannot
    serve -- another session, rows or columns it does not hold, a frame
    rather than a path -- reads the disk as it always did, so a spec missed
    from the shared set costs a read, never a row. ``disk_reads`` and
    ``served`` count what happened, so a caller can prove the one read.
    """

    def __init__(self, session_dir: Path, filters: list[list[Any]], columns: Sequence[str]) -> None:
        self.session_dir = session_dir
        self.filters = filters
        self.columns = tuple(columns)
        self.disk_reads = 0
        self.served = 0
        self._stored: pd.DataFrame | None = None
        self._acquired: pd.DataFrame | None = None

    def serves(
        self, session: Path | pd.DataFrame, filters: Any, columns: Sequence[str] | None
    ) -> bool:
        if not isinstance(session, Path) or Path(session) != self.session_dir:
            return False
        if columns is None or not set(columns) <= set(self.columns):
            return False
        return bool(self.filters) and _covers(self.filters, filters)

    def rows(self, filters: Any, *, acquired_time: bool) -> pd.DataFrame:
        if self._stored is None:
            self._stored = read_session(
                self.session_dir, filters=self.filters, columns=self.columns
            )
            self.disk_reads += 1
        frame = self._stored
        if acquired_time:
            if self._acquired is None:
                self._acquired = _on_acquisition_clock(self._stored.copy(), self.session_dir)[0]
            frame = self._acquired
        self.served += 1
        return filter_rows(frame, filters)


_SHARED_READ: ContextVar[SharedSessionRead | None] = ContextVar("_SHARED_READ", default=None)


@contextmanager
def shared_session_read(
    session_dir: Path,
    specs: Iterable[str],
    *,
    bindings_path: Path = BINDINGS_YAML,
    dids_path: Path = DIDS_YAML,
) -> Iterator[SharedSessionRead]:
    """Serve every :func:`read_channels` of ``session_dir`` inside the block
    whose rows ``specs`` cover from one read (P02.2).

    For a report that asks a session for many channels in many calls (the
    tuning digest): each call used to re-open every chunk. The read holds the
    rows of ``specs`` (:func:`raw_read_filters`) and the columns their readers
    use on either clock, so it costs what one call for all of them would.
    Inside an outer block that already holds those rows, the outer read is
    used.
    """
    wanted = list(specs)
    filters = raw_read_filters(wanted, bindings_path=bindings_path, dids_path=dids_path)
    columns = _read_columns(_parsed_specs(wanted), acquired_time=True)
    outer = _SHARED_READ.get()
    if outer is not None and all(outer.serves(session_dir, [term], columns) for term in filters):
        # Nested inside a read that already holds these rows (the tuning
        # frame inside the digest): keep using it rather than read again.
        yield outer
        return
    shared = SharedSessionRead(session_dir, filters, columns)
    token = _SHARED_READ.set(shared)
    try:
        yield shared
    finally:
        _SHARED_READ.reset(token)


def _long_frame_with_metadata(
    session: Path | pd.DataFrame,
    *,
    acquired_time: bool = False,
    decoded: DecodedRows = "on_read",
    filters: Any = None,
    columns: Sequence[str] | None = None,
) -> tuple[pd.DataFrame, _MetadataFields | None]:
    """A session (path or frame) as a long frame on the clock asked for, plus
    the metadata columns it had to parse.

    Handed back rather than recomputed so :func:`read_acquisition_frame` reuses
    the one parse. ``None`` covers both "nothing was parsed" cases: without
    ``acquired_time`` no metadata is wanted (and no caller should pay the parse
    for it), and a frame with no ``metadata_json`` column has none to parse.

    A session directory is read with its ``decoded:`` rows decoded on read
    (P02.8, ``loader.read_session``), so ``decoded:`` specs read the same rows
    from a session recorded before 2026-09-26 and from one recorded after,
    which stores none. ``decoded`` is the loader's mode, and ``columns`` the
    stored columns to read (``loader.read_session``).
    """
    frame = (
        session.copy()
        if isinstance(session, pd.DataFrame)
        else read_session(Path(session), decoded=decoded, filters=filters, columns=columns)
    )
    if not acquired_time:
        return frame, None
    return _on_acquisition_clock(frame, session)


def _on_acquisition_clock(
    frame: pd.DataFrame, session: Path | pd.DataFrame
) -> tuple[pd.DataFrame, _MetadataFields | None]:
    """``frame`` (modified in place) on the acquisition clock, with its
    ``timestamp_basis``, and the metadata columns parsed for it. Row by row:
    a row's clock and basis depend on that row and its session alone, so a
    subset of the result is the result for that subset."""
    fields = _metadata_fields(frame)
    frame[TIMESTAMP_BASIS_COLUMN] = _timestamp_basis(frame, session, fields)
    if fields is None:
        return frame, None
    acquired = pd.to_datetime(fields.acquired_at, unit="s", utc=True)
    # Broadcast and legacy rows have no acquisition field; retain their stored
    # timestamp rather than dropping an otherwise valid cross-source series.
    # One unit whatever rows are present (P02.2): ``fillna`` took the stored
    # column's unit (µs) when no row had an acquisition stamp and ns when one
    # did, so a narrower read of the same session changed the dtype.
    frame["timestamp"] = acquired.fillna(pd.to_datetime(frame["timestamp"], utc=True)).astype(
        "datetime64[ns, UTC]"
    )
    return frame, fields


def read_acquisition_frame(
    session: Path | pd.DataFrame,
    *,
    decoded: DecodedRows = "on_read",
    filters: Any = None,
    columns: Sequence[str] | None = None,
) -> pd.DataFrame:
    """The raw session on its acquisition clock, with ``timestamp_basis`` per row.

    For consumers that decode rows themselves (the broadcast byte sweep) but
    must still pair on the same clock, and with the same provenance rules, as
    :func:`read_channels`. Also carries ``acquired_at`` (float Unix seconds),
    ``generation`` and ``round_id`` (nullable) so round membership is a column
    rather than a JSON parse; :func:`round_skew` summarises them.
    ``decoded="as_recorded"`` is for an account of what is on disk (storage
    accounting), which the decode on read must not add to.

    ``filters`` (pyarrow DNF) and ``columns`` (stored column names) narrow a
    *session directory's* read at the parquet reader, as
    :func:`src.preprocessing.session_reader.read_session` takes them: a caller
    that scores one source reads only those rows and columns, never a whole
    drive. The clock is row-local, so a narrowed read's rows are exactly the
    whole read's rows for them. Neither means anything for a frame already in
    memory, so a frame given with either is refused rather than ignored.
    """
    if isinstance(session, pd.DataFrame) and (filters is not None or columns is not None):
        raise ValueError(
            "filters and columns narrow a session directory's read; a frame is already read"
        )
    frame, fields = _long_frame_with_metadata(
        session, acquired_time=True, decoded=decoded, filters=filters, columns=columns
    )
    if fields is None:
        for column in (ACQUIRED_AT_COLUMN, GENERATION_COLUMN, ROUND_ID_COLUMN):
            frame[column] = pd.Series(pd.NA, index=frame.index, dtype="Float64")
        return frame
    frame[ACQUIRED_AT_COLUMN] = fields.acquired_at.astype("Float64")
    frame[GENERATION_COLUMN] = fields.generation.astype("Float64").astype("Int64")
    frame[ROUND_ID_COLUMN] = fields.round_id.astype("Float64").astype("Int64")
    return frame


def round_skew(frame: pd.DataFrame) -> pd.DataFrame:
    """Measured acquisition spread per ``(generation, round_id)``.

    One row per round that has at least one round-tagged reading with an
    ``acquired_at``: ``n`` readings, ``first_acquired_at``, and ``skew_s`` =
    last minus first acquisition. This is the evidence a tuple check must
    consult (C03) — sharing a ``round_id`` says the scheduler *asked* for
    these together; ``skew_s`` says how far apart they actually arrived.
    Rows without round identity contribute nothing; an empty result means no
    round evidence was persisted, not that skew was zero.
    """
    frame = frame if ROUND_ID_COLUMN in frame else read_acquisition_frame(frame)
    tagged = frame.dropna(subset=[GENERATION_COLUMN, ROUND_ID_COLUMN, ACQUIRED_AT_COLUMN])
    if tagged.empty:
        return pd.DataFrame(columns=list(ROUND_SKEW_COLUMNS))
    grouped = tagged.groupby([GENERATION_COLUMN, ROUND_ID_COLUMN], sort=True)[ACQUIRED_AT_COLUMN]
    out = pd.DataFrame(
        {
            "n": grouped.size(),
            "first_acquired_at": grouped.min(),
            "skew_s": grouped.max() - grouped.min(),
        }
    ).reset_index()
    return out.loc[:, list(ROUND_SKEW_COLUMNS)]


def _numeric_series(frame: pd.DataFrame, spec: ChannelSpec, source: str) -> pd.DataFrame:
    """``obd:``/``uprev:``/``gps:`` — engineering units already, select and coerce.

    Matched case-insensitively rather than upper-cased: OBD and UpRev channels
    are stored upper-case but the GPS receiver's are lower-case (``speed``),
    and forcing one convention silently returned an empty frame for the other.
    """
    channel = frame["channel"].astype("string").str.casefold()
    rows = frame[(frame["source"] == source) & (channel == spec.name.casefold())]
    out = rows.loc[:, ["timestamp"]].copy()
    out["value"] = pd.to_numeric(rows["value_number"], errors="coerce")
    out["unit"] = rows["unit"].astype("string").fillna("")
    return out.dropna(subset=["value"])


def _kline_series(frame: pd.DataFrame, spec: ChannelSpec) -> pd.DataFrame:
    """``kline:<module>:<item>`` — K-line values, as raw counts.

    Stored like DID rows: the value is hex in ``value_text`` and
    ``value_number`` is empty, so a numeric select returned nothing. Decoded
    here as the unsigned big-endian integer the bytes hold, unit ``counts``:
    engineering units are the vehicle's K-line profile bindings, applied by
    ``src.analysis.kline_exchanges``, not guessed here.

    A legacy name (``DID_1202``, stored before K01.1) is refused: it names no
    module, and the ABS's 0x1202 is not the ECM's.
    """

    if is_legacy(spec.name):
        raise ChannelSpecError(
            f"{spec.text!r} is a pre-K01.1 K-line name with no module; recover the module "
            "with src.analysis.kline_exchanges.attribute_modules and ask for "
            "kline:<MODULE>:<hex>"
        )
    channel = frame["channel"].astype("string").str.casefold()
    rows = frame[(frame["source"] == "kline") & (channel == spec.name.casefold())]
    out = rows.loc[:, ["timestamp"]].copy()
    numeric = pd.to_numeric(rows["value_number"], errors="coerce")
    text = rows["value_text"].astype("string")
    counts = text.map(_hex_counts, na_action="ignore")
    out["value"] = numeric.where(numeric.notna(), pd.to_numeric(counts, errors="coerce"))
    out["unit"] = rows["unit"].astype("string").fillna("").where(numeric.notna(), _RAW_UNIT)
    return out.dropna(subset=["value"])


def _hex_counts(text: str) -> float | None:
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        return None
    return float(int.from_bytes(raw, "big")) if raw else None


def _memory_series(frame: pd.DataFrame, spec: ChannelSpec) -> pd.DataFrame:
    """``mem:`` — service-0x23 channels, scaled at collection.

    The collector applies the catalog's scale before the reading is stored
    (unlike ``did:``, whose payload is kept as hex), so this is a select and
    a coerce. The stored channel keeps its ``mem:`` prefix, which is what
    distinguishes it from a Mode 01 channel of the same name.

    A ``_SENTINEL`` row has no numeric value — it is a declared *state*, not
    a measurement, and scaling the fuel-cut word would put a phi of 0.0 into
    every mean that followed. It is surfaced as a 1.0 indicator with unit
    ``"flag"`` so the instants stay pairable while remaining impossible to
    mistake for a reading.
    """
    channel = frame["channel"].astype("string")
    rows = frame[(frame["source"] == "obd") & (channel == f"mem:{spec.name}")]
    out = rows.loc[:, ["timestamp"]].copy()
    numeric = pd.to_numeric(rows["value_number"], errors="coerce")
    text = rows["value_text"].astype("string")
    out["value"] = numeric.where(numeric.notna(), text.notna().map({True: 1.0, False: None}))
    out["unit"] = rows["unit"].astype("string").fillna("").where(numeric.notna(), "flag")
    return out.dropna(subset=["value"])


def _did_series(frame: pd.DataFrame, spec: ChannelSpec, catalogs: _Catalogs) -> pd.DataFrame:
    entry = catalogs.did(spec.name)
    channel = f"DID_{entry.did:04X}"
    # A legacy ``kline:DID_<hex>`` row is some K-line module's, not this
    # catalog's (the ABS's 0x1210 is yaw rate, the ECM's is TPS2).
    named = frame["channel"] == channel
    foreign = frame["source"].isin(dids_decode.FOREIGN_DID_SOURCES)
    rows = frame[named & ~foreign]
    if rows.empty and (named & foreign).any():
        # Say which kind of empty: the hex was read, but by some K-line module.
        raise ChannelSpecError(
            f"{spec.text!r}: this session's only {channel} rows are pre-K01.1 K-line rows, "
            "which name no module and are not this catalog's; recover their module with "
            "src.analysis.kline_exchanges.attribute_modules and ask for kline:<MODULE>:<hex>"
        )
    out = rows.loc[:, ["timestamp"]].copy()
    text = rows["value_text"].astype("string")

    def decode(hex_text: object) -> float | None:
        if not isinstance(hex_text, str) or not hex_text:
            return None
        try:
            payload = bytes.fromhex(hex_text)
        except ValueError:
            return None
        raw = dids_decode.decode_did_raw(entry, payload)
        if raw is None:
            return None
        scaled = dids_decode.apply_scaling(entry, raw)
        return float(raw) if scaled is None else float(scaled)

    out["value"] = [decode(v) for v in text]
    out["unit"] = entry.unit if entry.scale is not None else _RAW_UNIT
    return out.dropna(subset=["value"])


def _decoded_series(frame: pd.DataFrame, spec: ChannelSpec, catalogs: _Catalogs) -> pd.DataFrame:
    """``decoded:`` rows carry the RAW integer — scale it here, once."""
    signal = catalogs.signal(spec.name)
    rows = frame[(frame["source"] == "decoded") & (frame["channel"] == signal.name)]
    out = rows.loc[:, ["timestamp"]].copy()
    raw = pd.to_numeric(rows["value_number"], errors="coerce")
    factors = bindings_decode.scaling_factors(signal)
    if factors is None:
        out["value"] = raw
        out["unit"] = _RAW_UNIT
    else:
        scale, offset = factors
        out["value"] = raw * scale + offset
        out["unit"] = signal.unit
    return out.dropna(subset=["value"])


def _can_series(frame: pd.DataFrame, spec: ChannelSpec, catalogs: _Catalogs) -> pd.DataFrame:
    """Decode a bound signal straight from stored frame bytes.

    Named by *signal*, not by CAN id: one frame carries several signals, so
    ``can:0x1F9`` would not say which. Unbound payload archaeology stays in
    DuckDB (``hex(payload)``), deliberately — this reader's contract is
    engineering units, and an unbound byte range has none.
    """
    signal = catalogs.signal(spec.name)
    channel = f"0x{signal.can_id:03X}"
    rows = frame[(frame["source"] == "can") & (frame["channel"].str.upper() == channel.upper())]
    out = rows.loc[:, ["timestamp"]].copy()

    def decode(payload: object) -> float | None:
        if not isinstance(payload, (bytes, bytearray)):
            return None
        raw = bindings_decode.decode_raw(signal, bytes(payload))
        if raw is None:
            return None
        scaled = bindings_decode.apply_scaling(signal, raw)
        return float(raw) if scaled is None else float(scaled)

    out["value"] = [decode(p) for p in rows["payload"]]
    out["unit"] = signal.unit if signal.scale is not None else _RAW_UNIT
    return out.dropna(subset=["value"])


#: Derived spec: commanded AFR, reconstructed from PID 0x44 through the
#: vehicle's recorded phi/lambda convention (roadmap 1.3). Not a stored
#: channel — it exists only when the convention has been verified, because
#: guessing it wrong is a reciprocal error that hides at stoich.
COMMANDED_AFR_SPEC = "obd:COMMANDED_AFR"
_EQUIV_RATIO_CHANNEL = "COMMANDED_EQUIV_RATIO"


def _commanded_afr_series(
    frame: pd.DataFrame,
    calibration: AfrCalibration | None,
) -> pd.DataFrame:
    if calibration is None:
        raise ChannelSpecError(
            f"{COMMANDED_AFR_SPEC} needs an AFR calibration; pass calibration= "
            "(the vehicle profile's afr_calibration.yaml): its convention says phi or "
            "lambda, and its afr_scale is the AFR the ECM states at lambda = 1 (M01.3)"
        )
    rows = frame[(frame["source"] == "obd") & (frame["channel"] == _EQUIV_RATIO_CHANNEL)]
    out = rows.loc[:, ["timestamp"]].copy()
    ratio = pd.to_numeric(rows["value_number"], errors="coerce")
    # Raises for an unverified convention — deliberately loud, once per call.
    # A ratio of 0.0 is decel fuel cut (no target at all) and converts to NaN,
    # which is dropped below; see AfrCalibration.commanded_afr_series.
    out["value"] = calibration.commanded_afr_series(ratio)
    out["unit"] = "AFR"
    return out.dropna(subset=["value"])


def read_channels(
    session: Path | pd.DataFrame,
    specs: list[str],
    *,
    bindings_path: Path = BINDINGS_YAML,
    dids_path: Path = DIDS_YAML,
    calibration: AfrCalibration | None = None,
    acquired_time: bool = False,
) -> pd.DataFrame:
    """Read ``specs`` from one session as a long frame in engineering units.

    Returns columns ``timestamp``, ``spec``, ``value``, ``unit`` — long rather
    than wide because each source has its own arrival cadence; putting them on
    a shared clock is :func:`read_aligned`'s job, and doing it implicitly here
    is exactly the stale-join trap (G.4) this module exists to close.

    ``unit`` is ``"counts"`` wherever the catalog's ``scale`` is unpinned, so a
    caller can tell "raw" from "engineering" without consulting the YAML.
    Set ``acquired_time=True`` for lag/cross-source work: OBD/DID rows then
    use their persisted per-reading arrival time rather than a logical round
    timestamp, while sources without that field retain their stored time. The
    frame then also carries a ``timestamp_basis`` column per row
    (:data:`src.signals.pairing.TimestampBasis`) saying which of those
    happened, so a comparison can refuse a tolerance the clock cannot carry.

    A broadcast spec read from a session that stores CAN frames change-only
    (P02.10) returns its stored rows — the changes and the re-writes — and
    names itself in ``attrs[AS_OF_ATTR]`` with how long each row stays valid;
    :func:`align_long` and ``compare.align_pairs`` apply that. A caller that
    counts these rows is counting payload changes, not frames.
    """
    catalogs = _Catalogs(bindings_path=bindings_path, dids_path=dids_path)
    parsed = [parse_spec(text) for text in specs]
    frame = _long_frame(
        session,
        acquired_time=acquired_time,
        filters=_raw_read_filters(parsed, catalogs) if parsed else None,
        columns=_read_columns(parsed, acquired_time=acquired_time),
        narrow=_narrowing(parsed, catalogs) if parsed else None,
    )

    readers = {
        "obd": lambda s: _numeric_series(frame, s, "obd"),
        "uprev": lambda s: _numeric_series(frame, s, "uprev"),
        "gps": lambda s: _numeric_series(frame, s, "gps"),
        "imu": lambda s: _numeric_series(frame, s, "imu"),
        "sensor": lambda s: _numeric_series(frame, s, "sensor"),
        "kline": lambda s: _kline_series(frame, s),
        "mem": lambda s: _memory_series(frame, s),
        "did": lambda s: _did_series(frame, s, catalogs),
        "decoded": lambda s: _decoded_series(frame, s, catalogs),
        "can": lambda s: _can_series(frame, s, catalogs),
    }

    columns = ["timestamp", "spec", "value", "unit"]
    if acquired_time:
        columns.append(TIMESTAMP_BASIS_COLUMN)
    parts: list[pd.DataFrame] = []
    for spec in parsed:
        if spec.source == "obd" and spec.name.upper() == "COMMANDED_AFR":
            part = _commanded_afr_series(frame, calibration)
        else:
            part = readers[spec.source](spec)
        part = part.assign(spec=spec.text)
        if acquired_time:
            part = part.assign(
                **{TIMESTAMP_BASIS_COLUMN: frame.loc[part.index, TIMESTAMP_BASIS_COLUMN]}
            )
        parts.append(part.loc[:, columns])

    as_of = _as_of_validity(frame, parsed)
    if not parts:
        empty = pd.DataFrame(columns=columns)
        if as_of:
            empty.attrs[AS_OF_ATTR] = as_of
        return empty
    out = pd.concat(parts, ignore_index=True)
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
    out = out.sort_values(["timestamp", "spec"], kind="stable").reset_index(drop=True)
    out.attrs.pop(AS_OF_ATTR, None)
    if as_of:
        out.attrs[AS_OF_ATTR] = as_of
    return out


def _as_of_validity(frame: pd.DataFrame, parsed: list[ChannelSpec]) -> dict[str, float]:
    """:data:`AS_OF_ATTR` for ``parsed``: each broadcast spec's validity when
    the session stores CAN frames change-only, from the loader's attrs."""
    timeout = change_only_timeout(frame)
    if timeout is None:
        return {}
    return {spec.text: timeout for spec in parsed if spec.source in _BROADCAST_SPEC_SOURCES}


def concat_channel_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Concatenate :func:`read_channels` frames, keeping :data:`AS_OF_ATTR`.

    ``pd.concat`` drops ``attrs`` that differ between its inputs, and two
    reads of different specs always differ, so a plain concat would read a
    change-only broadcast channel as sparse samples (P02.10)."""
    out = pd.concat(frames, ignore_index=True)
    merged: dict[str, float] = {}
    for frame in frames:
        merged.update(as_of_validity(frame))
    out.attrs.pop(AS_OF_ATTR, None)
    if merged:
        out.attrs[AS_OF_ATTR] = merged
    return out


def as_of_validity(long: pd.DataFrame) -> dict[str, float]:
    """:data:`AS_OF_ATTR` of a :func:`read_channels` frame: spec -> seconds."""
    raw = long.attrs.get(AS_OF_ATTR) or {}
    return {str(spec): float(seconds) for spec, seconds in raw.items()}


def as_of_refusal(
    long: pd.DataFrame, *, reader: str, specs: Iterable[str] | None = None
) -> str | None:
    """Why ``reader`` may not treat ``long``'s rows as samples, or ``None``.

    The one statement of P02.10's rule for a :func:`read_channels` caller
    that pairs stored rows one-to-one, counts them, bins them by second, or
    reads rates, gaps or steadiness from them: a broadcast spec read from a
    change-only session (``attrs[AS_OF_ATTR]``) holds its payload changes and
    re-writes, not one row per frame, so each of those answers would be about
    how often the payload changed. ``specs`` narrows the check to the specs
    the caller uses that way (default: every spec with rows in the frame;
    a slice of one spec keeps its read's attrs for all of them). A caller
    that needs the value at an instant uses the held path instead
    (:func:`align_long` / :func:`read_aligned`, ``compare.align_pairs``).
    """
    held = as_of_validity(long)
    if specs is not None:
        wanted = {parse_spec(text).text for text in specs}
        held = {spec: seconds for spec, seconds in held.items() if spec in wanted}
    if held and "spec" in long.columns:
        # A slice of a multi-spec read keeps the whole frame's attrs; only
        # the specs whose rows are present are this caller's.
        present = set(long["spec"].astype(str).unique())
        held = {spec: seconds for spec, seconds in held.items() if spec in present}
    if not held:
        return None
    return (
        change_only_refusal(
            f"{reader} ({', '.join(sorted(held))})",
            _change_only_sessions(long),
            max(held.values()),
        )
        + " A read of such a spec holds its payload changes and re-writes, one row per "
        "change rather than per frame, so pairing its rows one-to-one, counting them or "
        "binning them by second measures how often the payload changed; the held readers "
        "(read_aligned, align_long, compare.align_pairs) give the value as of each instant."
    )


def refuse_as_of(long: pd.DataFrame, *, reader: str, specs: Iterable[str] | None = None) -> None:
    """Raise :class:`~src.storage.broadcast_storage.ChangeOnlyStorageError`
    when :func:`as_of_refusal` names a cause (P02.10)."""
    message = as_of_refusal(long, reader=reader, specs=specs)
    if message is not None:
        raise ChangeOnlyStorageError(message, reader=reader, sessions=_change_only_sessions(long))


def _change_only_sessions(long: pd.DataFrame) -> list[str]:
    info = long.attrs.get(FRAME_ATTR)
    names = list(info.get("change_only_sessions") or []) if isinstance(info, dict) else []
    return names or ["this session"]


def read_aligned(
    session: Path | pd.DataFrame,
    specs: list[str],
    *,
    grid: str = "1s",
    require_all: bool = True,
    cell_agg: Literal["last", "mean"] = "last",
    max_staleness: pd.Timedelta | str | None = None,
    with_age: bool = False,
    bindings_path: Path = BINDINGS_YAML,
    dids_path: Path = DIDS_YAML,
    calibration: AfrCalibration | None = None,
) -> pd.DataFrame:
    """Put ``specs`` on one clock, refusing stale joins by default.

    This is the cell-based *aligned frame* API — one row per ``grid`` cell,
    for band averages, gating and plotting. It is **not** the comparison
    contract: a cell holding a ``"last"`` sample from each channel does not
    say how far apart those samples were acquired, and a ``"mean"`` cell is
    a synthetic value that was never observed. Its output is therefore not
    pairing evidence; a claim that two channels agree goes through
    :func:`src.analysis.compare.compare_channels`, which pairs individual
    observations under an explicit :class:`~src.signals.pairing.PairingPolicy`.

    A row exists only where **every** requested channel has its own sample
    inside the grid cell (``require_all=True``). Nothing is forward-filled:
    the 2026-07 campaign recorded four "WOT AFR" samples that were one stale
    reading aged 24–35 s, paired in by an ASOF join and indistinguishable from
    measurements. Freshness is the default here so that cannot recur silently.

    ``max_staleness`` opts into carrying a value forward, and is explicit on
    purpose. When it is set — or ``with_age=True`` — the frame also carries a
    ``<spec>__age_s`` column per channel, so a caller can always see how old
    the number it is using actually is.

    ``cell_agg`` decides what a cell holding several samples reports.
    ``"last"`` (default) keeps a real sample taken at a known instant;
    ``"mean"`` averages, which is smoother but synthetic and can smear a
    transient. This is not cosmetic: on the 2026-08-05 capture the choice
    moved band-averaged delivery error from +0.58 % to +0.72 %, purely by
    reassigning boundary samples between rpm bands. It is a parameter rather
    than a hidden default for exactly that reason.
    """
    long = read_channels(
        session,
        specs,
        bindings_path=bindings_path,
        dids_path=dids_path,
        calibration=calibration,
    )
    return align_long(
        long,
        specs,
        grid=grid,
        require_all=require_all,
        cell_agg=cell_agg,
        max_staleness=max_staleness,
        with_age=with_age,
    )


def align_long(
    long: pd.DataFrame,
    specs: list[str],
    *,
    grid: str = "1s",
    require_all: bool = True,
    cell_agg: Literal["last", "mean"] = "last",
    max_staleness: pd.Timedelta | str | None = None,
    with_age: bool = False,
) -> pd.DataFrame:
    """:func:`read_aligned`'s alignment step over an already-read long frame.

    Split out so a caller that also needs :func:`read_channels`' per-row
    ``unit`` (the workbench log frame) reads the session once rather than
    twice. Same freshness contract as :func:`read_aligned`.

    A spec :func:`read_channels` marked as-of (``attrs[AS_OF_ATTR]``, a
    change-only broadcast channel, P02.10) is carried from each stored row
    into the following cells for its validity, whatever ``max_staleness``
    says: in that storage no row means "the same payload again", so the cell
    a session stored every frame would fill from a frame of its own is filled
    with the value that frame carried. ``__age_s`` still says how old the
    stored row is. ``cell_agg="mean"`` is refused for such a spec, because a
    mean over the stored changes is not a mean over the frames the bus sent.
    """
    names = [parse_spec(text).text for text in specs]
    if long.empty:
        return pd.DataFrame(columns=["timestamp", *names])

    held = {name: seconds for name, seconds in as_of_validity(long).items() if name in names}
    if held and cell_agg == "mean":
        raise ValueError(
            f"cell_agg='mean' over {sorted(held)}: this session stores CAN frames "
            "change-only (P02.10), so a mean over the stored rows averages payload "
            "changes, not the frames the bus sent; use cell_agg='last'"
        )
    tolerance = pd.Timedelta(max_staleness) if max_staleness is not None else None
    emit_age = with_age or tolerance is not None

    long = long.copy()
    long["bucket"] = long["timestamp"].dt.floor(grid)
    buckets = pd.DatetimeIndex(sorted(long["bucket"].unique()), name="timestamp")
    if held:
        buckets = _with_held_buckets(long, buckets, held, pd.Timedelta(grid))
    out = pd.DataFrame(index=buckets)

    for name in names:
        rows = long[long["spec"] == name]
        cell = rows.groupby("bucket", sort=True).agg(value=("value", cell_agg))
        series = cell["value"].reindex(buckets)
        seen = pd.Series(cell.index, index=cell.index).reindex(buckets)
        spec_tolerance = tolerance
        if name in held:
            validity = pd.Timedelta(seconds=held[name])
            spec_tolerance = validity if tolerance is None else max(tolerance, validity)
        if spec_tolerance is not None:
            series = series.ffill()
            seen = seen.ffill()
        age = (
            buckets.to_series().reset_index(drop=True).to_numpy() - seen.to_numpy()
        ) / pd.Timedelta(seconds=1)
        age_series = pd.Series(age, index=buckets, dtype="float64")
        if spec_tolerance is not None:
            too_old = age_series > spec_tolerance.total_seconds()
            series = series.mask(too_old)
            age_series = age_series.mask(too_old)
        out[name] = series
        if emit_age:
            out[f"{name}__age_s"] = age_series

    if require_all:
        out = out.dropna(subset=names)
    else:
        out = out.dropna(subset=names, how="all")
    return out.reset_index()


def _with_held_buckets(
    long: pd.DataFrame,
    buckets: pd.DatetimeIndex,
    held: dict[str, float],
    step: pd.Timedelta,
) -> pd.DatetimeIndex:
    """``buckets`` plus every cell an as-of spec's stored rows hold (P02.10).

    A session stored every frame has a row of a transmitting id in every
    cell; a change-only one has a row only where the payload changed or was
    re-written. Each stored row therefore stands for the cells after its own,
    up to its validity, clipped to the frame's last cell (nothing is held past
    the end of what was read).
    """
    last = buckets.max()
    extra: list[pd.DatetimeIndex] = []
    for name, seconds in held.items():
        starts = pd.DatetimeIndex(long.loc[long["spec"] == name, "bucket"].unique())
        if starts.empty:
            continue
        steps = int(pd.Timedelta(seconds=seconds) // step)
        for k in range(1, steps + 1):
            shifted = starts + k * step
            extra.append(shifted[shifted <= last])
    if not extra:
        return buckets
    merged = buckets.append(extra).unique().sort_values()
    return pd.DatetimeIndex(merged, name="timestamp")


def rescale_channel_rows(
    rows: list[dict[str, object]],
    *,
    bindings_path: Path = BINDINGS_YAML,
    dids_path: Path = DIDS_YAML,
    key_field: str = "channel_key",
    value_field: str = "value_number",
) -> list[dict[str, object]]:
    """Convert long ``source:channel`` rows in place to engineering units.

    The query layer reads ``value_number`` straight out of Parquet, which is
    correct for ``obd:`` and **wrong for ``decoded:``** — those rows hold raw
    integers by design, so a binned "trims by rpm" over ``decoded:ECM_RPM``
    silently bins on counts (×8 for rpm). This is the smallest fix that closes
    that on the remote/agent path without standing up a second query engine.

    ``did:`` channels are deliberately *not* handled here: their values live in
    ``value_text`` and never survive the query layer's numeric filter, so there
    is nothing to rescale. Use :func:`read_channels` for those.
    """
    catalogs = _Catalogs(bindings_path=bindings_path, dids_path=dids_path)
    factors: dict[str, tuple[float, float] | None] = {}

    for row in rows:
        key = str(row.get(key_field, ""))
        source, sep, name = key.partition(SOURCE_SEP)
        if not sep or source != DECODED_SOURCE:
            continue
        if key not in factors:
            try:
                signal = catalogs.signal(name)
            except ChannelSpecError:
                factors[key] = None
            else:
                factors[key] = bindings_decode.scaling_factors(signal)
        factor = factors[key]
        if factor is None:
            continue
        raw = row.get(value_field)
        if isinstance(raw, (int, float)):
            row[value_field] = float(raw) * factor[0] + factor[1]
    return rows
