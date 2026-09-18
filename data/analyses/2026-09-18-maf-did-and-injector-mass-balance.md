# D04.4 + D03.9 desk analysis: ECM_MAF_GPS linearity and the injector mass balance (2026-09-18)

Data: the three hybrid captures from 2026-09-17, pulled to `~/canary-data/sessions-0917/`.
b6 = `_capture-tuning-hybrid-research-2026-09-17T184821`, b7 = `…T191712`, b9 = `…T201000`.
Mode 01 at about 4 Hz and DIDs at about 2 Hz, alternating, with a median DID-to-Mode-01 separation of 100–130 ms.
Every comparison below goes through `src.analysis.compare.compare_channels` / `align_pairs` on the acquisition clock, with an explicit `PairingPolicy`. Nothing is averaged before pairing.
Machine-readable results are in `2026-09-18-maf-did-and-injector-mass-balance.json`. The scripts are in the session scratchpad (paths listed in the JSON).

## Summary

1. **The known answer reproduces.** DID `ECM_RPM` against PID 0x0C gives slope 0.995–1.007, r² 0.996–0.999 and `stable` in all three blocks.
2. **D04.4: `ECM_MAF_GPS` ×0.01 has no scale error and no nonlinearity.** In quasi-steady operation the per-band median ratio DID/PID 0x10 is 0.986–1.017 from 3 to 134 g/s, and 0.9996–1.0065 in every band with at least 26 pairs. Yesterday's whole-session b6 figure reproduces exactly (slope 0.9338, r² 0.909, 6.98 % `drifting`). It is **acquisition skew during transients**: the DID is acquired about 115 ms after the Mode 01 sample 77–81 % of the time, so rising-flow pairs read about 7 % high and falling-flow pairs read low. b6's >90 g/s samples are 20 pairs, all inside one pull.
3. **D03.9: `0x1208` is not injected fuel, so no PW calibration can close the gap.** At matched air per intake event, the register moves **+1 %** when the commanded (and measured) φ moves **+20 %**. During decel fuel cut, with λ pegged at 2.0, it reads the same counts per gram of air as fuelled operation. It is a function of air charge alone, at **≈ 63,850 counts per gram of air per intake event** (CV 1–2 %). That is the signature of a base-fuel-schedule (TP-like) quantity, not a final injection pulse. Gain-only and gain + dead-time fits both leave a −14 to −18 % residual in every commanded-rich cell, in training and in validation. The fitted "dead time" is 264 counts (b6) or −11 counts (b7), which is none.
4. **Closed-loop anchor:** φ_implied with the current constant is 1.0252–1.0267 in all three blocks, flat across fuel quantity. The constant is 2.6 % low even in its own domain; the closed-loop gain is 939,400 counts/g at φ = 1.

## Step A: known answer (`obd:RPM` → `did:ECM_RPM`)

| block | tol | n_pairs | slope | offset (rpm) | r² | drift | verdict | b−a (rpm) |
|---|---|---:|---:|---:|---:|---:|---|---:|
| b6 | 300 ms | 2,377 | 1.0072 | −13.7 | 0.9955 | 0.79 % | stable | +4.4 ± 23 |
| b6 | 150 ms | 2,307 | 1.0069 | −12.6 | 0.9964 | 0.78 % | stable | +4.6 ± 21 |
| b7 | 300 ms | 2,616 | 1.0001 | +3.4 | 0.9989 | 0.42 % | stable | +3.7 ± 24 |
| b7 | 150 ms | 2,486 | 1.0004 | +2.5 | 0.9989 | 0.36 % | stable | +3.6 ± 24 |
| b9 | 300 ms | 4,664 | 0.9951 | +15.9 | 0.9982 | 0.72 % | stable | +4.5 ± 23 |
| b9 | 150 ms | 4,079 | 0.9970 | +11.5 | 0.9987 | 0.68 % | stable | +4.4 ± 20 |

