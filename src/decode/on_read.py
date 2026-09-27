"""What a session's rows are read with: the on-read catalog and CAN channel spellings.

The pieces of decode-on-read (P02.8) that need no session and no frame:
:func:`on_read_catalog` picks the bindings every reader decodes with, and
:func:`can_channel_spellings` lists every spelling a raw CAN row's
``channel`` has used, for a parquet pushdown. The session readers
(``src.preprocessing.decode``, ``src.preprocessing.loader``) and the DuckDB
query layer (``src.storage.telemetry_query``) both use them, so they live
below both (R09.3).
"""

from __future__ import annotations

from collections.abc import Iterable

from src.decode.bindings import Bindings, on_read_signals
from src.decode.engineering import repo_bindings
from src.paths import BINDINGS_YAML

#: The catalog :func:`on_read_catalog` reads by default. Looked up at call
#: time, so a test can point the loader at its own catalog.
ON_READ_BINDINGS_PATH = BINDINGS_YAML


def can_channel_spellings(can_ids: Iterable[int]) -> list[str]:
    """Every spelling the ``channel`` of a raw CAN row has used for these ids.

    Sessions from different recorder eras write the arbitration id as
    ``0x551``, ``0x0551``, ``551`` and in either case; a parquet pushdown on
    one spelling silently reads nothing from the others. Callers still match
    rows exactly with :func:`src.can_ids.parse_can_channel` afterwards.
    """
    spellings: set[str] = set()
    for can_id in can_ids:
        for width in ("", "03", "04", "08"):
            for case in ("X", "x"):
                body = format(int(can_id), f"{width}{case}")
                spellings.update((f"0x{body}", f"0X{body}", body))
    return sorted(spellings)


def on_read_catalog(bindings: Bindings | None = None) -> Bindings | None:
    """The catalog a session's rows are read with (P02.8).

    ``bindings`` (default: the catalog at :data:`ON_READ_BINDINGS_PATH`,
    re-read when the file changes) narrowed to
    :func:`src.decode.bindings.on_read_signals`. ``None`` when there is no
    catalog or it holds no such signal: a session is then read with no
    ``decoded`` rows at all, never with its stored ones.
    """
    catalog = bindings if bindings is not None else repo_bindings(ON_READ_BINDINGS_PATH)
    if catalog is None:
        return None
    signals = on_read_signals(catalog)
    if not signals:
        return None
    return catalog.model_copy(update={"signals": signals})


__all__ = ["ON_READ_BINDINGS_PATH", "can_channel_spellings", "on_read_catalog"]
