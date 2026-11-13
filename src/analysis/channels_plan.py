"""What a :func:`~src.analysis.channels.read_channels` call reads, and how.

The first half of :mod:`src.analysis.channels` (R26.8): the spec grammar
(:func:`parse_spec`, :class:`ChannelSpec`) and its catalogs, the plan that
narrows a read to the rows and columns the specs need
(:func:`raw_read_filters`, :func:`channel_read_columns`), the one shared read
that serves many calls from one scan (:class:`SharedSessionRead`), and the
reads of a session as a long frame on the stored or the acquisition clock
(:func:`read_acquisition_frame`, :func:`round_skew`). Nothing here converts a
stored value to engineering units: that is the series builders in
``channels``, which imports this module and re-exports every public name, so
``from src.analysis.channels import parse_spec`` keeps working.

Every :func:`~src.preprocessing.loader.read_session` call a read makes is in
this module, so a test counting scans patches ``channels_plan.read_session``.
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

from src.decode import bindings as bindings_decode
from src.decode import dids as dids_decode
from src.decode.on_read import can_channel_spellings
from src.paths import BINDINGS_YAML, DIDS_YAML
from src.preprocessing.loader import DecodedRows, filter_rows, read_session
from src.storage.capture_manifest import read_capture_manifest
from src.telemetry import DECODED_SOURCE

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


_EQUIV_RATIO_CHANNEL = "COMMANDED_EQUIV_RATIO"
