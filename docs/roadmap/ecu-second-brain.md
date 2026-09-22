# ECU second brain — persistent understanding and adaptive models

Recorded 2026-09-22 from the operator discussion. **Exploratory roadmap;
implementation is not started or authorized by this document.** Checkboxes
below are future work, not additions to the overnight queue. Model choices
are candidates pending measurement, not new operator-approved defaults.

## Purpose

Make CANary a supervisory brain that remembers the vehicle's history,
models what should happen, investigates discrepancies, and learns whether
a change actually helped. The ECU continues running the engine. CANary
builds understanding across drives and experiments:

**Observe → predict → investigate → propose → verify → remember.**

The useful output is an evidence-backed answer to four questions: what
happened, what might explain it, what remains unknown, and what measurement
would resolve that uncertainty. This extends telemetry and anomaly alerts
into a persistent diagnostic and performance-analysis workbench.

Existing foundations include RAM windows, commanded-versus-measured
analysis, behavior surfaces, physical cross-checks, tune provenance,
capture profiles, replay, MCP tools, and structured LLM-assist cards.
These capabilities do not yet constitute an autonomous investigation loop.

## Relationship to existing plans

- [LLM-assist and compute](llm-assist-and-compute-roadmap.md) owns optional
  on-Pi reasoning, structured reports, retrieval, and the hosting seams.
  Its D4 already revised the original Qwen MoE target to Qwen3-4B Q4_K_M.
  Preserve its post-drive/review-time execution boundary; this proposal
  does not put an LLM on the live inference path.
- [Online training](online-training-strategy.md) owns training-data
  disposition, accepted baseline differences, batch retraining, and
  promotion. This proposal does not replace those rules with indiscriminate
  per-sample updates to IF or the sequence autoencoder.
- [Behavior-model work](tasks/B00-behavior-models-README.md) owns serving
  contracts, eligibility, independent-drive validation, and qualified
  model publication. Build on that work rather than a parallel framework.
- [Structured cards](../llm-assist-card-schema.md) and
  [MCP](../mcp-server.md) provide existing output and tool contracts.

## Five capabilities

### 1. Shared understanding of engine state

Give analyses a common representation of operating conditions, ECU
commands, measured responses, and measurement quality. Include acquisition
timestamps, uncertainty where supported, channel availability, vehicle and
program identity, tune revision, and model applicability.

Read `vehicle_state` before interpreting live detector behavior. Distinguish
absent inputs, stale inputs, unknown context, inadequate coverage, failed
validation, and measured deviation. An unevaluable result cannot become
"normal" or a zero residual. RAM windows offer same-instant observations
of related internal quantities where supported by the vehicle profile.

### 2. Persistent evidence and intervention memory

Sessions are raw memory; investigations need structured records of:

- The observation, operating conditions, affected quantities, and quality.
- Supporting and contradicting evidence, with exact session/window and
  analysis-artifact references.
- Candidate explanations, unresolved questions, and proposed experiments.
- Interventions: calibration, component, fuel, or instrumentation changes.
- Predictions made before an intervention and results measured afterward.
- Superseded or retracted findings, retaining their history and reasons.

Conditions and provenance must be required schema fields, not optional
prose. Store this per vehicle; distinguish ECU program identity from tune
revision. A calibration change does not by itself invalidate RAM addresses.

Start with structured storage and exact filtering by vehicle, conditions,
revision, and time. Semantic retrieval can supplement those filters.
Retrieved hypotheses remain hypotheses; generated text is not new evidence.

This should answer questions such as "Did this precede the last tune?" and
"Have we already tested this explanation, and what happened?"

### 3. Models that explain discrepancies

Connect models of air entering the engine, fuel commanded and delivered,
ignition behavior, cam response, and thermal behavior. Produce predictions
and qualified residuals, then use them as evidence for competing hypotheses.
Correlation alone cannot establish fault causation.

The desired explanation is: "Response differs from the command in this
region and from the validated baseline. These explanations fit the evidence;
this missing measurement would distinguish them."

