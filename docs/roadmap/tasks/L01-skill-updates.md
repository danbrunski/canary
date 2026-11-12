# L01 — Agent skill updates: `vehicle-nissan-xterra-2008` and `engine-management`

Opened 2026-11-12 from a read of both skills (`.claude/skills/…/SKILL.md` and
every `references/*.md`) against the repo, `docs/ecu-firmware-analysis.md`,
`data/nissan-xterra-2008/` and what the project has learned since each file was
last edited. Same conventions as the other task files: `[x]` only with the
edit made and checked.

Nothing here has been applied. Each item names the file, the text that is
wrong or missing, and the source for the replacement, so most are Light
dispatches. Where the source is a tracked document, quote that document, not
this file.

**What was checked and is fine.** Every symbol, file and MCP tool the skills
cite still exists (`compare_channels`, `scale_stability`, `align_pairs`,
`noise_floor.significance`, `tuning.delivery_error`, `compare_sessions`,
`resolve_session_revision`, `rom_trace`, `compute_binned_analysis`,
`fit_grid_binned_mean`, `compute_derived_channels`, `append_afr_channels`,
`afr_error_surface`, `latest_session`, `session_started_at`; `discover_sessions`
now lives in `src/preprocessing/session_reader.py` and `loader` re-exports it).
One exists but is not what it is cited for: `afr_error_surface` has no caller
outside its own tests (L01.23).
The arithmetic in `engine-management` was recomputed and holds: the 0.029031
speed-density constant, the 80 kPa / 63 kPa standard-atmosphere altitudes and
density ratios, the 7 psi worked boost case on all three compressor types, the
J1349 factor, duty = PW × rpm / 1200, lb/hr → cc/min, the returnless ΔP
deficits (7.1 % at sea level, 5.6 % at 80 kPa), the blower drive-power
fractions, and the spark-advance scaling table. One row is wrong (L01.14).

## Part A — `vehicle-nissan-xterra-2008`

Last content edit 2026-11-11 (`ca55b54e`). The truck was reflashed the same
night, so the newest section is already out of date.

- [x] **L01.1 The first-gear torque cap is described as a fact of the truck; it
  is a fact of the calibration, and the running one removed it.**
  `SKILL.md`, "Full-throttle enrichment is a switch on target throttle, and
  first gear never trips it", and the matching "WOT enrichment is a switch…"
  bullet in `references/engine-and-ecu.md`. `tune_revisions.yaml` has
  `ZS44A-20261111-2` flashed 2026-11-12T01:02:19Z: cal word `0x9308`
  3,584 → 32,767, the cap test off.
  - Say which revisions carry the cap (through `ZS44A-20261111`) and which do
    not (`-20261111-2` on). Resolve the session's revision first, as the skill
    already says for every other comparison.
  - With the cap off, first-gear full pedal is full throttle and reads the last
    Fuel Targets column: 14 of 14 gated lookups, measured AFR 12.5–12.8 from
    ~4,100 rpm where it was 11.8–12.0
    (`data/analyses/2026-11-11-first-gear-cap-off-prediction.md`,
    `2026-11-12-ratio-estimate-and-full-pedal-lean.md`).
  - Keep "sort full-pedal data by gear", and say how: rpm ÷ wheel speed, with
    the ranges per gear from the 11-12 analysis.
  - Add the third cap trigger from `docs/ecu-firmware-analysis.md`:
    `0xFFFF4856` bit 3 caps every gear. It is the first read when the truck is
    down on power in all gears with no codes.
  - The cap, its ratio threshold and the output-shaft constant are editable
    entries in `rom_map.yaml` now; point at them.
  - *Done 2026-11-12:* SKILL.md trap bullet rewritten (revision-dependent) and engine-and-ecu.md WOT-enrichment bullet gets a revision-split block with gear bands, the `0xFFFF4856` bit 3 trigger and the three `rom_map.yaml` entries. Checked: `tune_revisions.yaml` (`ZS44A-20261111-2`, flashed 2026-11-12T01:02:19.999Z, `0x9308` 3,584 -> 32,767), `2026-11-11-first-gear-cap-off-prediction.md` (14 RAM reads, last column on 14 of 14; the same file says AFR 12.5-12.8 in its summary and 12.5-12.9 in its scored table, written as 12.5-12.9), `2026-11-12-ratio-estimate-and-full-pedal-lean.md` section 1 (gear bands are rpm per 0x284 count, not per km/h, so written that way with the 0.005865 km/h per count unit), firmware doc "What limits first gear" (`0xFFFF4856` bit 3 also needs bit 7 of `0xFFFF4785`), `rom_map.yaml` ids `first_gear_torque_cap`, `first_gear_ratio_threshold`, `output_shaft_speed_constant`.

