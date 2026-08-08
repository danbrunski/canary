# STFT bank balance: what "normal" looks like — 2026-08-08

## Question

Session `2026-08-08T205935` showed bank 1 short-term fuel trim running several
points more negative than bank 2 (bank 1 richer, ECU pulling fuel from it) on
every one of 26 closed-loop samples. Is that new?

**No.** It is present in every session examined, always in the same direction,
and its magnitude is a function of coolant temperature. The 2026-08-08 reading
is normal for the temperature it was taken at.

## Method

`derived:STFT_BANK_BALANCE` from `src.analysis.derived.compute_anomaly_signals`
— the **30 s time-smoothed** difference, not the instantaneous one. This
matters: the raw per-sample difference is dominated by OBD round-robin skew,
because `SHORT_FUEL_TRIM_1` and `SHORT_FUEL_TRIM_2` are polled at different
points in the rotation and catch different phases of the same swing. That is
why `STFT_BANK_BALANCE_SMOOTHING_SECONDS` exists. The first pass at this
question used the raw difference and should not have.

Filter: closed loop (`FUEL_STATUS == 2`), 600–1300 rpm, speed < 3 km/h,
1 Hz grid. Sessions ordered by `src.control.sessions.session_started_at`,
never by directory name.

## Result

| session | band | n | smoothed balance | coolant |
|---|---|---:|---:|---:|
| `2026-08-07T155139` | cold | 15 | −3.12 | 62 °C |
| **`2026-08-08T205935`** | **cold** | **55** | **−3.91** | **55 °C** |
| `2026-08-06T151547` | warm | 10 | −0.44 | 75 °C |
| `2026-08-06T162657` | warm | 48 | −0.39 | 91 °C |
| `2026-08-07T155139` | warm | 244 | −2.34 | 89 °C |
| `2026-08-07T161543` | warm | 123 | −1.30 | 91 °C |
| `2026-08-07T164617` | warm | 272 | −0.78 | 91 °C |
| `2026-08-07T225451` | warm | 15 | −1.06 | 91 °C |

Bank 1 is richer in every session — not one positive reading. Roughly **−3 to
−4 points cold (40–62 °C)** and **−0.4 to −2.3 warm (70–100 °C)**.

## The trap this nearly fell into

`2026-08-08T205935` peaked at 59 °C — it never reached operating temperature,
so it has **no warm-band data at all**. A first pass filtering on ≥70 °C
returned `n=0` for it, which is what exposed the error: comparing its cold
−3.91 against the warm baseline would have manufactured a fault out of a
coolant difference. Condition-match before concluding anything from this
metric.

## What this is not

Not a noise floor. `data/nissan-xterra-2008/noise_floor.yaml` holds
same-tune null-run spreads, where a non-zero `mean` is itself a finding that
the runs were not equivalent. These numbers are a *level*, deliberately
non-zero, measured across different sessions and tunes — recording them as a
floor would make `sd_for()` hand `compare_sessions` a number that means
something else.

Not an accepted baseline either. `accepted_baselines.yaml` suppresses alerting
and is explicitly an operator declaration requiring `declared_by` and a
`review_by` date; nothing here judges this asymmetry acceptable, only normal
for this engine as currently observed.

## Limits

Only two cold-band observations exist and one of them is the session that
prompted the question. The warm band itself spans 0.4 to 2.3 across six
sessions, so run-to-run spread is real and no null-run floor has been measured
for this metric — a same-conditions repeat pair would be needed before calling
any single-session movement significant.

Misfire counts were checked at the same time and are also unremarkable:
totals of 2–44 per session across the same set, with 1–3 per cylinder routine.
`2026-08-08T205935`'s total of 2 (CYL4=1, CYL5=1) is the lowest observed.