The pairing, the clocks and both decoders are sound, so I proceeded to Steps B and C.

## Step B (D04.4): `obd:MAF` → `did:ECM_MAF_GPS`

### The gates, stated explicitly

- **Plateau** (the literal D04.4 definition): every Mode 01 MAF sample whose centred 5 s window has at least 2 Hz coverage and (max − min) ≤ max(2 g/s, 10 % of the window median). Contiguous passing samples, with gaps under 1 s, form a plateau if they last at least 10 s.
  - **Result: plateaus exist only below 67 g/s.** Across all three blocks there are 6 + 5 + 15 plateaus: 3–5 g/s at idle, 30–41 g/s at cruise, and one each at 50.7, 57.0 and 63.3 g/s. At 5 s windows, 60–200 g/s samples have a median relative range of 80–100 %; they are pulls, not holds. Plateaus alone cannot answer the high-flow question.
- **Quasi-steady** (used for the bands): a Mode 01 MAF sample is kept when |d ln MAF/dt| ≤ 5 %/s. The rate comes from a centred difference over the Mode 01 neighbours within ±0.6 s, and samples without neighbours on both sides are excluded. This bound is derived, not tuned: at the 95th-percentile separation of about 0.2 s, skew can contribute at most about 1 % error. DID rows are not filtered. The policy is 300 ms with reference = Mode 01.

### Unbanded (reproducing yesterday)

| block | policy | n | slope | offset | r² | drift | verdict |
|---|---|---:|---:|---:|---:|---:|---|
| b6 | 300 ms, ref auto | 2,398 | **0.9338** | 1.89 | **0.9094** | 6.98 % | drifting |
| b7 | 300 ms, ref auto | 2,631 | 0.9768 | 0.86 | 0.9611 | 0.08 % | stable |
| b9 | 300 ms, ref auto | 4,892 | 0.9817 | 0.52 | 0.9545 | 0.57 % | stable |
| b6 | quasi-steady | 950 | 0.9702 | 0.90 | 0.9516 | 4.69 % | drifting |
| b7 | quasi-steady | 910 | 0.9961 | 0.15 | 0.9894 | 2.99 % | drifting |
| b9 | quasi-steady | 2,358 | **0.9971** | 0.05 | **0.9934** | 0.41 % | **stable** |

Transient pairs, split by the sign of dMAF/dt (>10 %/s):

| block | rising: mean ratio | falling: mean ratio |
|---|---:|---:|
| b6 | 1.067 | 0.999 |
| b7 | 1.083 | 0.982 |
| b9 | 1.055 | 1.001 |

The DID comes after Mode 01 in 77 % (b6), 75 % (b7) and 81 % (b9) of pairs, with a median of +115 to +133 ms. The whole-session slope is a skew artefact, and b6 is worst because its hardest pull contributed 20 high-flow pairs, every one of them transient.

### Per band, quasi-steady (300 ms)

`slope`, `r²` and `verdict` come from `compare_channels` on the band subset. `ratio` is the median of b/a over the same pairs. Inside a narrow band the slope is dominated by noise on x (errors-in-variables dilution pulls it below 1), so the 1:1 ratio is the statistic to read there.

