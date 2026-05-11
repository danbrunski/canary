"""Bind captured Mode 22 DIDs to FSM-named CONSULT-III parameters.

Three signals feed into a binding:

  1. **Module attribution** — the diagnostic CAN ID the DID was read from
     determines which FSM module's name list is even a candidate.
     0x7E0 / 0x7E8 → ECM is the only mapping we've proven on this truck;
     others are TBD until we have NT510 captures of those modules.

  2. **Tag-label fuzzy match** — the user's tag (`ecm_livedata_maf`) and
     the FSM name (`MASS AIRFLOW`) use different nomenclature for the same
     thing. A small alias table bridges common scan-tool shorthands to
     FSM token vocabulary; matching is then token-overlap with a
     specificity weight.

  3. **Single-window concentration** — already surfaced by the analyzer
     (the ★ marker). For those DIDs the binding is high-confidence:
     the one tag's label *is* the most likely FSM name, given a fuzzy
     match exists.

Multi-window DIDs (DIDs the NT510 always polls regardless of which
live-data screen is active) deliberately get NO name suggestion: they're
housekeeping (battery V, ECM internals), and proposing a name based on
any one of N matching tags would be misleading.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

# CAN ID → module name. Hardcoded for now; populated as we confirm mappings
# from NT510 captures. SAE-standard physical addressing range, plus the
# Nissan manufacturer-specific bits we've seen (0x740 currently unknown).
CAN_ID_TO_MODULE: dict[int, str] = {
    0x7E0: "ECM",       # confirmed on canary truck
    0x7E8: "ECM",       # confirmed on canary truck
    0x7E1: "TCM_5AT",   # would be TCM on AT builds; M/T returns NoComm
    0x7E9: "TCM_5AT",
    # 0x7E2 / 0x7EA → likely ABS, TBD
    # 0x7E3 / 0x7EB → likely BCM, TBD
    # 0x740-0x747   → Nissan body / chassis range, TBD
}


# Common scan-tool / informal shorthands → tokens that appear in FSM names.
# Each tag-label token gets expanded through this map before token-overlap
# scoring against FSM names.
_ALIASES: dict[str, set[str]] = {
    "maf": {"maf", "mas", "mass", "airflow", "a/f"},
    "rpm": {"rpm", "eng", "engine", "speed"},
    "engspeed": {"eng", "engine", "speed"},
    "coolant": {"coolant", "coolan", "temp"},
    "throttle": {"throttle", "thl", "tp", "thrtl"},
    "steering": {"steering", "str"},
    "steeringangle": {"steering", "str", "angle"},
    "speed": {"speed", "vhcl", "vehicle"},
    "vehiclespeed": {"speed", "vhcl", "vehicle"},
    "battery": {"battery", "batt", "volt"},
    "voltage": {"volt", "battery"},
    "fuel": {"fuel"},
    "ignition": {"ignition", "ign", "ignit"},
    "brake": {"brake", "brk"},
    "wheel": {"wheel", "sensor"},
    "wheelspeed": {"wheel", "sensor", "speed"},
    "wheelspeeds": {"wheel", "sensor", "speed"},
    "door": {"door", "dr"},
    "wiper": {"wiper", "wip"},
    "fan": {"fan"},
    "ac": {"ac", "a/c", "air", "cond"},
    "headlamp": {"headlamp", "hl", "headlight"},
    "gear": {"gear", "shift", "shft"},
    "transfer": {"transfer", "trans", "4wd", "awd"},
    "transmission": {"trans", "transmission"},
    "ipdm": {"ipdm"},
    "iac": {"iac"},
}

# Tag label tokens we strip before matching — they're workflow markers,
# not signal hints.
_TAG_NOISE_TOKENS: frozenset[str] = frozenset(
    {
        "select",
        "module",
        "livedata",
        "live",
        "data",
        "open",
        "start",
        "end",
        "test",
        "active",
        "baseline",
        "phase",
        "ecm",
        "tcm",
        "abs",
        "bcm",
        "ipdm",  # also an alias source, but as a *prefix* token it's noise
    }
)


@dataclass(frozen=True)
class ParamRef:
    """One FSM-named parameter under a particular module."""

    name: str
    module: str
    page: int


@dataclass(frozen=True)
class SignalCatalog:
    """Loaded `data/fsm/signals.json` indexed for lookup."""

    by_module: dict[str, list[ParamRef]]

    @property
    def modules(self) -> list[str]:
        return sorted(self.by_module)

    def for_module(self, module: str) -> list[ParamRef]:
        return self.by_module.get(module, [])


@dataclass(frozen=True)
class NameMatch:
    """A scored candidate FSM name for a captured DID."""

    name: str
    module: str
    page: int
    score: float
    why: str  # which tokens matched, for transparency


def load_catalog(path: Path) -> SignalCatalog:
    """Load `data/fsm/signals.json` into a lookup-ready catalog."""
    payload = json.loads(path.read_text())
    by_module: dict[str, list[ParamRef]] = {}
    for module in payload["modules"]:
        by_module[module["module"]] = [
            ParamRef(name=p["name"], module=module["module"], page=int(p["page"]))
            for p in module["params"]
        ]
    return SignalCatalog(by_module=by_module)


def module_for_can_id(can_id: int) -> str | None:
    """Map a diagnostic CAN ID to its module, if known."""
    return CAN_ID_TO_MODULE.get(can_id)


# Tokens are lowercase alphanumeric runs; everything else is a separator.
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


def _expand_tag_terms(tag_label: str) -> set[str]:
    """Tag label → set of search terms after alias expansion + noise strip."""
    raw = _tokens(tag_label) - _TAG_NOISE_TOKENS
    expanded = set(raw)
    for token in raw:
        if token in _ALIASES:
            expanded |= _ALIASES[token]
    return expanded


def fuzzy_match_tag(
    tag_label: str, candidates: list[ParamRef], *, top_k: int = 3
) -> list[NameMatch]:
    """Score `candidates` against `tag_label`. Returns the top `top_k`.

    Scoring is `|matched_tokens| / |fsm_name_tokens|` — specificity-weighted
    overlap. A single-token FSM name like `4WD MODE` matched on one token
    scores higher than a multi-token name matched on the same one token,
    because the match accounts for a larger fraction of the candidate's
    identity.
    """
    terms = _expand_tag_terms(tag_label)
    if not terms:
        return []

    scored: list[NameMatch] = []
    for cand in candidates:
        name_tokens = _tokens(cand.name)
        if not name_tokens:
            continue
        matched = terms & name_tokens
        if not matched:
            continue
        score = len(matched) / len(name_tokens)
        scored.append(
            NameMatch(
                name=cand.name,
                module=cand.module,
                page=cand.page,
                score=score,
                why=f"matched {','.join(sorted(matched))}",
            )
        )

    scored.sort(key=lambda m: (-m.score, m.name))
    return scored[:top_k]
