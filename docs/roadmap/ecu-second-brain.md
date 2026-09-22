# ECU second brain — persistent understanding and adaptive models

Recorded 2026-09-22 from the operator discussion, and revised the same day
after a research review: Pi measurements, the diagnosis literature, a check
of River's source, and small-model fine-tuning practice. **Exploratory
roadmap; implementation is not started or authorized by this document.**
Checkboxes below are future work, not additions to the overnight queue.
Model choices are candidates pending measurement, not new operator-approved
defaults.

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
  Its D4 already revised the original Qwen MoE target to a 4B-class model,
  and its D7 records what a 16 GB board would change. Preserve its
  post-drive/review-time execution boundary; this proposal does not put an
  LLM on the live inference path.
- [Online training](online-training-strategy.md) owns training-data
  disposition, accepted baseline differences, batch retraining, and
  promotion. This proposal does not replace those rules with indiscriminate
  per-sample updates to IF or the sequence autoencoder.
- [Behavior-model work](tasks/B00-behavior-models-README.md) owns serving
  contracts, eligibility, independent-drive validation, and qualified
  model publication. Build on that work rather than a parallel framework.
- [Structured cards](../llm-assist-card-schema.md) and
  [MCP](../mcp-server.md) provide existing output and tool contracts.
- [Supercharger build](supercharger-build.md) is the largest intervention
  this vehicle will have. It is weeks away and invalidates every learned
  reference, which makes it the first real use of the intervention and
  prediction records below.

## Build on the records that already exist

The project already keeps several of the records this plan needs, in
different shapes. Extend them; do not start a parallel store.

| Existing record | What it is today | What this plan adds |
|---|---|---|
| `data/<profile>/tune_revisions.yaml` | Calibration flashes with time and ROM hash | All interventions, including hardware: the supercharger plan notes that nothing records hardware |
| `data/<profile>/accepted_baselines.yaml` | Explicitly accepted baseline differences | A link to the investigation that justified each acceptance |
| `superseded_by` in `data/bindings.yaml` | Retracted bindings kept for old data | The same supersession for findings and hypotheses |
| `tests/fixtures/ground_truth/expectations.yaml` | Known answers the analysis chain must reproduce | The seed of the known-answer investigation cases |
| AGENTS.md retractions and `data/analyses/*.md` | Prose findings, including six retractions | Structured records with evidence references; the prose stays as narrative |
| `data/<profile>/noise_floor.yaml` | Measured noise with required `conditions` | The detectable-effect bound every prediction must state |

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

**Evidence is minted by tools, never typed by a model.** Each analysis tool
that produces evidence writes an evidence record: id, tool and code
version, inputs, session and window, conditions, and the values it
computed. Investigations and cards cite evidence ids. A validator rejects
an id that no tool produced, and numbers shown with a claim are rendered
from the record. The current card contract does not do this:
`EvidenceReference` in `src/analysis/llm_assist/cards.py` is a free-text
description with optional session, channel, and timestamp fields, so a
producer can cite a session or a value no tool returned and the card still
validates. This is the "make it impossible, not documented" rule applied
where a language model is most likely to break it.