| band g/s | block | n_pairs | slope | offset | r² | verdict | median ratio (p10/p90) | pairs within ±2 % |
|---|---|---:|---:|---:|---:|---|---|---:|
| 3–10 | b6 | 105 | 1.97 | −6.9 | 0.08 | unresolved | 1.001 (0.976/1.026) | 67 % |
| 3–10 | b7 | 208 | 1.030 | −0.12 | 0.952 | unresolved | 1.000 (0.986/1.012) | 87 % |
| 3–10 | b9 | 290 | 1.027 | −0.12 | 0.948 | drifting | 1.000 (0.972/1.032) | 68 % |
| 10–20 | b6 | 44 | 1.18 | −1.9 | 0.23 | unresolved | 1.017 (0.849/1.268) | 31 % |
| 10–20 | b7 | refused (29 pairs) | | | | | 0.999 (n = 21) | |
| 10–20 | b9 | 105 | 0.951 | 1.00 | 0.806 | drifting | 1.000 (0.968/1.036) | 64 % |
| 20–40 | b6 | 567 | 0.982 | 0.51 | 0.708 | unresolved | 1.000 (0.983/1.016) | 86 % |
| 20–40 | b7 | 97 | 0.912 | 3.44 | 0.508 | unresolved | 1.002 (0.940/1.129) | 56 % |
| 20–40 | b9 | 1,481 | 0.984 | 0.48 | 0.930 | drifting | 1.000 (0.986/1.014) | 91 % |
| 40–60 | b6 | 197 | 1.048 | −2.30 | 0.864 | drifting | 1.000 (0.985/1.014) | 91 % |
| 40–60 | b7 | 270 | 0.999 | 0.14 | 0.862 | unresolved | 1.001 (0.983/1.033) | 78 % |
| 40–60 | b9 | 411 | 0.975 | 1.08 | 0.787 | unresolved | 1.000 (0.986/1.014) | 90 % |
| 60–90 | b6 | 33 | 0.910 | 5.72 | 0.902 | drifting | 1.002 (0.984/1.015) | 90 % |
| 60–90 | b7 | 259 | 1.034 | −2.79 | 0.740 | unresolved | 1.001 (0.985/1.017) | 87 % |
| 60–90 | b9 | refused (19 pairs) | | | | | 1.004 (n = 23) | |
| 90–134 | b6 | refused (10 pairs) | | | | | 0.986 (n = 8) | |
| 90–134 | b7 | 51 | **1.0085** | −0.94 | **0.981** | **stable** | 1.003 (0.986/1.025) | 77 % |
| 90–134 | b9 | 30 | 1.068 | −8.3 | 0.905 | unresolved | 1.007 (0.984/1.027) | 73 % |

The plateau windows agree (per-plateau ratios 0.998–1.004). Their within-plateau slopes of 0.76–1.09 read `unresolved` or `drifting`. That is D04.4's "drifting inside a single plateau is skew, not the sensor" case: over a 2–4 g/s span, the slope is set by pairing jitter.

### Conclusion for D04.4

- **There is no uniform scale error and no location-specific nonlinearity.** Band medians hold at 1.000 ± 0.007 wherever n ≥ 26, across 3–134 g/s. The only bands off by more than 1 % hold 35 or fewer pairs.
- **Usable range for ×0.01 g/s at ±2 %:** 3–134 g/s under the quasi-steady condition |d ln MAF/dt| ≤ 5 %/s, which is the limit of the data. The evidence is firmest at 20–90 g/s (≥ 2 blocks with hundreds of pairs, 86–91 % of individual pairs within ±2 %). **90–134 g/s rests on 51 + 30 pairs**, with medians of +0.3 % and +0.7 %.
- **Outside steady state, the DID and the PID disagree by acquisition timing**, not decode (mean ±5–8 % at >10 %/s). That is a constraint on pairing, not on the decode.
- Proposed `conditions` for 0x1209 (not written, per instructions): `steady_state: |dlnMAF/dt| <= 0.05/s; range_gps: [3, 134]; verified_1to1_pct: 2; thin_above_gps: 90`.

## Step C (D03.9): the injector mass balance

### How the constant is used today

- It appears in only one place, `data/dids.yaml` → 0x1208 `evidence[1].constant: 914943.0` ("counts per gram of fuel per injection"). No code in `src/` consumes it.
- Yesterday's scripts, `data/analyses/2026-09-17-drive/{arbitrate,phi_check}.py`, compute `phi_implied = 14.7·PW·(rpm/20)/(914943·MAF)` with the **DID** MAF.
- The 2026-08-07 fit assumed sequential injection: one injection per cylinder per 2 revolutions, so injections/s = rpm/120 · 6 = rpm/20. I used the same assumption.

### Construction

