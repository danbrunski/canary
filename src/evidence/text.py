"""Numbers in prose: which tokens state a number (E01.2, E01.5).

A number shown with a claim is rendered from the evidence record that holds
it, never typed into prose beside it. This is the one tokenizer that decides
what "a number in prose" is, shared by the investigation records
(:mod:`src.evidence.records`), the template cards (:mod:`src.evidence.cards`)
and the LLM-assist cards (``src.analysis.llm_assist.cards``). Stdlib only.

Prose is split into tokens on whitespace and punctuation; any token holding a
digit is a number unless it has an identifier's form. A number with its unit
attached (``3000rpm``, ``5psi``, ``80kPa``, ``3.5x``, ``1e3``, ``+8%``) is
still a number. Identifier forms: ``0x`` hex (``0x1206``), a namespaced or
underscored name (``obd:SHORT_FUEL_TRIM_1``, ``mem:ECM_FUEL_COMP_X_INPUT``),
or an upper-case-led name (``B1``, ``P0171``).
"""

from __future__ import annotations

import re

__all__ = ["IDENTIFIER", "no_numerals", "numeral_in"]

_TOKEN_SPLIT = re.compile(r"[\s,;()\[\]{}\"'!?/=<>|*\-–—]+")
IDENTIFIER = re.compile(
    r"(?:[a-z][a-z0-9_]*:)?"
    r"(?:0x[0-9A-Fa-f]+"
    r"|[A-Z][A-Z0-9]*(?:_[A-Za-z0-9]+)+"
    r"|[A-Z][A-Za-z]*[0-9][A-Za-z0-9]*)"
)


def numeral_in(value: str) -> str | None:
    """The first token of ``value`` that states a number, or ``None``."""
    for raw in _TOKEN_SPLIT.split(value):
        token = raw.strip(".:+~≈%")
        if not any(ch.isdigit() for ch in raw):
            continue
        if token and IDENTIFIER.fullmatch(token):
            continue
        return raw
    return None


def no_numerals(value: str, *, what: str) -> str:
    """``value`` unchanged, or :class:`ValueError` naming the number it states."""
    found = numeral_in(value)
    if found is not None:
        raise ValueError(
            f"{what} contains the number {found!r}; prose cannot state numbers — "
            "cite the evidence record whose values are rendered instead"
        )
    return value