- [x] **L01.2 `SKILL.md` and `engine-and-ecu.md` disagree on how trims
  compose, and the code sides with the reference file.** `SKILL.md` (LTFT
  bullet): "use total trim `(1+STFT)(1+LTFT)−1`". `engine-and-ecu.md`: "Trims
  add, they do not multiply: `k = 1 + STFT + LTFT`" (firmware, 2026-10-05).
  `derived:TOTAL_TRIM_B1/B2` (`src/signals/derived.py`) is the sum, and
  `src/analysis/recipes/shared.py` records its unit as "additive STFT+LTFT
  index". Change `SKILL.md` to the sum. The two differ by STFT × LTFT, 0.5
  point at ±7 % each, which is the size of effect the fuel work argues about.
  Eval 1 in `evals/evals.json` asserts the product; fix it with L01.3.
  - *Done 2026-11-12:* SKILL.md LTFT bullet now says total trim adds, `1 + STFT + LTFT`. Checked `src/signals/derived.py` (`derived:TOTAL_TRIM_B1/B2` docstring "LTFT + STFT"), `src/analysis/recipes/shared.py:41` (`TOTAL_TRIM_UNIT` "additive STFT+LTFT index") and the firmware doc's `k_b = STFT + LTFT + ...` line. Eval 1 fixed under L01.14.

- [x] **L01.3 The long-term-trim story stops in September.** `SKILL.md` says
  LTFT "relearns slowly" and "was still 0.00 on 09-23"; eval 1 expects "relearn
  has taken weeks". What is known now
  (`data/analyses/2026-11-08-ltft-learning.md`, firmware trace in
  `docs/ecu-firmware-analysis.md`):
  - Learning is not slow and not disabled by the tune. It is gated on purge:
    the canister clean-out accumulator has to cross its threshold within one
    engine start, about 20–30 minutes of purging cruise (43 minutes on
    2026-11-08). It restarts at every engine start, including a stall-restart
    with the key on. A run of short town drives never opens the gate.
  - So 0.00 on both banks means "the gate has not opened since the last reset",
    and a zero spell is a driving pattern, not a fault.
  - The warm-idle cell has never learned. Light-cruise cells were learned while
    purging, so they carry vapor: with purge off, the same cell needs about
    +12 points of STFT.
  - Analysis uses total trim split by purge (DID `0x1110`, the PID `0x2E`
    byte), not total trim alone.
  Rewrite the bullet and eval 1's expected output and assertions.
  - *Done 2026-11-12:* SKILL.md LTFT bullet (reset, neutral 0x80, purge-gated, sum rule, DIDs, pointer) and a new engine-and-ecu.md section "Learned fuel trim: why it reads 0.00 and when it learns" (gate, per-engine-start accumulator, idle cell never learned, split by DID `0x1110`/PID `0x2E`). **Source differs from this item:** `2026-11-08-ltft-learning.md` measures the gate at about 32-35 min of steady purging cruise at the 11-08 leg-3 rate and 43 min into the first start, not "20-30 minutes" (the 10-27 estimate, now noted as superseded). The "+12 points of STFT" figure is in no tracked document, and section 7 does **not** say the light-cruise cells carry vapor: it says the near-idle cells r0c0 and r1c0 (< 6 g/s) were learned with purge duty 6-30 % and "most likely carry that purge-compensation error"; the skill and eval 1 now say exactly that.

- [x] **L01.4 The fuel equation section has no purge term, and its known answer
  only holds with purge off.** `engine-and-ecu.md`, "As decoded from this ECM
  … data-checked, median 1.006 over 5,039 samples". On the four post-pump
  captures the same chain reads 1.006 with purge off and 0.90–0.95 while
  purging (E07.3, E09.11). Add the purge factor α (`0xFFFF4698` / `469A`,
  "EVAP purge factor α" in the firmware doc) to the equation as written there,
  and the rule that goes with it: score the chain on purge-off reads until α is
  read live, and never run a plant evaluation on a purging read without it.
  - *Done 2026-11-12:* engine-and-ecu.md fuel-equation list gets an `α_purge` bullet (`0xFFFF4698`/`469A`, clamp [0.5, 1.0], reset when purge inactive) with the purge-off-only rule, named apart from the patent-form α in the same section. Checked firmware doc "EVAP purge factor α" and its Data bullet (1.006 purge off, n 1,659; settled 0.904-0.944 by drive) and `2026-11-08-ltft-learning.md` section 7 (D03: 0.898 at idle, 0.950 off idle).

