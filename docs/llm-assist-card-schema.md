# LLM-assist structured-card contract (Tasks S.1, S.5; E01.2)

Companion to `docs/roadmap/llm-assist-and-compute-roadmap.md` — this is the
schema deliverable for the checklist's S.1 item, revised for S.5 (evidence
references resolve) under `docs/roadmap/tasks/E01-evidence-and-memory.md`.
**No producer exists.** This document (and the Pydantic models it's
generated from, `src/analysis/llm_assist/cards.py`) is the seam a future
LLM-assist producer fills in; nothing here calls a model.

## The two card shapes

- **`DifferentialCard`** (case 4 — structured fault-hypothesis generation):
  a ranked list of `DiagnosticHypothesis`, each requiring at least one
  supporting evidence citation. `framing` carries the mandatory
  "hypotheses, not verdicts" disclaimer, schema-locked (a validator
  overwrites any supplied value with the canonical text).
- **`NarrativeCard`** (cases 1/2/6): sectioned heading/body prose; each
  section cites the evidence it rests on, at least one citation per card.

Both carry `schema: "llm-assist-card/2"`, `vehicle` (the profile slug whose
memory ledger the citations resolve on) and `model_name` for producer
provenance — always `None` today.

## Evidence is cited by id; numbers come from the record

The first contract carried a free-text `EvidenceReference`, so a producer
could cite a session or a value no tool returned and the card still
validated. Now:

- A citation (`EvidenceCitation`) is an `evidence_id` minted by an analysis
  tool on the vehicle's memory ledger (`src.evidence`, e.g.
  `src.analysis.compare_evidence.mint_channel_comparison`), a `note` in
  words, and optional `quoted` values.
- Card prose — hypothesis labels, notes, titles, headings, bodies — cannot
  state a number: any token holding a digit is refused (`+8%`, `3.5`,
  `bank 1`, `3000rpm`, `80kPa`, `1e3`, `x3`, dates) unless it has an
  identifier's form — `0x` hex, a namespaced or upper-case underscored name
  (`obd:SHORT_FUEL_TRIM_1`, `did:0x1206`), or an upper-case-led code (`B1`,
  `P0171`). Numbers reach
  the operator only rendered from the resolved record, with their units.
- `resolve_card` (used by `LlmAssistCardStore.append`, which refuses, and by
  `GET /analysis/llm-assist/cards`, which labels) rejects an id no tool
  minted, an id or card for another vehicle, a quoted value that differs
  from the record (or names a value it lacks), and a `session` /
  `source_sessions` entry that no cited evidence read. An id whose stored
  line no longer validates is reported as a corrupt record. A superseded
  citation still resolves and carries `superseded_at`/`superseded_by`; a
  card citing a *retracted* record resolves as `retracted`, never
  `verified`, and the store refuses to append it.
- A card written under the first contract (no `schema` key) loads as
  `UnverifiedHistoricalCard`: kept verbatim, labelled "unverified historical
  narrative", never resolved and never shown as a finding.

The endpoint returns each card as
`{verification: "verified" | "retracted" | "rejected" | "unverified_historical", problems,
card, evidence}`, `evidence` mapping each resolved id to its tool, conditions,
times, sources, values (with units), quality, result status and supersession.

## Regenerating this schema

The JSON Schema below is
`pydantic.TypeAdapter(LlmAssistCard).json_schema(by_alias=True)` against
`src.analysis.llm_assist.cards`. To regenerate after a schema change:

```
.venv/bin/python -c "
import json
from pydantic import TypeAdapter
from src.analysis.llm_assist.cards import LlmAssistCard
print(json.dumps(TypeAdapter(LlmAssistCard).json_schema(by_alias=True), indent=2, sort_keys=True))
"
```

`tests/test_llm_assist_cards.py` pins the contract (round-trip, the locked
framing, the numeral refusal, resolution refusals, historical cards) so a
schema change here fails a test rather than silently drifting from this doc.

