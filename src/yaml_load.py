"""The one YAML loader: PyYAML's safe loader, parsed by libyaml when built in.

``yaml.safe_load`` parses in pure Python, 8-10x slower than libyaml on this
repo's catalogs (``data/<vehicle>/ecu_application_model.yaml`` 130 ms against
15 ms, ``data/dids.yaml`` 103 ms against 10 ms; 2026-10-23). The push gate
spent over a third of its profiled test time inside it, re-parsing the same
catalogs. Only the parser differs: the constructor and resolver are the same
Python ``SafeConstructor``, so a document loads to the same values either way.
``tests/test_yaml_load.py`` refuses any other YAML load in the repo.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Generic, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

#: libyaml's parser under the safe constructor, or the pure-Python one on a
#: PyYAML built without libyaml.
SAFE_LOADER: Any = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def load_yaml(stream: str | bytes | IO[str] | IO[bytes]) -> Any:
    """``yaml.safe_load``, through :data:`SAFE_LOADER`."""
    return yaml.load(stream, Loader=SAFE_LOADER)  # noqa: S506 - a safe loader


M = TypeVar("M", bound=BaseModel)


class YamlModelError(ValueError):
    """A YAML file that did not load into its model; the message names the file.

    A ``ValidationError`` out of ``Model.model_validate(load_yaml(path))`` says
    which field was wrong and not which of the repo's two dozen YAML files it
    was reading. The pydantic detail is kept in the message and chained as
    ``__cause__``.
    """


@dataclass(frozen=True)
class LoadedModel(Generic[M]):
    """A model read from a file, and the sha256 of the bytes it was read from.

    ``sha256`` is the provenance a caller records ("validated against this
    exact file"); it is computed over the raw bytes, before parsing.

    ``model`` is shared between every caller that reads the same unchanged
    file (see :func:`load_yaml_model`): treat it as read-only, or take
    ``model.model_copy(deep=True)`` first.
    """

    model: M
    sha256: str


#: Distinct (file, Model) entries kept; the repo has about two dozen model
#: files, so this holds them all and still bounds a test run that writes many
#: temporary ones.
_MODEL_CACHE_MAX = 64
#: A file modified within this long before it was cached cannot be trusted by
#: its stat alone (see :func:`load_yaml_model`). Git's "racily clean" window.
RACY_WINDOW_NS = 2_000_000_000
#: key -> (loaded, ``time.time_ns()`` when it was cached)
_model_cache: dict[tuple[str, int, int, type[BaseModel]], tuple[LoadedModel[Any], int]] = {}
_model_cache_lock = threading.Lock()


def clear_yaml_model_cache() -> None:
    """Forget every cached model (for a test that rewrites a file in place)."""
    with _model_cache_lock:
        _model_cache.clear()


def _parse(path: Path, raw_bytes: bytes, model: type[M]) -> LoadedModel[M]:
    try:
        document = load_yaml(raw_bytes)
    except yaml.YAMLError as exc:
        raise YamlModelError(f"{path}: not valid YAML: {exc}") from exc
    if not isinstance(document, dict):
        kind = "empty" if document is None else f"a {type(document).__name__}, not a mapping"
        raise YamlModelError(f"{path} is {kind}; {model.__name__} needs a mapping")
    try:
        validated = model.model_validate(document)
    except ValidationError as exc:
        raise YamlModelError(f"{path}: {exc}") from exc
    return LoadedModel(validated, hashlib.sha256(raw_bytes).hexdigest())


def load_yaml_model(path: Path, model: type[M]) -> LoadedModel[M]:
    """Read ``path`` once, parse it with :func:`load_yaml`, validate it as ``model``.

    * A parse or validation error raises :class:`YamlModelError` naming
      ``path`` with the pydantic detail kept.
    * An empty file, or one whose document is not a mapping, raises too, and
      says so: a truncated file must not read as a model of defaults, which is
      a different answer (``dyno_profile`` guarded this alone before).
    * The result is cached by ``(resolved path, st_mtime_ns, st_size, model)``,
      so an edited file is re-read and two model types over one file do not
      share an entry. The cache holds the one instance: **callers must not
      mutate** ``result.model`` and take ``model_copy(deep=True)`` if they
      need to. A missing file raises ``FileNotFoundError`` as ``stat`` does.
    * **The stat key is not enough for a file written just now.** A
      filesystem stamps mtime from a coarse kernel clock: on this repo's
      ext4/WSL2 kernel 1987 of 2000 back-to-back same-size writes kept the
      same ``st_mtime_ns``, so a same-size rewrite after a load looked
      unchanged and returned the stale model and a ``sha256`` of the wrong
      bytes. Git's "racily clean" rule applies: an entry cached within
      :data:`RACY_WINDOW_NS` of its file's mtime is verified by reading the
      bytes and comparing their sha256 (re-validating on a difference). Once
      the entry has settled (the verification happens after the window) the
      stat key stands and a hit costs one ``stat``.
    * Failures are never cached.
    """
    stat = path.stat()
    key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size, model)
    with _model_cache_lock:
        entry = _model_cache.get(key)
    if entry is not None:
        hit, cached_at = entry
        if cached_at - stat.st_mtime_ns >= RACY_WINDOW_NS:
            return hit
        # Racily clean: the file may have been rewritten in the same tick.
        raw_bytes = path.read_bytes()
        now = time.time_ns()
        if hashlib.sha256(raw_bytes).hexdigest() == hit.sha256:
            if now - stat.st_mtime_ns >= RACY_WINDOW_NS:
                with _model_cache_lock:
                    _model_cache[key] = (hit, now)  # settled: trust the stat from here
            return hit
    else:
        raw_bytes = path.read_bytes()
    loaded = _parse(path, raw_bytes, model)
    with _model_cache_lock:
        if len(_model_cache) >= _MODEL_CACHE_MAX:
            _model_cache.pop(next(iter(_model_cache)))
        _model_cache[key] = (loaded, time.time_ns())
    return loaded