**Every record carries two times.** One is when the thing happened (valid
time); the other is when CANary recorded or believed it (record time).
Superseded records also carry when they were superseded. "Did this precede
the last tune?" is a valid-time question. "What did we believe on
2026-09-10?" is a record-time question. Retractions need both
([bitemporal history](https://martinfowler.com/articles/bitemporal-history.html)).
Append-only JSONL, as the existing ledgers use, already supplies record
time; add `valid_from`/`valid_to` and `superseded_at`. DuckDB and SQLite
need no special support; overlap checks live in the application. The W3C
[PROV](https://www.w3.org/TR/prov-dm/) terms `wasDerivedFrom` and
`wasInvalidatedBy` are a reasonable naming guide for derivation and
retraction links.

Start with structured storage and exact filtering by vehicle, conditions,
revision, and time. At hundreds of records, field filters plus SQLite or
DuckDB full-text search are enough. Add embedding retrieval only after a
measured miss, because an embedding model also occupies Pi RAM. Retrieved
hypotheses remain hypotheses; generated text is not new evidence.

This should answer questions such as "Did this precede the last tune?" and
"Have we already tested this explanation, and what happened?"

### 3. Models that explain discrepancies

Connect models of air entering the engine, fuel commanded and delivered,
ignition behavior, cam response, and thermal behavior. Produce predictions
and qualified residuals, then use them as evidence for competing hypotheses.
Correlation alone cannot establish fault causation.

The established framework is consistency-based diagnosis. Write the
physical relations as equations. Derive residuals that are zero when those
equations hold. Then tabulate which fault moves which residual; that table
is the fault signature matrix. It states, before any drive, which faults
the current channels can detect and which they can tell apart
(isolability). Residual evidence then ranks hypotheses by computation, not
by generated prose.

The [Fault Diagnosis Toolbox](https://github.com/faultdiagnosistoolbox/pyfaultdiagnosistoolbox)
(Frisk, Krysander and Jung; Python package `faultdiagnosistoolbox`, MIT
licence, aarch64 wheels) implements the structural analysis, isolability
analysis, sensor placement, and residual generation. Its
[worked example](https://faultdiagnosistoolbox.github.io/usecase/) is a
turbocharged spark-ignition air path. Spike it before writing our own.

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

Choose that measurement by computation. With a small, discrete set of
hypotheses, pick the test with the largest expected reduction in
uncertainty about which hypothesis holds, divided by its cost: drive time,
a key-on engine-off window, or an added gauge. This is the entropy rule of
[de Kleer & Williams (1987)](https://dekleer.org/Publications/Diagnosing%20Multiple%20Faults%20AIJ%20reprint.pdf)
and the cost-weighted troubleshooting of
[Heckerman, Breese & Rommelse (1995)](https://arxiv.org/abs/1302.3563).
Where no available channel separates two hypotheses, sensor-placement
analysis names the measurement that would.

The open WOT-lean question is the textbook case
([2026-09-21](../../data/analyses/2026-09-21-drive-tuning-2.md)). WOT reads
a flat ~3 % lean while part throttle is pulled rich. A MAF transfer error
and fuel-delivery sag leave the same signature on the current channels,
because the ECM reports no fuel pressure. Sensor-placement analysis would
name fuel pressure, which is the conclusion reached by hand
(supercharger-build NA2).

If a physical action is required, describe that action and its purpose.
An eventual automatic capture selector can use deterministic policies;
live operation must not depend on an LLM response.

No heavy session analysis runs on the Pi while it collects. Laptop analysis
remains the development workflow. In the standalone field scenario, where
the laptop is absent, optional on-Pi reasoning and bounded analysis wait
until collection is inactive. The phone/tablet remains a thin client.

### 5. A learning loop around repairs and calibration

Before an intervention, record the expected direction, affected operating
region, and magnitude when the evidence supports one. Make that ordering a
schema rule, not a habit. A prediction whose record time is not earlier
than the intervention is stored as post-hoc and cannot count as confirmed.
Each magnitude sits beside the noise floor measured under the same
conditions; `noise_floor.yaml` already requires `conditions`. A null result
then says which kind it is: no effect, or an effect too small to see.

Afterward, evaluate under matched conditions. Bin rpm × load × IAT × baro
and compare within bins
([coarsened exact matching](https://gking.harvard.edu/cem)), never across
drives with different composition. Retain failed predictions. Failure can
identify a weak model or hypothesis rather than a failed repair.

A before/after comparison across a flash date is confounded by season,
altitude, and fuel batch. Where a calibration can be flashed back, an
A→B→A sequence is the design that separates the tune from those; plan it
with the `designed-experiments` skill. Each flash in such a sequence is a
separate non-volatile write under the rule below.

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
| Predict behavior | Existing behavior surfaces and physical residuals | Periodic refits through the existing lifecycle |
| Bound predictions | Conformal intervals per operating mode or baro band, calibrated one held-out drive at a time | Recalibrated with each promoted reference |
| Detect change | CUSUM/GLR on decorrelated residuals against the frozen reference | Fixed tests; alarms become evidence |
| Track slow parameters | Recursive least squares or a Kalman filter over a few physical parameters, such as VE or an injector-scale term | Online, isolated as the adaptive candidate |
| Represent temporal behavior | Existing sequence model; evaluate a small GRU/TCN only for a demonstrated gap | Batch training on recorded sequences with recalibration |
| Rank hypotheses, choose tests | Fault signature matrix and expected-information-gain selection | Grows with the model equations and hypothesis catalog |
| Remember findings | Structured evidence and intervention store | Append evidence, outcomes, and explicit supersession |
| Translate and explain | Quantized 4B-class Qwen using tools and retrieved evidence | Fixed weights initially; better context and tools over time |

The language model's job is narrower than "reason about the engine". It
turns an operator question into tool calls, explains computed results,
and suggests hypotheses outside the catalog, marked unscored until a
residual can test them. It does not rank hypotheses or choose
measurements; those are computed.

The reasoning layer that exists today is Claude Code over MCP on the
laptop. Expose the store and evidence tools through MCP first, so Claude
can use them now and an on-Pi model can use the same tools later. The real
investigations that result become the evaluation set.

### Adaptive numerical models

**River's `BayesianLinearRegression` does not provide calibrated
uncertainty.** This was checked in the River 0.26.1
[source](https://github.com/online-ml/river/blob/0.26.1/river/linear_model/bayesian_lin_reg.py).
The noise precision `beta` is a fixed hyperparameter that is never
estimated, and the predictive variance is `1/beta + xᵀS⁻¹x`. Interval
width therefore reflects that setting, not the data. With `smoothing`,
each update is `S ← s·S + (1−s)·β·xxᵀ`. The weights sum to 1, so the
posterior never becomes more certain than about one observation. A
held-out calibration check would fail by construction. `xᵀS⁻¹x` remains
usable as an extrapolation flag.

Use instead:

- **Intervals.** Split conformal prediction wraps any predictor and needs
  only exchangeability ([Lei et al. 2018](https://arxiv.org/abs/1604.04173)).
  Samples within one drive are not exchangeable, so calibrate and evaluate
  one held-out drive at a time, with separate calibration per operating
  mode or baro band ("Mondrian" conformal;
  [crepes](https://github.com/henrikbostrom/crepes),
  [MAPIE](https://github.com/scikit-learn-contrib/MAPIE)).
- **Change detection.** Use CUSUM when the shift size of interest is
  known. Use GLR when size and time are unknown; it also estimates when
  the change happened. For slow wear, test GLR against a gradual-drift
  alternative ([Basseville & Nikiforov](https://people.irisa.fr/Michele.Basseville/kniga/)).
  Decorrelate residuals first, or the false-alarm rates mean nothing.
  River's drift module (ADWIN, Page-Hinkley) has no CUSUM or GLR.
- **Adaptive candidate.** Use recursive least squares with a forgetting
  factor, or its Kalman-filter form with per-parameter drift variances,
  over a few physically meaningful parameters. A discount dynamic linear
  model (West & Harrison, *Bayesian Forecasting and Dynamic Models*) also
  learns its own observation noise. All of these wind up on idle-heavy
  drives that barely excite the parameters, so gate updates on excitation.

Keep the LLM's weights fixed initially. The system still learns through
numerical model updates, growing evidence, operator feedback, and remembered
experiment outcomes. Continual LLM fine-tuning on the Pi is not required for
these capabilities. An MoE architecture does not itself provide continual
learning or persistent memory.

Later, evaluate domain distillation as a separate, offline improvement to
the reasoning layer (below). This does not require continual LLM training
during operation or replace numerical learning and persistent memory.

### Preserve a reference while learning

A model that adapts to every observation can learn a developing fault as
normal. Maintain three distinct elements:

1. **Validated reference:** established behavior under stated conditions,
   including explicitly accepted baseline differences.
2. **Adaptive candidate:** a model of recent qualified observations,
   initially isolated from published alerts and diagnostic conclusions.
3. **Evaluation and promotion:** predict before updating; evaluate on
   independent drives and known-answer cases before replacing a reference.

This is the champion/challenger pattern
([CD4ML](https://martinfowler.com/articles/cd4ml.html)). Two rules keep it
honest:

- Change detection runs against the frozen reference, never against the
  adaptive candidate.
- The candidate may not train on drives recorded after an unresolved change
  alarm; otherwise it learns the very change the alarm is about.

Adaptive conformal inference
([Gibbs & Candès 2021](https://arxiv.org/abs/2106.00170)) widens its
interval as a fault develops, which is the same absorption trap. Use it to
monitor the candidate, not to judge faults.

A deliberate hardware change is not drift. When an intervention invalidates
a reference, retire that reference as historical under the intervention's
id and build new ones. The supercharger build invalidates all of them. Do
not let the candidate "adapt" across it.

Candidate/reference disagreement is evidence worth investigating. Better
fit to recent data is not sufficient grounds for promotion. Preserve
historical references and lineage; reuse the existing disposition and model
lifecycle mechanisms. Streaming experiments are additive and gated by that
framework, not an exception to it.

## Pi hardware: what fits, and what 16 GB changes

**The current Pi is 8 GB. A 16 GB Pi 5 is an upgrade under consideration
(operator, 2026-09-22).** Figures below are community measurements unless
stated otherwise. Verify them on this installation before treating any of
them as acceptance numbers.

**Speed is set by memory bandwidth, not RAM size.** Generating a token
reads roughly the whole model from memory. The Pi 5 measures about
11.5–12.4 GB/s
([STREAM](https://forums.raspberrypi.com/viewtopic.php?t=369585)), which
caps a 2.5 GB model at about 4.6 tok/s. Measured 3–4B Q4 models generate
3.9–5.2 tok/s
([Pi 5 leaderboard](https://huggingface.co/spaces/nyunai/edge-llm-leaderboard/raw/main/dataset/llm-perf-leaderboard-Raspberry%20Pi%205(8GB).csv)).
The old 6–12 tok/s estimate is not achievable. A 16 GB board is no
faster; it only holds bigger models.

**Prompt processing dominates.** A 3–4B model reads a prompt at roughly
11–16 tok/s at Q4_K_M. Q4_0 is about twice as fast on this CPU since
llama.cpp's [ARM repack](https://github.com/ggml-org/llama.cpp/pull/17494).
A 3,000-token investigation prompt takes 2–5 minutes. An agent loop is
workable only if the server's prompt cache hits, so each turn processes
only the new tool output. Keep the system prompt and tool schemas at the
start of the prompt and unchanged between turns.

**Thinking mode is expensive here.** 1,000 thinking tokens take about four
minutes. Benchmark
[Qwen3-4B-Instruct-2507](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507)
first. It has no thinking mode and scores 61.9 on BFCL-v3, against 57.6
for the original Qwen3-4B. Keep Qwen3-4B Q4_K_M
([2.5 GB](https://huggingface.co/Qwen/Qwen3-4B-GGUF/tree/main)) as the
existing reference.

**Context memory.** Qwen3-4B's context cache is 144 KiB per token at f16:
about 0.56 GiB at 4k, 1.1 GiB at 8k, and 4.5 GiB at 32k. `-ctk/-ctv q8_0`
halves it. Avoid q4_0, which llama.cpp documents as degrading tool calling.
Set `--cache-ram` explicitly: its default of 8192 MiB is the whole of the
current board.

**Qwen3.5-4B** ([card](https://huggingface.co/Qwen/Qwen3.5-4B), February
2026) mixes linear-attention (Gated DeltaNet) and full-attention layers,
so its context cache is only about 32 KiB per token. It is not yet usable
for the tool loop:

- llama-server invalidates the prompt cache for hybrid models
  ([#24055](https://github.com/ggml-org/llama.cpp/issues/24055)).
- Slot save/restore loses prompt reuse
  ([#25913](https://github.com/ggml-org/llama.cpp/issues/25913)).
- Tool-call parsing fails when text precedes a call
  ([#21158](https://github.com/ggml-org/llama.cpp/issues/21158)).
- The CPU kernel for its new layer type is a basic implementation.

Re-check when those close.

**Budget on the 8 GB board.** The daemon normally sits at 150–650 MB. The
trip-end digest runs in a child allowed up to 4 GiB, at exactly the
post-drive moment an LLM would want to run. A 4B model (2.5 GB plus about
0.6 GB of context at 4k) fits if it starts after the digest finishes, not
beside it.

**What 16 GB would buy.**

- **Dense 8B models** fit, but generate only about 2 tok/s; Llama-3.1-8B
  measured 2.1.
- **A mixture-of-experts model is the worthwhile case**, because only its
  active parameters are read per token. Qwen3-30B-A3B-Instruct-2507 in a
  2.70-bit quant (9.58 GiB) was measured on a 16 GB Pi 5 at about:
  - 8 tok/s generation;
  - 9–11 tok/s prompt processing;
  - a 4k context;
  - 10.5 W.

  ([Geerling #47](https://github.com/geerlingguy/ai-benchmarks/issues/47).)
- **At ordinary 4-bit that model does not fit.** It is 16.4–18.6 GB
  ([GGUFs](https://huggingface.co/unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF)),
  too big for 16 GB even alone. Answer quality at 2.7 bits on our tasks is
  unmeasured.
- **Price.** The 16 GB Pi 5 lists at $305 after three memory-driven price
  rises since December 2025
  ([Raspberry Pi](https://www.raspberrypi.com/news/more-memory-driven-price-rises/)).

**Upgrade trigger.** Buy the larger board when two things have both been
measured. First, the 4B model, run on the current Pi against the
known-answer investigation cases, fails them for lack of capability.
Second, a 30B-A3B-class model fixes those failures. Until the evidence
tools and cases exist, there is nothing to choose a model with.

**Alternatives considered.**

- **Raspberry Pi AI HAT+ 2** (Hailo-10H, January 2026). It runs
  precompiled models up to about 2B with a 2,048-token context, and it is
  slower than the Pi's own CPU on them
  ([CNX review](https://www.cnx-software.com/2026/01/20/raspberry-pi-ai-hat-2-review-a-40-tops-ai-accelerator-tested-with-computer-vision-llm-and-vlm-workloads/)).
  It cannot run this workload.
- **Jetson Orin Nano Super 8 GB** (102 GB/s). It generates about 9×
  faster by NVIDIA's figures, but costs $399 after July's increase
  ([CNX](https://www.cnx-software.com/2026/07/22/nvidia-increases-the-price-of-jetson-modules-and-devkits-by-up-to-101/)),
  has only 8 GB, and adds a second box to power.

**The in-vehicle window is short regardless of model.** On accessory power,
the UPS keeps the Pi up for 300 s after key-off (`UPS_SHUTDOWN_DELAY` in
`scripts/ups_shutdown_watch.py`), and the trip-end digest needs that time
too. On-Pi LLM work therefore runs while idling, under the watchdog's
keep-alive hold, or on bench power. Energy is not the constraint: the
board draws 10–14 W under load, about 2 Wh for a 10-minute report.

**Memory limits must be enforced first.** D03.21 is still open: the kernel
disables the memory cgroup, so `MemoryMax` is not enforced and 2 GB of swap
is live. A co-resident model could push the collector into the 38-minute
thrash of 2026-09-17. Resolve D03.21 first. Then run the model as its own
unit with its own memory cap and a high OOM score, stopped whenever a
capture starts.

Remaining measurements:

- Measure the following with the actual daemon and chosen context:
  - total peak memory;
  - prompt-processing time and time to first token;
  - complete report latency;
  - sustained thermals (cores throttle from 80 °C and hard at 85 °C, so
    active cooling is required);
  - power use.
- Evaluate retrieval, tool selection, structured output, evidence fidelity,
  and missing-input recognition. A model that fits RAM has not yet proved
  useful diagnostic capability.
- Preserve the no-LLM dependency/import boundary on capture. Being resident
  in memory does not authorize heavy inference during collection.

Sources above were consulted on 2026-09-22. Package support, model
artifacts, and device performance need verification at implementation.

## Optional domain distillation for engine management

Distillation could make a small Qwen better at CANary's specific tasks while
retaining a Pi-sized inference footprint. This is a hypothesis to evaluate,
not a promise that a 4B student will inherit all of a larger teacher's
capabilities. [DeepSeek-R1](https://arxiv.org/abs/2501.12948) demonstrates
distillation into smaller Qwen-based models; it does not establish engine
diagnostic accuracy.

Start with **distillation through supervised fine-tuning**: a capable
teacher produces candidate examples, those examples are verified, and the
student trains on the accepted responses and tool interactions. This is
output-based distillation; matching teacher probability distributions is
another technique, not a prerequisite for this experiment. LoRA/QLoRA are
parameter-efficient training methods, not themselves distillation methods.
[Qwen's training guidance](https://github.com/QwenLM/Qwen3/blob/main/docs/source/training/llama_factory.md)
documents support for both.

Train on a GPU-equipped development machine, then deploy the quantized
student to the Pi. A larger Qwen MoE could serve as an off-device teacher
without becoming the field runtime. Keep teacher/model selection and
training-framework compatibility subject to verification for the exact
student checkpoint.

### What small-data fine-tuning can change

With dozens to low hundreds of examples, supervised fine-tuning reliably
changes output format, tool-call syntax, and the investigation template.
It is unlikely to teach new diagnostic reasoning. See
[LIMA](https://arxiv.org/abs/2305.11206), and
[LIMO](https://arxiv.org/abs/2502.03387) on how gains depend on what
pretraining already holds.

Published tool-use distillations used about 2,000
([Agent Distillation](https://arxiv.org/abs/2505.17612)) to 80,000
([TinyAgent](https://arxiv.org/abs/2409.00608)) examples. Examples that
carry new facts increase hallucination
([Gekhman et al. 2024](https://arxiv.org/abs/2405.05904)), which is
another reason vehicle facts stay in retrieval. Small students learn better
from short teacher traces than from long ones
([Li et al. 2025](https://arxiv.org/abs/2502.12143)), so keep teacher
reasoning brief.

### Cheaper steps first

Measure these against the unmodified model before training anything:

- Skill files loaded on demand, one per investigation type.
- Past verified investigations retrieved as worked examples; nearest cases
  beat randomly chosen ones.
- Grammar-constrained decoding for the tool-call JSON only (llama-server
  `json_schema` or GBNF). Leave the reasoning text unconstrained.
- Tool names and parameters phrased the way the model expects
  ([PA-Tool](https://arxiv.org/abs/2510.07248)).

### If a LoRA is trained

Follow [RAFT](https://arxiv.org/abs/2403.10131):

- Each example includes irrelevant tool outputs alongside the relevant
  ones.
- Some examples have no usable evidence; the right answer names what is
  missing.
- Answers quote exact tool values.

Load the adapter at runtime (llama.cpp `--lora`,
[#8332](https://github.com/ggml-org/llama.cpp/pull/8332)) on the stock
quantized base rather than merging it. The base stays verifiable, scale 0
versus 1 gives a clean comparison, and rollback is instant. Evaluate at the
deployed quantization, not at bf16.

### Teach investigation skills

| Skill | Training example |
|---|---|
| Interpret qualified evidence | Explain a fuel-delivery discrepancy within its measured operating conditions |
| Recognize insufficient evidence | Identify that commanded AFR was never captured |
| Select the right analysis | Request acquisition-time pairing instead of comparing window means |
| Distinguish explanations | Choose a measurement that could separate sensor error from delivery error |
| Use prior outcomes | Incorporate a previously rejected hypothesis into the next investigation |
| Communicate uncertainty | Separate observations, hypotheses, and the next useful measurement |

Project retractions are valuable examples: preserve the convincing wrong
answer, the evidence that disproved it, and the method that should have
been used. Teach concise, evidence-backed explanations and structured cards;
long explanations are not a quality metric.

### Evidence-backed training records

Each example should contain:

- An evidence packet from actual CANary tools, with session/window and
  artifact references, conditions, availability, and measurement quality.
- Applicable profile facts and technical reference excerpts with provenance.
- A verified interpretation and its supporting and contradicting evidence.
- The next tool call or physical measurement where the evidence is incomplete.
- A concise explanation and a schema-valid investigation/card output.
- Teacher and student-relevant data versions, verification disposition,
  and whether any part of the example is synthetic.

Give the teacher only information available at that stage of the
investigation. Do not let a known eventual outcome leak into an earlier
decision. Split by underlying incident and drive before generating variants;
near-duplicate windows or synthetic derivatives of a training case cannot
serve as independent test cases.

Include incomplete captures, contradictory instruments, incorrect decode
assumptions, changed operating conditions, and cases where the evidence
cannot distinguish the hypotheses. Synthetic variations can extend coverage,
but teacher-generated diagnoses are not ground truth. Reject unsupported
claims and invalid tool references rather than training the student to
repeat them. Keep a record of how each accepted answer was verified.

### Teacher and data licensing

Qwen models, including the MoE teacher candidates, are Apache-2.0. Some
commercial terms restrict using a model's outputs to train competing
models:

- Anthropic's [Commercial Terms](https://www.anthropic.com/legal/commercial-terms)
  (D.4);
- Anthropic's [Consumer Terms](https://www.anthropic.com/legal/consumer-terms)
  (§3);
- OpenAI's [Services Agreement](https://cdn.openai.com/osa/openai-services-agreement.pdf).

Whether a private 4B model "competes" is a legal question this roadmap does
not answer. Use an open-weight teacher, and do not treat Claude Code
investigation transcripts as training data without checking the applicable
terms.

### Keep changing facts in retrieval

Tune revisions, RAM addresses, component history, and individual findings
remain in profile data and persistent memory. Fine-tuning teaches how to
use those facts. It must not embed development-vehicle assumptions into
shared behavior or require a training run whenever a calibration changes.

Numerical models learn engine relationships; persistent memory records
experience; the distilled Qwen learns better evidence interpretation and
investigation procedures. These remain separate update mechanisms.

### First experiment and acceptance

Begin with fuel-delivery investigations, expanding only after measurable
improvement over the unmodified model:

1. Build an independent held-out evaluation set of complete investigations,
   separated by drive and underlying incident, and held out by date rather
   than at random.
2. Measure the unmodified 4B model with good prompting, the same tools, and
   the same retrieval available to the candidate, including the cheaper
   steps above.
3. Train a LoRA adapter on a separate set of verified examples.
4. Compare evidence accuracy, tool selection, unsupported claims,
   missing-input recognition, and structured-output validity. Include
   adjacent tasks to detect regressions from specialization.
   - Score programmatically: every cited value, channel, and session must
     appear in a tool result in the same transcript.
   - Do not use the teacher as judge, because models favour their own
     outputs ([Panickssery et al. 2024](https://proceedings.neurips.cc/paper_files/paper/2024/hash/7f1f0218e45f5414c79c0679633e47bc-Abstract-Conference.html)).
   - Run each case several times and report the fraction of cases where
     every run passes (pass^k, [τ-bench](https://arxiv.org/abs/2406.12045)).
5. Export the adapter to GGUF, quantize the base as needed, and repeat the
   evaluation on the Pi. Measure latency and peak memory as well as answer
   quality.

With 20–30 cases, a difference of two or three cases is noise. Compare
per case, not by totals.

Loaded at runtime, the adapter keeps the base architecture and adds little
to the inference footprint. Quantization may change the measured gains; the
deployed artifact must pass evaluation itself. Record dataset lineage, base
checkpoint, adapter, quantization, and evaluation results so a regression
can be traced and the previous model restored. Set acceptance criteria
before examining held-out results, and do not repeatedly train against the
test set.

This is an optional future experiment. It does not replace the initial
fixed-weight baseline or authorize training, hardware purchases, or hosting.

## First vertical slice: fuel-delivery investigation

Use the open WOT-lean investigation as the first case. Its hypotheses are
concrete: MAF transfer error, fuel-delivery sag, and an additive
fixed-quantity error such as an idle-only air leak or injector dead time.
Its separating measurement is known: fuel pressure through a pull.

It also has a deadline. The supercharger build replaces the pump,
injectors, throttle body, and intake, so the naturally aspirated question
closes at teardown. Either resolve it first (supercharger-build NA2), or
record it as unresolved and superseded by the build.

Use the existing commanded-versus-measured analysis to complete one loop:

1. Detect a qualified discrepancy in a stated operating region.
2. Create a persistent investigation with evidence and alternative explanations.
3. Identify the next useful capture, or state why current evidence suffices.
4. Record a prediction before the experiment or intervention.
5. Evaluate the outcome and preserve both supporting and refuting evidence.
6. Produce a post-drive explanation and retrieve it in a later investigation.

Then record the supercharger build itself as the first intervention. Before
first start, write predictions for:

- the MAF transfer change from the new intake and housing;
- air per cylinder-event under boost;
- IAT behavior with the intercooler;
- fuel supply pressure with the new pump.

Retire the naturally aspirated references under that intervention.

No new model is required just to prove the evidence/memory workflow.
Add adaptive numerical modeling only where it improves this measured task.

## Future implementation checklist

- [~] Reconcile target Pi RAM and record measured daemon/resource budgets.
  *2026-09-22:* the current board is 8 GB. D02.9's OOM kill came at 7.7 GB
  resident. 16 GB is an optional upgrade with the trigger above. The
  daemon's normal 150–650 MB and the trip-end child's 4 GiB ceiling are
  known; the LLM-side budgets are not yet measured.
- [ ] Resolve D03.21 (memory limits actually enforced) before any
  co-resident model; run that model as its own capped, preemptible unit.
- [ ] Define required investigation, evidence, prediction, intervention, and
  outcome schemas with per-vehicle scope, valid and record times, and
  explicit applicability. Extend `tune_revisions.yaml` to hardware
  interventions rather than starting a parallel record.
- [ ] Mint evidence records from analysis tools. Replace free-text card
  evidence with evidence ids that must resolve (LLM roadmap S.5).
- [ ] Connect existing analyses and structured cards to persistent records;
  distinguish unsupported, missing, and contradictory evidence.
- [ ] Settle the naturally aspirated WOT-lean investigation before teardown
  (supercharger-build NA2), or record it as superseded by the build.
- [ ] Record the supercharger build as an intervention, with predictions
  written before first start.
- [ ] Implement the fuel-delivery vertical slice over stored sessions.
- [ ] Spike the Fault Diagnosis Toolbox on the air/fuel relations. Produce
  the fault signature matrix, isolability with the current channels, and
  its sensor-placement output.
- [ ] Build known-answer investigation cases, including absent channels,
  misleading correlations, changed tune, repeated findings, and retractions.
- [ ] Add residual change detection (CUSUM/GLR) against frozen references,
  and conformal intervals per mode or baro band calibrated per held-out
  drive.
- [ ] Evaluate candidate/reference numerical learning through replay, with
  predictions scored before learning and independent-drive promotion
  checks. Start with recursive least squares or a Kalman filter over a few
  physical parameters.
- [ ] Expose the store and evidence tools over MCP, and use them from Claude
  Code before any on-Pi model.
- [ ] Benchmark Qwen3-4B-Instruct-2507 and Qwen3-4B Q4_K_M on those cases
  and on the actual Pi. Select by evidence fidelity, prompt-processing
  time, and peak memory as well as generation speed. Re-test Qwen3.5-4B
  once the llama.cpp hybrid-cache issues close.
- [ ] Add retrieval and constrained analysis-tool use; validate generated
  references and structured outputs against actual tool results.
- [ ] Measure skill files, retrieved past cases, and constrained tool calls
  against the unmodified model before any fine-tuning.
- [ ] Build an independently split, verified fuel-investigation distillation
  dataset with RAFT-style distractors. Retain provenance, rejected
  answers, and synthetic-example lineage, and use an open-weight teacher.
- [ ] Compare a LoRA-distilled student against the unmodified model with
  identical tools/retrieval; test adjacent-task regressions and repeat the
  comparison on the final quantized Pi artifact before selecting a release.
- [ ] Add next-measurement recommendations computed by expected information
  gain, with timing and capture ownership constraints; evaluate automatic
  selection separately before enabling it.
- [ ] Demonstrate an end-to-end retained investigation: recognize recurrence,
  retrieve prior evidence, identify missing inputs, choose a useful next
  measurement, and evaluate its result.

Acceptance is traceable understanding, not convincing prose. A successful
case can show exactly why its conclusion is supported, where its limits
are, and how new evidence changed it. This checklist does not alter
`OVERNIGHT-QUEUE.md` ordering or mark related roadmaps complete.
