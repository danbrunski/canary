"""Atomic tmp-file + rename writers (Task 4.2, refactoring.md; R23.13).

The same four-line body — ``tmp = path.with_suffix(path.suffix + ".tmp");
tmp.write_text(text); tmp.replace(path)``, optionally preceded by a
``path.parent.mkdir`` — was copied at a dozen call sites across
``analysis/``, ``control/``, ``collection/``, ``dashboard/``, and
``alerts/``: sidecar JSON (``classification.json``, ``fingerprint.json``,
``dtc.json``, ``trip_summary.json``, the digest, the reference epoch, the
UPS keep-alive float, the trends alert state), and YAML config (the profile
registry, the tuning config, the custom-PID set).

R23.13 folded in the four durable lookalikes that each carried their own
tmp + fsync + replace + fsync-directory sequence (the odometer sidecar, the
paired-device registry, the NetworkManager keyfiles, every Parquet chunk) and
the two hand-built per-process temp names (the feature-frame cache, the ROM
library's images):

* ``durable=True`` fsyncs the temp file before the rename and the directory
  after it, so the new contents survive a hard power cut (key-off, no UPS)
  the moment the call returns. Without it a power cut can leave the rename
  committed but the data still in the page cache: a renamed-but-empty file.
* ``strict_fsync`` decides what a failed fsync means (see below).
* ``hidden_tmp=True`` names the temp file ``.<name>.tmp``, which a directory
  reader that loads every file it finds (NetworkManager's keyfile plugin)
  skips. The name is fixed, so the next write replaces a temp a crash left
  behind; the writers that use it are serialized by their own lock.
* ``unique_tmp=True`` names it ``.<name>.<pid>.<random>.tmp``: hidden and
  unique, for writers of one path that are not serialized (concurrent
  feature-cache builds, ROM-library adds from several processes). A crash
  can orphan one, so never use it for a secret.
  The default is the fixed ``<name>.tmp``.
* ``mode`` creates the temp file with those permission bits, so a secret is
  never readable by anyone else, not even before the rename.
* :func:`publish_atomic` is the second half alone, for a library that writes
  the temp file itself (``pyarrow.parquet.write_table``,
  ``DataFrame.to_pickle``); :func:`temp_path` names that temp file.

A failed fsync is best effort by default: logged, and the write continues.
The data is already written and the rename still gives atomicity; failing the
Parquet writer over it would lose more telemetry than the power cut it guards
against (``storage/parquet_writer.py``'s reasoning since its own copy), and the
odometer sidecar loses at most one persist interval. ``strict_fsync=True``
raises the ``OSError`` instead (the temp file is still removed): for a write
whose caller promises the change is live only once the disk took it -- the
paired-device registry ("a pairing the disk did not take must not work") and
the NetworkManager keyfiles (a join reports an internal error rather than
loading a profile the disk may not hold).

Still deliberately not used by ``control/models.py``, which publishes an
artifact and its manifest as a pair.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

log = logging.getLogger(__name__)


def temp_path(path: Path, *, unique: bool = False, hidden: bool = False) -> Path:
    """The same-directory temp file a write of ``path`` goes through.

    ``<name>.tmp`` by default, ``.<name>.tmp`` when ``hidden``, and
    ``.<name>.<pid>.<random>.tmp`` when ``unique``. Same directory, so the
    rename that publishes it is atomic.
    """
    path = Path(path)
    if unique:
        return path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    if hidden:
        return path.with_name(f".{path.name}.tmp")
    return path.with_name(f"{path.name}.tmp")


def _fsync(path: Path, *, strict: bool) -> None:
    """fsync a file or directory. A failure raises when ``strict``; otherwise
    it is logged and the caller continues."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as exc:
        if strict:
            raise
        log.warning("fsync open failed for %s: %s", path, exc)
        return
    try:
        os.fsync(fd)
    except OSError as exc:
        if strict:
            raise
        log.warning("fsync failed for %s: %s", path, exc)
    finally:
        os.close(fd)


def fsync_dir(path: Path, *, strict_fsync: bool = False) -> None:
    """fsync directory ``path`` so a rename into it survives a power cut.

    A failure is logged, not raised, unless ``strict_fsync``.
    """
    _fsync(Path(path), strict=strict_fsync)


def publish_atomic(
    tmp: Path, path: Path, *, durable: bool = True, strict_fsync: bool = False
) -> None:
    """Rename the finished temp file ``tmp`` over ``path``.

    ``durable`` fsyncs ``tmp`` before the rename and ``path``'s directory
    after it, so the contents are on disk when this returns; ``strict_fsync``
    raises a failed fsync instead of logging it. For writers that produce the
    temp file themselves; :func:`atomic_write_bytes` covers the rest.
    """
    tmp = Path(tmp)
    path = Path(path)
    if durable:
        _fsync(tmp, strict=strict_fsync)
    tmp.replace(path)
    if durable:
        fsync_dir(path.parent, strict_fsync=strict_fsync)


def atomic_write_bytes(
    path: Path,
    data: bytes,
    *,
    durable: bool = False,
    strict_fsync: bool = False,
    unique_tmp: bool = False,
    hidden_tmp: bool = False,
    mkdir: bool = False,
    mode: int | None = None,
) -> None:
    """Write ``data`` to ``path`` via a temp file and an atomic rename.

    A crash mid-write leaves the previous file intact (or nothing, if this
    is the first write): readers never see a half-written file. A failed
    write removes its temp file. ``mkdir`` creates ``path``'s parent first;
    the other keywords are described in the module docstring.
    """
    if strict_fsync and not durable:
        raise ValueError("strict_fsync applies to a durable write")
    path = Path(path)
    if mkdir:
        path.parent.mkdir(parents=True, exist_ok=True)
    tmp = temp_path(path, unique=unique_tmp, hidden=hidden_tmp)
    try:
        if mode is None:
            tmp.write_bytes(data)
        else:
            # A leftover temp file keeps its old permission bits through
            # O_CREAT; remove it so ``mode`` is what the new file gets.
            tmp.unlink(missing_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view) :]
            finally:
                os.close(fd)
        publish_atomic(tmp, path, durable=durable, strict_fsync=strict_fsync)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def atomic_write_text(
    path: Path,
    text: str,
    *,
    mkdir: bool = False,
    durable: bool = False,
    strict_fsync: bool = False,
    unique_tmp: bool = False,
    hidden_tmp: bool = False,
    mode: int | None = None,
) -> None:
    """Write ``text`` (UTF-8) to ``path`` via :func:`atomic_write_bytes`."""
    atomic_write_bytes(
        path,
        text.encode("utf-8"),
        durable=durable,
        strict_fsync=strict_fsync,
        unique_tmp=unique_tmp,
        hidden_tmp=hidden_tmp,
        mkdir=mkdir,
        mode=mode,
    )


__all__ = [
    "atomic_write_bytes",
    "atomic_write_text",
    "fsync_dir",
    "publish_atomic",
    "temp_path",
]
