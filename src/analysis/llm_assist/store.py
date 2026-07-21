"""Storage for LLM-assist output cards (Task S.1/S.2 seam).

Append-only JSONL, the same shape as the anomaly/training/vehicle ledgers
(:class:`~src.jsonl_store.JsonlStore`). No producer writes to this store yet
(Task S.1's producer is explicitly absent) — it exists so the feature-flagged
dashboard endpoint (Task S.2) has somewhere to read from today, and so a
future producer has an obvious place to append to without inventing new
storage plumbing. Import-isolated like the rest of this package: no torch,
no LLM library, safe to import unconditionally (see ``__init__.py``).
"""

from __future__ import annotations

from pathlib import Path

from src.analysis.llm_assist.cards import LlmAssistCard, card_from_dict, card_to_dict
from src.jsonl_store import JsonlStore

__all__ = ["DEFAULT_LLM_ASSIST_CARDS_PATH", "LlmAssistCardStore"]

DEFAULT_LLM_ASSIST_CARDS_PATH = Path("data/analyses/llm_assist_cards.jsonl")


class LlmAssistCardStore(JsonlStore[LlmAssistCard]):
    """Append-only JSONL store of differential/narrative cards."""

    def __init__(self, path: Path = DEFAULT_LLM_ASSIST_CARDS_PATH) -> None:
        super().__init__(path, from_dict=card_from_dict)

    def append(self, card: LlmAssistCard) -> LlmAssistCard:
        return self._append_record(card, to_dict=card_to_dict)

    def cards(self) -> list[LlmAssistCard]:
        """Every card on file, oldest first. Empty in every deployment until
        a producer exists (there isn't one yet — see module docstring)."""
        return self.raw_records()