- [x] **L01.5 "CANary's voltage curve skips the barometric gain" has been false
  since 2026-10-07.** `engine-and-ecu.md`, upstream-sensor row. `e023c740`
  (B06.7) made the A/F voltage route apply the ECM's gain through the ECM's own
  AFR table; `tests/test_af_baro_gain.py` pins it. The lean bias now applies to
  UpRev's AFR and to CANary analyses made before that commit. Add the two
  consequences the row leaves out: short-term trim controls on that same word,
  so closed-loop agreement between λ and target proves nothing about the
  sensor; and an independent λ needs an external wideband, which this truck
  does not have (the AEM gauge is fuel pressure).
  - *Done 2026-11-12:* SKILL.md AFR trap and engine-and-ecu.md upstream-sensor row rewritten: gain applied by CANary since `e023c740` (B06.7, 2026-10-07, commit message and `tests/test_af_baro_gain.py` verified), UpRev's AFR and older CANary analyses still lean, STFT controls on the same word, independent λ needs an external wideband (none on the truck).

- [x] **L01.6 The hardware boundary has already started, and the fuel-pressure
  sensor is not the one the skill describes.** `SKILL.md` boundary 2 says the
  build "lands from late 2026-09" and lists every part as still to come.
  - The Aeromotive 340 pump assembly went in between 2026-10-10 and 2026-10-26.
    Warm-idle rail pressure reads 53.78 psi against 50.79 stock on the same
    instrument, about +2.5–3 % injector flow, shrinking to +1.3 psi at
    60–100 g/s (`docs/roadmap/supercharger-build.md`, P2 notes). The
    `engine-and-ecu.md` fuel-system row ("≈ 350 kPa (51 psi), regulator in the
    in-tank module") describes the module that is no longer fitted.
  - Say what is fitted today and what is still pending, with dates, and tell
    the reader to place a session against the pump as well as the flash.
  - `SKILL.md` "Air, fuel and altitude numbers": fuel pressure comes "from an
    AEM CAN gauge". It is the AEM 30-0301's analog output through a USB ADC
    (`hardware.usb_adc`, `data/usb_adc_sensors.yaml`,
    `docs/aem-fuel-pressure-analog-input.md`), live since 2026-10-04.
  - Add how to read that channel: stored rows are 5-sample means of a
    503.9 Hz stream since 2026-10-04T21:52Z (plain 100 Hz before); the gauge
    regenerates its output at 176.4 Hz, so nothing above ~88 Hz is rail
    pressure; average at least 0.5 s for a pressure; the +1.8 psi
    analog-versus-face offset is unresolved, so compare ratios on the one
    instrument; the feed enters on the driver side, so it mostly sees bank 2.
  - *Done 2026-11-12:* SKILL.md boundary 2 (fitted vs pending, window 2026-10-10..26, place a session against the pump), the "AEM CAN gauge" sentence (now the analog output through the USB ADC) and a new engine-and-ecu.md section "Fuel pressure: the pump and the AEM channel"; the fuel-system row now says the module is gone. Checked `supercharger-build.md` F0 (53.78 vs 50.79 psi, +3.0 -> +1.3 psi, +2.5-3 % flow), `docs/aem-fuel-pressure-analog-input.md` (503.9 Hz, 5-sample mean unless `analog_full_rate`, 176 Hz steps, >= 0.5 s, +1.8 psi), `2026-10-08-fuel-pulsation-capture.md` (driver side = bank 2). **Not verified:** the item's "since 2026-10-04T21:52Z" hour is in no tracked source, so the skill says "flashed 2026-10-04".

- [~] **L01.7 `labelled_events.yaml` has no entry for the pump.** The skill
  tells the reader to record the build as an `intervention`; the first part of
  it is in the truck and unrecorded, so nothing stops a fuel comparison from
  pooling across it. Add the event with the 2026-10-10..10-26 window. Whether
  it carries `corpus_boundary: true` is the operator's call (a ~3 % fuel
  delivery step that closed-loop trim absorbs). The same file's title for
  `ltft-cleared-flash-2026-09-20` still says "still relearning"; close it with
  the 2026-11-08 result.
  - *Partial 2026-11-12:* Text only, as instructed: SKILL.md boundary 2 now says the pump is unrecorded in `labelled_events.yaml` and that the `corpus_boundary` call is the operator's. **Remaining:** (a) the `labelled_events.yaml` entry for the 2026-10-10..10-26 pump window, and (b) closing the `ltft-cleared-flash-2026-09-20` title ("still relearning") with the 2026-11-08 result. Both are data edits, not made. **Operator question:** is the pump a `corpus_boundary: true` intervention (a ~3 % delivery step that closed-loop trim absorbs), or an unflagged `intervention`?

