# LLM-assist & compute roadmap (future, optional)

Forward-looking planning for an **optional** on-device LLM-assist tier and
the compute/memory/accelerator decisions around it. Nothing here is
committed or required: the goal is to keep the architectural seams in place
so these capabilities can be added later without rework, and to record the
reasoning and trigger conditions so the decisions are not re-litigated.
None of this runs on, or is a dependency of, the real-time capture +
detection path. Companion to Phase 9 in `docs/implementation-tasks.md`
(selectable TCN/LSTM sequence detector) and Phase 7 (feature-spec safety,
alert fusion).

## Status

Not started. Optional add-on. The base product (capture + Isolation Forest
+ sequence autoencoder + dashboard) must remain fully functional and lean
with this tier absent. The discipline that makes "leave the hooks" free:
the analysis/LLM code must be import-isolated and feature-flagged — **no
`torch`/LLM import on the capture hot path, no RAM reservation, no daemon
dependency when disabled**, and dashboard cards degrade cleanly when the
tier is not present.

## Deployment assumptions (the constraints that drive every decision)

- Field-deployed in a vehicle; **limited or no internet** (no cloud LLM,
  models pre-provisioned offline on the Pi image).
- **No reliable companion**: a laptop is *not* dependably present. The
  phone/tablet is a **thin client only** — a browser pointed at the Pi's
  existing PWA dashboard. It does not run models.
- Target hardware: Raspberry Pi 5, 16 GB, likely a sealed enclosure
  (thermal-constrained), drawing vehicle power.
- The base CANary daemon (capture + detection + dashboard) is the
  always-on, safety-relevant workload and must not be jeopardised to make
  room for an optional tier.

## The optional LLM-assist feature set

All six are **post-drive / review-time / batch** — none on the 1 Hz live
path. This temporal disjointness from live capture is load-bearing for
every decision below.

1. **Session narrative reports** — batch a full Parquet session into a
   structured report (LTFT trends, anomaly counts by operating mode,
   recurring patterns). The real purpose of `src/analysis/`: post-session
   analysis, not reactive per-anomaly interpretation.
