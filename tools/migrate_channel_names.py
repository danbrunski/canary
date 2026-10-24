#!/usr/bin/env python3
"""One-time migration of renamed DID and memory channel names in stored artifacts.

The 2026-09-18 fuel relabel renamed three wrongly-identified DIDs (0x1208 is
air charge, 0x1206/0x1207 are the injected pulse, 0x1134 is an enrichment
target) and, by operator decision, kept **no aliases**: the wrong names are
gone from ``data/dids.yaml``. Anything stored under an old name must therefore
be rewritten, or it stops matching what the live pipeline emits — and a
capture profile or ``collection.obd_dids`` entry still naming an old DID makes
``parse_did_token`` refuse it at daemon start.

The rename map is an input (``--map``, default
``tools/channel_renames/2026-09-18-fuel-relabel.yaml``): ``channels`` maps bare
DID names, which also covers their ``did:<NAME>`` column form and the derived
feature columns ``did:<NAME>__<suffix>``; ``identifiers`` maps exact
non-channel names (the behavior-model recipe fitted on 0x1208);
``memory_channels`` maps service-0x23 RAM channel names from the
``data/dids.yaml`` ``memory:`` catalog. A memory name is stored bare (a capture
profile's ``memory:`` and ``kline:`` lists), as the reading ``mem:<NAME>`` (its
fuel-cut state ``mem:<NAME>_SENTINEL``), as the wide column ``obd:mem:<NAME>``
(and ``obd:mem:<NAME>__<suffix>``), and on K-line as ``<module>:MEM_<NAME>``,
pre-K01.1 ``MEM_<NAME>`` and the capture key ``kline:<NAME>``; all are covered.

What gets rewritten, and how:

* **Session telemetry Parquet** (``telemetry-*.parquet`` anywhere under a
  sessions root, captures and archives included): exact ``channel`` values
  only. Every other column, row order, the schema and its metadata, and the
  file's compression are preserved.
* **JSON / JSONL** (session sidecars such as ``digest.json``,
  ``capture_profile.json``, ``capture_quality.json``; model
  ``*.manifest.json``): keys and string values that are *exactly* a mapped
  name. Prose that merely mentions a name (operator notes, tags) is left alone.
* **YAML** (behavior models, capture profiles, the daemon config): token
  replacement on the text, so comments and layout survive.
* **Model binaries** (``*.joblib``, ``*.pt`` under a models dir): the
  embedded manifest/scaler — the copy the detectors actually load — is
  rewritten exactly like a JSON sidecar. Weights are untouched.

Dry run by default. ``--apply`` writes, each file via a temp file + rename in
the same directory (mode and, when permitted, owner preserved), and refuses
unless ``--daemon-stopped`` is given *and* no ``python -m src.main`` process is
running: the daemon appends to session directories and hot-reloads models and
profiles, so migrating under it races its writes. Re-running is a no-op.

Usage (from the repo root, so relative config paths resolve as the daemon's):
  tools/migrate_channel_names.py --config /etc/canary/canary.yaml
  tools/migrate_channel_names.py --config /etc/canary/canary.yaml --apply --daemon-stopped
  tools/migrate_channel_names.py --sessions-root ~/canary-data/sessions-0918 \\
      --file canary.laptop.yaml --apply --daemon-stopped
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.yaml_load import load_yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MAP = Path(__file__).resolve().parent / "channel_renames" / "2026-09-18-fuel-relabel.yaml"
DEFAULT_DIDS = REPO_ROOT / "data" / "dids.yaml"
DID_PREFIX = "did:"
#: Separator between a base column and its derived-feature suffix
#: (``did:ECM_AIR_CHARGE__mean``) — see ``src.preprocessing.features``.
FEATURE_SEP = "__"
PARQUET_GLOB = "telemetry-*.parquet"
#: Model payload entries that hold weights / fitted estimators, never names.
_BINARY_SKIP_KEYS = frozenset({"state_dict", "models"})
EXIT_OK = 0
EXIT_INCOMPLETE = 1
EXIT_REFUSED = 2


#: What precedes a memory channel's name in its stored forms: the
#: service-0x23 reading ``mem:<NAME>`` (source ``obd``) and its wide column
#: ``obd:mem:<NAME>``; on K-line (``src.collection.kline.names``) the capture
#: key ``kline:<NAME>``, the value row ``<module>:MEM_<NAME>`` (wide column
#: ``kline:<module>:MEM_<NAME>``) and the pre-K01.1 row ``MEM_<NAME>``.
_MEMORY_HEAD = re.compile(r"(?:obd:)?mem:|kline:(?:[A-Za-z0-9]+:MEM_)?|(?:[A-Za-z0-9]+:)?MEM_")
#: Suffix of the state channel a memory sentinel decodes to
#: (``src.decode.slices.SENTINEL_SUFFIX``): ``mem:<NAME>_SENTINEL``.
SENTINEL_SUFFIX = "_SENTINEL"


class MapError(ValueError):
    """The rename map is malformed or disagrees with the DID catalog."""


@dataclass(frozen=True)
class RenameMap:
    channels: dict[str, str]
    identifiers: dict[str, str] = field(default_factory=dict)
    #: Service-0x23 RAM channel names (the ``data/dids.yaml`` ``memory:`` catalog).
    memory: dict[str, str] = field(default_factory=dict)

    def rename(self, value: str) -> str:
        """The migrated form of one exact name, or ``value`` unchanged."""
        if value in self.channels:
            return self.channels[value]
        if value in self.identifiers:
            return self.identifiers[value]
        if value in self.memory:
            return self.memory[value]
        if value.startswith(DID_PREFIX):
            base, sep, suffix = value[len(DID_PREFIX) :].partition(FEATURE_SEP)
            new = self.channels.get(base)
            if new is not None:
                return f"{DID_PREFIX}{new}{sep}{suffix}"
        if self.memory:
            head = _MEMORY_HEAD.match(value)
            if head is not None:
                renamed = self._rename_memory_rest(value[head.end() :])
                if renamed is not None:
                    return f"{head.group(0)}{renamed}"
        return value

    def _rename_memory_rest(self, rest: str) -> str | None:
        """``<OLD>[_SENTINEL][__suffix]`` with the new name, else ``None``."""
        base, sep, suffix = rest.partition(FEATURE_SEP)
        tail = ""
        if base not in self.memory and base.endswith(SENTINEL_SUFFIX):
            base, tail = base[: -len(SENTINEL_SUFFIX)], SENTINEL_SUFFIX
        new = self.memory.get(base)
        return None if new is None else f"{new}{tail}{sep}{suffix}"

    def token_pattern(self) -> re.Pattern[str]:
        """Old names as whole tokens in free text (YAML).

        A token may be followed by a ``__suffix`` (derived feature) but not by
        any other identifier character, so ``ECM_INJECTOR_PW_B1X`` is not
        ``ECM_INJECTOR_PW_B1``. A memory name may also be followed by
        ``_SENTINEL`` (``mem:<NAME>_SENTINEL`` in a spec); a DID name may not.
        """
        alternatives: list[str] = []
        names = sorted({*self.channels, *self.identifiers}, key=len, reverse=True)
        if names:
            alternation = "|".join(re.escape(name) for name in names)
            alternatives.append(rf"(?:{alternation})(?=__|[^A-Za-z0-9_]|$)")
        memory = sorted(self.memory, key=len, reverse=True)
        if memory:
            alternation = "|".join(re.escape(name) for name in memory)
            alternatives.append(
                rf"(?:{alternation})"
                rf"(?=__|{SENTINEL_SUFFIX}(?![A-Za-z0-9])|[^A-Za-z0-9_]|$)"
            )
        if not alternatives:
            return re.compile(r"(?!)")  # an empty map renames nothing
        return re.compile(rf"(?<![A-Za-z0-9_])(?:{'|'.join(alternatives)})")

    def replacement(self, token: str) -> str:
        return self.channels.get(token) or self.identifiers.get(token) or self.memory[token]


def _str_map(raw: Any, key: str) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and k and v for k, v in raw.items()
    ):
        raise MapError(f"{key} must map non-empty names to non-empty names")
    return dict(raw)


#: The sections a rename map may carry.
MAP_SECTIONS = ("channels", "identifiers", "memory_channels")


def load_rename_map(path: Path) -> RenameMap:
    raw = load_yaml(path.read_text())
    if not isinstance(raw, dict) or set(raw) - set(MAP_SECTIONS):
        raise MapError(f"{path}: expected only {', '.join(repr(s) for s in MAP_SECTIONS)}")
    channels = _str_map(raw.get("channels"), "channels")
    identifiers = _str_map(raw.get("identifiers"), "identifiers")
    memory = _str_map(raw.get("memory_channels"), "memory_channels")
    if not channels and not identifiers and not memory:
        raise MapError(f"{path}: empty rename map")
    olds = [*channels, *identifiers, *memory]
    if len(set(olds)) != len(olds):
        raise MapError(f"{path}: a name appears in more than one section")
    news = {*channels.values(), *identifiers.values(), *memory.values()}
    unchanged = sorted(k for k in olds if k in news)
    if unchanged:
        # A chain (A->B, B->C) or a no-op would make the result depend on
        # order and break idempotence.
        raise MapError(f"{path}: names are both renamed and rename targets: {unchanged}")
    return RenameMap(channels=channels, identifiers=identifiers, memory=memory)


def check_against_catalog(rename_map: RenameMap, dids_path: Path) -> list[str]:
    """Why this map disagrees with the DID catalog; empty when it agrees.

    Every target must be a current DID name, and no source may still resolve
    (as a name or alias) — otherwise the migration would write names the
    daemon cannot poll, or strand ones it still emits. ``memory_channels`` is
    held to the same rule against the catalog's ``memory:`` section: the target
    is a memory channel, and the source no longer names any channel (memory
    and DID names share one namespace, ``DidMap._unique_channel_names``).
    """
    raw = load_yaml(dids_path.read_text())
    current: set[str] = set()
    former: set[str] = set()
    for entry in raw.get("dids", []):
        current.add(str(entry["name"]))
        former.update(str(alias) for alias in entry.get("aliases", []) or [])
    memory = {str(entry["name"]) for entry in raw.get("memory", []) or []}
    problems = [
        f"target {new!r} is not a DID name in {dids_path}"
        for new in rename_map.channels.values()
        if new not in current
    ]
    problems += [
        f"source {old!r} still resolves in {dids_path}"
        for old in rename_map.channels
        if old in current or old in former
    ]
    problems += [
        f"memory target {new!r} is not a memory channel in {dids_path}"
        for new in rename_map.memory.values()
        if new not in memory
    ]
    problems += [
        f"memory source {old!r} still resolves in {dids_path}"
        for old in rename_map.memory
        if old in memory or old in current or old in former
    ]
    return problems


# --- rewriting ----------------------------------------------------------------


def rewrite_structure(obj: Any, rename_map: RenameMap) -> tuple[Any, int]:
    """Rename exact-match keys and string values; returns ``(obj, count)``.

    Copy-on-write: a container with nothing to rename is returned as the same
    object, so untouched subtrees (and their types) survive.
    """
    if isinstance(obj, str):
        new = rename_map.rename(obj)
        return new, int(new != obj)
    if isinstance(obj, dict):
        total = 0
        items: list[tuple[Any, Any]] = []
        for key, value in obj.items():
            new_key, key_count = (
                rewrite_structure(key, rename_map) if isinstance(key, str) else (key, 0)
            )
            new_value, value_count = rewrite_structure(value, rename_map)
            total += key_count + value_count
            items.append((new_key, new_value))
        if not total:
            return obj, 0
        rebuilt = dict(items)
        if len(rebuilt) != len(items):
            raise ValueError("rename would merge two keys of one mapping")
        return (type(obj)(rebuilt) if type(obj) is not dict else rebuilt), total
    if isinstance(obj, list | tuple):
        results = [rewrite_structure(item, rename_map) for item in obj]
        total = sum(count for _, count in results)
        if not total:
            return obj, 0
        values = [value for value, _ in results]
        return (tuple(values) if isinstance(obj, tuple) else values), total
    return obj, 0


def rewrite_text(text: str, rename_map: RenameMap) -> tuple[str, int]:
    pattern = rename_map.token_pattern()
    count = sum(1 for _ in pattern.finditer(text))
    if not count:
        return text, 0
    return pattern.sub(lambda m: rename_map.replacement(m.group(0)), text), count


def _replace_atomically(path: Path, write_tmp: Callable[[Path], object]) -> None:
    """Write via ``write_tmp`` to a sibling temp file, then rename over ``path``.

    Mode and (when the caller may) owner are copied from the original, so a
    migration run with ``sudo`` cannot leave root-owned files the daemon can
    no longer rotate or delete.
    """
    tmp = path.with_name(f".{path.name}.migrate-{uuid.uuid4().hex}.tmp")
    try:
        write_tmp(tmp)
        stat = path.stat()
        os.chmod(tmp, stat.st_mode & 0o7777)
        try:
            os.chown(tmp, stat.st_uid, stat.st_gid)
        except PermissionError:
            pass
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def migrate_parquet(path: Path, rename_map: RenameMap, *, apply: bool) -> int:
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    schema = pq.read_schema(path)  # type: ignore[no-untyped-call, unused-ignore]
    if "channel" not in schema.names:
        return 0
    channels = pq.read_table(path, columns=["channel"]).column("channel")  # type: ignore[no-untyped-call, unused-ignore]
    renames = {
        value: rename_map.rename(value)
        for value in pc.unique(channels.cast(pa.string())).to_pylist()  # type: ignore[attr-defined, unused-ignore]
        if isinstance(value, str) and rename_map.rename(value) != value
    }
    if not renames:
        return 0
    as_text = channels.cast(pa.string())
    count = sum(int(pc.sum(pc.equal(as_text, old)).as_py() or 0) for old in renames)  # type: ignore[attr-defined, misc, unused-ignore]
    if not apply:
        return count

    table = pq.read_table(path)  # type: ignore[no-untyped-call, unused-ignore]
    index = table.schema.get_field_index("channel")
    column = table.column(index).cast(pa.string())
    for old, new in renames.items():
        column = pc.if_else(pc.equal(column, old), new, column)  # type: ignore[attr-defined, unused-ignore]
    field_ = table.schema.field(index)
    table = table.set_column(index, field_, column.cast(field_.type))
    metadata = pq.ParquetFile(path).metadata  # type: ignore[no-untyped-call, unused-ignore]
    compression = "snappy"
    row_group_size = None
    if metadata.num_row_groups and metadata.num_columns:
        compression = str(metadata.row_group(0).column(0).compression).lower()
        if compression == "uncompressed":
            compression = "none"
        row_group_size = metadata.row_group(0).num_rows or None
    _replace_atomically(
        path,
        lambda tmp: pq.write_table(  # type: ignore[no-untyped-call, unused-ignore]
            table, tmp, compression=compression, row_group_size=row_group_size
        ),
    )
    return count


def migrate_json(path: Path, rename_map: RenameMap, *, apply: bool) -> int:
    text = path.read_text()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return 0
    new, count = rewrite_structure(payload, rename_map)
    if count and apply:
        indent = 2 if "\n" in text.strip() else None
        body = json.dumps(new, indent=indent, ensure_ascii=False) + ("\n" if indent else "")
        _replace_atomically(path, lambda tmp: tmp.write_text(body))
    return count


def migrate_jsonl(path: Path, rename_map: RenameMap, *, apply: bool) -> int:
    lines = path.read_text().splitlines(keepends=True)
    out: list[str] = []
    total = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            out.append(line)
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            out.append(line)
            continue
        new, count = rewrite_structure(record, rename_map)
        total += count
        ending = "\n" if line.endswith("\n") else ""
        out.append(json.dumps(new, ensure_ascii=False) + ending if count else line)
    if total and apply:
        body = "".join(out)
        _replace_atomically(path, lambda tmp: tmp.write_text(body))
    return total


def migrate_yaml_text(path: Path, rename_map: RenameMap, *, apply: bool) -> int:
    text = path.read_text()
    new, count = rewrite_text(text, rename_map)
    if count and apply:
        _replace_atomically(path, lambda tmp: tmp.write_text(new))
    return count


def _rewrite_payload(payload: Any, rename_map: RenameMap) -> tuple[Any, int]:
    if not isinstance(payload, dict):
        raise ValueError(f"model payload is a {type(payload).__name__}, not a dict")
    total = 0
    rebuilt = dict(payload)
    for key, value in payload.items():
        if key in _BINARY_SKIP_KEYS:
            continue
        new, count = rewrite_structure(value, rename_map)
        rebuilt[key] = new
        total += count
    return rebuilt, total


def migrate_joblib(path: Path, rename_map: RenameMap, *, apply: bool) -> int:
    import joblib

    new, count = _rewrite_payload(joblib.load(path), rename_map)
    if count and apply:
        _replace_atomically(path, lambda tmp: joblib.dump(new, tmp))
    return count


def migrate_torch(path: Path, rename_map: RenameMap, *, apply: bool) -> int:
    import torch

    payload = torch.load(path, map_location="cpu", weights_only=False)
    new, count = _rewrite_payload(payload, rename_map)
    if count and apply:
        _replace_atomically(path, lambda tmp: torch.save(new, tmp))
    return count


_HANDLERS: dict[str, Callable[..., int]] = {
    "parquet": migrate_parquet,
    "json": migrate_json,
    "jsonl": migrate_jsonl,
    "yaml": migrate_yaml_text,
    "joblib": migrate_joblib,
    "torch": migrate_torch,
}


def classify(path: Path) -> str | None:
    """Handler kind for one file, or ``None`` if the tool does not touch it."""
    name = path.name
    if name.startswith("."):
        return None
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        return "parquet" if path.match(PARQUET_GLOB) else None
    return {
        ".json": "json",
        ".jsonl": "jsonl",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".joblib": "joblib",
        ".pt": "torch",
    }.get(suffix)


# --- target discovery ---------------------------------------------------------


def session_files(root: Path) -> Iterable[Path]:
    """Telemetry chunks and JSON sidecars anywhere under a sessions root."""
    for path in sorted(root.rglob("*")):
        if path.is_file() and classify(path) in {"parquet", "json", "jsonl"}:
            yield path


def model_files(root: Path) -> Iterable[Path]:
    """Manifests and binaries anywhere under a models dir."""
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        kind = classify(path)
        if kind in {"joblib", "torch"} or (kind == "json" and path.name.endswith(".manifest.json")):
            yield path


def running_daemons(proc: Path = Path("/proc")) -> list[str]:
    """Command lines of running ``python -m src.main`` (the CANary daemon)."""
    found: list[str] = []
    if not proc.is_dir():
        return found
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if any(arg == b"src.main" or arg.endswith(b"src/main.py") for arg in argv):
            found.append(f"pid {entry.name}: " + " ".join(a.decode(errors="replace") for a in argv))
    return found


@dataclass
class Targets:
    sessions_roots: list[Path] = field(default_factory=list)
    models_dirs: list[Path] = field(default_factory=list)
    files: list[Path] = field(default_factory=list)


def resolve_targets(args: argparse.Namespace) -> Targets:
    targets = Targets(
        sessions_roots=[p.expanduser() for p in args.sessions_root],
        models_dirs=[p.expanduser() for p in args.models_dir],
        files=[p.expanduser() for p in args.file],
    )
    if args.config is not None:
        sys.path.insert(0, str(REPO_ROOT))
        from src.config import CanaryConfig
        from src.vehicle.profiles import resolve_profile_for_tooling, scope_config

        config = CanaryConfig.from_yaml(args.config)
        if config.profiles.enabled:
            config = scope_config(config, resolve_profile_for_tooling(config, args.profile))
        targets.sessions_roots.append(config.collection.storage_path)
        targets.models_dirs.append(config.daemon.models_dir)
        targets.files += [
            config.daemon.behavior_models,
            config.daemon.capture_profiles,
            config.daemon.capture_profile_overrides,
            args.config,
        ]
    return targets


# --- CLI ----------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        "migrate_channel_names",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--map", type=Path, default=DEFAULT_MAP, help="Rename map YAML.")
    parser.add_argument(
        "--dids",
        type=Path,
        default=DEFAULT_DIDS,
        help="DID catalog the map must agree with (targets present, sources gone).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="canary.yaml: adds its (profile-scoped) sessions root, models dir, "
        "behavior_models, capture_profiles (and this machine's overrides) and the config "
        "file itself.",
    )
    parser.add_argument("--profile", default=None, help="Vehicle profile slug for --config.")
    parser.add_argument(
        "--sessions-root", type=Path, action="append", default=[], help="Repeatable."
    )
    parser.add_argument("--models-dir", type=Path, action="append", default=[], help="Repeatable.")
    parser.add_argument(
        "--file",
        type=Path,
        action="append",
        default=[],
        help="Extra file (YAML/JSON/JSONL/parquet/joblib/pt). Repeatable.",
    )
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry run).")
    parser.add_argument(
        "--daemon-stopped",
        action="store_true",
        help="Required with --apply: confirms the CANary daemon is stopped.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        rename_map = load_rename_map(args.map)
    except (OSError, MapError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    problems = check_against_catalog(rename_map, args.dids)
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return EXIT_REFUSED
    if args.apply:
        if not args.daemon_stopped:
            print(
                "error: --apply rewrites files the daemon writes and hot-reloads; stop it "
                "and pass --daemon-stopped",
                file=sys.stderr,
            )
            return EXIT_REFUSED
        running = running_daemons()
        if running:
            print("error: the CANary daemon is running:", file=sys.stderr)
            for line in running:
                print(f"  {line}", file=sys.stderr)
            return EXIT_REFUSED

    targets = resolve_targets(args)
    if not (targets.sessions_roots or targets.models_dirs or targets.files):
        print("error: nothing to migrate (give --config, --sessions-root, --models-dir or --file)")
        return EXIT_REFUSED

    work: list[Path] = []
    missing: list[Path] = []
    for root in targets.sessions_roots:
        if root.is_dir():
            work.extend(session_files(root))
        else:
            missing.append(root)
    for root in targets.models_dirs:
        if root.is_dir():
            work.extend(model_files(root))
        else:
            missing.append(root)
    for path in targets.files:
        if path.is_file():
            work.append(path)
        else:
            missing.append(path)

    verb = "rewrote" if args.apply else "would rewrite"
    changed_files = 0
    changed_names = 0
    failures: list[str] = []
    for path in dict.fromkeys(work):
        kind = classify(path)
        if kind is None:
            failures.append(f"{path}: unsupported file type")
            continue
        try:
            count = _HANDLERS[kind](path, rename_map, apply=args.apply)
        except ImportError as exc:
            failures.append(f"{path}: not migrated ({exc.name} is not installed)")
            continue
        except Exception as exc:  # noqa: BLE001 — report every file, then fail the run
            failures.append(f"{path}: {type(exc).__name__}: {exc}")
            continue
        if count:
            changed_files += 1
            changed_names += count
            print(f"{verb} {count:6d} name(s)  {path}")

    for path in missing:
        print(f"warning: not found, skipped: {path}", file=sys.stderr)
    for failure in failures:
        print(f"error: {failure}", file=sys.stderr)
    print(
        f"{'applied' if args.apply else 'dry run'}: {changed_names} name(s) in "
        f"{changed_files} of {len(work)} file(s) scanned; {len(failures)} failure(s)"
    )
    return EXIT_INCOMPLETE if failures else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