- PW (`did:ECM_INJECTOR_PW_B1`) is the reference side. Every partner is paired with `align_pairs`:
  - DID RPM: 150 ms
  - Mode 01 MAF, RPM, λ (PID 0x24), commanded φ (PID 0x44), load: 300 ms
  - FUEL_STATUS: 500 ms
- Derived quantities:
  - air per injection = MAF_obd·20/rpm_did
  - fuel per injection = PW/K
  - φ_implied = 14.7·fuel/air
  - φ_meas = 1/λ
- Exclusions:
  - **DFCO:** φ_cmd = 0, λ ≥ 1.9, or PW < 2000.
  - **Transients:** |d ln/dt| ≤ 5 %/s required on Mode 01 MAF, Mode 01 RPM, PW and λ, each measured over its own neighbours within ±0.6 s.
  - Any row missing a partner.
- Steady rows kept, split by regime:

| block | closed loop | commanded rich (φ_cmd > 1.05) | open loop at φ_cmd = 1 |
|---|---:|---:|---:|
| b6 | 625 | 2 | — |
| b7 | 447 | 50 | 24 |
| b9 | 1,689 | 19 | — |

### The register does not see enrichment or fuel cut

The quantity tested is PW counts per gram of air per intake event. The rows are the non-DFCO rows, gated only on pairing, grouped by commanded φ, plus the DFCO rows as a separate group.

| block | φ_cmd 0.99–1.01 | φ_cmd 1.15–1.30 | DFCO (λ = 2.0 / φ_cmd = 0) | steady CL CV |
|---|---:|---:|---:|---:|
| b6 | 63,938 (n = 1,526) | 64,626 (n = 19) | 64,023 (n = 138) | 1.6 % |
| b7 | 63,875 (n = 1,698) | 64,446 (n = 71) | 63,908 (n = 136) | 2.1 % |
| b9 | 63,808 (n = 2,858) | 64,309 (n = 64) | 63,932 (n = 268) | 1.0 % |

- A 20 % enrichment should move a final-injection quantity by about 20 %. This register moves 0.8–1.1 %.
- With the injectors cut, a final-injection quantity goes to about 0. This register stays at about 3,500–3,900 counts, exactly proportional to the air still flowing.
- In steady closed loop it does not correlate with measured φ (r = 0.25, −0.04 and 0.01), so it does not carry the closed-loop trim either.

The same pattern holds in matched air-charge cells (0.48–0.54 g air per injection), from `cell.py`:

| block | cell | regime | n | PW | φ_cmd | φ_meas | φ_implied |
|---|---|---|---:|---:|---:|---:|---:|
| b7 | 0.50–0.52 | CL | 38 | 32,443 | 1.000 | 0.999 | 1.028 |
| b7 | 0.50–0.52 | rich | 28 | 33,315 | 1.195 | 1.209 | 1.041 |
| b9 | 0.50–0.52 | CL | 6 | 32,230 | 1.003 | 0.997 | 1.019 |
| b9 | 0.50–0.52 | rich | 10 | 33,432 | 1.227 | 1.237 | 1.046 |

In the same cell, PW rises 2.7 % (b7) and 3.7 % (b9) while the measured fuel rises 21 % and 24 %.

The external instrument agrees, using UpRev logs on disk (not same-day, so this is supporting evidence only). UpRev's `INJ PULSE-B1` per gram of air rises +19 to +22 % in rich (AFR 11.9–12.7) against stoich (7-29 AM/PM logs). `B-FUEL SCHDL` per gram of air rises only +3 to +5 % (31.2 → 32.8 ms/g). So **0x1208 behaves like the base fuel schedule, not like the injection pulse.**

The earlier identifications cannot tell the two apart:
- The NT510 idle and blip signature is taken at φ ≈ 1, where BFS and the injection pulse differ only by a constant factor (UpRev INJ/BFS 0.67–0.79).
- The 2026-08-07 fit (r² 0.962, flat ±1.3 %, zero intercept) is what a register computed from MAF/rpm must produce against MAF-derived fuel in closed loop. It was a near-tautology, not a test.