Use `compare_channels` for raw signal comparisons, acquisition-time pairing,
and per-sample ratio checks where meaningful. Across interventions, compare
matched operating conditions, preserving independent-drive evidence. Do not
infer an improvement from aggregate means with different drive composition.

### 4. An investigator that selects the next measurement

When evidence is ambiguous, propose a targeted capture recipe and a
predefined analysis. Explain which hypotheses its result would distinguish.
Account for acquisition timing, complete channel tuples, and bus capacity.
Preserve the relevant event window and respect an active operator capture.

If a physical action is required, describe that action and its purpose.
An eventual automatic capture selector can use deterministic policies;
live operation must not depend on an LLM response.

No heavy session analysis runs on the Pi while it collects. Laptop analysis
remains the development workflow. In the standalone field scenario, where
the laptop is absent, optional on-Pi reasoning and bounded analysis wait
until collection is inactive. The phone/tablet remains a thin client.

### 5. A learning loop around repairs and calibration

Before an intervention, record the expected direction, affected operating
region, and magnitude when the evidence supports one. Afterward, evaluate
that prediction under matched conditions and retain failed predictions.
Failure can identify a weak model or hypothesis rather than a failed repair.

Future calibration proposals could include affected cells, supporting
evidence, expected effects, and unresolved uncertainty. Every ECU
non-volatile write still requires explicit operator permission for that
specific operation and a fresh `vehicle_state` verdict of `ignition_only`.
Refuse `engine_running` and unclassifiable states. This proposal authorizes
no ECU writes. Reads and ordinary volatile diagnostics retain the existing
project policy.

## Model architecture and what "self-learning" means

Use several small, purpose-specific components rather than asking one
language model to learn numerical engine dynamics from raw telemetry.

| Responsibility | Starting candidate | Update mechanism |
|---|---|---|
| Predict behavior | Existing behavior surfaces; small Bayesian regression models for suitable residuals | Qualified incremental updates or periodic refits |
| Represent temporal behavior | Existing sequence model; evaluate a small GRU/TCN only for a demonstrated gap | Batch training on recorded sequences with recalibration |
| Remember findings | Structured evidence and intervention store, optionally augmented by semantic retrieval | Append evidence, outcomes, and explicit supersession |
| Explain and investigate | Quantized 4B-class Qwen using tools and retrieved evidence | Fixed weights initially; better context and tools over time |