- [x] **L01.8 Speed and gearing: the truck is on 4.08 axles and the skill never
  says so.** It is in `docs/ecu-firmware-analysis.md` ("The 4.08 regear makes
  every gear read about 10–12 % short"), in `dyno.yaml` (`ratio_to_gps`
  0.9203) and in a comment in `src/preprocessing/segmenter.py`, and it changes
  how this truck's data must be read:
  - ECM vehicle speed (PID `0x0D`, DID `0x1102`, the broadcast speed) is about
    0.92 × GPS speed. Distance, economy and acceleration from OBD speed are 8 %
    low unless corrected; `dyno.yaml` holds the factor per period.
  - Rolling circumference is 2.559 m since 2026-10-03 and was 2.609 m before
    (H07.11). That is a boundary for anything derived from wheel speed.
  - The ECM's own gear-ratio estimate (`0xFFFF27D2`) reads 10–12 % high in
    every gear because cal `0x91F8` still holds the stock axle. The firmware
    doc's conclusion is to leave that word alone.
  Add a short "Driveline and speed" block under "Physical and environmental
  facts", pointing at those three sources.
  - *Done 2026-11-12:* SKILL.md "Driveline and speed" block under Physical and environmental facts, plus a canonical-homes row (`profiles.yaml` marked repo root). Checked `dyno.yaml`: 0.9203 is the committee relation for `obd:SPEED` (PID `0x0D`) and `WHEEL_SPEED_6F7_A`; `VEHICLE_SPEED_2D1` (`0x2D1`) was measured separately (H07.3, 0.9314-0.9348, 0.932) and "does NOT share their 0.9203"; `H07-driving-dyno.md` H07.11 (2.559 m set 2026-10-03, 2.609 m before); firmware doc "The 4.08 regear makes every gear read about 10-12 % short". **Source differs from this item:** `dyno.yaml` holds the ratio per speed channel, not "per period".

- [x] **L01.9 The injector-rescale advice contradicts itself inside one file.**
  `engine-and-ecu.md` says "Rescale injectors with `0x9254`" (the flow scaler
  moves no load axis), and later "The injector swap needs more than K … lower
  K (first cut K × old/new flow)", and "K changes anyway with the 700 cc
  injectors". `docs/roadmap/supercharger-build.md` ("Injector rescale") chose
  the scaler, with new dead-time and short-pulse constants. Keep UpRev's
  K-first procedure as what UpRev documents, state the project's plan once,
  and drop "K changes anyway".
  - *Done 2026-11-12:* engine-and-ecu.md: the "Rescale injectors with `0x9254`" bullet now states the project plan once (scaler + new dead-time and short-pulse constants, K untouched, per `supercharger-build.md` "Injector rescale"); the later paragraph is rewritten as "UpRev's procedure, and the project's plan" with K-first kept as UpRev's documentation; "K changes anyway with the 700 cc injectors" removed.

- [x] **L01.10 The boosted fuel deficit is quoted two ways.** `SKILL.md`
  boundary 2: "a ~5–7 % WOT flow deficit". That is the √ΔP term alone. The
  ECM's pressure factor also assumes at least 0.2 × baro of vacuum that is not
  there, so the firmware doc and `supercharger-build.md` put it at roughly 10 %
  lean at 50 kPa of boost on a fixed rail and about 2 % with a 1:1
  boost-referenced regulator. Quote the build doc's figure and say what it is
  made of.
  - *Done 2026-11-12:* SKILL.md boundary-2 deficit bullet: quotes the build doc's roughly 10 % lean at 50 kPa boost on a fixed rail (about 2 % with a 1:1 regulator) and says it is the sqrt(dP) term (5-7 %) plus the ECM's >= 0.2 x baro vacuum assumption. Checked `supercharger-build.md` "What the ECM firmware says" (10 %, 2 %, 366 kPa) and "Boosted fuelling without a pressure input" (7 %).

- [x] **L01.11 "WOT ran +2–4 % lean on three instruments" should go.**
  `engine-and-ecu.md`, "The WOT fuel error is airflow-dependent". The three
  instruments are one A/F sensor per bank (L01.5). The +3 % was in-sample, and
  the held-out pulls were inside the 2.5 % noise floor. Since then two flashes
  changed WOT fuel: `ZS44A-20261110` cut Fuel Comp at high rpm and load, taking
  the error against target at 4,400–5,600 rpm from −4.2/−7.1/−6.8 % to
  +1.2/+0.6/+0.4 %, and `ZS44A-20261111` set the last-column target to
  AFR 12.3. Replace the bullet with the current state, read from
  `tune_revisions.yaml` and the two `data/analyses/2026-11-1{0,1}-*-prediction.md`
  write-ups. "Don't propose a K change for this" still stands. The same
  paragraph's "a plausible contributor to the WOT lean" (the k105 axis edit)
  needs the same treatment.
  - *Done 2026-11-12:* engine-and-ecu.md: the "+2-4 % lean on three instruments" bullet replaced with the state after `ZS44A-20261110` (Fuel Comp cut, -4.2/-7.1/-6.8 % -> +1.2/+0.6/+0.4 %, verified in `2026-11-10-fuel-comp-wot-cut-prediction.md` lines 52-54, 178-180) and `ZS44A-20261111` (last column to AFR 12.3); "Don't propose a K change" kept. The k105 sentence: **source differs from this item's premise** - `tune_revisions.yaml` shows `ZS44A-20261004` restored the Fuel Comp axis and cells to UpRev base, so the "plausible contributor to the WOT lean" is moot (the later WOT error was rich); written that way.

