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
from dataclasses import dataclass
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


def extract_param_names(pdf: Any, page_number: int, *, max_x: float = 135.0) -> list[str]:
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


# ---------------------------------------------------------------------------
# Spec / expected-range extraction (the right side of the table)
# ---------------------------------------------------------------------------

# Match "X - Y unit" / "X to Y unit" / "X – Y unit". Captures both numbers
# and the trailing unit token greedily but bounded so we don't swallow the
# next sentence. Examples we want to catch:
#   "2.0 - 6.0 g·m/s"   "11 - 14V"   "0 - 0.3V"    "13 - 18° BTDC"
#   "0% - 35%"          "0.65 - 0.87V"            "−5° - 5°CA"
_RANGE_RE = re.compile(
    r"(?P<lo>[+-−]?\d+(?:\.\d+)?)"
    # Optional unit attached to lo ("0%" or "−5°"); kept loose so we don't
    # accidentally swallow following words.
    r"\s*[°%a-zA-Z·/]{0,4}\s*"
    r"[-–—]\s*"
    r"(?P<hi>[+-−]?\d+(?:\.\d+)?)"
    r"(?P<unit>(?:\s*°?[A-Za-z%·/]+(?:\s*[A-Za-z]+)?)?)"
)

# Match unbounded directional bounds: "More than 70°C", "Less than 4.75V".
_UNBOUNDED_RE = re.compile(
    r"(?P<dir>More\s+than|Less\s+than|Below|Above)\s+"
    r"(?P<val>[+-−]?\d+(?:\.\d+)?)"
    r"(?P<unit>(?:\s*°?[A-Za-z%·/]+)?)"
)

# Approx. single-value: "Approx. 2.5V", "Approximately 5V".
_APPROX_RE = re.compile(
    r"(?:Approx(?:imately)?\.?)\s+(?P<val>[+-−]?\d+(?:\.\d+)?)"
    r"(?P<unit>(?:\s*°?[A-Za-z%·/]+)?)"
)


def _normalize_minus(s: str) -> str:
    return s.replace("−", "-")


# A cross-page reference: the numeric spec lives in another FSM section/page
# the data-monitor table just points at ("See EC-79.", "Refer to EC-123").
_CROSS_REF_RE = re.compile(r"\b(?:See|Refer\s+to)\s+[A-Z]{2,4}-\d+", re.IGNORECASE)

# Discrete-state vocabulary: a param whose "spec" is a set of named states
# (LEAN/RICH, ON/OFF, an "A ←→ B" toggle) has no numeric range *by nature* —
# distinct from a numeric param we simply failed to parse.
_STATE_TOKENS = frozenset(
    (
        "ON",
        "OFF",
        "OPEN",
        "CLOSE",
        "CLOSED",
        "LEAN",
        "RICH",
        "YES",
        "NO",
        "HI",
        "LO",
        "HIGH",
        "LOW",
        "ENGAGED",
        "RELEASED",
        "ACTIVE",
        "INACTIVE",
        "LOCK",
        "UNLOCK",
        "FORWARD",
        "REVERSE",
        "GOOD",
        "NG",
        "ENABLE",
        "DISABLE",
        "PRESS",
        "RELEASE",
        "DETECT",
        "STOP",
        "START",
        "RESET",
    )
)
_ARROW_RE = re.compile(r"←→|<->|→|->")


def classify_spec(text: str, ranges: list[dict[str, float | str]]) -> str:
    """Why a param does/doesn't have a numeric range.

    Returns one of: ``numeric_range`` (≥1 range parsed), ``cross_ref``
    (spec points to another FSM page), ``categorical`` (named states, no
    number by nature), ``descriptive`` (prose/garbled — a number may exist
    but we couldn't extract it), ``empty`` (no spec text). This makes "9%
    coverage" honest: the real metric is numeric / (numeric + descriptive
    + cross_ref) — categorical/empty are *inherently* unrangeable, not
    extraction failures, and `range_match` must skip them as such.
    """
    s = text.strip()
    if not s:
        return "empty"
    if ranges:
        return "numeric_range"
    if _CROSS_REF_RE.search(s):
        return "cross_ref"
    if _ARROW_RE.search(s):
        return "categorical"
    upper_tokens = set(re.findall(r"[A-Z]{2,}", s.upper()))
    if len(upper_tokens & _STATE_TOKENS) >= 2:
        return "categorical"
    return "descriptive"


