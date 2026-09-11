# D02.6 — anomaly-event rates by detector, and a replay that OOM-killed the daemon

Bench session, 2026-09-11. Replay of the 2026-09-10 `default`-profile drive
(`2026-09-10T142711`) through the live pipeline at 1x, isolated outputs under
`_replay-2026-09-11T164903`. Daemon `3c2ae25` on the Pi, engine absent
(`vehicle_state: no_data`), so nothing but the replay was feeding the pipeline.

## The rate, and what produces it

**104 events over 1,910 s = 3.27 events/min.** The live drive recorded
**3.1/min** over its capture window, so **the replay reproduces the live rate**
— which is the first thing worth knowing, because it means replay is a valid
instrument for this question and the live number was not an artifact of
driving conditions.

| detector | events | rate | share |
|---|---:|---:|---:|
| `lstm_autoencoder` | 62 | 1.95 /min | 60 % |
| `behavior` | 30 | 0.94 /min | 29 % |
| `fused` | 8 | 0.25 /min | 8 % |
| `misfire` | 4 | 0.13 /min | 4 % |
| `isolation_forest` | **0** | — | — |

**The sequence detector dominates.** That is D02.6's stated decision
criterion, so it is now D01.7's input. The Isolation Forest contributed
nothing, as expected — it is structurally blind against this artifact and has
been since two bindings were corrected out from under it.

## Detector against operating mode

| detector | city | highway | idle | idle_transient | warmup | none |
|---|---:|---:|---:|---:|---:|---:|
| `lstm_autoencoder` | 32 | 13 | 7 | 10 | 0 | 0 |
| `behavior` | 4 | 0 | 9 | 16 | 1 | 0 |
| `fused` | 0 | 0 | 1 | 7 | 0 | 0 |
| `misfire` | 0 | 0 | 0 | 0 | 0 | 4 |

The two main detectors fire in **complementary regimes**: the sequence model
almost entirely while driving (45 of 62 in `city`/`highway`), the behavior
overlay almost entirely while stationary or transitional (25 of 30 in
`idle`/`idle_transient`). That is not the signature of one detector flooding
across the board.

**What this does not establish.** Whether 3.27/min is *too high* needs a
known-good session for comparison, and the Pi's retention has swept every
session the models were trained on — none are on the box. So the honest
verdict is: the rate is characterised and attributed, not yet judged. Getting
the baseline means pushing one training session back from the laptop backup.

Per-mode rates normalised by time-in-mode were also not obtainable: the replay
output does not persist `operating_mode` as a channel, so the raw counts above
cannot be turned into events-per-minute-in-mode. Counting events per mode
without the time denominator is the aggregate comparison this repo has a rule
against, so the counts are left as counts.

## The defect this turned up: replay grows without bound

**The daemon was OOM-killed 32 minutes into the replay.** Kernel:

```
Out of memory: Killed process 1217 (python)
total-vm:15756560kB anon-rss:7744720kB
```

**7.7 GB resident on an 8 GB Pi.** systemd restarted it cleanly (`NRestarts:
1`, models reloaded from `prior_version: null`, healthy since at 469 MB), so
nothing was lost but the tail of the replay.

Two facts bound the problem:

| | rows | chunks | span |
|---|---:|---:|---:|
| source session | 4,544,560 | 511 | 5,889 s |
| replay output before the kill | 5,860,000 | 586 | 1,910 s |

The replay wrote **more rows than the entire source session contains** while
covering **32 %** of its span. Something is amplifying rows, and memory grows
with it. The mechanism is not established here and should not be guessed at.

This is **pre-existing, not a regression from D01.9** — that change is
committed on the laptop but was never deployed; the Pi ran `3c2ae25`
throughout.

It also explains a hazard nobody had written down: `start_replay` is offered
as the safe way to evaluate detectors without the truck, and on this hardware
a long replay takes the daemon down. The 25-minute drive window completed
before the kill, so the numbers above stand.

## What closed and what did not

Closes **D02.6**: the rate is measured, attributed by detector and mode, and
the sequence detector's dominance is established as D01.7's input.

Opens two things: the replay memory defect above, and the known-good baseline
comparison, which needs a training session pushed back to the Pi.
