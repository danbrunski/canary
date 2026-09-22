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

Not started. Optional add-on. **Priority lowered 2026-09-22 (operator).**
The [ECU second brain](ecu-second-brain.md)'s reasoning and learning are
deterministic and run on the Pi, live during the drive. This tier is an
optional language interface on top of them, and hosting it on the Pi is
the last priority (D6).

An LLM on the Pi has limited utility. During collection it would compete
with the collector for cores and memory bandwidth. After key-off it has
only the UPS window, at minutes per answer.

The base product (capture + Isolation Forest + sequence autoencoder +
dashboard + the deterministic second brain) must remain fully functional
and lean with this tier absent. The discipline that makes "leave the
hooks" free: the analysis/LLM code must be import-isolated and
feature-flagged — **no `torch`/LLM import on the capture hot path, no RAM
reservation, no daemon dependency when disabled**, and dashboard cards
degrade cleanly when the tier is not present.

## Deployment assumptions (the constraints that drive every decision)

- Field-deployed in a vehicle; **limited or no internet**. Nothing may
  require a cloud model; on-device models are pre-provisioned offline.
- **Design target: a Pi and an Android phone, nothing else.** No laptop,
  no Claude, no fast internet. A laptop is *not* dependably present even
  for the operator. The phone shows the Pi's existing PWA dashboard, and
  may host the model when it is capable (D3).
- Hardware: Raspberry Pi 5, **8 GB** today. A 16 GB board is an optional
  upgrade with a measured trigger (D7). Likely a sealed enclosure
  (thermal-constrained), on accessory power behind the X1202 UPS.
- **The parked window is short.** The UPS keeps the Pi up for 300 s after
  key-off (`UPS_SHUTDOWN_DELAY` in `scripts/ups_shutdown_watch.py`), and
  the trip-end digest needs that time too. Longer post-drive work runs
  while idling, under the watchdog's keep-alive hold, or on bench power.
- The base CANary daemon (capture + detection + dashboard) is the
  always-on workload. The cost of getting this wrong is a lost or
  truncated drive, not a safety hazard. It must not be jeopardised to make
  room for an optional tier.

## The optional LLM-assist feature set

On the Pi, all six are **post-drive / review-time / batch**. On the phone
or a sidecar they may run during a drive (D1, D6). None sits on the 1 Hz
live path or decides anything live; keeping the model off the collector's
CPU during collection is load-bearing for every decision below.

1. **Session narrative reports** — batch a full Parquet session into a
   structured report (LTFT trends, anomaly counts by operating mode,
   recurring patterns). The real purpose of `src/analysis/`: post-session
   analysis, not reactive per-anomaly interpretation.