- [x] **L01.12 Two idle and trim traps are missing from "The traps that have
  cost the most".**
  - Warm-idle lean follows heat. Purge off, total trim at idle was 0.0 / −1.6 %
    on a cool morning (IAT 19–28 °C) and +7–10 / +4–8 % on a hot afternoon
    (IAT 41–65 °C). A fixed idle correction sized from one hot drive was
    proposed and withdrawn (`data/analyses/2026-11-08-tuning-proposals.md`,
    "Corrected 2026-11-10"). Compare idle trims at matched IAT and rail
    pressure before calling a leak or sizing an edit.
  - Polled rows in a `tuning` capture carry the round's scheduled start as
    `timestamp`. The wire time is `metadata_json.acquired_at`; RAM reads sit a
    median 164 ms after their stamp. Join polled rows to broadcast frames on
    the acquired clock (`read_channels(…, acquired_time=True)`, which
    `compare_channels` already does). A hand join on `timestamp` produced a
    withdrawn "stray read" on 2026-11-12 (`docs/capture-rounds.md`).
  - *Done 2026-11-12:* SKILL.md traps: warm-idle-lean-follows-heat bullet (figures from `2026-11-08-tuning-proposals.md` "Corrected 2026-11-10") and the acquired-clock bullet (median 164 ms, `read_channels(..., acquired_time=True)`). **Source differs from this item:** the withdrawn "stray read" is documented in `2026-11-11-first-gear-cap-off-prediction.md` ("Clock"), not in `docs/capture-rounds.md`; both are cited.

- [x] **L01.13 `references/diagnostic-access.md`: four additions.**
  - DID `0x120B`: reading it clears bit 0 on every DTC item. The catalog
    carries that as `side_effect`; the reference says only that it "reads P0328
    while driving". Say it is a read with a write in it and that sweeps skip it.
  - Knock: P0328 has been confirmed (not just pending) since 2026-10-10, the
    fallback holds for the whole key cycle, and Mode 04 clears the code but not
    the fault. One line, pointing at "Knock monitoring".
  - SRS on K-line: only the 25 ms / 25 ms fast init is answered (the reverse
    of the ECM), request bytes must be spaced 15 ms apart (P4) or they are
    ignored, reads are `A2 00` (current) and `A2 01` (past) → `E2 n [hi lo]×n`.
    The reference has only the clear.
  - BCM: local identifiers are one byte, so `POST /control/obd_request` needs
    `identifier_bytes: 1` with `header` 0x745 and `response_id` 0x765. Frame
    `0x385` carries the tire pressures (bytes 2–5 × 0.25 psi, byte 6 high
    nibble = sensors received) and is not in `bindings.yaml` yet. TPMS sensor
    registration writes the BCM's non-volatile memory, so it falls under the
    `AGENTS.md` conditions (`data/analyses/2026-11-11-tpms-bcm-read.md`).
  - *Done 2026-11-12:* diagnostic-access.md: `0x120B` side effect (verified against `data/dids.yaml` `side_effect`, `did_scan.without_side_effect_dids`, `refuse_side_effect_dids`), knock-code line (firmware "Knock monitoring": confirmed `0x3F`, fallback holds the key cycle, Mode 04 clears the code only), SRS K-line reads (`kline.yaml` `request_timing.p4_ms`, `2026-10-08-srs-kline-code-read.md`, H05 fast-init answered / long-low not), BCM one-byte ids with `identifier_bytes: 1`, header 0x745 / response_id 0x765 (documented in `docs/headless-api.md`, committed) and frame `0x385`. `bindings.yaml` does carry bytes 2-5 as `hypothesized`, so the skill says that rather than "not in bindings.yaml".

- [x] **L01.14 `evals/evals.json`: fix eval 1, add three.** Eval 1 per L01.2
  and L01.3. New cases, each with the trap as the prompt's premise:
  first-gear full pedal on a named revision (answer depends on which); a pulse
  chain that reads 0.93 on a purging cruise (split by purge before blaming the
  injector model); a rail-pressure comparison across 2026-10-10..26 (the pump).
  - *Done 2026-11-12:* `evals/evals.json` (valid JSON): eval 1 rewritten (purge gate, sum not product); evals 4 (first gear by revision), 5 (0.93 pulse chain while purging) and 6 (rail pressure across the pump swap) added; eval 3 updated to the L01.26 figures. `.venv/bin/python -m json.tool` passes.

