"""In-process analysis helpers — no daemon hop needed."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.analysis.signal_lookup import (
    NameMatch,
    fuzzy_match_tag,
    load_catalog,
    module_for_can_id,
)


def _to_dict(match: NameMatch) -> dict[str, Any]:
    return {
        "name": match.name,
        "module": match.module,
        "page": match.page,
        "score": match.score,
        "why": match.why,
    }


def signal_lookup(
    catalog_path: Path,
    *,
    tag_label: str,
    can_id: int | None = None,
    top_k: int = 3,
) -> dict[str, Any]:
    """Fuzzy-match a tag label against the FSM signal catalog.

    Parameters
    ----------
    catalog_path:
        ``data/fsm/signals.json`` (or wherever ``CANARY_SIGNALS_PATH`` points).
    tag_label:
        The informal tag used during reverse engineering — e.g.
        ``"ecm_livedata_maf"``.
    can_id:
        Optional. When provided, the search is narrowed to that module's
        parameter list (e.g. ``0x7E0`` → ECM only). Improves precision.
    top_k:
        Up to this many name matches are returned, sorted best-first.

    Returns ``{"matches": [...], "module": "...", "count": N}`` or an error
    payload when the catalog file is missing.
    """
    if not catalog_path.exists():
        return {
            "ok": False,
            "error": f"FSM signal catalog not found at {catalog_path}",
            "matches": [],
            "count": 0,
        }
    catalog = load_catalog(catalog_path)
    module = module_for_can_id(can_id) if can_id is not None else None
    if module is not None:
        candidates = catalog.for_module(module)
    else:
        candidates = [p for params in catalog.by_module.values() for p in params]
    matches = fuzzy_match_tag(tag_label, candidates, top_k=top_k)
    return {
        "ok": True,
        "module": module,
        "matches": [_to_dict(m) for m in matches],
        "count": len(matches),
    }


__all__ = ["signal_lookup"]
