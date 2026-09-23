# D05 — Drive findings, 2026-09-23 afternoon

A separate file on purpose: `D03-open-work.md` was mid-edit by another session
(D03.29) when these were found. Same conventions as D03: `[x]` only with the
measurement in hand and the session named. Fold into D03 when it is free.
Evidence: [drive log, "Drive 2"](../../../data/analyses/2026-09-23-drive-log.md).

- [ ] **D05.1 Six of eight `tuning` DIDs were silent for the first 37.5 s of a
  capture.** `_capture-tuning-2026-09-23T164007` started at 16:40:07.5Z with the
  engine warm and running on `default`. First stored row, seconds after start:

  | channel | first row |
  |---|---|
  | DID 0x1206 (`ECM_INJECTOR_PULSE_1`), DID 0x1209 (`ECM_MAF_GPS`), all four RAM channels | 0.04 |
  | DID 0x1207, 0x1208, 0x1219, 0x1225 | 37.53 |
  | DID 0x120F, 0x1226 | 38.03 |

  For those 37.5 s the two live DIDs ran at 2 Hz each (77 rows apiece), so the DID
  sweep ran but with two members only. The journal has no DID, window or session
  line in that span. The capture verdict reports it as `missing_initial_s`
  ≈ 37.6 on each of the six, and it is why their coverage is ~0.91 against ~0.96
  on Mode 01.
  - **Not the same as the fixed defect.** `cdd355e` removed a 27.6 s wait-out of
    the old profile's window at a profile change, and was verified at 0.16 s on
    2026-09-22 (KOEO). The morning `tuning` start today (14:29:26Z) had every DID
    within 0.7 s. And the two DIDs that did answer are not simply `default`'s
    members: 0x1209 is in `tuning` only.
  - [ ] Reproduce at a desk against a scripted ECU: switch `default` → `tuning`
    at several points in the default DID cycle, and assert every profile DID's
    first request goes out within one `did_cycle_seconds` (0.5 s).
  - [ ] Find what admits only two members for ~37 s. Candidates to check, not
    conclusions: miss back-off carried over from `default` (six DIDs benched at
    once would fit D03.18's "benched 30–48 s"), rate tiering, or window budget.
  - [ ] Add a counter or a log line when a profile DID is withheld at capture start,
    so the next occurrence says why. An empty channel must say which kind of empty.
  - [ ] Verify on the truck: three `tuning` starts from a running `default`, every
    DID's first row within 1 s.

- [ ] **D05.2 The trip-end digest summarised the wrong session.** Handed to the
  D03.29 owner on 2026-09-23, who folded it into D03.29 as a sub-item; tracked
  there, listed here only so the finding is not lost. The GPS trip end fired at
  16:57:33Z, 300 s after the last movement, and wrote `digest.json` for
  `2026-09-23T165316`. That is the 257 s parked `default` session the writer
  rotated to when the capture stopped. The drive itself, in
  `_capture-tuning-2026-09-23T164007`, was never digested.