2. **Cross-session pattern recognition** — summarise the last N sessions'
   anomaly logs and reason about longitudinal degradation ("bank 1 LTFT
   +6%→+14% at highway cruise over two weeks") for early warning.
3. **Grounded RAG over own data** — small on-device vector store
   (sqlite-vec/Chroma) over anomaly-event history; "have I seen this
   before, when, what was the outcome?" — a vehicle memory system.
4. **Structured fault-hypothesis generation** — ranked differential
   diagnosis with supporting/refuting evidence from current PIDs, emitted
   as JSON the dashboard renders as a structured card.
5. **Active labeling partner (highest value)** — at anomaly review the
   model proposes a hypothesis and asks one targeted clarifying question
   ("possible MAF fault — were you on a dirt road or in heavy rain?"); the
   one-word answer enriches the label into the retraining ledger. Builds a
   far richer supervised dataset than binary FP/TP.
6. **Fuel-map analysis narration** — interpret the LTFT correction surface
   from `src/analysis/fuel_analysis.py` ("persistent +10–15% bank 1 at
   1500–2500 RPM × 40–60% load — what to investigate first").

## Decisions reached (with rationale)

**D1 — The LLM tier is offline/post-drive, temporally disjoint from live
capture.** All six cases run after the drive or during review. The live
detector runs *during* the drive. They essentially never contend for CPU.
This is why concurrent-contention engineering (incl. the Hailo, see D5) is
*not* the justification it first appears to be.

**D2 — On-Pi only in the real scenario.** With no reliable laptop and
limited internet, if the tier runs at all it runs on the Pi. The earlier
"offload heavy LLM to the companion" idea is viable *only* as a footnote
for the rare case a laptop is present — it is not the field path.

**D3 — Phone/tablet is a thin client.** LLM outputs surface as
structured dashboard cards via the existing PWA (service worker / manifest
already in place). No model runs on the handheld.

**D4 — Model sizing: design the seams around a resident Qwen3-4B
(Q4_K_M).** Rationale:

- 16 GB RAM with a resident capture daemon + OS + dashboard + (optional)
  vector store leaves ~9–11 GB usable for an LLM *with* a field safety
  margin. GGUF sizing rule ≈ 0.6 GB/1B params at Q4.
- **Qwen3-4B Q4 ≈ ~3 GB + KV** → comfortably resident, ~6–12 tok/s.
  Sweet spot for "resident alongside the daemon, decent reasoning."
- **Qwen3-8B Q4 ≈ ~5 GB + multi-GB KV** for long session contexts → fits
  with a lean daemon but eats the margin; ~3–6 tok/s. A *measured*
  stretch, not the baseline.
- **<4B (e.g. 1.7B)** → fits trivially but is too shallow for cases 2/4/6
  (the plan's own original concern). Don't go below 4B for the
  diagnosis-grade cases.
- **Qwen3-30B-A3B** (MoE; the original plan target) → Q3 ≈ ~15 GB, does
  **not** co-reside with a running daemon in 16 GB; only viable via a
  post-drive workload swap (D6 option B) or on a companion. Keep it as a
  companion-only footnote.
- Update vs the original "7B vs 1.5B" framing: Qwen3-4B with thinking mode
  reasons materially better than older 4B models — the cliff between
  "resident-small" and "useful-for-diagnosis" is *narrower* than the plan
  assumed, so a single resident 4B plausibly covers most of all six
  acceptably and collapses tiering complexity.
- Do **not** over-quantize to claw back RAM: 4B at Q4_K_M is right;
  Q3/Q2 on a 4B trades away the one thing the model exists for. To save
  RAM, drop a size tier, not quant precision.
- "Smaller" is over-determined here — it's the right call independently on
  RAM fit, post-drive **power** draw, and **thermal** soak (shorter
  inference = less time at high CPU in a sealed enclosure on vehicle
  power).

**D5 — Hailo NPU is a documented contingency, not P1.** Clarifications:

- The Hailo (8L/8) accelerates quantized **CNN/Conv** graphs. It does
  **not** run decoder LLMs (Qwen). Qwen always runs on the Pi CPU. The
  Hailo's only possible role is offloading the *one neural detector* (the
  sequence autoencoder); IF is a tree ensemble and stays on CPU.
- Its real (and only) justification under the field scenario: permanently
  remove the sequence-AE's RAM/CPU/thermal footprint so a resident on-Pi
  LLM tier has more of the 16 GB budget and the enclosure has more thermal
  headroom. It is **not** justified by drive-time contention (D1).
- This is why Phase 9 builds the TCN as a clean Conv1D **autoencoder**
  with export-clean ops: it keeps the NPU option open at zero present
  cost. The LSTM cannot be offloaded to a Hailo effectively; a
  Conv1D TCN-AE can.
- **Adopt-Hailo trigger:** only if (a) the post-drive workload swap (D6-B)
  proves operationally unsafe in the field *and* (b) a resident small
  model (D6-A) proves too shallow for cases 2/4/6 — both *measured*, not
  assumed. Adopting it adds an int8-quantization + on-device
  threshold-recalibration workstream (see Risks).

**D6 — On-Pi hosting options (decide later, behind a measurement pass):**

- **A. Small resident model (Qwen3-4B), always co-resident.** Powers the
  interactive cases (5, 4) at review time with the daemon untouched;
  heavy cases (1–3, 6) get shallower output. Lowest risk/capability.
  *Recommended default to design around.*
- **B. Big model via post-drive workload swap.** Daemon quiesces when
  the session ends and the vehicle is parked; LLM loads, runs, unloads;
  daemon resumes. Full capability; the hard, risky part is safely
  pausing/resuming the safety-critical capture daemon in the field — that
  lifecycle is the real engineering, not the LLM.
- **C. Hailo offload → larger model co-resident.** Per D5; contingency
  only, with the stated trigger.

## Risks / open technical issues

- **Power budget.** When does a multi-minute batch report run — engine on
  (alternator) or parked-and-off (battery)? A long 30B-class run after
  shutdown draws vehicle battery with no charging. May by itself force
  "reports only while the engine is running" or push toward D6-A.
- **Thermal.** Sustained LLM inference in a sealed enclosure on a Pi 5
  needs an active-cooling/throttling plan; interacts with HAT stacking if
  D5 is ever taken.
- **Quantization integrity (only if D5/NPU taken).** The detector's
  decision is a calibrated reconstruction-error threshold. int8
  quantization perturbs the reconstruction-error *distribution* and can
  move the false-positive rate off the ≤1 FP/hr budget. Post-quant
  **on-device threshold recalibration is mandatory**, and quantization
  error on a regression-style (reconstruction) output is worse-behaved
  than on a classification logit. This workstream is absent from Phase 9
  by design (contingency-gated) and must be added if D5 is triggered.
- **Diagnostic safety discipline.** A 4B *will* sometimes be confidently
  wrong about fault causation (cases 2/6). Tolerable **only** because the
  output contract is a differential — ranked hypotheses with
  supporting/refuting evidence the operator verifies — never an
  assertion/verdict. This framing is mandatory for *all* diagnostic
  outputs, not just case 4.

## Recommended sequencing

1. **Build the seams now, commit to no model hosting yet:** the
   `src/analysis/` module boundary, the case-5 label-capture loop
   (hypothesis + one clarifying question → enriched label → retraining
   ledger — highest value, barely needs model size), and the JSON
   structured-card dashboard contract. All import-isolated and
   feature-flagged per Status.
2. **One measurement pass before choosing D6-A/B/C:** actual resident
   daemon RAM/thermal; acceptable report latency at the chosen tok/s;
   whether pausing the capture daemon in-field is acceptable; real
   power draw for a representative report.
3. **Default to D6-A (resident Qwen3-4B).** Treat deep-diagnosis quality
   as best-effort, not a guarantee.
4. **D6-B/C only if step 2 forces them**, with the D5 trigger for Hailo.

## Non-goals (explicitly not committed)

- Running an LLM on the live 1 Hz path or as a daemon dependency.
- Running Qwen on the Hailo (not possible).
- Cloud/API inference (field, limited internet).
- Removing the LSTM detector (Phase 9 makes the sequence detector
  *selectable*, not LSTM-replaced-and-deleted).
- Buying/integrating the Hailo before its D5 trigger is met by measurement.

## Cross-references

- `docs/implementation-tasks.md` — Phase 9 (selectable TCN/LSTM sequence
  autoencoder — since shipped), Phase 7 (feature-spec safety 7.1/7.3,
  alert fusion 7.2).
- `docs/companion-app-plan.md` — the companion path referenced in D2.
- `docs/daemon-architecture.md` — capture/detection runtime the base tier
  must not regress.
- `docs/mcp-server.md` — the MCP tool surface that lets Claude (and an
  optional on-Pi Qwen) inspect daemon state, query telemetry, and drive
  the labeling loop in feature 5. The MCP server is the data hook every
  feature here implicitly depends on.

## Implementation checklist — seams only (sequencing step 1)

No model hosting. Everything here must satisfy the Status discipline:
import-isolated, feature-flagged, no daemon dependency when disabled,
dashboard cards degrade cleanly when the tier is absent.

- [ ] **S.1 — Structured-card contract.** Define the JSON schema for
  LLM-assist outputs (differential card: ranked hypotheses, each with
  supporting/refuting evidence references and the mandatory
  "hypotheses-not-verdicts" framing; narrative card: sectioned text +
  source-session references). Pydantic models + JSON-schema doc; the
  producer is absent for now, the contract is the seam.
- [ ] **S.2 — Dashboard card renderer.** A panel/card component that
  renders S.1 documents from a feature-flagged endpoint, with an
  explicit empty/absent state when the tier is disabled (the default).
  FE tests over a fixture card.
- [ ] **S.3 — Enriched-label capture loop (case 5, model-free core).**
  Extend the anomaly-review flow so a review can attach a hypothesis
  string + one clarifying question/answer pair to the feedback record
  (`src/alerts/feedback.py` schema + `anomaly_feedback.jsonl`), flowing
  into the retraining ledger like existing verdicts. Works today with
  operator-authored text; an LLM later fills the same fields. Tests for
  schema round-trip + retrain consumption.
- [ ] **S.4 — Analysis module boundary.** Ensure any future LLM code
  has an import-isolated home (`src/analysis/llm_assist/` or similar)
  with a feature flag in config (default off) and a test asserting the
  capture path imports no torch/LLM modules when disabled.