def parse_ranges(text: str) -> list[dict[str, float | str]]:
    """Pull numeric expected ranges out of a CONSULT-III spec text fragment.

    Returns a list of `{"min": float, "max": float, "unit": str}` entries.
    Multiple ranges per param are common (different operating conditions, e.g.
    "2.0 - 6.0 g·m/s at idle, 7.0 - 20.0 g·m/s at 2,500 rpm"). Unbounded
    bounds use ±inf for the missing side.

    Heuristic: greedy regex passes; entries with min > max are dropped (likely
    misparses where the cleaner caught a sentence boundary). Best-effort —
    unparseable text just yields zero ranges.
    """
    text = _normalize_minus(text)
    out: list[dict[str, float | str]] = []

    for m in _RANGE_RE.finditer(text):
        try:
            lo = float(m.group("lo"))
            hi = float(m.group("hi"))
        except ValueError:
            continue
        if lo > hi:
            continue
        unit = m.group("unit").strip()
        out.append({"min": lo, "max": hi, "unit": unit})

    for m in _UNBOUNDED_RE.finditer(text):
        try:
            val = float(m.group("val"))
        except ValueError:
            continue
        unit = m.group("unit").strip()
        if m.group("dir").lower() in ("more than", "above"):
            out.append({"min": val, "max": float("inf"), "unit": unit})
        else:  # less than / below
            out.append({"min": float("-inf"), "max": val, "unit": unit})

    for m in _APPROX_RE.finditer(text):
        try:
            val = float(m.group("val"))
        except ValueError:
            continue
        unit = m.group("unit").strip()
        # Approx is ±10% by convention — a usable range for matching.
        slack = abs(val) * 0.1 or 0.1
        out.append({"min": val - slack, "max": val + slack, "unit": unit})

    return out


# ---------------------------------------------------------------------------
# Physical (decode-level) limits — online-training-strategy.md Task 1.1
# ---------------------------------------------------------------------------
#
# "Physical limits" are a different axis than the "expected ranges" above.
# An expected range is *spec normal* (what a healthy engine reads); a
# physical limit is *decode-level impossibility* (sensor-open, sensor-short,
# a 16-bit rollover) — garbage, not an unhealthy reading. Mixing the two
# is exactly the trap online-training-strategy.md warns about: filtering
# training data to "expected" ranges throws away legitimate but abnormal
# engine states, whereas physical limits only ever discard decode noise.
#
# Primary source would be the FSM itself, if its CONSULT-III data-monitor
# tables spelled out sensor-fault codes in the spec text (e.g. "Sensor
# open: -40°C"). ``extract_physical_limits_from_fsm`` below looks for that
# vocabulary and is exercised by its own tests, but a scan of the real
# ``data/fsm/signals.json`` corpus turns up nothing — the Nissan tables only
# ever describe nominal operating conditions, never decode-fault codes, so
# it always returns an empty dict on real data. ``PHYSICAL_LIMITS`` is
# therefore the actual source of truth: a hand-curated table keyed by the
# same channel names the preprocessing pipeline uses (``obd:RPM``, etc.),
# seeded from the OBD-II decode formulas in
# ``src/collection/can_obd_client.py`` — a bound is *only* added when the
# decode formula (or basic physics) makes a value outside it impossible,
# never a guess at a "typical" range. A channel absent from this table has
# no physical-limit reject — that is the safe default, not an oversight.


@dataclass(frozen=True)
class PhysicalLimit:
    """One channel's absolute physical min/max — decode-level "impossible"
    bounds, not the FSM spec's nominal operating range.

    Same ``{min, max, unit}`` shape as :class:`~src.analysis.signal_lookup.
    ExpectedRange` (``float("-inf")``/``float("inf")`` for a one-sided bound)
    so ``quality_gate.py`` and ``range_match.py`` share one schema. ``reason``
    is why the bound is safe to reject on (cites the decode formula or the
    physical fact), surfaced in the disposition report so a reject is never
    an unexplained number.
    """

    min: float
    max: float
    unit: str
    reason: str


