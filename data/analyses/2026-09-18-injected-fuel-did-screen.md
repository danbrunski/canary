# Screen for the injected-fuel register in the 2026-09-17 did_log surveys (2026-09-18)

Follow-up to `2026-09-18-maf-did-and-injector-mass-balance.md`, which showed that 0x1208 tracks air charge (about 63,850 counts per gram of air per intake event) and carries no enrichment, fuel cut or trim.
Data: `~/canary-data/did_log/did_log-2026-09-17-{roadload-3, roadload-2, roadload-1, paced47}.jsonl`, in which the daemon was passive and Mode 22 was read one DID per request.
Results: `2026-09-18-injected-fuel-did-screen.json`. Scripts: scratchpad `didlog_load.py`, `screen_fuel.py`, `inspect1206.py`, `replicate.py`.

**Everything here is within-instrument, same-pass evidence. The findings are CANDIDATES, not bindings.**

## Method

- **Pass boundaries:** a pass starts at each 0x1100 read, and all values come from that same pass.
  - Candidates are ratioed only to 0x1208 or 0x1209 from the same pass.
  - 0x1206 is read 16 ms from 0x1208 in the sequential passes (43 ms in paced47; 4 ms in roadload-1).
  - No values are aggregated across passes.
- **Regimes are set from same-pass DIDs:**
  - **Cruise:** 0x1134 = 64, A/F V (0x1225 × 0.005) between 2.0 and 2.3, MAF (0x1209 × 0.01) > 8 g/s, and MAF within ±15 % of both neighbouring passes.
  - **Idle:** the cruise conditions with rpm < 900.
  - **Rich:** 0x1134 ≥ 68, A/F V < 2.0, and the same MAF stability.
  - **DFCO:** both A/F sensors at or above 4.9 V (the lean rail). This occurs at 1,650–3,500 rpm, so the engine is running.
- **Candidates:** every DID, decoded as u8, u16, hi/lo byte, u32 and the 16-bit halves of each 4-byte DID, plus adjacent 1-byte DIDs taken as u16 pairs. Widths come from the did_log header.
- **Criteria:**
  - ratio to 0x1208 flat in cruise (robust CV),
  - rich/cruise ratio close to 0x1134/64 (1.156 in block 5),
  - DFCO ratio falls to a floor or zero.

  The ratio to 0x1209 (fuel per second) was screened the same way.

## Result: one bank pair stands out, 0x1206 (B1?) and 0x1207 (B2?), u16

| file (block) | DID | n cruise / rich / idle / DFCO | cruise ratio (CV) | rich ÷ cruise (0x1134/64) | idle ÷ cruise | DFCO raw (ratio ÷ cruise) |
|---|---|---|---|---|---|---|
| roadload-3 (5) | 0x1206 | 496 / 8 / 122 / 119 | 0.0317 (1.6 %) | **1.170** (1.156) | 1.18 | **55** (0.45) |
| roadload-3 (5) | 0x1207 | same | 0.0319 (2.6 %) | **1.166** (1.156) | 1.16 | **55** (0.45) |
| paced47 (8) | 0x1206 | 17 / 1 / 0 / 154 | 0.0321 (0.6 %) | 1.13 (1.156, n = 1) | — | 55 (0.52) |
| paced47 (8) | 0x1207 | same | 0.0325 (1.8 %) | 1.15 (n = 1) | — | 55 (0.51) |
| roadload-1 (3a) | 0x1206 | 55 / 0 / 55 / 64 | 0.0312 (3.1 %) | — | 1.20 | 56 (0.46) |
| roadload-2 (3b) | 0x1206 | 3 / 0 / 71 / 21 | (n = 3) | — | 1.16 | 55 (0.43) |

The eight steady rich passes in block 5 (TPS 823–827, 2,440–3,800 rpm) show the enrichment directly. 0x1208 is flat at 35,100–36,200 while 0x1206 reads 1,291–1,345 and 0x1207 reads 1,295–1,351. The ratio to 0x1208 is 0.037–0.038, against 0.032 in cruise, so the register carries the enrichment that 0x1208 does not.

In DFCO, 0x1208 still reads 3,540–4,190, proportional to the air still flowing. 0x1206/0x1207 drop to a **constant 54–57 counts** in every file, independent of air. That is a floor, not a scaled air term.

The two DIDs agree with each other within about 1–3 % per pass, which fits a bank pair.

**Physical sanity, if 1 count = 0.01 ms:**
- Idle reads 2.2–2.8 ms, against NT510 "Inj pulse-b1" at 2.14 ms and UpRev `INJ PULSE-B1` at 2.2 ms.
- WOT reads about 13 ms.
- The DFCO floor is about 0.55 ms.
- The implied injector flow in cruise at φ = 1 is **256–278 cc/min**, which is right for a VQ40DE.

The scale is a hypothesis: it is not fitted against any external instrument.

**No other DID or decode shows the signature.** The next best rows are 0x1206/0x1207's own hi bytes (the same register) and 0x1209's hi byte (air). Nothing else is simultaneously flat in cruise, up about 15 % in rich and at a floor in DFCO. My literal pass rule required a DFCO ratio ≤ 0.35 and therefore passed nothing; the DFCO behaviour of 0x1206/0x1207 is a fixed 55-count floor rather than zero, which the task allowed.

## Unresolved

- **Rich evidence is thin:** 8 steady rich passes, all from one pull in block 5, plus 1 in block 8. Blocks 3a and 3b contain no rich passes.
- **Idle reads 16–21 % above the cruise ratio.** That could be a dead-time/additive term, idle enrichment, or an idle-specific 0x1208 effect. The fitted cruise intercept is about 0 (−9 to −36 counts in block 5 and −3 in block 3a), so the data does not settle it.
- **Bank assignment** (0x1206 = B1) is by numbering only.
- **This is not yet a same-instant check against PID 0x44 or λ.** The decisive test is to add 0x1206/0x1207 to `tuning-hybrid-research` beside PID 0x44/0x24 and 0x1208. That gives φ_implied = 14.7 · fuel(0x1206) / air(0x1208 or MAF), compared 1:1 with 1/λ over closed loop, rich and DFCO, through `compare_channels`. Cataloguing the DIDs is the operator's call; `dids.yaml` has not been edited.