A cross-day scale candidate is 1 count ≈ 30.9 ms/g ÷ 63,850 counts/g ≈ 4.8 × 10⁻⁴ ms of BFS. Confirming it needs a same-instant UpRev window (D03.10).

### PW models: fit on one block, validate on the others

The residual is φ_model/φ_meas − 1, in percent, as a median. Fits are weighted least squares of PW = K·m_fuel,true + c, where m_fuel,true = air·φ_meas/14.7, weighted to minimise the relative φ error.

| model | K | c (counts) | CL b6 | CL b7 | CL b9 | rich b6 (n = 2) | rich b7 (n = 50) | rich b9 (n = 19) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| current | 914,943 | 0 | +2.69 | +2.67 | +2.60 | −15.2 | −13.1 | −15.9 |
| gain, fit b6 | 939,388 | 0 | **+0.02** | −0.01 | −0.07 | −17.4 | −15.4 | −18.1 |
| gain + offset, fit b6 | 925,976 | 264 | +0.05 | +0.37 | 0.00 | −16.8 | −14.8 | −17.5 |
| gain, fit b7 (incl. rich) | 924,447 | 0 | +1.63 | +1.61 | +1.55 | −16.0 | −14.0 | −16.7 |
| gain + offset, fit b7 | 925,028 | −11 | +1.62 | +1.60 | +1.54 | −16.1 | −14.1 | −16.8 |

- Fitting on b7, which has the most rich samples, only pulls closed loop off: the least-squares answer splits the difference, because no single fuel function f(PW) can serve both regimes.
- The dead-time offset comes out at about 0 counts either way. 264 counts is about 0.1 ms, against 1 ms ≈ 2,500 counts, and b7 gives −11. Dead time is not the mechanism.

Residuals of the current constant, binned by fuel per injection (m_true, g). The pattern is identical under every model:

| fuel/inj g | b6 CL | b7 CL | b7 rich | b9 CL | b9 rich |
|---|---:|---:|---:|---:|---:|
| < 0.010 | +5.6 (3) | +1.8 (58) | | −0.2 (61) | |
| 0.010–0.015 | +3.3 (27) | +7.8 (4) | | +2.4 (94) | |
| 0.015–0.020 | +2.7 (318) | +3.9 (17) | | +2.7 (455) | |
| 0.020–0.025 | +2.7 (178) | +2.6 (66) | | +2.6 (794) | |
| 0.025–0.030 | +2.6 (60) | +2.7 (76) | | +2.9 (96) | |
| 0.030–0.035 | +2.4 (23) | +2.8 (221) | −0.3 (3) | +2.6 (189) | |
| 0.035–0.040 | +2.6 (16) | +2.0 (5) | −6.8 (16) | — | |
| 0.040–0.050 | | | −15.4 (31) | | −15.9 (19) |

