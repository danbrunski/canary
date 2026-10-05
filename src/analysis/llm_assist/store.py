"""Storage for LLM-assist output cards (Task S.1/S.2 seam).

Append-only JSONL, the same shape as the anomaly/training/vehicle ledgers
(:class:`~src.jsonl_store.JsonlStore`). No producer writes to this store yet
(Task S.1's producer is explicitly absent). :meth:`LlmAssistCardStore.append`
resolves a card's citations against the vehicle's memory ledger first and
refuses one that does not resolve (S.5, E01.2), so an invented, wrong-vehicle
or misquoted evidence id never reaches the file. Lines written under the
first contract read back as unverified historical narrative
(:class:`~src.analysis.llm_assist.cards.UnverifiedHistoricalCard`).
Import-isolated like the rest of this package: no torch, no LLM library,
safe to import unconditionally (see ``__init__.py``).
"""

from __future__ import annotations

from pathlib import Path

from src.analysis.llm_assist.cards import (
    LlmAssistCard,
    StoredCard,
    card_from_dict,
    card_to_dict,
    require_resolved,
)
from src.evidence import MemoryLedger
from src.jsonl_store import JsonlStore
from src.paths import ANALYSES_DIR

__all__ = ["DEFAULT_LLM_ASSIST_CARDS_PATH", "LlmAssistCardStore"]

DEFAULT_LLM_ASSIST_CARDS_PATH = ANALYSES_DIR / "llm_assist_cards.jsonl"


class LlmAssistCardStore(JsonlStore[StoredCard]):
    """Append-only JSONL store of differential/narrative cards."""

    def __init__(self, path: Path = DEFAULT_LLM_ASSIST_CARDS_PATH) -> None:
        super().__init__(path, from_dict=card_from_dict)

    def append(self, card: LlmAssistCard, *, ledger: MemoryLedger) -> LlmAssistCard:
        """Persist ``card`` once every citation resolves on ``ledger``.

        Raises :class:`~src.analysis.llm_assist.cards.EvidenceResolutionError`
        naming each problem otherwise; nothing is written.
        """
        require_resolved(card, ledger)
        self._append_record(card, to_dict=card_to_dict)
        return card

    def cards(self) -> list[StoredCard]:
        """Every card on file, oldest first. Empty in every deployment until
        a producer exists (there isn't one yet — see module docstring)."""
        return self.raw_records()