#: Curated physical-impossibility bounds, keyed by the wide-frame channel
#: name (``<source>:<CHANNEL>``, matching ``src.telemetry.channel_key``).
#: Every bound below traces to a decode formula in
#: ``src/collection/can_obd_client.py`` (or basic physics) — see each entry's
#: ``reason``. Channels not listed here are deliberately left out: either the
#: decode formula alone can't produce an out-of-range value (most single-byte
#: percent PIDs), or the "physically impossible" bound would require
#: vehicle-specific knowledge (electrical system voltage, redline) this
#: module has no principled way to guess — an absent entry means "no
#: reject", which is the safe default.
PHYSICAL_LIMITS: dict[str, PhysicalLimit] = {
    "obd:RPM": PhysicalLimit(
        min=0.0,
        max=10_000.0,
        unit="rpm",
        reason=(
            "PID 0x0C decodes (A*256+B)/4, ceiling 16383.75 at raw 0xFFFF — a "
            "classic all-ones sensor-fault code, not a real engine speed; no "
            "production gasoline engine sustains anywhere near this either"
        ),
    ),
    "obd:COOLANT_TEMP": PhysicalLimit(
        min=-39.0,
        max=150.0,
        unit="degC",
        reason=(
            "PID 0x05 decodes A-40; raw A=0 -> exactly -40degC is the sensor-"
            "open floor, not a real coolant reading. >150degC is beyond "
            "boiling/failure, still short of the 215degC encoding ceiling"
        ),
    ),
    "obd:INTAKE_TEMP": PhysicalLimit(
        min=float("-inf"),
        max=250.0,
        unit="degC",
        reason=(
            "intake air can legitimately read very cold (cold-climate "
            "startup), so no lower bound; >=250degC is decode/sensor-fault "
            "territory for intake air on any port/direct-injection engine"
        ),
    ),
    "obd:MAF": PhysicalLimit(
        min=0.0,
        max=float("inf"),
        unit="g/s",
        reason="PID 0x10 decodes (A*256+B)/100, a magnitude — negative is impossible",
    ),
    "obd:THROTTLE_POS": PhysicalLimit(
        min=0.0,
        max=100.0,
        unit="%",
        reason="PID 0x11 decodes A*100/255 (single byte); formula cannot exceed 0-100%",
    ),
    "obd:RELATIVE_THROTTLE_POS": PhysicalLimit(
        min=0.0,
        max=100.0,
        unit="%",
        reason="PID 0x45, same A*100/255 single-byte formula as THROTTLE_POS",
    ),
    "obd:ENGINE_LOAD": PhysicalLimit(
        min=0.0,
        max=100.0,
        unit="%",
        reason="PID 0x04, same A*100/255 single-byte formula as THROTTLE_POS",
    ),
    "obd:FUEL_LEVEL": PhysicalLimit(
        min=0.0,
        max=100.0,
        unit="%",
        reason="PID 0x2F, same A*100/255 single-byte formula as THROTTLE_POS",
    ),
    "obd:SHORT_FUEL_TRIM_1": PhysicalLimit(
        min=-100.0,
        max=100.0,
        unit="%",
        reason="PID 0x06 decodes (A-128)*100/128; formula bounds -100%..+99.2%",
    ),
    "obd:SHORT_FUEL_TRIM_2": PhysicalLimit(
        min=-100.0,
        max=100.0,
        unit="%",
        reason="PID 0x08, same signed-byte formula as SHORT_FUEL_TRIM_1",
    ),
    "obd:LONG_FUEL_TRIM_1": PhysicalLimit(
        min=-100.0,
        max=100.0,
        unit="%",
        reason="PID 0x07, same signed-byte formula as SHORT_FUEL_TRIM_1",
    ),
    "obd:LONG_FUEL_TRIM_2": PhysicalLimit(
        min=-100.0,
        max=100.0,
        unit="%",
        reason="PID 0x09, same signed-byte formula as SHORT_FUEL_TRIM_1",
    ),
    "obd:BAROMETRIC_PRESSURE": PhysicalLimit(
        min=50.0,
        max=110.0,
        unit="kPa",
        reason=(
            "PID 0x33 decodes the raw byte directly (0-255 kPa possible), but "
            "Earth's terrestrial atmospheric pressure never leaves roughly "
            "30-108 kPa; loose margin kept either side of that"
        ),
    ),
}

# Fault-vocabulary the FSM *would* use if a data-monitor spec ever encoded a
# decode-level physical limit instead of a nominal operating range (see
# module docstring above — this never matches the real corpus, only kept so
# a future FSM revision that does carry this language is picked up for free).
_PHYSICAL_FAULT_RE = re.compile(
    r"(?:sensor|circuit)\s+(?:open|short(?:ed)?)\s*:?\s*"
    r"(?P<val>[+-−]?\d+(?:\.\d+)?)\s*(?P<unit>°?[A-Za-z%·/]*)",
    re.IGNORECASE,
)