(b6's 2 rich samples also fall in 0.040–0.050, at −15.2.)

- By load (PID 0x04), closed loop is +2.6 to +2.8 in every bin from 30 to 101 %, and rich is −13 to −16 at 85–101 %.
- The rich residual grows with the enrichment itself. In b7's 0.48–0.50 g air cell, φ_cmd 1.117 and measured φ 1.096 give φ_implied 1.040, a residual of about −5 %. At φ 1.2 the residual is about −15 %. That is the dose-response of a register that ignores enrichment. Fuel quantity is not the driver.
- The −4 to −6 % gain + offset residual at < 0.010 g in b7/b9 is idle. Low-flow pairing jitter dominates there, as the wide p10 (−16 %) shows.

### Closed-loop anchor

In closed loop, φ_implied with the current constant has a median of **1.0267 (b6), 1.0255 (b7) and 1.0252 (b9)**, p10/p90 about 1.013/1.039, flat from 0.010 to 0.037 g. Yesterday's 1.026 came from the DID MAF; the Mode 01 MAF gives the same, which fits Step B. In closed loop the constant should be 939,400 counts/g at φ ≡ 1 (b6 fit; b7/b9 validate at −0.01 %/−0.07 %). Two likely reasons it was 2.6 % off on 2026-08-07: the old 1 s grid fit, which averaged bins, or a trim state that differed that day.

## Resolved vs unresolved

**Resolved**
- Time alignment and decode are proven again by the RPM check.
- `ECM_MAF_GPS` ×0.01 g/s is 1:1 with PID 0x10 in quasi-steady operation from 3 to 134 g/s. The "5 % low at high flow" was acquisition skew in transients.
- The 15 % WOT mass-balance gap is **not** a PW-constant or dead-time error, and a re-fit cannot fix it. `0x1208` does not carry enrichment, fuel cut or closed-loop trim. It tracks air charge at about 63,850 counts per gram of air per intake event.

**Unresolved**
- Which DID, if any, carries the *final* injection pulse. The fuel-mass route stays closed until one is found.
- Whether 0x1208 is exactly UpRev's BFS. That is a cross-day correlation; it needs one same-instant UpRev window.
- The +1 % residual of 0x1208 per gram of air in rich cells. It could be real, or an rpm effect: every rich sample is at 3,950–4,850 rpm, while closed loop at ≥ 0.45 g air never exceeds about 2,900 rpm.
- The ±2 % MAF band above 90 g/s rests on 51 + 30 pairs.

## Candidate calibration (a candidate, not a binding; nothing written to `dids.yaml`)

- **0x1208 as air charge:** counts ≈ 63,850 × (MAF_gps·120 / (rpm·6)).
  - Validated domain: b6/b7/b9; 700–4,850 rpm; 0.05–0.55 g air per intake event; closed loop, commanded rich to φ 1.23, and DFCO.
  - Accuracy: ±1 % median per regime and 1–2 % CV per sample.
  - It must **refuse** to be read as fuel mass, and must **not** be used with a fuel-per-count constant outside φ ≡ 1.
- **If a fuel figure is wanted from it:** fuel ≈ (counts/63,850) × φ/14.7, taking φ from PID 0x44 (commanded) or 1/λ (measured). In other words, fuel comes from air × φ, and the register adds nothing beyond MAF/rpm.
- **The 914,943 constant** should be demoted to "closed-loop φ = 1 identity only, 2.6 % low". Its provenance text in `dids.yaml` (notes: "effective (dead-time-excluded) injection quantity") is contradicted by this analysis. **Operator's call to edit.**

## What today's drive (or the desk) would need to close it

1. **Find the real injection-pulse DID (desk first).** Screen the existing 138-DID `did_log` surveys (blocks 3b and 5 contain a pull and DFCO coasts) for a DID whose ratio to 0x1208:
   - stays flat in cruise,
   - rises about 15–20 % during the pull,
   - and collapses to a floor in DFCO.

   If none passes, drive one `did_log` full-survey window containing ≥ 3 held enrichment plateaus (WOT ≥ 3 s at 3,500–4,500 rpm), 2 in-gear coasts (DFCO) and steady cruise.
2. **Separate rpm from enrichment for the +1 % residual.** Record closed-loop high air charge (0.45–0.55 g per intake event, about 70–100 g/s) at **3,500–4,500 rpm**, for example part throttle up a sustained climb in a lower gear. Match it against WOT at the same rpm. This cell currently has **zero closed-loop samples in all three blocks**.
3. **Extend the MAF DID band.** Record ≥ 20 s holds at **60–90 g/s** (b9 has 23 steady pairs; there are no plateaus above 67 g/s anywhere) and more quasi-steady time at **> 90 g/s** (≤ 51 pairs per block).
4. **Confirm the 0x1208 ↔ BFS scale.** Run one UpRev window alongside a hybrid capture (D03.10's window; deferred today by the CAN-only constraint).