```json
{
  "$defs": {
    "DiagnosticHypothesis": {
      "additionalProperties": false,
      "description": "One ranked hypothesis in a :class:`DifferentialCard`.\n\n``rank`` is 1-indexed, most-likely first. At least one supporting\ncitation is required \u2014 an unsupported hypothesis has nothing for the\noperator to verify against.",
      "properties": {
        "confidence": {
          "anyOf": [
            {
              "maximum": 1.0,
              "minimum": 0.0,
              "type": "number"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Confidence"
        },
        "label": {
          "title": "Label",
          "type": "string"
        },
        "rank": {
          "minimum": 1,
          "title": "Rank",
          "type": "integer"
        },
        "refuting_evidence": {
          "items": {
            "$ref": "#/$defs/EvidenceCitation"
          },
          "title": "Refuting Evidence",
          "type": "array"
        },
        "supporting_evidence": {
          "items": {
            "$ref": "#/$defs/EvidenceCitation"
          },
          "minItems": 1,
          "title": "Supporting Evidence",
          "type": "array"
        }
      },
      "required": [
        "rank",
        "label",
        "supporting_evidence"
      ],
      "title": "DiagnosticHypothesis",
      "type": "object"
    },
    "DifferentialCard": {
      "additionalProperties": false,
      "description": "Ranked differential diagnosis (case 4).\n\n``framing`` is locked, not merely defaulted: the validator overwrites\nwhatever a producer supplies with :data:`HYPOTHESES_NOT_VERDICTS_FRAMING`.\n``vehicle`` is the profile slug whose memory ledger the citations resolve\nagainst.",
      "properties": {
        "card_type": {
          "const": "differential",
          "default": "differential",
          "title": "Card Type",
          "type": "string"
        },
        "framing": {
          "default": "These are ranked hypotheses to weigh against the evidence shown, not a diagnosis or a repair instruction. Verify against the referenced evidence (and a real diagnostic procedure) before acting on any of them.",
          "title": "Framing",
          "type": "string"
        },
        "generated_at": {
          "title": "Generated At",
          "type": "string"
        },
        "hypotheses": {
          "items": {
            "$ref": "#/$defs/DiagnosticHypothesis"
          },
          "minItems": 1,
          "title": "Hypotheses",
          "type": "array"
        },
        "model_name": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Model Name"
        },
        "schema": {
          "const": "llm-assist-card/2",
          "default": "llm-assist-card/2",
          "title": "Schema",
          "type": "string"
        },
        "session": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Session"
        },
        "vehicle": {
          "minLength": 1,
          "title": "Vehicle",
          "type": "string"
        }
      },
      "required": [
        "vehicle",
        "generated_at",
        "hypotheses"
      ],
      "title": "DifferentialCard",
      "type": "object"
    },
    "EvidenceCitation": {
      "additionalProperties": false,
      "description": "One evidence record a claim cites, by the id its tool returned.\n\n``quoted`` is optional: values the producer asserts, by the record's value\nname. Resolution refuses a quoted name the record lacks or a value that\ndiffers from it; what is displayed is always the record's value.",
      "properties": {
        "evidence_id": {
          "minLength": 1,
          "title": "Evidence Id",
          "type": "string"
        },
        "note": {
          "default": "",
          "title": "Note",
          "type": "string"
        },
        "quoted": {
          "additionalProperties": {
            "type": "number"
          },
          "title": "Quoted",
          "type": "object"
        }
      },
      "required": [
        "evidence_id"
      ],
      "title": "EvidenceCitation",
      "type": "object"
    },
    "NarrativeCard": {
      "additionalProperties": false,
      "description": "Sectioned narrative report (cases 1/2/6).\n\nAt least one section cites evidence, and every ``source_sessions`` entry\nmust be a source of some cited record \u2014 a session no tool read cannot be\nclaimed.",
      "properties": {
        "card_type": {
          "const": "narrative",
          "default": "narrative",
          "title": "Card Type",
          "type": "string"
        },
        "generated_at": {
          "title": "Generated At",
          "type": "string"
        },
        "model_name": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Model Name"
        },
        "schema": {
          "const": "llm-assist-card/2",
          "default": "llm-assist-card/2",
          "title": "Schema",
          "type": "string"
        },
        "sections": {
          "items": {
            "$ref": "#/$defs/NarrativeSection"
          },
          "minItems": 1,
          "title": "Sections",
          "type": "array"
        },
        "source_sessions": {
          "items": {
            "type": "string"
          },
          "minItems": 1,
          "title": "Source Sessions",
          "type": "array"
        },
        "title": {
          "title": "Title",
          "type": "string"
        },
        "vehicle": {
          "minLength": 1,
          "title": "Vehicle",
          "type": "string"
        }
      },
      "required": [
        "vehicle",
        "generated_at",
        "title",
        "sections",
        "source_sessions"
      ],
      "title": "NarrativeCard",
      "type": "object"
    },
    "NarrativeSection": {
      "additionalProperties": false,
      "description": "One heading/body pair in a :class:`NarrativeCard`, with its evidence.",
      "properties": {
        "body": {
          "title": "Body",
          "type": "string"
        },
        "evidence": {
          "items": {
            "$ref": "#/$defs/EvidenceCitation"
          },
          "title": "Evidence",
          "type": "array"
        },
        "heading": {
          "title": "Heading",
          "type": "string"
        }
      },
      "required": [
        "heading",
        "body"
      ],
      "title": "NarrativeSection",
      "type": "object"
    }
  },
  "anyOf": [
    {
      "$ref": "#/$defs/DifferentialCard"
    },
    {
      "$ref": "#/$defs/NarrativeCard"
    }
  ]
}
```

## Example: a differential card

```json
{
  "schema": "llm-assist-card/2",
  "card_type": "differential",
  "vehicle": "nissan-xterra-2008",
  "generated_at": "2026-07-20T18:00:00+00:00",
  "session": "2026-07-20T170000",
  "hypotheses": [
    {
      "rank": 1,
      "label": "possible MAF sensor drift",
      "confidence": null,
      "supporting_evidence": [
        {
          "evidence_id": "ev-3f9c0d1e2a4b5c6d7e8f9a0b",
          "note": "STFT bank one trending rich at cruise",
          "quoted": {}
        }
      ],
      "refuting_evidence": []
    }
  ],
  "framing": "These are ranked hypotheses to weigh against the evidence shown, not a diagnosis or a repair instruction. Verify against the referenced evidence (and a real diagnostic procedure) before acting on any of them.",
  "model_name": null
}
```

## Example: a narrative card

```json
{
  "schema": "llm-assist-card/2",
  "card_type": "narrative",
  "vehicle": "nissan-xterra-2008",
  "generated_at": "2026-07-20T18:00:00+00:00",
  "title": "Session summary",
  "sections": [
    {
      "heading": "Fuel trims",
      "body": "Bank one STFT stayed inside its band for the whole drive.",
      "evidence": [{ "evidence_id": "ev-3f9c0d1e2a4b5c6d7e8f9a0b", "note": "", "quoted": {} }]
    }
  ],
  "source_sessions": ["2026-07-20T170000"],
  "model_name": null
}
```