2. **Cross-session pattern recognition** — summarise the last N sessions'
   anomaly logs and reason about longitudinal degradation ("bank 1 LTFT
   +6%→+14% at highway cruise over two weeks") for early warning.
3. **Grounded retrieval over own data** — "have I seen this before, when,
   what was the outcome?", a vehicle memory system. At hundreds of
   records, structured filters plus SQLite/DuckDB full-text search over
   the investigation store ([ECU second brain](ecu-second-brain.md)) are
   enough. Add an embedding index (sqlite-vec or similar) only after a
   measured retrieval miss, because its embedding model also takes RAM.
4. **Structured fault-hypothesis generation** — ranked differential
   diagnosis with supporting/refuting evidence from current PIDs, emitted
   as JSON the dashboard renders as a structured card.
5. **Active labeling partner (highest value)** — at anomaly review the
   model proposes a hypothesis and asks one targeted clarifying question
   ("possible MAF fault — were you on a dirt road or in heavy rain?"); the
   one-word answer enriches the label into the retraining ledger. Builds a
   far richer supervised dataset than binary FP/TP.
6. **Fuel-map analysis narration** — interpret the total-trim behavior
   surfaces (B05; recipes in `src/analysis/refit.py`), for example
   "persistent +10–15% bank 1 at 1500–2500 RPM × 40–60% load — what to
   investigate first".

## Decisions reached (with rationale)

**D1 — A language model never runs on the collector's CPU during
collection.** Revised 2026-09-22; it previously read "the LLM tier is
offline/post-drive".
- Reasoning and learning *during* the drive are deterministic streaming
  updates on the Pi ([ECU second brain](ecu-second-brain.md#reasoning-and-learning-while-driving)),
  not a language model.
- On the Pi, all six cases run after the drive or during review.
- A model may run during a drive only on its own hardware (the phone or a
  sidecar, D6), and nothing live waits on it.
- A 16 GB Pi does not change this. It adds memory, not cores or memory
  bandwidth, and contention with the collector is the constraint.

**D2 — The model host is interchangeable.** Revised 2026-09-22; it
previously read "on-Pi only in the real scenario".
- The analysis tools stay on the Pi behind HTTP/MCP, and whichever host is
  available calls them (D6).
- Evidence records carry the facts, so the same card validates whichever
  host wrote it.
- With no laptop and limited internet, the offline fallback is on-device:
  the phone when capable, the Pi last.

**D3 — The phone is a thin client by default, and may host the model when
capable.** Revised 2026-09-22; it previously read "no model runs on the
handheld".
- LLM outputs surface as structured dashboard cards via the existing PWA
  (service worker / manifest already in place).
- A recent mid-to-high-end Android phone has several times the Pi's memory
  bandwidth and a GPU or NPU for prompt processing, and it is not bound by
  the Pi's key-off window.
- The trip digest and evidence records sync to it at trip end, so
  questions work with the Pi off.
- Phones vary widely, and a 4–6 GB budget phone cannot run a 4B model, so
  measure per device.
- A browser PWA needs a WebGPU runtime; a native app has more options.
  Phone performance is not yet researched or measured.

**D4 — Model sizing on the Pi: a 4B-class model at Q4.** This applies only
when the Pi is the host, which is the last choice (D6). Revised 2026-09-22
against published Pi 5 measurements; the detail and sources are in
[ECU second brain](ecu-second-brain.md#if-it-is-hosted-on-the-pi), and
candidate models other than Qwen are in its
[candidate-models list](ecu-second-brain.md#candidate-models).
Rationale:

- **Memory on the current 8 GB board.** The daemon normally uses
  150–650 MB. The trip-end digest child may use up to 4 GiB, and the MCP
  server, gpsd and the OS take the rest. A 4B at Q4 (2.5 GB) plus about
  0.6 GB of context at 4k fits if it runs after the digest, not beside
  it. The sizing rule of ≈ 0.6 GB per 1B params at Q4_K_M holds.
- **Speed is set by memory bandwidth** (~12 GB/s measured), not RAM size.
  3–4B Q4 models generate 3.9–5.2 tok/s. The earlier "~6–12 tok/s" is
  above the bandwidth ceiling (~4.6 tok/s for 2.5 GB) and is withdrawn.
- **Prompt processing governs every case here.** It runs at about
  11–16 tok/s at Q4_K_M, and Q4_0 is about twice as fast on this CPU. A
  3,000-token prompt takes 2–5 minutes, so the server's prompt cache must
  hit between agent turns.
- **Thinking mode is not free.** It was cited here as the reason a 4B is
  enough. At about 4 tok/s, 1,000 thinking tokens take about 4 minutes.
  The first candidate is Qwen3-4B-Instruct-2507 (no thinking mode; 61.9 on
  BFCL-v3 against 57.6 for the original). Qwen3-4B Q4_K_M stays as the
  reference. Qwen3.5-4B is a later challenger, blocked by open llama.cpp
  prompt-cache and tool-call issues for hybrid models.
- **Dense 8B** (Qwen3-8B class) generates about 2 tok/s on this CPU at any
  RAM size. Not worth it.
- **<4B (e.g. 1.7B)** → fits trivially but is too shallow for cases 2/4/6
  (the plan's own original concern). Don't go below 4B for the
  diagnosis-grade cases.
- **Qwen3-30B-A3B** (MoE; the original plan target) is 16.4–18.6 GB at
  4-bit and fits no Pi. A 2.7-bit quant (9.6 GiB) was measured on a 16 GB
  Pi 5 at about 8 tok/s generation and 9–11 tok/s prompt processing, with
  a 4k context. That needs the 16 GB board (D7), runs only after the
  digest, and its answer quality at 2.7 bits is unmeasured.
- **Context cache.** Qwen3-4B needs 144 KiB per token at f16. `q8_0`
  halves it; avoid `q4_0`, which llama.cpp documents as degrading tool
  calling. Set `--cache-ram` explicitly, because its default of 8192 MiB
  is the whole current board.
- Do **not** over-quantize to claw back RAM: 4B at 4-bit is right; Q3/Q2
  on a 4B trades away the one thing the model exists for. To save RAM,
  drop a size tier, not quant precision. Choosing between Q4_K_M and Q4_0
  (similar bit width, faster prompt processing) is a speed choice;
  measure answer quality on the evaluation cases before switching.
- "Smaller" is over-determined here — it's the right call independently on
  RAM fit, post-drive **power** draw, and **thermal** soak (shorter
  inference = less time at high CPU in a sealed enclosure on vehicle
  power).

**D5 — Hailo NPU is a documented contingency, not P1.** Clarifications:

- The Hailo-8L/8 accelerates quantized **CNN/Conv** graphs. It does
  **not** run decoder LLMs.
- The Hailo-10H (Raspberry Pi AI HAT+ 2, January 2026, 8 GB on board)
  *does* run LLMs, but only precompiled models up to ~2B with a
  2,048-token context. It is slower than the Pi's own CPU on them:
  Qwen2.5-1.5B runs at 6.7 tok/s against 11.7 on the CPU
  ([CNX](https://www.cnx-software.com/2026/01/20/raspberry-pi-ai-hat-2-review-a-40-tops-ai-accelerator-tested-with-computer-vision-llm-and-vlm-workloads/)).
- Neither runs the 4B-class model this tier needs, so Qwen stays on the Pi
  CPU. The Hailo's only possible role is offloading the *one neural
  detector* (the sequence autoencoder); IF is a tree ensemble and stays on
  CPU.
- Its real (and only) justification under the field scenario: permanently
  remove the sequence-AE's RAM/CPU/thermal footprint so a resident on-Pi
  LLM tier has more of the RAM budget and the enclosure has more thermal
  headroom. It is **not** justified by drive-time contention (D1).
- This is why Phase 9 builds the TCN as a clean Conv1D **autoencoder**
  with export-clean ops: it keeps the NPU option open at zero present
  cost. The LSTM cannot be offloaded to a Hailo effectively; a
  Conv1D TCN-AE can.
- **Adopt-Hailo trigger:** only if (a) post-drive preemption (D6-B)
  proves operationally unsafe in the field *and* (b) a resident small
  model (D6-A) proves too shallow for cases 2/4/6 — both *measured*, not
  assumed. Adopting it adds an int8-quantization + on-device
  threshold-recalibration workstream (see Risks).

**D6 — Hosts, in priority order** (revised 2026-09-22). Every host calls the
same tools on the Pi.

1. **The phone, when capable** (D3).
2. **A sidecar box beside the Pi**, for example a Jetson. This is the only
   way a model runs *during* a drive without touching the collector.
3. **The laptop with Claude Code over MCP**, when present. This is the
   working reasoning layer today, and its real investigations become the
   evaluation set.
4. **A hosted model, when there is any signal.** An evidence packet is a
   few KB, so weak cellular is enough. Optional, because it needs an
   account, costs money, and sends data off the vehicle; on-device stays
   the fallback.
5. **The Pi, last**: post-drive only, under the options below.

Pi-hosting options (decide later, behind a measurement pass):

- **A. Small resident model (4B-class, D4), always co-resident.** Powers
  the interactive cases (5, 4) at review time with the daemon untouched;
  heavy cases (1–3, 6) get shallower output. Lowest risk/capability.
  *Recommended default to design around.*
- **B. Bigger model via post-drive preemption.** A larger model loads
  once the session has ended, the vehicle is parked, and the trip-end
  digest has finished. It is stopped the moment a capture starts or the
  vehicle wakes.
  - The daemon never pauses. llama.cpp maps its weights from disk, so
    killing the process frees memory at once.
  - The engineering is a preemptible unit with its own memory cap and a
    high OOM score. That requires D03.21 (memory limits actually
    enforced) first; today they are not, and 2 GB of swap is live.
  - On the current 8 GB board this is still a 4B; anything larger needs
    D7.
- **C. Hailo offload → larger model co-resident.** Per D5; contingency
  only, with the stated trigger.

**D7 — A 16 GB board buys capacity, not speed.** The operator
considered one (2026-09-22). It applies only to post-drive Pi hosting; it
does nothing for reasoning during a drive (D1).

- Generation speed is memory bandwidth divided by the bytes read per
  token, so it is the same on the 8 GB and 16 GB boards.
- 16 GB makes a 30B-A3B-class mixture-of-experts model possible (D4), at
  about 8 tok/s because only ~3B parameters are read per token. Dense 8B
  runs at about 2 tok/s.
- The 16 GB Pi 5 lists at $305 after three memory-driven price rises since
  December 2025.
- **Trigger:** the 4B, run on the current board against the known-answer
  investigation cases ([ECU second brain](ecu-second-brain.md)), fails them
  for lack of capability, *and* a 30B-A3B-class model measurably fixes
  those failures.
- Alternatives considered:
  - Jetson Orin Nano Super 8 GB: $399, 102 GB/s, about 9× faster
    generation by NVIDIA's figures, but only 8 GB and a second box to
    power.
  - AI HAT+ 2: see D5.

## Risks / open technical issues

- **Power and time budget.** Energy is small: the Pi draws 10–14 W under
  llama.cpp load, about 2 Wh for a 10-minute report. The constraint is
  time. On accessory power the UPS allows 300 s after key-off, and the
  trip-end digest shares that window. A multi-minute report runs while
  idling, under the keep-alive hold (drawing the UPS cells), or on bench
  power. Budget any keep-alive hold against the X1202's capacity, not the
  vehicle battery.
- **Thermal.** Sustained LLM inference in a sealed enclosure on a Pi 5
  needs an active-cooling/throttling plan. Cores throttle progressively
  from 80 °C and hard at 85 °C; the stock fan reaches 100 % at 75 °C.
  This interacts with HAT stacking if D5 is ever taken.
- **Memory enforcement.** D03.21 is open: the firmware disables the memory
  cgroup, so `MemoryMax` is not enforced and 2 GB of swap is live. A
  co-resident model could push the collector into the 38-minute thrash of
  2026-09-17. No model runs co-resident until D03.21 is resolved and the
  model has its own capped unit (D6-B).
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
  - The framing alone is not enough. Where residual evidence exists, the
    ranking itself should be computed (fault signature matrix, ECU second
    brain capabilities 3–4), with the model narrating it.
  - Every evidence reference must resolve to a record a tool produced
    (S.5).

## Recommended sequencing

0. **Build the deterministic second brain first** ([ECU second
   brain](ecu-second-brain.md), checklist Part A). It must give the
   Pi-and-phone user useful answers with no model at all. Everything
   below is optional and comes after it.
1. **Build the seams now, commit to no model hosting yet:** the
   `src/analysis/` module boundary, the case-5 label-capture loop
   (hypothesis + one clarifying question → enriched label → retraining
   ledger — highest value, barely needs model size), and the JSON
   structured-card dashboard contract. All import-isolated and
   feature-flagged per Status.
2. **One measurement pass before choosing a host (D6), and D6-A/B/C if
   the host is the Pi:**
   - actual resident daemon RAM and thermals;
   - prompt-processing time and whether the prompt cache hits between
     turns;
   - report latency at the measured generation speed;
   - real power draw for a representative report;
   - answer quality on the known-answer investigation cases from
     [ECU second brain](ecu-second-brain.md). Those cases are what any
     model choice is measured against;
   - candidate models on a representative mid-range and budget Android
     phone.
3. **Prefer the highest-ranked host that passes (D6).** If the Pi is the
   host, default to D6-A (resident 4B-class, D4 candidates). Treat
   deep-diagnosis quality as best-effort, not a guarantee.
4. **D6-B/C and D7 only if step 2 forces them**, with the D5 trigger for
   Hailo and the D7 trigger for a 16 GB board.

## Non-goals (explicitly not committed)

- Running an LLM on the live 1 Hz path, on the collector's CPU during
  collection, or as a daemon dependency (D1).
- Running the reasoning model on a Hailo. The 8/8L cannot run decoder
  LLMs, and the 10H is limited to ~2B models at a 2k context (D5).
- Cloud/API inference as a requirement. A hosted model is an optional
  host when there is signal (D6); on-device stays the offline fallback.
- Removing the LSTM detector (Phase 9 makes the sequence detector
  *selectable*, not LSTM-replaced-and-deleted).
- Buying/integrating the Hailo before its D5 trigger is met by measurement.

## Cross-references

- [ECU second brain](ecu-second-brain.md) — exploratory extension connecting
  this reasoning tier to persistent investigation memory, adaptive numerical
  models, and experiment outcomes. It includes:
  - the live reasoning loop and the models that run on the Pi;
  - candidate language models and where each can be hosted;
  - Pi 5 measurements and the 16 GB analysis;
  - computed hypothesis ranking and next-measurement selection;
  - calibrated uncertainty and change detection for adaptive models;
  - optional engine-management distillation.

  From the 2026-09-22 discussion and review. Does not change queue order
  or authorize model hosting.
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

- [x] **S.1 — Structured-card contract.** Landed: Pydantic models
  (`src/analysis/llm_assist/cards.py`) for a differential card (ranked
  hypotheses + evidence refs) and a narrative card (sectioned text +
  source-session refs). The "hypotheses-not-verdicts" framing is
  schema-LOCKED, not just documented — a validator unconditionally
  overwrites any input framing value with the canonical string, so a
  card literally cannot be constructed without it. JSON-schema doc
  generated from the real Pydantic `TypeAdapter`, not hand-written. No
  producer exists — the contract is the seam, per scope.
- [x] **S.2 — Dashboard card renderer.** Landed: feature-flagged
  `GET /analysis/llm-assist/cards` (disabled by real config default,
  not just example YAML), new `LlmAssistPanel.tsx` whose explicit
  absent-state is genuinely the default render path given that
  default. FE tests over fixture cards.
- [x] **S.3 — Enriched-label capture loop.** Landed: `AnomalyFeedback`
  gains `hypothesis`/`clarifying_question`/`clarifying_answer` with a
  real both-or-neither validator (not just documented intent) —
  verified rejecting a lone question or lone answer. `AnomalyPanel.tsx`
  gained the actual input fields (the doc's "operator-authored text"
  requires a real UI surface, which the first pass missed and a
  follow-up added), with client-side pairing validation mirroring the
  backend rule so a malformed submission is caught before the network
  round-trip, not just bounced off a 422. `RetrainingFeedbackReport.
  enriched_labels` genuinely reaches `scripts/retrain.py`'s ledger
  record, not a computed-but-unused field.
- [x] **S.4 — Analysis module boundary.** Landed:
  `src/analysis/llm_assist/` as the import-isolated home. Import-no-
  torch verified via a real fresh-subprocess check (avoids the
  false-negative risk of an in-process check where torch might already
  be loaded by something else). One real gap caught by review and
  fixed before landing: the capture-path guard originally checked only
  a hardcoded 6-file allowlist that missed the actual daemon entrypoint
  (`runtime.py`/`inference.py`) and most of `src/collection/` — a
  future accidental import in the real hot loop wouldn't have been
  caught. Fixed to a genuine recursive walk of `src/daemon/` +
  `src/collection/` (all ~30 files, file-count parity independently
  confirmed), re-verified.

## Follow-ups from the 2026-09-22 review (seams, still no model hosting)

Same discipline as S.1–S.4: import-isolated, feature-flagged, no daemon
dependency when disabled.

- [ ] **S.5 — Evidence references resolve.** `EvidenceReference` is a
  free-text description with optional session/channel/timestamp fields,
  so a producer can cite a session or value no tool returned and the card
  still validates. Replace it with evidence ids minted by analysis tools
  ([ECU second brain](ecu-second-brain.md), capability 2). The validator
  rejects an id no tool produced, and numbers are rendered from the
  evidence record, not typed by the producer.
- [ ] **S.6 — Structured clarifying answers.** `clarifying_answer` is free
  text. Add optional condition tags beside it (road surface, weather, fuel
  fill, towing/load) so an answer can join the retraining ledger as a
  covariate. Free text cannot.
- [ ] **S.7 — Confidence only from calibrated producers.**
  `DiagnosticHypothesis.confidence` is accepted only from deterministic
  scoring. A language model's self-reported confidence is uncalibrated;
  such a producer omits it.