- [x] **L01.26 The WOT VE range does not follow from its own airflow
  figure.** `SKILL.md`, "Air, fuel and altitude numbers worth knowing": "WOT
  airflow at home plateaus near 145 g/s from ~5,000–5,850 rpm. Against the
  measured 80 kPa that is 85–93 % VE". With airflow flat, VE goes as 1/rpm, so
  the two ends must differ by 5,850 / 5,000 = 1.17; 85–93 is a ratio of 1.09.
  At 80 kPa, 25 °C and 3.954 L the figures are 94 % at 5,000 rpm and 80 % at
  5,850. Either the plateau is narrower than stated or the range is about
  80–94 %; read a stored WOT pull and write whichever it is. The
  sea-level comparison in the same bullet ("67–73 %") needs the same
  correction. (Added from the physics review, [M01](M01-physics-model-accuracy.md).)
  - *Done 2026-11-12:* SKILL.md air bullet rewritten from a stored-pull read: four tuning captures (`_capture-tuning-2026-11-10T170119`, `-11-10T235310`, `-11-11T174602`, `-11-12T034249`), Mode 01 MAF/RPM/throttle >= 80 % paired within 250 ms, VE = MAF / (rpm/120 x 3.954 L x rho), rho from the session's median baro (81-82 kPa) and IAT (10-20 C). Airflow is not flat to 5,850: 138-141 g/s at 5,000, 144-147 at 5,500-5,750, no samples above ~5,750 rpm; VE about 0.90 at 4,000-4,500, 0.86-0.87 at 5,000, 0.80-0.83 at 5,500-5,750; at an assumed 101.3 kPa that is 65-73 %. The skill text itself says these are approximate (manifold pressure taken as baro at WOT, rho from session median baro and IAT) and no longer calls the curve "healthy"; eval 3 follows.

- [x] **L01.27 The skill does not say what fuel the truck burns.** Add to
  "Air, fuel and altitude numbers worth knowing" (operator, 2026-11-12):
  standard pump gasoline, usually E10, so fuel-mass arithmetic uses a stoich
  of about 14.1 with 14.7 as the E0 bound, while the ECM's AFR numbers stay
  λ × 14.7 whatever is in the tank (M01.3). With the supercharger the truck
  moves to 91 octane. The build section quotes Z1's figures "on 93 octane";
  say beside them that this truck will run 91, so those figures and any
  timing borrowed from a 93-octane tune are an upper bound, not a target.
  - *Done 2026-11-12:* SKILL.md air section gets the fuel bullet (E10 stoich ~14.1, 14.7 the E0 bound, ECM AFR = λ x 14.7, 91 octane with the supercharger) and the build bullet says Z1's 93-octane figures are an upper bound. Checked `M01-physics-model-accuracy.md` M01.3(b) (operator answer 2026-11-12).

## Part B — `engine-management`

Not edited since 2026-09-27 (path renames only). The physics is sound. The
gaps are lessons the project paid for in October and November that are true of
engines in general, and the CANary mapping file.

- [x] **L01.15 The altitude table's idle-vacuum row is wrong.**
  `references/air-load-and-boost.md`, "Barometric pressure and altitude":
  "Idle manifold *gauge* vacuum … reads ~20 % lower … ~38 % lower". That scales
  vacuum by the pressure ratio, which would need idle MAP to scale with baro.
  It does not: idle needs a roughly fixed air mass, so absolute MAP stays about
  where it was and gauge vacuum falls by about the barometric difference,
  21 kPa (6.3 inHg) at 80 kPa and 38 kPa (11.3 inHg) at 63 kPa. From a
  20 inHg sea-level idle that is about 30 % and 55 % lower (a little less,
  since pumping work falls too). The service rule of 1 inHg per 1,000 ft says
  the same. Replace the row, and give it in inHg or kPa rather than percent.
  - Evidence: Edited 2026-11-12: `references/air-load-and-boost.md` table row and the paragraph under it. Recomputed with `.venv/bin/python`: standard atmosphere drop 21.34 kPa (6.30 inHg) at 1,950 m and 38.31 kPa (11.31 inHg) at 3,830 m; from a 20 inHg (67.7 kPa) idle, MAP 33.6 kPa stays put, so vacuum goes to 46.4 kPa (13.7 inHg, 31 % lower) at 80 kPa and 29.4 kPa (8.7 inHg, 57 % lower) at 63 kPa. The old claim (20 % / 38 % lower) was the pressure ratio. The 1 inHg per 1,000 ft rule gives 6.4 inHg at 1,950 m (agrees) but 12.6 inHg at 3,830 m against 11.3 (overstates above ~10,000 ft; the text says so).

