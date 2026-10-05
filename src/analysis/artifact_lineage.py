"""Artifact input lineage against the catalog that was in force when it was built (B08.1).

A model artifact names its inputs by channel name, and a name is only a label
on a catalog entry. On 2026-10-04 the CAN DID catalog was corrected
(``c897a62``): DID ``0x1102``, catalogued from UpRev's K-line stream as long-term
fuel trim bank 1, is road speed on CAN, and the name ``ECM_LONG_FUEL_TRIM_B1``
moved to DID ``0x1125``. An artifact that names ``did:ECM_LONG_FUEL_TRIM_B1``
reads LTFT today, whatever it was trained on. The current catalog cannot make
an old artifact's inputs correct, so this module never trusts a name:

1. every input an artifact names (behavior-model recipe axes, conditions and
   eligibility channels; IF/sequence manifest feature columns) is expanded
   through ``derived:`` dependencies;
2. each leaf is resolved to its **source identity** (Mode 01 PID, Mode 22 DID
   number, RAM address, CAN id and byte slice) in the catalog in force when
   the artifact was built *and* in the current one, through the stored-name
   migrations in ``tools/channel_renames``;
3. a changed identity, a changed decode or a retired entry makes the artifact
   **affected**; an input that cannot be resolved at build time, or a
   broadcast decode that varied across the window its as-recorded rows were
   written in, makes it **unverifiable**; otherwise it is **unaffected**.

Recoverable raw samples are re-decoded where the payload is stored: Mode 01
replies against the PID decoder table, raw ``DID_<hex>`` replies against both
the build-time and the current entry. A trim input qualifies only through a
register whose quantity is a fuel trim (``observation_aliases.yaml``); the
road-speed register is the negative control, which DID ``0x1102`` falls into
under any name.

Nothing here edits an artifact, a policy or a catalog. Superseding an affected
artifact goes through B03's mechanisms and is the caller's decision.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from src.collection.obd_registry import MISFIRE_COUNT_CHANNEL_PREFIX, MODE01_REGISTRY
from src.collection.pid_decoders import PID_DECODERS
from src.storage.sessions import session_started_at

#: Channel source prefixes an artifact may name. Anything else in an artifact
#: (mode labels, session columns) is not an observation.
CHANNEL_PREFIXES: frozenset[str] = frozenset(
    {"obd", "did", "mem", "decoded", "dbc", "derived", "gps", "imu", "adc", "can", "kline"}
)
_CHANNEL_RE = re.compile(r"^(?P<prefix>[a-z_]+):(?P<name>[A-Za-z0-9_:]+)$")
_RAW_DID_RE = re.compile(r"^DID_(?P<hex>[0-9A-Fa-f]{4})$")
_FEATURE_SUFFIX_RE = re.compile(r"__[a-z0-9_]+$")

#: Paths of the decode catalogs inside the repository.
DIDS_PATH = "data/dids.yaml"
BINDINGS_PATH = "data/bindings.yaml"
#: Every path the derived-channel definitions have lived at (R09.4 moved it).
DERIVED_MODULE_PATHS: tuple[str, ...] = ("src/signals/derived.py", "src/analysis/derived.py")

#: P02.8 (``a09dcd4``, 2026-09-26): from here on every reader decodes broadcast
#: and DID rows on read with the current catalog. Before it, a trainer read
#: the ``decoded`` rows the Pi stored, decoded with the Pi's catalog at record
#: time, so the binding in force over the whole recording window matters.
DECODE_ON_READ_SINCE = datetime(2026, 9, 26, 18, 1, 31, tzinfo=UTC)

_DEFAULTS: Mapping[str, Any] = {
    "byte_start": 0,
    "byte_order": "big",
    "signed": False,
    "scale": None,
    "offset": None,
    "sentinel_raw": None,
}
DID_DECODE_KEYS: tuple[str, ...] = (
    "byte_start",
    "byte_length",
    "byte_order",
    "signed",
    "scale",
    "offset",
)
MEMORY_DECODE_KEYS: tuple[str, ...] = (*DID_DECODE_KEYS, "sentinel_raw")
BINDING_IDENTITY_KEYS: tuple[str, ...] = ("can_id", "byte_start", "byte_length")
BINDING_DECODE_KEYS: tuple[str, ...] = ("byte_order", "signed", "scale", "offset")

#: Input statuses, by the artifact verdict they force.
AFFECTED_STATUSES: frozenset[str] = frozenset(
    {
        "identity_changed",
        "decode_changed",
        "retired",
        "trim_identity_refused",
        "definition_changed",
    }
)
UNVERIFIABLE_STATUSES: frozenset[str] = frozenset(
    {
        "unresolved_at_build",
        "as_recorded_varied",
        "as_recorded_unknown",
        "definition_unlocated",
        "trim_register_unknown",
    }
)
#: A derived channel's fallback input that no catalog held at build time or
#: holds now: nothing could have been decoded under it, so it fed nothing.
NEVER_CATALOGUED = "never_catalogued"


# --------------------------------------------------------------------------
# Catalog snapshots and their history
# --------------------------------------------------------------------------


def _norm(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int | float):
        return float(value)
    return value


def _spec(entry: Mapping[str, Any], keys: Sequence[str]) -> tuple[tuple[str, Any], ...]:
    """The decode as the decoder applies it (``src.decode.slices.linear_factors``).

    An unset offset is zero, and an unpinned scale yields the raw count whatever
    the offset says, so ``offset: null -> 0.0`` beside ``scale: null`` is no
    change, while ``scale: null -> 0.01`` is (raw counts became milliseconds).
    """
    spec = {key: _norm(entry.get(key, _DEFAULTS.get(key))) for key in keys}
    if "offset" in spec:
        if spec.get("scale") is None:
            spec["offset"] = None
        elif spec["offset"] is None:
            spec["offset"] = 0.0
    return tuple(spec.items())


def _changed_keys(
    before: Mapping[str, Any], after: Mapping[str, Any], keys: Sequence[str]
) -> list[str]:
    a, b = dict(_spec(before, keys)), dict(_spec(after, keys))
    return [key for key in keys if a[key] != b[key]]


@dataclass(frozen=True)
class CatalogSnapshot:
    """The DID, RAM and broadcast catalogs as one revision held them."""

    label: str
    committed_at: datetime | None
    dids: Mapping[str, Mapping[str, Any]]
    memory: Mapping[str, Mapping[str, Any]]
    bindings: Mapping[str, Mapping[str, Any]]

    @classmethod
    def from_documents(
        cls,
        label: str,
        committed_at: datetime | None,
        dids_doc: Mapping[str, Any] | None,
        bindings_doc: Mapping[str, Any] | None,
    ) -> CatalogSnapshot:
        dids_doc = dids_doc or {}
        bindings_doc = bindings_doc or {}
        return cls(
            label=label,
            committed_at=committed_at,
            dids={e["name"]: e for e in dids_doc.get("dids") or [] if "name" in e},
            memory={e["name"]: e for e in dids_doc.get("memory") or [] if "name" in e},
            bindings={e["name"]: e for e in bindings_doc.get("signals") or [] if "name" in e},
        )

    def did_by_id(self, did: int) -> Mapping[str, Any] | None:
        # The decoder keeps the last entry per id (did_entries_by_hex).
        found = None
        for entry in self.dids.values():
            if entry.get("did") == did:
                found = entry
        return found


@dataclass(frozen=True)
class CatalogHistory:
    """Snapshots in commit order, plus the catalog the code reads now."""

    snapshots: tuple[CatalogSnapshot, ...]
    current: CatalogSnapshot

    def at(self, when: datetime) -> CatalogSnapshot | None:
        """The last committed snapshot at or before ``when`` (``None`` if none)."""
        found = None
        for snapshot in self.snapshots:
            if snapshot.committed_at is not None and snapshot.committed_at <= when:
                found = snapshot
        return found

    def spanning(self, start: datetime, end: datetime) -> list[CatalogSnapshot]:
        """Every snapshot in force at some instant of ``[start, end]``."""
        first = self.at(start)
        inside = [
            s
            for s in self.snapshots
            if s.committed_at is not None and start < s.committed_at <= end
        ]
        return ([first] if first is not None else []) + inside


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, timeout=60
    )
    return result.stdout


def _git_show(repo: Path, rev: str, path: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"{rev}:{path}"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


#: libyaml's safe loader when built in: the history is ~60 revisions of a
#: 3,000-line catalog, and the pure-Python loader takes seconds over it.
_YAML_LOADER: Any = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def _load_yaml(text: str | None) -> Mapping[str, Any] | None:
    if text is None:
        return None
    loaded = yaml.load(text, Loader=_YAML_LOADER)  # noqa: S506 - a safe loader
    return loaded if isinstance(loaded, Mapping) else None


def load_catalog_history(repo: Path, *, since: datetime | None = None) -> CatalogHistory:
    """Every commit that touched a decode catalog, and the working tree's files.

    ``since`` limits the history to commits after it (a known-answer check
    needs only a few revisions; an audit needs them all).
    """
    window = [] if since is None else [f"--since={since.isoformat()}"]
    log = _git(repo, "log", "--reverse", "--format=%H %cI", *window, "--", DIDS_PATH, BINDINGS_PATH)
    snapshots = []
    for line in log.splitlines():
        rev, stamp = line.split(" ", 1)
        snapshots.append(
            CatalogSnapshot.from_documents(
                rev[:7],
                datetime.fromisoformat(stamp).astimezone(UTC),
                _load_yaml(_git_show(repo, rev, DIDS_PATH)),
                _load_yaml(_git_show(repo, rev, BINDINGS_PATH)),
            )
        )
    current = CatalogSnapshot.from_documents(
        "working-tree",
        None,
        _load_yaml((repo / DIDS_PATH).read_text()),
        _load_yaml((repo / BINDINGS_PATH).read_text()),
    )
    return CatalogHistory(tuple(snapshots), current)


# --------------------------------------------------------------------------
# Stored-name migrations
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Rename:
    """One ``old -> new`` stored-name migration (``tools/channel_renames``)."""

    kind: str  # "did" | "memory"
    old: str
    new: str
    source: str


def load_renames(directory: Path) -> tuple[Rename, ...]:
    renames: list[Rename] = []
    for path in sorted(directory.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text()) or {}
        for kind, key in (("did", "channels"), ("memory", "memory_channels")):
            for old, new in (doc.get(key) or {}).items():
                renames.append(Rename(kind, str(old), str(new), path.name))
    return tuple(renames)


def former_names(name: str, kind: str, renames: Sequence[Rename]) -> list[tuple[str, str]]:
    """``(old name, migration file)`` pairs that became ``name``, nearest first."""
    out: list[tuple[str, str]] = []
    frontier = [name]
    seen = {name}
    while frontier:
        target = frontier.pop(0)
        for rename in renames:
            if rename.kind == kind and rename.new == target and rename.old not in seen:
                seen.add(rename.old)
                out.append((rename.old, rename.source))
                frontier.append(rename.old)
    return out


# --------------------------------------------------------------------------
# Shared registers (observation_aliases.yaml)
# --------------------------------------------------------------------------

IdentityKey = tuple[str, int]


@dataclass(frozen=True)
class Register:
    register: int
    quantity: str
    identities: frozenset[IdentityKey]
    source: str


@dataclass(frozen=True)
class DependentRelation:
    identities: frozenset[IdentityKey]
    agreement_counts_as: str
    condition: str


#: What an agreement between two dependent identities may count as.
AGREEMENT_KINDS: frozenset[str] = frozenset({"decode_verification", "dependent_evidence"})


@dataclass(frozen=True)
class ObservationAliases:
    registers: tuple[Register, ...] = ()
    relations: tuple[DependentRelation, ...] = ()
    #: channel -> the register quantity it claims (``fuel_trim_long_b1`` ...).
    trim_claims: Mapping[str, str] = field(default_factory=dict)

    def register_of(self, key: IdentityKey) -> Register | None:
        for register in self.registers:
            if key in register.identities:
                return register
        return None

    def relations_of(self, key: IdentityKey) -> list[DependentRelation]:
        return [r for r in self.relations if key in r.identities]


def _identity_key(raw: Mapping[str, Any]) -> IdentityKey:
    kind = str(raw["kind"])
    field_name = {"mode01": "pid", "did": "did", "memory": "address"}[kind]
    return (kind, int(raw[field_name]))


def load_observation_aliases(path: Path) -> ObservationAliases:
    if not path.is_file():
        return ObservationAliases()
    doc = yaml.safe_load(path.read_text()) or {}
    registers = tuple(
        Register(
            register=int(item["register"]),
            quantity=str(item["quantity"]),
            identities=frozenset(_identity_key(i) for i in item["identities"]),
            source=str(item.get("source", "")).strip(),
        )
        for item in doc.get("shared_registers") or []
    )
    relations: list[DependentRelation] = []
    for item in doc.get("dependent_relations") or []:
        kind = str(item["agreement_counts_as"])
        if kind not in AGREEMENT_KINDS:
            raise ValueError(
                f"{path}: agreement_counts_as {kind!r} is not one of {sorted(AGREEMENT_KINDS)}"
            )
        relations.append(
            DependentRelation(
                identities=frozenset(_identity_key(i) for i in item["identities"]),
                agreement_counts_as=kind,
                condition=" ".join(str(item.get("condition", "")).split()),
            )
        )
    quantities = {register.quantity for register in registers}
    claims = {str(k): str(v) for k, v in (doc.get("trim_claims") or {}).items()}
    unknown = sorted(f"{k}: {v}" for k, v in claims.items() if v not in quantities)
    if unknown:
        raise ValueError(f"{path}: trim_claims name no register quantity: {unknown}")
    return ObservationAliases(registers, tuple(relations), claims)


def trim_qualification(
    key: IdentityKey | None, claimed: str, aliases: ObservationAliases
) -> tuple[bool, str]:
    """Whether a source identity may serve as the trim it is ``claimed`` to be.

    Qualification is by register, never by name: the identity must read a
    shared register whose quantity is exactly the claim -- same term
    (short/long) and same bank. DID ``0x1102`` resolves to the road-speed
    register and is refused under any name; DID ``0x1125`` claimed as LTFT
    bank 2 (the pre-``c897a62`` label) reads the bank-1 word and is refused.
    """
    if key is None:
        return False, "source identity unresolved"
    register = aliases.register_of(key)
    label = _identity_label(key)
    if register is None:
        return False, f"{label} has no register evidence in observation_aliases.yaml"
    where = f"{label} reads register 0x{register.register:08X} ({register.quantity})"
    if register.quantity != claimed:
        return False, f"{where}, not the claimed {claimed}"
    return True, where


def _identity_label(key: IdentityKey) -> str:
    kind, value = key
    if kind == "mode01":
        return f"Mode 01 PID 0x{value:02X}"
    if kind == "did":
        return f"DID 0x{value:04X}"
    return f"RAM 0x{value:08X}"


# --------------------------------------------------------------------------
# Resolving one input
# --------------------------------------------------------------------------

#: Mode 01 channel name -> PID: the collector's registry (every PID CANary may
#: request), with the poll loop's decoder table as a fallback.
MODE01_PIDS: Mapping[str, int] = {
    **{name: pid for pid, (name, _) in PID_DECODERS.items()},
    **{definition.channel: pid for pid, definition in MODE01_REGISTRY.items()},
}


def mode01_decoder(pid: int) -> Callable[[bytes], Any] | None:
    definition = MODE01_REGISTRY.get(pid)
    if definition is not None and definition.decoder is not None:
        return definition.decoder
    entry = PID_DECODERS.get(pid)
    return None if entry is None else entry[1]


#: ``obd:`` channels the collector writes from a service other than a Mode 01
#: data PID. Their identity is the service, which no catalog correction moves.
NON_PID_OBD_CHANNELS: Mapping[str, str] = {
    "MIL_STATUS": "Mode 01 PID 0x01 byte A bit 7 (MIL lamp)",
    "DTC_STORED": "Mode 03 stored DTC count",
    "DTC_PENDING": "Mode 07 pending DTC count",
    "MONITOR_RESULTS": "Mode 06 monitor results",
}


@dataclass(frozen=True)
class InputLineage:
    """One leaf input of an artifact, resolved at build time and now."""

    channel: str
    via: tuple[str, ...]
    kind: str
    status: str
    identity_at_build: str | None = None
    identity_now: str | None = None
    identity_key: IdentityKey | None = None
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "channel": self.channel,
            "kind": self.kind,
            "status": self.status,
            "identity_at_build": self.identity_at_build,
            "identity_now": self.identity_now,
        }
        if self.via:
            out["via"] = list(self.via)
        if self.notes:
            out["notes"] = list(self.notes)
        return out


def normalise_channel(column: str) -> str | None:
    """The channel a stored column or feature column names, or ``None``."""
    base = column
    while True:
        stripped = _FEATURE_SUFFIX_RE.sub("", base)
        if stripped == base:
            break
        base = stripped
    if base.startswith("obd:mem:"):
        base = base[len("obd:") :]
    match = _CHANNEL_RE.match(base)
    if match is None or match.group("prefix") not in CHANNEL_PREFIXES:
        return None
    return base


def _did_label(entry: Mapping[str, Any]) -> str:
    return f"DID 0x{int(entry['did']):04X} {entry.get('name')}"


def _resolve_catalog_entry(
    name: str,
    kind: str,
    build: Mapping[str, Mapping[str, Any]],
    renames: Sequence[Rename],
) -> tuple[Mapping[str, Any] | None, list[str]]:
    if name in build:
        return build[name], []
    for old, source in former_names(name, kind, renames):
        if old in build:
            return build[old], [
                f"stored name migrated from {old} by tools/channel_renames/{source}"
            ]
    return None, []


def _absent_status(via: tuple[str, ...], now: Mapping[str, Any] | None) -> str:
    """Absent from the build-time catalog: harmless only as an unused fallback."""
    return NEVER_CATALOGUED if via and now is None else "unresolved_at_build"


def _resolve_did(
    channel: str,
    name: str,
    via: tuple[str, ...],
    build: CatalogSnapshot,
    current: CatalogSnapshot,
    renames: Sequence[Rename],
) -> InputLineage:
    then, notes = _resolve_catalog_entry(name, "did", build.dids, renames)
    now = current.dids.get(name)
    if then is None:
        return InputLineage(
            channel,
            via,
            "did",
            _absent_status(via, now),
            identity_now=_did_label(now) if now else None,
            notes=(f"{name} names no DID in the catalog at {build.label}",),
        )
    key: IdentityKey = ("did", int(then["did"]))
    if now is None:
        return InputLineage(
            channel,
            via,
            "did",
            "retired",
            _did_label(then),
            None,
            key,
            (*notes, f"{name} is not in the current catalog"),
        )
    if int(now["did"]) != int(then["did"]):
        moved = current.did_by_id(int(then["did"]))
        return InputLineage(
            channel,
            via,
            "did",
            "identity_changed",
            _did_label(then),
            _did_label(now),
            key,
            (
                *notes,
                f"the name now reads DID 0x{int(now['did']):04X}; the bytes it was built on "
                f"(DID 0x{int(then['did']):04X}) are now catalogued as "
                f"{moved.get('name') if moved else 'nothing'}",
            ),
        )
    changed = _changed_keys(then, now, DID_DECODE_KEYS)
    if changed:
        return InputLineage(
            channel,
            via,
            "did",
            "decode_changed",
            _did_label(then),
            _did_label(now),
            key,
            (*notes, f"decode changed: {', '.join(changed)}"),
        )
    return InputLineage(
        channel, via, "did", "unchanged", _did_label(then), _did_label(now), key, tuple(notes)
    )


def _as_recorded(
    name: str,
    catalog: str,
    keys: Sequence[str],
    history: CatalogHistory,
    renames: Sequence[Rename],
    recorded_from: datetime | None,
    built_at: datetime,
) -> tuple[str | None, str]:
    """Check that one decode was committed across the recording window.

    For values the Pi decoded at record time and stored (RAM channels always;
    broadcast rows before P02.8), the artifact trained on whatever catalog the
    Pi held when each row was written. Returns ``(status, note)``; ``status``
    is ``None`` when one decode spans ``[first session, build]``.
    """
    if recorded_from is None:
        return "as_recorded_unknown", (
            "values were decoded at record time and the artifact names no dated "
            "session, so the recording window and its catalog are unknown"
        )
    specs = set()
    for snapshot in history.spanning(recorded_from, built_at):
        table = getattr(snapshot, catalog)
        entry, _ = _resolve_catalog_entry(name, catalog_kind(catalog), table, renames)
        if entry is not None:
            specs.add(_spec(entry, keys))
    if len(specs) > 1:
        return "as_recorded_varied", (
            f"{len(specs)} different decodes were committed between the first session "
            f"({recorded_from:%Y-%m-%d}) and the build; the stored values may carry any of them"
        )
    return None, (
        "trained on values the Pi decoded at record time; one decode was committed "
        "across the recording window (the Pi may have lagged the repository)"
    )


def catalog_kind(catalog: str) -> str:
    return {"memory": "memory", "dids": "did"}.get(catalog, catalog)


def _resolve_memory(
    channel: str,
    name: str,
    via: tuple[str, ...],
    build: CatalogSnapshot,
    current: CatalogSnapshot,
    renames: Sequence[Rename],
    history: CatalogHistory,
    recorded_from: datetime | None,
    built_at: datetime,
) -> InputLineage:
    base = name.removesuffix("_SENTINEL")
    then, notes = _resolve_catalog_entry(base, "memory", build.memory, renames)
    now = current.memory.get(base)

    def label(entry: Mapping[str, Any] | None) -> str | None:
        return None if entry is None else f"RAM 0x{int(entry['address']):08X} {entry['name']}"

    if then is None:
        return InputLineage(
            channel,
            via,
            "memory",
            _absent_status(via, now),
            identity_now=label(now),
            notes=(f"{base} names no RAM channel in the catalog at {build.label}",),
        )
    key: IdentityKey = ("memory", int(then["address"]))
    if now is None:
        return InputLineage(channel, via, "memory", "retired", label(then), None, key, tuple(notes))
    if int(now["address"]) != int(then["address"]):
        return InputLineage(
            channel, via, "memory", "identity_changed", label(then), label(now), key, tuple(notes)
        )
    changed = _changed_keys(then, now, MEMORY_DECODE_KEYS)
    if changed:
        notes.append(f"decode changed: {', '.join(changed)}")
        return InputLineage(
            channel, via, "memory", "decode_changed", label(then), label(now), key, tuple(notes)
        )
    # RAM channels are stored decoded, at every build date (no decode-on-read).
    status, note = _as_recorded(
        base, "memory", ("address", *MEMORY_DECODE_KEYS), history, renames, recorded_from, built_at
    )
    notes.append(note)
    return InputLineage(
        channel, via, "memory", status or "unchanged", label(then), label(now), key, tuple(notes)
    )


def _binding_label(entry: Mapping[str, Any]) -> str:
    start = int(entry.get("byte_start", 0))
    return (
        f"CAN 0x{int(entry['can_id']):03X}[{start}:{start + int(entry['byte_length'])}] "
        f"{entry['name']}"
    )


def _resolve_binding(
    channel: str,
    name: str,
    via: tuple[str, ...],
    build: CatalogSnapshot,
    current: CatalogSnapshot,
    history: CatalogHistory,
    recorded_from: datetime | None,
    built_at: datetime,
) -> InputLineage:
    then = build.bindings.get(name)
    now = current.bindings.get(name)
    if then is None:
        return InputLineage(
            channel,
            via,
            "binding",
            _absent_status(via, now),
            identity_now=_binding_label(now) if now else None,
            notes=(f"{name} names no binding in bindings.yaml at {build.label}",),
        )
    if now is None:
        return InputLineage(
            channel,
            via,
            "binding",
            "retired",
            _binding_label(then),
            None,
            None,
            (f"{name} was removed from bindings.yaml after the build",),
        )
    notes: list[str] = []
    if now.get("superseded_by"):
        notes.append(
            f"superseded_by {now['superseded_by']}: the same bytes under another name "
            "(an alias, not independent evidence)"
        )
    if _spec(then, BINDING_IDENTITY_KEYS) != _spec(now, BINDING_IDENTITY_KEYS):
        return InputLineage(
            channel,
            via,
            "binding",
            "identity_changed",
            _binding_label(then),
            _binding_label(now),
            None,
            tuple(notes),
        )
    changed = _changed_keys(then, now, BINDING_DECODE_KEYS)
    if changed:
        notes.append(f"decode changed: {', '.join(changed)}")
        return InputLineage(
            channel,
            via,
            "binding",
            "decode_changed",
            _binding_label(then),
            _binding_label(now),
            None,
            tuple(notes),
        )
    status: str | None = None
    if built_at < DECODE_ON_READ_SINCE:
        status, note = _as_recorded(
            name,
            "bindings",
            (*BINDING_IDENTITY_KEYS, *BINDING_DECODE_KEYS),
            history,
            (),
            recorded_from,
            built_at,
        )
        notes.append(f"built before decode-on-read (P02.8): {note}")
    if status is not None:
        return InputLineage(
            channel,
            via,
            "binding",
            status,
            _binding_label(then),
            _binding_label(now),
            None,
            tuple(notes),
        )
    return InputLineage(
        channel,
        via,
        "binding",
        "unchanged",
        _binding_label(then),
        _binding_label(now),
        None,
        tuple(notes),
    )


@dataclass(frozen=True)
class LineageContext:
    """Everything resolution needs besides the artifact itself."""

    history: CatalogHistory
    renames: tuple[Rename, ...]
    aliases: ObservationAliases
    #: The derived-channel inputs the code declares now (required groups).
    derived_inputs: Mapping[str, tuple[tuple[str, ...], ...]]
    #: Build instant -> the derived definitions committed then (``None`` if
    #: no derived module existed). Expansion uses these, never today's.
    derived_at: Callable[[datetime], DerivedDefinitions | None]


def resolve_input(
    channel: str,
    ctx: LineageContext,
    build: CatalogSnapshot,
    *,
    built_at: datetime,
    recorded_from: datetime | None,
    via: tuple[str, ...] = (),
) -> list[InputLineage]:
    """Leaf lineage for one channel (a ``derived:`` channel expands to its inputs)."""
    prefix, _, name = channel.partition(":")
    current = ctx.history.current
    if prefix == "derived":
        if channel in via:
            return []
        definitions = ctx.derived_at(built_at)
        groups = None if definitions is None else definitions.inputs.get(channel)
        where = "no derived module" if definitions is None else definitions.source
        if groups is None:
            return [
                InputLineage(
                    channel,
                    via,
                    "derived",
                    "definition_unlocated",
                    notes=(
                        f"no literal input declaration for {channel} in {where}; its "
                        "build-time inputs cannot be traced, so none is counted as unchanged",
                    ),
                )
            ]
        out: list[InputLineage] = []
        now = ctx.derived_inputs.get(channel)
        if now is None:
            out.append(
                InputLineage(
                    channel,
                    via,
                    "derived",
                    "retired",
                    notes=(f"declared in {where}; no longer declared now",),
                )
            )
        elif tuple(map(tuple, now)) != groups:
            out.append(
                InputLineage(
                    channel,
                    via,
                    "derived",
                    "definition_changed",
                    notes=(f"inputs at build ({where}): {groups}; now: {now}",),
                )
            )
        for group in groups:
            for member in group:
                out.extend(
                    resolve_input(
                        member,
                        ctx,
                        build,
                        built_at=built_at,
                        recorded_from=recorded_from,
                        via=(*via, channel),
                    )
                )
        return out
    if prefix == "did":
        return [_resolve_did(channel, name, via, build, current, ctx.renames)]
    if prefix == "mem":
        return [
            _resolve_memory(
                channel,
                name,
                via,
                build,
                current,
                ctx.renames,
                ctx.history,
                recorded_from,
                built_at,
            )
        ]
    if prefix in ("decoded", "dbc"):
        return [
            _resolve_binding(
                channel,
                name.removesuffix("_SENTINEL"),
                via,
                build,
                current,
                ctx.history,
                recorded_from,
                built_at,
            )
        ]
    if prefix == "obd":
        raw = _RAW_DID_RE.match(name)
        if raw is not None:
            did = int(raw.group("hex"), 16)
            did_then, did_now = build.did_by_id(did), current.did_by_id(did)
            return [
                InputLineage(
                    channel,
                    via,
                    "raw_did",
                    "unchanged",
                    _did_label(did_then) if did_then else f"DID 0x{did:04X}",
                    _did_label(did_now) if did_now else f"DID 0x{did:04X}",
                    ("did", did),
                    ("raw reply addressed by DID number; identity cannot drift",),
                )
            ]
        pid = MODE01_PIDS.get(name)
        if pid is not None:
            label = f"Mode 01 PID 0x{pid:02X} {name}"
            return [
                InputLineage(
                    channel,
                    via,
                    "mode01",
                    "unchanged",
                    label,
                    label,
                    ("mode01", pid),
                    ("J1979 PID; the name is the collector's PID-table entry",),
                )
            ]
        service = NON_PID_OBD_CHANNELS.get(name)
        if service is None and name.startswith(MISFIRE_COUNT_CHANNEL_PREFIX):
            service = "Mode 06 misfire monitor (per-cylinder count)"
        if service is not None:
            return [
                InputLineage(
                    channel,
                    via,
                    "obd_service",
                    "unchanged",
                    service,
                    service,
                    None,
                    ("not a data PID; identity is the OBD service",),
                )
            ]
        return [
            InputLineage(
                channel,
                via,
                "mode01",
                "unresolved_at_build",
                notes=(f"{name} is in neither the Mode 01 registry nor the PID decoder table",),
            )
        ]
    return [
        InputLineage(
            channel,
            via,
            "out_of_scope",
            "unchanged",
            notes=(f"{prefix}: is decoded by no DID, RAM or broadcast catalog",),
        )
    ]


def _trim_check(leaf: InputLineage, aliases: ObservationAliases) -> InputLineage:
    """Check a leaf with a declared trim claim against the register its bytes read."""
    claimed = aliases.trim_claims.get(leaf.channel)
    if claimed is None or leaf.identity_key is None or leaf.status in AFFECTED_STATUSES:
        return leaf
    qualifies, why = trim_qualification(leaf.identity_key, claimed, aliases)
    if qualifies:
        status, note = leaf.status, f"trim qualifies: {why}"
    elif aliases.register_of(leaf.identity_key) is None:
        status, note = "trim_register_unknown", f"trim claim not checkable: {why}"
    else:
        status, note = "trim_identity_refused", f"cannot qualify as {claimed}: {why}"
    return InputLineage(
        leaf.channel,
        leaf.via,
        leaf.kind,
        status,
        leaf.identity_at_build,
        leaf.identity_now,
        leaf.identity_key,
        (*leaf.notes, note),
    )


# --------------------------------------------------------------------------
# Artifacts
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Artifact:
    """One built thing whose inputs are audited."""

    path: str
    kind: str  # "behavior_model" | "detector_manifest"
    name: str
    built_at: datetime | None
    built_at_source: str
    inputs: tuple[str, ...]
    sessions: tuple[str, ...]


def _walk_strings(node: Any) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, Mapping):
        for value in node.values():
            yield from _walk_strings(value)
    elif isinstance(node, list | tuple):
        for value in node:
            yield from _walk_strings(value)


def _parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def behavior_artifacts(path: Path, *, label: str | None = None) -> list[Artifact]:
    """One :class:`Artifact` per model in a ``behavior_models.yaml``."""
    doc = yaml.safe_load(path.read_text()) or {}
    out: list[Artifact] = []
    for model in doc.get("models") or []:
        recipe = {k: v for k, v in model.items() if k not in ("fit", "validation", "description")}
        channels = sorted(
            {c for c in (normalise_channel(s) for s in _walk_strings(recipe)) if c is not None}
        )
        fit = model.get("fit") or {}
        sessions = tuple(
            str(s).split("#", 1)[0]
            for s in [*(fit.get("sessions") or []), *(fit.get("calibration_sessions") or [])]
        )
        built = _parse_time(fit.get("fitted_at"))
        out.append(
            Artifact(
                path=label or str(path),
                kind="behavior_model",
                name=f"{model['name']} v{model.get('version', '?')}",
                built_at=built,
                built_at_source="fit.fitted_at" if built else "absent",
                inputs=tuple(channels),
                sessions=tuple(dict.fromkeys(sessions)),
            )
        )
    return out


def manifest_artifact(path: Path, *, label: str | None = None) -> Artifact:
    """An IF/sequence ``model.manifest.json`` as an :class:`Artifact`."""
    manifest = json.loads(path.read_text())
    schema = manifest.get("schema") or {}
    columns = [
        *(schema.get("feature_columns") or []),
        *(schema.get("behavior_model_columns") or []),
    ]
    channels = sorted({c for c in (normalise_channel(str(col)) for col in columns) if c})
    built = _parse_time(manifest.get("created_at"))
    sessions = [
        *(manifest.get("training_sessions") or []),
        *(manifest.get("calibration_sessions") or []),
    ]
    return Artifact(
        path=label or str(path),
        kind="detector_manifest",
        name=str(manifest.get("model_version") or manifest.get("model_type") or path.parent.name),
        built_at=built,
        built_at_source="created_at" if built else "absent",
        inputs=tuple(channels),
        sessions=tuple(dict.fromkeys(str(s).split("#", 1)[0] for s in sessions)),
    )


@dataclass(frozen=True)
class ArtifactLineage:
    artifact: Artifact
    verdict: str  # affected | unaffected | unverifiable
    catalog_at_build: str | None
    leaves: tuple[InputLineage, ...]
    reasons: tuple[str, ...]

    def merged_inputs(self) -> list[dict[str, Any]]:
        """One record per resolved channel, with every derived path that reaches it."""
        merged: dict[tuple[str, str], dict[str, Any]] = {}
        for leaf in self.leaves:
            if leaf.kind == "out_of_scope":
                continue
            record = leaf.as_dict()
            record.pop("via", None)
            entry = merged.setdefault((leaf.channel, leaf.status), {**record, "via": []})
            if leaf.via and " <- ".join(leaf.via) not in entry["via"]:
                entry["via"].append(" <- ".join(leaf.via))
            elif not leaf.via:
                entry["named_directly"] = True
        return [merged[key] for key in sorted(merged)]

    def as_dict(self) -> dict[str, Any]:
        merged = self.merged_inputs()
        counts = Counter(record["status"] for record in merged)
        return {
            "path": self.artifact.path,
            "kind": self.artifact.kind,
            "name": self.artifact.name,
            "built_at": self.artifact.built_at.isoformat() if self.artifact.built_at else None,
            "built_at_source": self.artifact.built_at_source,
            "catalog_at_build": self.catalog_at_build,
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "input_status_counts": dict(sorted(counts.items())),
            "inputs": [r for r in merged if _detailed(r)],
            "unchanged_inputs": sorted(
                f"{r['channel']} = {r['identity_at_build']}" for r in merged if not _detailed(r)
            ),
            "out_of_scope_inputs": sorted(
                {leaf.channel for leaf in self.leaves if leaf.kind == "out_of_scope"}
            ),
            "session_count": len(self.artifact.sessions),
        }


def _detailed(record: Mapping[str, Any]) -> bool:
    """Whether a merged input record carries more than "same identity, same decode"."""
    if record["status"] not in ("unchanged", NEVER_CATALOGUED):
        return True
    notes = " ".join(record.get("notes", []))
    return record["kind"] in ("did", "memory", "raw_did") or "trim" in notes or "supersed" in notes


def audit_artifact(artifact: Artifact, ctx: LineageContext) -> ArtifactLineage:
    """Resolve every input of one artifact and give its verdict."""
    if artifact.built_at is None:
        return ArtifactLineage(
            artifact,
            "unverifiable",
            None,
            (),
            ("build time unknown: no catalog to resolve against",),
        )
    build = ctx.history.at(artifact.built_at)
    if build is None:
        return ArtifactLineage(
            artifact, "unverifiable", None, (), ("built before any committed catalog",)
        )
    starts = [t for t in (session_started_at(s) for s in artifact.sessions) if t is not None]
    recorded_from = min(starts) if starts else None
    leaves: list[InputLineage] = []
    for channel in artifact.inputs:
        for leaf in resolve_input(
            channel, ctx, build, built_at=artifact.built_at, recorded_from=recorded_from
        ):
            leaves.append(_trim_check(leaf, ctx.aliases))
    unique = tuple(dict.fromkeys(leaves))
    affected = [leaf for leaf in unique if leaf.status in AFFECTED_STATUSES]
    unverifiable = [leaf for leaf in unique if leaf.status in UNVERIFIABLE_STATUSES]
    if affected:
        verdict = "affected"
        reasons = tuple(sorted({f"{leaf.channel}: {leaf.status}" for leaf in affected}))
    elif unverifiable:
        verdict = "unverifiable"
        reasons = tuple(sorted({f"{leaf.channel}: {leaf.status}" for leaf in unverifiable}))
    else:
        verdict = "unaffected"
        reasons = ("every input resolves to the same source identity and decode as at build",)
    return ArtifactLineage(artifact, verdict, build.label, unique, reasons)


# --------------------------------------------------------------------------
# Catalog correction ledger
# --------------------------------------------------------------------------


def correction_ledger(history: CatalogHistory) -> list[dict[str, Any]]:
    """Identity and decode changes between consecutive catalog revisions (and now)."""
    events: list[dict[str, Any]] = []
    revisions = [*history.snapshots, history.current]
    for before, after in zip(revisions, revisions[1:], strict=False):
        stamp = after.committed_at.isoformat() if after.committed_at else "uncommitted"
        ids_before = {int(e["did"]): e for e in before.dids.values() if "did" in e}
        ids_after = {int(e["did"]): e for e in after.dids.values() if "did" in e}
        for did in sorted(ids_before.keys() & ids_after.keys()):
            a, b = ids_before[did], ids_after[did]
            changed = _changed_keys(a, b, DID_DECODE_KEYS)
            if a["name"] != b["name"] or changed:
                events.append(
                    {
                        "revision": after.label,
                        "at": stamp,
                        "catalog": "did",
                        "key": f"0x{did:04X}",
                        "name_before": a["name"],
                        "name_after": b["name"],
                        "decode_changed": changed,
                    }
                )
        for name in sorted(before.dids.keys() & after.dids.keys()):
            a, b = before.dids[name], after.dids[name]
            if "did" in a and "did" in b and int(a["did"]) != int(b["did"]):
                events.append(
                    {
                        "revision": after.label,
                        "at": stamp,
                        "catalog": "did",
                        "key": name,
                        "name_now_reads": f"0x{int(b['did']):04X}",
                        "name_used_to_read": f"0x{int(a['did']):04X}",
                    }
                )
        names_before = {int(e["address"]): n for n, e in before.memory.items() if "address" in e}
        names_after = {int(e["address"]): n for n, e in after.memory.items() if "address" in e}
        shared = Counter(int(e["address"]) for e in before.memory.values() if "address" in e)
        shared.update(int(e["address"]) for e in after.memory.values() if "address" in e)
        for address in sorted(names_before.keys() & names_after.keys()):
            # An address two entries share in one revision names no single channel.
            if shared[address] > 2:
                continue
            if names_before[address] != names_after[address]:
                events.append(
                    {
                        "revision": after.label,
                        "at": stamp,
                        "catalog": "memory",
                        "key": f"0x{address:08X}",
                        "name_before": names_before[address],
                        "name_after": names_after[address],
                    }
                )
        for name in sorted(before.memory.keys() & after.memory.keys()):
            a, b = before.memory[name], after.memory[name]
            changed = _changed_keys(a, b, MEMORY_DECODE_KEYS)
            if int(a["address"]) != int(b["address"]) or changed:
                events.append(
                    {
                        "revision": after.label,
                        "at": stamp,
                        "catalog": "memory",
                        "key": name,
                        "address_before": f"0x{int(a['address']):08X}",
                        "address_after": f"0x{int(b['address']):08X}",
                        "decode_changed": changed,
                    }
                )
        for name in sorted(before.bindings.keys()):
            bound = before.bindings[name]
            rebound = after.bindings.get(name)
            if rebound is None:
                events.append(
                    {
                        "revision": after.label,
                        "at": stamp,
                        "catalog": "binding",
                        "key": name,
                        "removed": True,
                    }
                )
                continue
            changed = _changed_keys(bound, rebound, (*BINDING_IDENTITY_KEYS, *BINDING_DECODE_KEYS))
            superseded = rebound.get("superseded_by") and not bound.get("superseded_by")
            if changed or superseded:
                events.append(
                    {
                        "revision": after.label,
                        "at": stamp,
                        "catalog": "binding",
                        "key": name,
                        "changed": changed,
                        **({"superseded_by": rebound["superseded_by"]} if superseded else {}),
                    }
                )
    return events


# --------------------------------------------------------------------------
# Re-decoding stored raw samples
# --------------------------------------------------------------------------


def find_session(name: str, roots: Sequence[Path]) -> Path | None:
    for root in roots:
        for candidate in (root / name, root / "_archive" / name):
            if candidate.is_dir():
                return candidate
    return None


def session_obd_rows(session: Path, channels: Sequence[str]) -> list[tuple[Any, ...]]:
    import duckdb

    files = sorted(str(p) for p in session.glob("*.parquet"))
    if not files or not channels:
        return []
    con = duckdb.connect()
    try:
        relation = con.read_parquet(files, union_by_name=True)
        columns = set(relation.columns)
        payload = "payload" if "payload" in columns else "NULL::BLOB"
        text = "value_text" if "value_text" in columns else "NULL::VARCHAR"
        listed = ", ".join("'" + c.replace("'", "''") + "'" for c in channels)
        rows = con.execute(
            f"SELECT channel, {payload}, value_number, {text} FROM relation "  # noqa: S608
            f"WHERE source = 'obd' AND channel IN ({listed})"
        ).fetchall()
    finally:
        con.close()
    return [tuple(row) for row in rows]


@dataclass
class Mode01Recheck:
    """Stored Mode 01 values against the PID decoder applied to their payloads."""

    rows: int = 0
    with_payload: int = 0
    agree: int = 0
    disagree: int = 0
    undecodable: int = 0
    examples: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "with_payload": self.with_payload,
            "agree": self.agree,
            "disagree": self.disagree,
            "undecodable": self.undecodable,
            "examples": self.examples[:5],
        }


def recheck_mode01(rows: Iterable[tuple[Any, ...]], *, tolerance: float = 1e-6) -> Mode01Recheck:
    out = Mode01Recheck()
    for channel, payload, value, _text in rows:
        pid = MODE01_PIDS.get(str(channel))
        if pid is None:
            continue
        out.rows += 1
        if payload is None:
            continue
        out.with_payload += 1
        decoder = mode01_decoder(pid)
        decoded = None if decoder is None else decoder(bytes(payload))
        if decoded is None or value is None:
            out.undecodable += 1
            continue
        if abs(float(decoded[0]) - float(value)) <= tolerance:
            out.agree += 1
        else:
            out.disagree += 1
            out.examples.append(
                f"{channel} {bytes(payload).hex()} stored {value} decodes {decoded[0]}"
            )
    return out


def _decode_did(entry: Mapping[str, Any] | None, payload_hex: Any) -> float | None:
    if entry is None or not isinstance(payload_hex, str) or not payload_hex:
        return None
    try:
        data = bytes.fromhex(payload_hex)
    except ValueError:
        return None
    start = int(entry.get("byte_start", 0))
    length = int(entry["byte_length"])
    if len(data) < start + length:
        return None
    raw = int.from_bytes(
        data[start : start + length],
        str(entry.get("byte_order", "big")),  # type: ignore[arg-type]
        signed=bool(entry.get("signed", False)),
    )
    scale = entry.get("scale")
    if scale is None:
        return float(raw)
    return raw * float(scale) + float(entry.get("offset") or 0.0)


def _memory_value(entry: Mapping[str, Any], payload: bytes, offset: int) -> float | None:
    start = offset + int(entry.get("byte_start", 0))
    length = int(entry["byte_length"])
    if offset < 0 or len(payload) < start + length:
        return None
    raw = int.from_bytes(
        payload[start : start + length],
        str(entry.get("byte_order", "big")),  # type: ignore[arg-type]
        signed=bool(entry.get("signed", False)),
    )
    sentinel = entry.get("sentinel_raw")
    if sentinel is not None and raw == int(sentinel):
        return None
    scale = entry.get("scale")
    if scale is None:
        return float(raw)
    return raw * float(scale) + float(entry.get("offset") or 0.0)


@dataclass
class MemoryRecheck:
    """Stored RAM values against the stored window bytes, then and now.

    A stored ``mem:`` row keeps the whole window reply, and the window's start
    depended on which channels the capture selected, so a channel's offset in
    the reply is not recorded. It is recovered as the offset at which the
    catalog in force at recording reproduces the stored value (any window
    member's address is a candidate start); those bytes are then decoded with
    the current entry. A row no candidate reproduces is ``offset_unresolved``.
    """

    rows: int = 0
    with_payload: int = 0
    reproduced_at_recording: int = 0
    agree_now: int = 0
    disagree_now: int = 0
    offset_unresolved: int = 0
    examples: list[str] = field(default_factory=list)

    def add(self, other: MemoryRecheck) -> None:
        for name in (
            "rows",
            "with_payload",
            "reproduced_at_recording",
            "agree_now",
            "disagree_now",
            "offset_unresolved",
        ):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.examples.extend(other.examples)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "with_payload": self.with_payload,
            "reproduced_at_recording": self.reproduced_at_recording,
            "agree_now": self.agree_now,
            "disagree_now": self.disagree_now,
            "offset_unresolved": self.offset_unresolved,
            "examples": self.examples[:5],
        }


def recheck_memory(
    rows: Iterable[tuple[Any, ...]],
    name: str,
    recorded: CatalogSnapshot,
    current: CatalogSnapshot,
    renames: Sequence[Rename],
    *,
    tolerance: float = 1e-6,
) -> MemoryRecheck:
    """Re-decode one RAM channel's stored rows (see :class:`MemoryRecheck`)."""
    out = MemoryRecheck()
    then, _ = _resolve_catalog_entry(name, "memory", recorded.memory, renames)
    now = current.memory.get(name)
    stored_names = {
        f"mem:{name}",
        *(f"mem:{old}" for old, _ in former_names(name, "memory", renames)),
    }
    if then is None or now is None:
        return out
    window = then.get("window")
    starts = sorted(
        {
            int(e["address"])
            for e in recorded.memory.values()
            if window is not None and e.get("window") == window
        }
        | {int(then["address"])}
    )
    offsets = [int(then["address"]) - start for start in starts if start <= int(then["address"])]
    for channel, payload, value, _text in rows:
        if str(channel) not in stored_names:
            continue
        out.rows += 1
        if payload is None or value is None:
            continue
        out.with_payload += 1
        data = bytes(payload)
        matches = [
            o
            for o in offsets
            if (v := _memory_value(then, data, o)) is not None
            and abs(v - float(value)) <= tolerance
        ]
        if not matches:
            out.offset_unresolved += 1
            continue
        out.reproduced_at_recording += 1
        current_values = {_memory_value(now, data, o) for o in matches}
        if (
            len(current_values) == 1
            and (cv := current_values.pop()) is not None
            and abs(cv - float(value)) <= tolerance
        ):
            out.agree_now += 1
        else:
            out.disagree_now += 1
            out.examples.append(
                f"{channel} {data.hex()} stored {value} now {sorted(map(str, current_values))}"
            )
    return out


def redecode_raw_dids(
    rows: Iterable[tuple[Any, ...]], build: CatalogSnapshot, current: CatalogSnapshot
) -> dict[str, dict[str, Any]]:
    """Each raw ``DID_<hex>`` reply decoded with the build-time and the current entry."""
    grouped: dict[str, list[str]] = {}
    for channel, _payload, _value, text in rows:
        if _RAW_DID_RE.match(str(channel)):
            grouped.setdefault(str(channel), []).append(str(text) if text is not None else "")
    out: dict[str, dict[str, Any]] = {}
    for channel, payloads in sorted(grouped.items()):
        did = int(channel[4:], 16)
        then, now = build.did_by_id(did), current.did_by_id(did)
        old = [_decode_did(then, p) for p in payloads]
        new = [_decode_did(now, p) for p in payloads]
        old_ok = [v for v in old if v is not None]
        new_ok = [v for v in new if v is not None]
        out[channel] = {
            "replies": len(payloads),
            "reply_bytes": sorted({len(p) // 2 for p in payloads}),
            "build_entry": _did_label(then) if then else None,
            "build_decoded": len(old_ok),
            "build_range": [min(old_ok), max(old_ok)] if old_ok else None,
            "current_entry": _did_label(now) if now else None,
            "current_decoded": len(new_ok),
            "current_range": [min(new_ok), max(new_ok)] if new_ok else None,
            "identical": old == new,
        }
    return out


# --------------------------------------------------------------------------
# Session sidecars: names stored beside a request identity
# --------------------------------------------------------------------------


def stale_sidecar_labels(
    doc: Any, current: CatalogSnapshot, *, path: str = ""
) -> list[dict[str, str]]:
    """Name/identity pairs in a sidecar whose name no longer means that DID.

    A capture manifest stores each request twice: by identity
    (``identifier: 0x1102`` / ``transport_channel: DID_1102``) and by the
    name the catalog gave it then (``profile_channel`` / ``channel``). The
    identity is ground truth; a name that the current catalog gives to a
    different DID is a stale label.
    """
    found: list[dict[str, str]] = []
    if isinstance(doc, Mapping):
        did = None
        identifier = doc.get("identifier")
        transport = doc.get("transport_channel") or doc.get("channel")
        if isinstance(identifier, str) and doc.get("kind") == "did":
            did = int(identifier, 16)
        elif isinstance(transport, str) and _RAW_DID_RE.match(transport):
            did = int(transport[4:], 16)
        if did is not None:
            for key in ("profile_channel", "channel"):
                name = doc.get(key)
                if not isinstance(name, str) or _RAW_DID_RE.match(name):
                    continue
                entry = current.dids.get(name)
                actual = current.did_by_id(did)
                if entry is not None and int(entry["did"]) != did:
                    found.append(
                        {
                            "at": path or "/",
                            "request": f"DID 0x{did:04X}",
                            "stored_name": name,
                            "stored_name_now_reads": f"DID 0x{int(entry['did']):04X}",
                            "request_is_now": str(actual.get("name")) if actual else "uncatalogued",
                        }
                    )
        for key, value in doc.items():
            found.extend(stale_sidecar_labels(value, current, path=f"{path}/{key}"))
    elif isinstance(doc, list):
        for index, value in enumerate(doc):
            found.extend(stale_sidecar_labels(value, current, path=f"{path}/{index}"))
    return found


# --------------------------------------------------------------------------
# Historical derived definitions
# --------------------------------------------------------------------------


def summarize(lineages: Sequence[ArtifactLineage]) -> dict[str, int]:
    counts = Counter(lineage.verdict for lineage in lineages)
    return {key: counts.get(key, 0) for key in ("affected", "unaffected", "unverifiable")}


def json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, set | frozenset | tuple):
        return sorted(value) if isinstance(value, set | frozenset) else list(value)
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


# --------------------------------------------------------------------------
# Build-time derived definitions (parsed, never imported)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DerivedDefinitions:
    """Derived-channel input groups as one revision of the source declared them."""

    source: str
    inputs: Mapping[str, tuple[tuple[str, ...], ...]]


def _string_of(node: ast.AST, constants: Mapping[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


def _groups_of(
    nodes: Sequence[ast.AST], constants: Mapping[str, str]
) -> tuple[tuple[str, ...], ...] | None:
    groups: list[tuple[str, ...]] = []
    for node in nodes:
        if not isinstance(node, ast.Tuple):
            return None
        members = [_string_of(element, constants) for element in node.elts]
        if not members or any(m is None for m in members):
            return None
        groups.append(tuple(m for m in members if m is not None))
    return tuple(groups) if groups else None


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def parse_derived_definitions(source: str) -> dict[str, tuple[tuple[str, ...], ...]]:
    """Literal derived-channel input declarations in one revision of the source.

    Three forms have existed, in order of authority: the
    ``DERIVED_CHANNEL_INPUTS`` dict's literal entries, ``DerivedSignal(name,
    groups, ...)`` registry entries (R05.4) and the older
    ``_append_dependency_gap(out, name, *groups)`` calls. A channel declared
    any other way (a comprehension, a vehicle module, local variables) is
    absent from the result: *not located*, which callers must not read as
    "no inputs".
    """
    tree = ast.parse(source)
    constants: dict[str, str] = {}
    for statement in tree.body:
        targets: list[ast.expr] = []
        if isinstance(statement, ast.Assign):
            targets, value = list(statement.targets), statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            targets, value = [statement.target], statement.value
        else:
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            for target in targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = value.value
    declared: dict[str, tuple[tuple[str, ...], ...]] = {}
    registry: dict[str, tuple[tuple[str, ...], ...]] = {}
    gaps: dict[str, tuple[tuple[str, ...], ...]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign | ast.AnnAssign):
            target = node.targets[0] if isinstance(node, ast.Assign) else node.target
            if (
                isinstance(target, ast.Name)
                and target.id == "DERIVED_CHANNEL_INPUTS"
                and isinstance(node.value, ast.Dict)
            ):
                for key, value in zip(node.value.keys, node.value.values, strict=True):
                    name = None if key is None else _string_of(key, constants)
                    if name is None or not isinstance(value, ast.Tuple):
                        continue
                    groups = _groups_of(value.elts, constants)
                    if groups is not None:
                        declared[name] = groups
        elif isinstance(node, ast.Call):
            called = _call_name(node)
            if called == "DerivedSignal" and len(node.args) >= 2:
                name = _string_of(node.args[0], constants)
                arg = node.args[1]
                groups = _groups_of(arg.elts, constants) if isinstance(arg, ast.Tuple) else None
                if name is not None and groups is not None:
                    registry.setdefault(name, groups)
            elif called == "_append_dependency_gap" and len(node.args) >= 3:
                name = _string_of(node.args[1], constants)
                groups = _groups_of(node.args[2:], constants)
                if name is not None and groups is not None:
                    gaps.setdefault(name, groups)
    return {**gaps, **registry, **declared}


def derived_definitions_loader(
    repo: Path,
) -> Callable[[datetime], DerivedDefinitions | None]:
    """``when -> DerivedDefinitions`` from the derived module committed before ``when``."""
    cache: dict[str, DerivedDefinitions | None] = {}

    def load(when: datetime) -> DerivedDefinitions | None:
        rev = _git(repo, "rev-list", "-1", f"--before={when.isoformat()}", "HEAD").strip()
        if not rev:
            return None
        if rev not in cache:
            found = None
            for path in DERIVED_MODULE_PATHS:
                text = _git_show(repo, rev, path)
                if text is not None:
                    found = DerivedDefinitions(f"{rev[:7]}:{path}", parse_derived_definitions(text))
                    break
            cache[rev] = found
        return cache[rev]

    return load
