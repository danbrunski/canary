# LLM-assist structured-card contract (Task S.1)

Companion to `docs/roadmap/llm-assist-and-compute-roadmap.md` — this is the
schema deliverable for the checklist's S.1 item. **No producer exists.**
This document (and the Pydantic models it's generated from,
`src/analysis/llm_assist/cards.py`) is the seam a future LLM-assist
producer fills in; nothing here calls a model.

## The two card shapes

- **`DifferentialCard`** (case 4 — structured fault-hypothesis generation):
  a ranked list of `DiagnosticHypothesis`, each requiring at least one
  supporting-evidence reference. `framing` carries the mandatory
  "hypotheses, not verdicts" disclaimer — the roadmap's "Diagnostic safety
  discipline" risk explicitly requires this on every diagnostic output,
  since a 4B-class model *will* sometimes be confidently wrong about
  causation and the differential-not-verdict framing is the only reason
  that's tolerable. The field is schema-locked (a `field_validator`
  overwrites whatever value is supplied with the canonical text at
  validation time) so a producer cannot omit, reword, or drop it.
- **`NarrativeCard`** (cases 1/2/6 — session narrative reports,
  cross-session pattern recognition, fuel-map narration): sectioned
  heading/body prose plus `source_sessions`, the sessions a narrative claim
  traces back to — the report-level equivalent of the differential card's
  per-hypothesis evidence references.

Both carry `model_name: str | None` for producer provenance — always `None`
today.

## Regenerating this schema

The JSON Schema below is `pydantic.TypeAdapter(LlmAssistCard).json_schema()`
against `src.analysis.llm_assist.cards`, pasted as of this task. To
regenerate after a schema change:

```
.venv/bin/python -c "
import json
from pydantic import TypeAdapter
from src.analysis.llm_assist.cards import LlmAssistCard
print(json.dumps(TypeAdapter(LlmAssistCard).json_schema(), indent=2, sort_keys=True))
"
```

`tests/test_llm_assist_cards.py` pins the contract (round-trip, the locked
framing, the min-length evidence/session requirements) so a schema change
here fails a test rather than silently drifting from this doc.

```json
{
  "$defs": {
    "DiagnosticHypothesis": {
      "additionalProperties": false,
      "properties": {
        "confidence": {
          "anyOf": [
            { "maximum": 1.0, "minimum": 0.0, "type": "number" },
            { "type": "null" }
          ],
          "default": null,
          "title": "Confidence"
        },
        "label": { "title": "Label", "type": "string" },
        "rank": { "minimum": 1, "title": "Rank", "type": "integer" },
        "refuting_evidence": {
          "items": { "$ref": "#/$defs/EvidenceReference" },
          "title": "Refuting Evidence",
          "type": "array"
        },
        "supporting_evidence": {
          "items": { "$ref": "#/$defs/EvidenceReference" },
          "minItems": 1,
          "title": "Supporting Evidence",
          "type": "array"
        }
      },
      "required": ["rank", "label", "supporting_evidence"],
      "title": "DiagnosticHypothesis",
      "type": "object"
    },
    "DifferentialCard": {
      "additionalProperties": false,
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
        "generated_at": { "title": "Generated At", "type": "string" },
        "hypotheses": {
          "items": { "$ref": "#/$defs/DiagnosticHypothesis" },
          "minItems": 1,
          "title": "Hypotheses",
          "type": "array"
        },
        "model_name": {
          "anyOf": [{ "type": "string" }, { "type": "null" }],
          "default": null,
          "title": "Model Name"
        },
        "session": {
          "anyOf": [{ "type": "string" }, { "type": "null" }],
          "default": null,
          "title": "Session"
        }
      },
      "required": ["generated_at", "hypotheses"],
      "title": "DifferentialCard",
      "type": "object"
    },
    "EvidenceReference": {
      "additionalProperties": false,
      "properties": {
        "channel": {
          "anyOf": [{ "type": "string" }, { "type": "null" }],
          "default": null,
          "title": "Channel"
        },
        "description": { "title": "Description", "type": "string" },
        "detail": {
          "anyOf": [{ "type": "string" }, { "type": "null" }],
          "default": null,
          "title": "Detail"
        },
        "session": {
          "anyOf": [{ "type": "string" }, { "type": "null" }],
          "default": null,
          "title": "Session"
        },
        "timestamp": {
          "anyOf": [{ "type": "string" }, { "type": "null" }],
          "default": null,
          "title": "Timestamp"
        }
      },
      "required": ["description"],
      "title": "EvidenceReference",
      "type": "object"
    },
    "NarrativeCard": {
      "additionalProperties": false,
      "properties": {
        "card_type": {
          "const": "narrative",
          "default": "narrative",
          "title": "Card Type",
          "type": "string"
        },
        "generated_at": { "title": "Generated At", "type": "string" },
        "model_name": {
          "anyOf": [{ "type": "string" }, { "type": "null" }],
          "default": null,
          "title": "Model Name"
        },
        "sections": {
          "items": { "$ref": "#/$defs/NarrativeSection" },
          "minItems": 1,
          "title": "Sections",
          "type": "array"
        },
        "source_sessions": {
          "items": { "type": "string" },
          "minItems": 1,
          "title": "Source Sessions",
          "type": "array"
        },
        "title": { "title": "Title", "type": "string" }
      },
      "required": ["generated_at", "title", "sections", "source_sessions"],
      "title": "NarrativeCard",
      "type": "object"
    },
    "NarrativeSection": {
      "additionalProperties": false,
      "properties": {
        "body": { "title": "Body", "type": "string" },
        "heading": { "title": "Heading", "type": "string" }
      },
      "required": ["heading", "body"],
      "title": "NarrativeSection",
      "type": "object"
    }
  },
  "anyOf": [
    { "$ref": "#/$defs/DifferentialCard" },
    { "$ref": "#/$defs/NarrativeCard" }
  ]
}
```

## Example: a differential card

```json
{
  "card_type": "differential",
  "generated_at": "2026-07-20T18:00:00+00:00",
  "session": "2026-07-20T170000",
  "hypotheses": [
    {
      "rank": 1,
      "label": "possible MAF sensor drift",
      "confidence": 0.4,
      "supporting_evidence": [
        {
          "description": "STFT bank 1 trending +8% over last 20 min at cruise",
          "channel": "obd:SHORT_FUEL_TRIM_1",
          "session": "2026-07-20T170000"
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
  "card_type": "narrative",
  "generated_at": "2026-07-20T18:00:00+00:00",
  "title": "Session summary — 2026-07-20T170000",
  "sections": [
    {
      "heading": "Fuel trims",
      "body": "Bank 1 STFT held within +/-3% for the whole drive; no LTFT drift."
    }
  ],
  "source_sessions": ["2026-07-20T170000"],
  "model_name": null
}
```