- [x] **L01.16 Purge does more than add unmetered fuel.** `SKILL.md` and
  `references/fuel-and-lambda.md` treat purge as a trim confounder only. Add:
  - Many ECUs cut the injector pulse by a purge factor of their own. A
    pulse-versus-airflow check, or any fuel model built from the pulse, has to
    be split by purge state, or the factor reads as a 5–10 % injector error.
  - Adaptation can be gated on purge: some ECUs learn only after the canister
    has been cleaned out in the current engine start, or only in purge-off
    phases. "Exactly 0.0 % on both banks" then has a fifth cause beside the
    four listed (a gate that a short-trip pattern never opens), and a learned
    cell may have been learned with vapor in it.
  - Report trims split by purge state before reading their shape.
  - Evidence: Edited 2026-11-12: purge bullet in `references/fuel-and-lambda.md` (own pulse factor, purge-gated adaptation, vapor in learned cells, split trims by purge), the "exactly 0.0 %" caution there (fifth cause), the pulse equation (purge factor term), and the two matching bullets in `SKILL.md`. Cross-checked against the firmware doc's α clamp [0x3200, 0x6400] (never below 0.5), consistent with the 5-10 % figure.

- [x] **L01.17 "Two independent instruments" needs a definition of
  independent.** `references/log-analysis-method.md`, "Reproduce a known answer
  …" offers "a wideband against the ECU's own λ". Add the test: count sensors,
  not channels. A scan tool's AFR, the ECU's λ PID and the sensor-voltage DID
  from one probe are one instrument read three ways, and a third-party tool
  that converts the raw voltage skips whatever compensation the ECU applies
  (barometric or exhaust-pressure gain), so the three can also disagree by a
  scale factor with nothing wrong. A closed-loop controller working on that
  sensor makes agreement with target circular. Put the one-line version in
  `SKILL.md` → "The traps".
  - Evidence: Edited 2026-11-12: "count sensors, not channels" paragraph in `references/log-analysis-method.md` ("Reproduce a known answer") and a one-line trap in `SKILL.md` "The traps".

- [x] **L01.18 An idle-only lean trim is not always a leak.**
  `references/fuel-and-lambda.md`, differential-diagnosis table. "Large at
  idle, shrinks as load rises" lists leaks and unmetered air. Add a row or a
  note: if the idle trim moves with under-hood or fuel temperature between
  sessions (hot fuel, rail pressure sag, hot air at the meter), it has the
  additive shape without being a fixed quantity. Compare idle trim at matched
  intake temperature and rail pressure across sessions before sizing anything.
  - Evidence: Edited 2026-11-12: note after the differential-diagnosis table in `references/fuel-and-lambda.md` and the same caveat in the `SKILL.md` table row.

- [x] **L01.19 Full pedal is not full throttle, and the fuel target can follow
  the throttle.** `references/torque-structure.md` lists a gear-dependent
  torque limit among the silent limiters. Add the consequence for fuel: an ECU
  that switches to its power-enrichment target on *target throttle* fuels the
  same pedal differently in a capped gear, so full-pedal λ, timing and airflow
  have to be sorted by gear and read against throttle, not pedal. On a vehicle
  without the torque PIDs, pedal versus throttle is the requested-versus-actual
  pair that is available. One sentence in `SKILL.md` → "Torque-structured
  ECUs".
  - Evidence: Edited 2026-11-12: new paragraph "Full pedal is not full throttle" in `references/torque-structure.md` (by gear via rpm ÷ road speed, against throttle, resolve the calibration revision) and one sentence in `SKILL.md` "Torque-structured ECUs".

- [x] **L01.20 Comparing pulls: name the statistic before the drive, and match
  on rpm.** `references/log-analysis-method.md`, "What a valid single pull
  needs". Add: band medians move with where in the band the samples fall (a
  timing median moved +2.5° from rpm composition alone on 2026-11-11), so
  compare at matched rpm and matched pedal or throttle; drop the first
  ~0.05 s after tip-in; and write the statistic and its pass band down before
  the data exists (`SKILL.md` "Prescribing a change" step 6 says this for the
  prediction, not for the statistic).
  - Evidence: Edited 2026-11-12: "Comparing pulls adds three rules" in `references/log-analysis-method.md` (matched rpm, drop first ~0.05 s after tip-in, fix statistic and pass band beforehand) and `SKILL.md` "Prescribing a change" step 6. The +2.5 deg example was checked against `data/analyses/2026-11-11-wot-target-lean-prediction.md` ("the +2.5° at 5,200–5,600 is rpm mix").

- [x] **L01.21 Knock-sensor faults can be invisible to a generic reader and can
  switch timing tables.** `references/spark-and-knock.md` already says a
  faulted knock system falls back to a fixed retard and that some ECUs expose
  knock only through DTCs. Add: knock-sensor circuit codes are often non-MIL
  and may be absent from Mode 03/07 altogether (manufacturer self-diagnosis
  only), so "no codes" on a generic reader does not clear the knock system; a
  fallback can also select a different timing table (a high-detonation map), so
  a timing-table edit may not be the table being read. Check which table is
  live before editing, and treat timing data taken in fallback as describing
  the fallback.
  - Evidence: Edited 2026-11-12: two bullets in `references/spark-and-knock.md` (non-MIL knock codes absent from Mode 03/07; fallback can select another timing table, check the live table, fallback data describes the fallback).

