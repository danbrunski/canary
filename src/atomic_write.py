"""Atomic tmp-file + rename text writer (Task 4.2, refactoring.md).

The same four-line body — ``tmp = path.with_suffix(path.suffix + ".tmp");
tmp.write_text(text); tmp.replace(path)``, optionally preceded by a
``path.parent.mkdir`` — was copied at a dozen call sites across
``analysis/``, ``control/``, ``collection/``, ``dashboard/``, and
``alerts/``: sidecar JSON (``classification.json``, ``fingerprint.json``,
``dtc.json``, ``trip_summary.json``, the digest, the reference epoch, the
UPS keep-alive float, the trends alert state), and YAML config (the profile
registry, the tuning config, the custom-PID set).

Deliberately **not** used by three lookalikes with a genuine reason to
diverge:

* ``analysis/gps_tracker.py``'s odometer persist fsyncs the temp file *and*
  the directory for hard-power-loss durability — a stronger guarantee than
  a plain rename gives, and losing it to a generic helper would be a real
  regression, not a simplification.
* ``preprocessing/feature_cache.py`` writes a pickle (not text) and
  pid-disambiguates its tmp name so concurrent feature-cache builds for
  different sessions can't collide on one tmp path.
* ``control/models.py`` writes base64-decoded artifact *bytes*, not text.
* ``storage/parquet_writer.py`` (binary Parquet) stays separate per the
  roadmap doc's own call — different data type, not force-unified here.
"""

from __future__ import annotations

from pathlib import Path


def atomic_write_text(path: Path, text: str, *, mkdir: bool = False) -> None:
    """Write ``text`` to ``path`` via tmp-file + rename.

    A crash mid-write leaves the previous file intact (or nothing, if this
    is the first write) — readers never see a half-written file. ``mkdir``
    creates ``path``'s parent first, for callers where it isn't already
    guaranteed to exist.
    """
    path = Path(path)
    if mkdir:
        path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


__all__ = ["atomic_write_text"]