def extract_physical_limits_from_fsm(
    entries: list[dict[str, Any]],
) -> dict[str, PhysicalLimit]:
    """Best-effort scan of parsed FSM entries for decode-fault vocabulary.

    ``entries`` is :func:`extract_pdf_section_with_specs`'s output (each with
    a ``spec_text``). Looks for phrasing like "Sensor open: -40°C" or
    "Circuit short: 5V" — a fault *value*, not an operating range — and
    turns each hit into a one-sided :class:`PhysicalLimit` (the fault value
    becomes the excluded bound; which side is excluded is inferred from the
    fault value's sign relative to zero, since the FSM text doesn't say).
    Real Nissan CONSULT-III data-monitor tables don't use this vocabulary
    (checked against the full ``data/fsm/signals.json`` corpus — zero
    matches), so this returns an empty dict there; ``PHYSICAL_LIMITS`` is the
    actual source of truth. Kept so a future FSM revision (or another
    vehicle's service manual) that *does* spell out fault codes is picked up
    without code changes beyond wiring this dict in alongside the curated one.
    """
    out: dict[str, PhysicalLimit] = {}
    for entry in entries:
        match = _PHYSICAL_FAULT_RE.search(str(entry.get("spec_text", "")))
        if match is None:
            continue
        try:
            val = float(match.group("val").replace("−", "-"))
        except ValueError:
            continue
        unit = match.group("unit").strip()
        name = str(entry["name"])
        # The fault value itself must be excluded, not just approached — a
        # tiny margin (scaled to the value's own magnitude, floored so a
        # fault value of exactly 0 still gets one) nudges the bound past it.
        margin = max(abs(val) * 1e-3, 1e-3)
        if val >= 0:
            # A high fault reading (e.g. "Circuit short: 16V") — valid
            # values sit strictly below it.
            out[name] = PhysicalLimit(
                min=float("-inf"),
                max=val - margin,
                unit=unit,
                reason=f"FSM-declared fault value: {val}{unit}",
            )
        else:
            # A low/floor fault reading (e.g. "Sensor open: -40°C") — valid
            # values sit strictly above it.
            out[name] = PhysicalLimit(
                min=val + margin,
                max=float("inf"),
                unit=unit,
                reason=f"FSM-declared fault value: {val}{unit}",
            )
    return out


def extract_pdf_section_with_specs(
    pdf_path: Path, pages: list[int], *, max_x: float = 135.0
) -> list[dict[str, Any]]:
    """Like `extract_pdf_section` but also collects the spec text per param.

    The spec column in Nissan FSM tables is to the right of the name column;
    a single param often spans multiple sub-rows (one per operating condition),
    so we accumulate every right-side text fragment we see for that name into
    a single combined string. `parse_ranges()` then pulls structured numeric
    ranges out of the combined text.

    Output schema per param:
      {
        "name": str,
        "page": int,                 # first page seen
        "spec_text": str,            # combined right-column text, " | " joined
        "expected_ranges": list[{"min", "max", "unit"}],
      }
    """
    by_name: dict[str, dict[str, Any]] = {}
    with pdfplumber.open(pdf_path) as pdf:
        for page_no in pages:
            page = pdf.pages[page_no - 1]
            words = page.extract_words(use_text_flow=False)
            rows: dict[float, list[tuple[float, str]]] = defaultdict(list)
            for w in words:
                rows[round(w["top"] / 2) * 2].append((w["x0"], w["text"]))

            # Walk rows top-to-bottom. A row with a param-name in its left
            # column starts a new attribution scope; subsequent rows without
            # a name (continuation of multi-row condition/spec) contribute
            # their right-column text to the most recent name's spec.
            current_name: str | None = None
            for top in sorted(rows):
                ordered = sorted(rows[top])
                left_words = [t for x, t in ordered if x < max_x]
                right_words = [t for x, t in ordered if x >= max_x]
                left_text = " ".join(left_words).strip()
                right_text = " ".join(right_words).strip()

                if looks_like_param_name(left_text):
                    current_name = clean_param_name(left_text)
                    entry = by_name.setdefault(
                        current_name,
                        {"name": current_name, "page": page_no, "spec_lines": []},
                    )
                    if right_text:
                        entry["spec_lines"].append(right_text)
                elif current_name is not None and right_text:
                    # Continuation row — attribute its spec text to the
                    # parameter we're currently inside.
                    by_name[current_name]["spec_lines"].append(right_text)

    out: list[dict[str, Any]] = []
    for entry in by_name.values():
        joined = " | ".join(entry["spec_lines"])
        ranges = parse_ranges(joined)
        out.append(
            {
                "name": entry["name"],
                "page": entry["page"],
                "spec_text": joined,
                "expected_ranges": ranges,
                "spec_kind": classify_spec(joined, ranges),
            }
        )
    return out