- [x] **L01.22 `references/sensors-and-failure-signatures.md` has no
  fuel-pressure entry.** Add one: an added pressure gauge's analog output may
  be regenerated in steps at the gauge's own update rate, which aliases rail
  pulsation and limits bandwidth to half that rate; average over at least half
  a second for a pressure; compare ratios on one instrument rather than
  absolute readings across two; a brown-out during cranking reads as a
  pressure fault. Also one line under "Reading DTCs": a diagnostic read can
  have a side effect (a status read that clears flags), so know what a read
  does before polling it.
  - Evidence: Edited 2026-11-12: new "Fuel pressure (rail pressure)" section and a "diagnostic read can have a side effect" bullet under "Reading DTCs" in `references/sensors-and-failure-signatures.md`.

- [~] **L01.23 `references/canary-workflow.md` is two months behind the
  tools.**
  - `vehicle_state` lists five states. There are nine: add `parked`, `bench`,
    `passive` and `unknown` (`AGENTS.md`). `bench` and `parked` matter here
    because neither is a fault.
  - "CANary can view a ROM and prepare a hash-bound *offline* Fuel Targets
    edit" undersells it. Add the ROM Editor (`preview_rom_edit`,
    `simulate_rom_edit`, `src/analysis/rom_editor.py`, `tools/rom_sim.py`;
    which tables are editable comes from `rom_map.yaml`), the twin and plant
    tools (`get_twin_session_evaluation`, `get_ecu_application_model`,
    `tools.fuel_plant_check`), and the flash record
    (`scripts/decode_uprev_flash.py`, `tune_revisions.yaml`). Only the operator
    flashes; that sentence stays.
  - "Rate and staleness": add the acquired-clock rule from L01.12 in its
    generic form (a round-stamped capture's `timestamp` is a presentation
    stamp; pair on `acquired_at`).
  - "Getting data": add the two operating rules that are in `AGENTS.md` and
    not here: pull a session to the laptop before analyzing it (no analysis on
    the Pi while it collects), and a tuning capture runs for the whole drive.
  - The analysis table has no row for the driving dyno (`src/analysis/dyno_*`)
    or for fuel-pressure evidence (`delivery_evidence`); add them once M01 has
    settled which of their outputs are trustworthy.
  - The analysis table points at `afr_error_surface` (`src/signals/afr.py`).
    Nothing in `src/`, `scripts/` or `tools/` calls it; the surface the
    dashboard draws is `workbench.trim_surface`. Point the row there, and drop
    the function from the table if R26.5 deletes it.
  - Evidence: Five of six bullets done 2026-11-12 in `references/canary-workflow.md`: nine `vehicle_state` values (checked against `AGENTS.md` and `docs/mcp-server.md`), ROM Editor / twin and plant / flash record (tool names and files confirmed to exist: `preview_rom_edit`, `simulate_rom_edit`, `get_twin_session_evaluation`, `get_ecu_application_model`, `tools/fuel_plant_check.py`, `scripts/decode_uprev_flash.py`, `rom_map.yaml`), the acquired-clock rule, the two operating rules, and the `afr_error_surface` row (now points at `workbench.trim_surface`; `afr_error_surface` still exists in `src/signals/afr.py`, so it is flagged as caller-less rather than dropped; drop it if R26.5 deletes it). Remaining: the driving-dyno (`src/analysis/dyno_*`) and `delivery_evidence` rows, which wait on M01.

- [x] **L01.24 Trim composition is ECU-specific.** `references/fuel-and-lambda.md`,
  "How big is too big" uses "Total (STFT+LTFT)" and the pulse equation shows a
  sum. Say in one sentence that some ECUs add the two corrections and others
  multiply them, that the difference is the product term (under 1 point inside
  ±10 %), and that a reconstruction of the pulse must use the ECU's own form.
  - Evidence: Edited 2026-11-12: sum-versus-product paragraph under "How big is too big" in `references/fuel-and-lambda.md`, and a pointer in the pulse-building section.

- [x] **L01.25 Evals.** `evals/evals.json` has five cases, none on the items
  above. Add: a purge-split case (L01.16), a same-sensor "three instruments
  agree" case (L01.17), and a hot-idle lean case (L01.18).
  - Evidence: Edited 2026-11-12: `evals/evals.json` now has 8 cases; new ids 5 `purge-split-pulse-and-trim`, 6 `three-instruments-same-sensor`, 7 `idle-only-lean-trim-follows-heat`. `.venv/bin/python -m json.tool` accepts it.

## Order

L01.1–L01.3 and L01.5 first: they are statements a reader would act on today
and they are wrong. L01.15 is a one-row fix. The rest in any order; L01.23's
last bullet waits on M01. L01.26 needs one stored pull read. L01.27 is two
sentences and can go with L01.1.
