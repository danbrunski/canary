"""Import-isolated home for the optional LLM-assist tier (Task S.4,
docs/roadmap/llm-assist-and-compute-roadmap.md).

The roadmap's Status section states the hard discipline this package
exists to satisfy: **no torch/LLM import on the capture hot path, no RAM
reservation, no daemon dependency when disabled**, and dashboard cards
degrade cleanly when the tier is not present. Concretely:

* Everything importable from this package today (:mod:`.cards`,
  :mod:`.store`) is schema/storage only — pydantic + stdlib, nothing heavy
  (cards resolve evidence through :mod:`src.evidence`, also pydantic-only).
  Importing ``src.analysis.llm_assist`` (or anything under it) never touches
  torch or an LLM runtime, regardless of ``config.llm_assist.enabled``.
* ``config.llm_assist.enabled`` (``src.config.LLMAssistConfig``, default
  ``False``) is the feature flag every future producer must gate on. No
  code under ``src/daemon/`` or ``src/collection/`` (the capture path)
  imports this package or reads that flag — the tier is reachable only
  from the dashboard's read-only analysis routes.
* A future producer (an actual LLM call filling in :class:`.cards.LlmAssistCard`
  instances) belongs in a new module here, e.g. ``src.analysis.llm_assist.
  producer`` — mirroring the lazy-import pattern this repo already uses for
  torch (see ``src.models.sequence_registry``'s module docstring: loaders
  import torch lazily inside functions, never at module import time, so
  importing the registry never pulls in the ML stack). Whatever producer
  eventually lands here must keep the same discipline: torch/LLM imports
  live inside the producer's own functions, never at this package's or the
  producer module's top level, so this package stays safe to import
  unconditionally even with a producer installed and the flag off.

``tests/test_llm_assist_import_isolation.py`` is the guarantee this
docstring describes, checked mechanically: importing the daemon's capture
entrypoint with the tier disabled never loads ``torch`` into the process.
"""

from __future__ import annotations

from src.analysis.llm_assist.cards import (
    CARD_SCHEMA,
    HISTORICAL_NARRATIVE_NOTICE,
    HYPOTHESES_NOT_VERDICTS_FRAMING,
    CardResolution,
    DiagnosticHypothesis,
    DifferentialCard,
    EvidenceCitation,
    EvidenceResolutionError,
    LlmAssistCard,
    NarrativeCard,
    NarrativeSection,
    StoredCard,
    UnverifiedHistoricalCard,
    card_from_dict,
    card_to_dict,
    require_resolved,
    resolve_card,
)
from src.analysis.llm_assist.store import DEFAULT_LLM_ASSIST_CARDS_PATH, LlmAssistCardStore

__all__ = [
    "CARD_SCHEMA",
    "DEFAULT_LLM_ASSIST_CARDS_PATH",
    "HISTORICAL_NARRATIVE_NOTICE",
    "HYPOTHESES_NOT_VERDICTS_FRAMING",
    "CardResolution",
    "DiagnosticHypothesis",
    "DifferentialCard",
    "EvidenceCitation",
    "EvidenceResolutionError",
    "LlmAssistCard",
    "LlmAssistCardStore",
    "NarrativeCard",
    "NarrativeSection",
    "StoredCard",
    "UnverifiedHistoricalCard",
    "card_from_dict",
    "card_to_dict",
    "require_resolved",
    "resolve_card",
]