For the first adaptive numerical experiment, test Bayesian regression over
physically meaningful features in a defined operating regime. Nonlinear
features or separate regions can represent more than a straight-line
relationship. Predictive uncertainty is useful, but its calibration must be
checked on held-out drives; the model name alone does not guarantee it.
[River's Bayesian regression](https://riverml.xyz/0.21.2/api/linear-model/BayesianLinearRegression/)
supports incremental updates and predictive distributions. It is a candidate
to compare against the existing models, not a selected dependency or a
universal replacement.

Keep the LLM's weights fixed initially. The system still learns through
numerical model updates, growing evidence, operator feedback, and remembered
experiment outcomes. Continual LLM fine-tuning on the Pi is not required for
these capabilities. An MoE architecture does not itself provide continual
learning or persistent memory.

### Preserve a reference while learning

A model that adapts to every observation can learn a developing fault as
normal. Maintain three distinct elements:

1. **Validated reference:** established behavior under stated conditions,
   including explicitly accepted baseline differences.
2. **Adaptive candidate:** a model of recent qualified observations,
   initially isolated from published alerts and diagnostic conclusions.
3. **Evaluation and promotion:** predict before updating; evaluate on
   independent drives and known-answer cases before replacing a reference.

Candidate/reference disagreement is evidence worth investigating. Better
fit to recent data is not sufficient grounds for promotion. Preserve
historical references and lineage; reuse the existing disposition and model
lifecycle mechanisms. Streaming experiments are additive and gated by that
framework, not an exception to it.

## Does the Qwen MoE plan fit a Pi 5?

**Qwen remains a plausible reasoning layer. The original Qwen3-30B-A3B
is not a practical default alongside CANary on a 16 GB Pi.**

The [model reference](https://recipes.vllm.ai/Qwen/Qwen3-30B-A3B)
lists 30.5B total parameters and 3.3B active per token. Sparse activation
reduces computation, but all expert weights still require storage and
efficient access. A rough idealized 30B × 4-bit calculation is 15 GB for
weights alone, before quantization metadata, context cache, runtime, OS,
and CANary. This is a sizing argument, not a measured GGUF footprint.
Paging experts from storage is not the planned field experience.

The existing roadmap's baseline, **Qwen3-4B Q4_K_M**, has an official
[2.5 GB weight file](https://huggingface.co/Qwen/Qwen3-4B-GGUF/tree/main).
Runtime memory additionally includes context cache and working buffers.
Use a bounded context with retrieved evidence and tool-produced summaries;
do not send whole raw drives to the LLM.

Benchmark [Qwen3.5-4B](https://huggingface.co/Qwen/Qwen3.5-4B) as a newer
challenger. Its published agent/tool-use support does not establish its
accuracy, latency, or runtime fit on our board. Preserve Qwen3-4B as the
existing baseline until an application-specific comparison supports change.

Hardware and performance questions remain open:

- The LLM roadmap assumes a **16 GB Pi 5**, while the project overview
  lists **8 GB**. Confirm the actual deployment board before budgeting.
- Measure total peak memory with the actual daemon and chosen context,
  prompt-processing time, time to first token, complete report latency,
  sustained thermals, and power use.
- The old roadmap's 6–12 tok/s estimate for 4B is **not a verified result
  on this installation**. It must not become an acceptance claim.
- Evaluate retrieval, tool selection, structured output, evidence fidelity,
  and missing-input recognition. A model that fits RAM has not yet proved
  useful diagnostic capability.
- Preserve the no-LLM dependency/import boundary on capture. Being resident
  in memory does not authorize heavy inference during collection.

Sources above were consulted for the 2026-09-22 discussion. Package support,
model artifacts, and device performance need verification at implementation.

## First vertical slice: fuel-delivery investigation

Use the existing commanded-versus-measured analysis to complete one loop:

1. Detect a qualified discrepancy in a stated operating region.
2. Create a persistent investigation with evidence and alternative explanations.
3. Identify the next useful capture, or state why current evidence suffices.
4. Record a prediction before the experiment or intervention.
5. Evaluate the outcome and preserve both supporting and refuting evidence.
6. Produce a post-drive explanation and retrieve it in a later investigation.

No new model is required just to prove the evidence/memory workflow.
Add adaptive numerical modeling only where it improves this measured task.

## Future implementation checklist

- [ ] Reconcile target Pi RAM and record measured daemon/resource budgets.
- [ ] Define required investigation, evidence, prediction, intervention, and
  outcome schemas with per-vehicle scope and explicit applicability.
- [ ] Connect existing analyses and structured cards to persistent records;
  distinguish unsupported, missing, and contradictory evidence.
- [ ] Implement the fuel-delivery vertical slice over stored sessions.
- [ ] Build known-answer investigation cases, including absent channels,
  misleading correlations, changed tune, repeated findings, and retractions.
- [ ] Evaluate candidate/reference numerical learning through replay, with
  predictions scored before learning and independent-drive promotion checks.
- [ ] Benchmark Qwen3-4B Q4_K_M and a suitable Qwen3.5-4B quantization on
  those cases and on the actual Pi; select by evidence fidelity and resource
  use as well as latency.
- [ ] Add retrieval and constrained analysis-tool use; validate generated
  references and structured outputs against actual tool results.
- [ ] Add next-measurement recommendations with timing and capture ownership
  constraints; evaluate automatic selection separately before enabling it.
- [ ] Demonstrate an end-to-end retained investigation: recognize recurrence,
  retrieve prior evidence, identify missing inputs, choose a useful next
  measurement, and evaluate its result.

Acceptance is traceable understanding, not convincing prose. A successful
case can show exactly why its conclusion is supported, where its limits
are, and how new evidence changed it. This checklist does not alter
`OVERNIGHT-QUEUE.md` ordering or mark related roadmaps complete.
