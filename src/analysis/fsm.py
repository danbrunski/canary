"""FSM signal-name extraction and lookup.

The 2008 Xterra FSM publishes "CONSULT-III Reference Value in Data Monitor
Mode" tables per module (ECM, ABS, BCM, etc.). Each table is the canonical
list of named parameters that the NT510 / any CONSULT-III tool can display.
The names come from the FSM; the DIDs come from runtime tagged-capture
sessions — this module only handles the names side.

`extract_param_names(pdf, page)` uses a positional-words approach because
`extract_tables()` chokes on the merged cells in these tables: parameter
names live in the leftmost column at consistent x < ~130, with a column
header row near the top of the page that we strip out.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import pdfplumber

# A param name is mostly uppercase, may include digits/spaces/punctuation
# common in Nissan signal naming (B1, B2, /, -, ·, *, parens, dots).
_PARAM_NAME_RE = re.compile(r"^[A-Z0-9·][A-Z0-9 /\-\(\)\.\*·\+]{2,28}$")

# Lines we want to drop from the leftmost-column extraction even though they
# fit the regex. Mostly section headers, page headers/footers, and table-
# header text. Compared case-insensitively after stripping.
_NOISE = frozenset(
    s.upper()
    for s in (
        "MONITOR ITEM",
        "MONITOR ITEMS",
        "ITEM NAME",
        "MONITORED ITEM",
        "ITEM",
        "REVISION:",
        "REFERENCE",
        "REFERENCE VALUE",
        "REFERENCE VALUES",
        "CONSULT-III",
        "CONSULT-III FUNCTION",
        "CONSULT-III REFERENCE",
        "CONSULT-III DATA",
        "ECU DIAGNOSIS",
        "DIAGNOSIS",
        "DTC",
        "ECM",
        "ABS",
        "BCM",
        "TCM",
        "BCS",
        "DLN",
        "MWI",
        "TM",
        "TERMINAL",
        "TERMINAL LAYOUT",
        "PHYSICAL VALUES",
        "VALUES ON THE",
        "VALUE ON THE",
        "TRANSFER",
        "DIFFERENTIAL",
        "COMBINATION",
        "TRANSMISSION",
        "DRIVELINE",
        "BCM (BODY",
        "ECU INPUT",
        "MAIN SIG-",
        "INPUT SIG-",
        "MAIN SIGNALS",
        "INPUT SIGNALS",
        "SELEC-",
        "SELECTION",
        "REMARKS",
        "TRIP COMPUTER",
        # Page-section / mode headers occasionally caught by the leftmost
        # column when a Data Monitor section starts mid-page.
        "DATA MONITOR",
        "DATA MONITOR MODE",
        "DIAGNOSIS SYSTEM",
        "DIFFERENTIAL LOCK CONTROL",
        "DIFF LOCK",
        "SELF-DIAGNOSTIC RESULTS",
        "CAN DIAG SUPPORT MNTR",
        "ECU PART NUMBER",
        "DTC WORK SUPPORT MODE",
        "OBD-II SELF-DIAGNOSTIC",
        "TCM SELF-DIAGNOSTIC",
        "CHECK O/D OFF INDICATOR",
        "JUDGEMENT PROCEDURE",
    )
)


def clean_param_name(text: str) -> str:
    """Strip inline unit annotations (`[V]`, `(rpm)`) while keeping bank
    annotations (`(B1)`, `(B2)`) that are part of the canonical name.

    Rule: brackets `[...]` are always units (Nissan FSM convention).
    Parens `(...)` are units when their content is a single character or
    contains lowercase letters; otherwise they're a bank/sensor annotation
    that belongs to the name.
    """
    import re as _re

    s = text.strip()
    # Brackets: always units, always strip.
    s = _re.sub(r"\s*\[[^\]]*\]\s*", " ", s)

    # Parens: strip if content looks like a unit, else keep.
    def _drop_unit_parens(m: _re.Match[str]) -> str:
        content = m.group(1)
        is_unit = len(content) <= 1 or any(c.islower() for c in content)
        return " " if is_unit else m.group(0)

    s = _re.sub(r"\s*\(([^)]*)\)\s*", _drop_unit_parens, s)
    return _re.sub(r"\s+", " ", s).strip()


def looks_like_param_name(text: str) -> bool:
    """True if `text` plausibly names a CONSULT-III data-monitor parameter.

    The heuristic first strips inline unit annotations via `clean_param_name`,
    then accepts the visible token shape (uppercase-dominant, short) and
    rejects known section-header / table-header / footer noise. False
    positives are tolerable — they're filtered out at lookup time when we
    check whether they bind to any DID — but false *negatives* mean we lose
    real signals, so the regex is intentionally permissive.
    """
    s = clean_param_name(text)
    if not 3 <= len(s) <= 30:
        return False
    if not _PARAM_NAME_RE.match(s):
        return False
    if s.upper() in _NOISE:
        return False
    # Reject "all-letters one-word section labels" like "PREPARATION".
    if " " not in s and s.isalpha() and len(s) > 5 and s.isupper():
        return False
    # Reject unbalanced brackets/parens — those are page-heading fragments
    # the cleaner couldn't strip because their close/open is on another line.
    if s.count("(") != s.count(")") or s.count("[") != s.count("]"):
        return False
    return True


def extract_param_names(
    pdf: Any, page_number: int, *, max_x: float = 135.0
) -> list[str]:
    """Pull leftmost-column param names from one PDF page.

    `page_number` is 1-indexed (matching how the FSM and pdfplumber output
    refer to pages). `max_x` is the right boundary of the leftmost column;
    most Nissan FSM tables have the param-name column ending around x=130.
    """
    page = pdf.pages[page_number - 1]
    words = page.extract_words(use_text_flow=False)
    rows: dict[float, list[tuple[float, str]]] = defaultdict(list)
    for w in words:
        if w["x0"] < max_x:
            # Bucket by visual row (y in 2-px buckets) and keep x for ordering.
            rows[round(w["top"] / 2) * 2].append((w["x0"], w["text"]))

    out: list[str] = []
    for top in sorted(rows):
        ordered = [t for _, t in sorted(rows[top])]
        joined = " ".join(ordered).strip()
        if looks_like_param_name(joined):
            out.append(clean_param_name(joined))
    return out


def extract_pdf_section(
    pdf_path: Path, pages: list[int], *, max_x: float = 135.0
) -> list[dict[str, str | int]]:
    """Extract param names from a contiguous section of a PDF.

    Returns deduplicated `[{"name": ..., "page": ...}]`, preserving the
    page on which the name was first seen so we can cite the FSM.
    """
    seen: set[str] = set()
    out: list[dict[str, str | int]] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_no in pages:
            for name in extract_param_names(pdf, page_no, max_x=max_x):
                if name not in seen:
                    seen.add(name)
                    out.append({"name": name, "page": page_no})
    return out
