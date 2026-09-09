# Pending-hardware measurements — Pi session, 2026-09-09

The vehicle measurements `docs/roadmap/tasks/PENDING-HARDWARE.md` was holding
open. Raw evidence: `2026-09-09-pending-hardware/` (status snapshots,
benchmark reports, H5 harness output).

**Session frame (H0).** Pi reachable at `192.168.137.2`, daemon and
`canary-mcp` active, Raspberry Pi 5 Model B Rev 1.1, kernel
`6.18.29+rpt-rpi-2712`, Python 3.13.5, git `09ba604` (same commit as the
workstation, clean tree both sides). Vehicle: 2008 Xterra, profile
`nissan-xterra-2008`. Every number below carries the phase it was taken in.

## H0 — vehicle state read before every interpretation ✅

Read first, and re-read before each item. The session opened at
`no_data` and that reading was itself the finding: `can0` was UP at 500 kbit/s
but `ERROR-PASSIVE` with **0 RX / 0 TX packets**, and the collector was failing
every 60 s with `Failed to transmit: No buffer space available [105]`. The
OBD harness was not plugged in. Once plugged in and the engine started, the
same fields read `engine_running`, and `can0` went `ERROR-ACTIVE` with 44,244
RX packets and 0 dropped.

This is exactly the case H0 exists for: at `no_data` the silent detectors and
the timing-out probes were correct behaviour, not a bug.

| phase | state | rpm | coolant | obd_fresh | can_fresh |
|---|---|---|---:|---:|---:|
| harness unplugged | `no_data` | – | – | – | – |
| cold start | `engine_running` | 1200 | 39 °C | 1.12 s | 1.06 s |
| warming | `engine_running` | 1012 | 54 °C | 2.32 s | 2.21 s |
| warm idle (H3/H4/H6) | `engine_running` | 737–762 | 75–91 °C | ≤ 2.5 s | ≤ 2.3 s |

## H2 (C05.11) — live-pipeline benchmark on the Pi ✅

Same commit, same `--seed 0`, same `canary.example.yaml` and
`data/bindings.yaml` (sha256 verified identical on both hosts), 327 feature
columns both sides.

| host | profile | tick median | tick p95 | 1/p95(total) |
|---|---|---:|---:|---:|
| Pi 5 | smoke | 122.7 ms | 149.4 ms | 6.7 Hz |
| Pi 5 | steady | 127.6 ms | 165.8 ms | 6.0 Hz |
| AsusLaptop (WSL2) | smoke | 159.4 / 151.1 ms | 186.4 / 176.0 ms | 5.4 Hz |

Both Pi runs `valid: true`, `completed == measured` ticks, `empty_ticks == 0`,
600 feature rows for steady, no segmenter fallback, no quality-gate rejects.

**The stored workstation baseline is not a valid comparison and was not used
as one.** `2026-09-06-benchmark-live-pipeline-baseline.md` records
tick median 40.1 ms; this workstation measures 154 ms *at that same commit*
(`83ede5b`, checked in a throwaway worktree, 312 columns). The baseline's host
table names `CorsairDesktop` / kernel 6.6.87.2; this machine is `AsusLaptop` /
kernel 5.15.167.4. Different hardware, not a regression and not a Pi/workstation
ratio. A host-neutral probe puts the Pi at 4.2× the laptop's BLAS time
(33.3 vs 7.9 ms) but level on pure-Python (537 vs 557 ms), which is why a
pandas-shaped feature stage lands close.

**Power/thermal (what H2 asks to identify).** `get_throttled` was `0x0` at
session start and `0x50000` after the steady run — under-voltage *and*
throttling occurred. `dmesg` pins it to one 2-second event
(`Undervoltage detected!` at uptime 928 s, `Voltage normalised` at 930 s),
during the steady run, on vehicle power with the engine running. ARM clock
back at 2.4 GHz, core 0.875 V, 52 → 59 °C across the run.

## H4 (C06.12) — discovery counters and frame continuity ✅

Phase `engine_running`, warm idle.

Discovery at rest and after `POST /control/obd_discovery/reset` both complete
inside budget: **mode01 3 probes → 41 PIDs**, **mode06 6 probes → 23 MIDs**
(≤ 7 and ≤ 8 respectively), 9 requests / 0.49 s total, sole responder `0x7E8`,
`pending` empty by t=10 s, no `last_result` other than `answered`.

**Continuity: NOT CLEAN.** Over a bracketed 60 s window at `engine_running`:

| counter | delta |
|---|---:|
| kernel `can0` rx_packets | 110,327 |
| daemon `frames_seen` | 103,051 |
| **unaccounted** | **7,276 (6.6 %)** |
| kernel rx_over_errors / rx_dropped / rx_missed | 0 / 0 / 0 |
| daemon parse_errors | 0 |

An independent reader settles it: over the same bus, kernel 49,062 vs
`candump` 49,262 (equal within timing skew) vs daemon 46,186 — the daemon is
~6 % short while a plain reader is not. The daemon's bracket was slightly
*wider* than the kernel's, so 6 % is a floor. This is precisely what step 7
exists to catch: no received count alone proves nothing was lost upstream,
and here the kernel's counters are clean while frames are still missing.

**Constraint named.** `_drain_frames_until` only calls `recv()` while the loop
is between other work; frames arriving during request/response handling,
scheduling and metrics queue in the socket buffer, and a SocketCAN receive
overflow is invisible to `ip -s link`. `/proc/net/can/stats` shows a current
match ratio of 100 %, so frames reach the socket and are lost after that.
Counting was verified before this was believed: `_drain_frames_until` passes
the raw `on_frame` (so `_FrameTap` does not fire) while the client path passes
`tap` — each frame is counted exactly once, no double count.

This is **pre-existing**; the metric added in this session only made it
visible. It is not fixed here — the fix is a dedicated always-draining receive
path, not a bigger buffer, and that is an architecture change worth its own
task.

### Fix landed: the counters now reach `/status`

`frames_seen`, `discovery_frames_seen` and `parse_errors` were tracked in
`CanObdLoopStats` from C06 on but never serialized, so steps 3 and 7 could not
be executed at all. They are now `/status.obd_scheduler.receive`.
`docs/diagnostic-discovery.md` also pointed at `can_receive` for this, which
is a *broadcast-collector* sink that `_default_collectors` never registers on
the active canable/socketcan path — corrected to `obd_scheduler.receive`.

## H3 (C04.12) — hybrid 4 Hz against the real ECM ✅ (claim denied)

Phase `engine_running`, warm idle (coolant 91 °C), profile
`tuning-hybrid-research`, capture
`_capture-tuning-hybrid-research-2026-09-09T190901`, 300 s observed.

**Infeasible by plan, before any timing was measured.** On arming, the
one-time warning fired exactly as step 2 anticipated: 11 Mode 01 + 9 DID +
2 session requests at 90 req/s plus 0.05 s grace need **0.294 s** against a
0.250 s cycle — short by 0.044 s, `cycle_feasible=false`. Per the procedure
the budget was **not** raised to make it go away.

Measured over 300 s, snapshot every 30 s:

| quantity | measured | required for the 4 Hz claim |
|---|---:|---|
| `cycle_duration_s` median | **0.510 s** (≈1.96 Hz) | ≈ 0.25 s |
| `cycle_duration_s` p95 / max | 0.76 / 2.09 s | – |
| cycles started / missed | 590 / **760** | missed ≈ 0 |
| mode01_shortfall | 1,966 | – |
| did_shortfall | 0 | – |
| cross-mode separation median | 0.43 s | – |

Persisted rows agree (`capture_quality.json`, verdict `fail`), and they are
what the claim is judged on:

| channel | achieved | coverage | max gap |
|---|---:|---:|---:|
| RPM (Mode 01) | 3.307 Hz | 0.599 | 45.1 s |
| ECM_RPM (DID) | 1.953 Hz | 0.634 | 26.9 s |
| ECM_MAF_GPS (DID) | 1.973 Hz | 0.633 | 59.9 s |

Against the 2026-08-07 drive (Mode 01 1.46 Hz, DIDs 0.90 Hz, 0.341 s median
separation) this is **2.26× on Mode 01 and 2.17× on DIDs** — real improvement.
It is still not 4 Hz. `tuning-hybrid-research` **stays experimental**; the
achieved figure to quote is ~1.95 Hz on the cycle with ~0.6 coverage.

## H6 (C10.12) — budget ladder against the real ECM ⚠️ idle only

Phase `engine_running`, warm idle, `default` profile, 45 s per rung.

| rung | requests sent (45 s) | effective | response rate | timeouts |
|---:|---:|---:|---:|---:|
| 8 req/s | 365 | 8.1/s | 0.976 | 3 |
| 16 req/s | 352 | 7.8/s | 0.976 | 4 |
| 32 req/s | 354 | 7.9/s | 0.976 | 5 |

**Throughput is flat across a 4× budget increase.** The ECM answered 97.6 % of
requests with 3–5 timeouts, so the ECM's response time is *not* where the
floor sits — the daemon cannot issue more than ~8 req/s. That matches the
archived synthetic finding that the bus is never the constraint, and locates
the real one on hardware (see H5).

Caveat: `achieved_hz_60s` is a trailing 60 s window and the rungs are 45 s, so
the per-rung RPM figures (0.27 / 0.67 / 0.97 Hz) are smeared across rungs and
are not quoted as rung results. `requests_sent` deltas are per-rung and clean.

Left open: the ladder under the drive protocol step 5 actually asks for.

## H5 (C08.12) — Pi CPU, memory, event-loop delay, latency, losses ✅

Two halves: the isolated harness the item prescribes, and the live daemon.

### Isolated harness (`--inference-tick-s`, deployed defaults untouched)

| scenario | tick | feature p95 | lateness p95 | lateness max | missed | heartbeat p99 | storage dropped |
|---|---|---:|---:|---:|---:|---:|---:|
| fuel-map | 1.0 s | 143.2 ms | 1.06 ms | 1.27 ms | **0** | 105.8 ms | 0 |
| tuning-did | 1.0 s | 190.5 ms | 0.89 ms | 1.23 ms | **0** | 145.1 ms | 0 |
| fuel-map | 0.5 s | 148.2 ms | 28.85 ms | 273.2 ms | **6** | 129.9 ms | 0 |
| tuning-did | 0.5 s | 209.1 ms | 1.15 ms | 10.66 ms | **12** | 153.1 ms | 0 |

`limiting_stage` flips from `inference_feature_stage_on_loop` at 1 Hz to
`inference_loop` at 2 Hz. Against the archived workstation half (feature stage
flat at 51 / 72 ms, zero missed at both cadences, heartbeat p99 35 → 58 ms):
the Pi's feature stage is **2.6–2.8× slower**, it still holds 1 Hz with zero
missed deadlines and zero record loss, and **2 Hz is where it breaks**. That
is the "what the truck adds" answer — 2 Hz is not a supported cadence here.

### Live daemon, same phase — the real finding

The deployed daemon is a different and much worse picture than the harness,
because it runs the full model set against a real 3,000 frame/s bus:

| stage | median | p95 | max |
|---|---:|---:|---:|
| feature | 331.9 ms | 710.2 ms | 22,157 ms |
| sequence (LSTM) | 378.2 ms | 955.8 ms | 27,752 ms |
| isolation_forest | 92.0 ms | 198.9 ms | 2,317 ms |
| **total** | **863.1 ms** | **2,032 ms** | **51,110 ms** |
| loop_occupancy | 340.8 ms | 858.9 ms | 25,523 ms |

At a 1.0 s tick, `total` median 863 ms is **86 % of the period** and p95 is
over 2× it. Consequences measured in the same window:

- **306 of 633 inference ticks missed (48 %)** — `compute_busy` 135,
  `overrun` 177.
- `/status` HTTP latency 0.7–5.1 s; two control requests timed out outright.
- storage `max_write_ms` **18,475 ms**, queue high-water **7,778 / 10,000**.
- `load_1m` 1.59 on 4 cores, memory 13 %, 63.4 °C, under-voltage and
  throttling flagged since boot.

`lstm_autoencoder.enabled: true` in `/etc/canary/canary.yaml`. The sequence
detector is the single largest stage (378 ms median, above the feature stage
and 4× the IF).

**This is what broke the other captures in this session.** A `fuel-map`
capture running at the time scored verdict `fail` with **0 of 468 tuples
complete** and every channel showing a ~100 s gap — coverage 0.03–0.34 over
328 s — while its *in-sample* rate was a healthy 3.957 Hz against a 4.0 target.
The daemon is not sampling slowly; it stalls completely for ~100 s at a time.
H3's 0.6 coverage and 27–60 s gaps are the same cause.

## Not measured — needs a drive (operator deferred, 2026-09-09)

Untouched, with the reason recorded rather than guessed at:

- **H1 (C10.10)** sustained 20–30 min captures per supported profile.
- **H7 (C09.12)** normal-monitoring coverage. Partial evidence exists: probe
  overhead is 9 requests / 0.49 s, and batching from the failed `fuel-map`
  capture was `mean_pids_per_batch` 2.02 with 65 failures, 8 desyncs and 3
  size narrowings. No default budget was changed.
- **H8 (C09.14)** fast-dynamics vs slow-context classification.
- **H9 (C09.16)** capacity notes; depends on H1. No default was raised.

Nothing in this session licenses raising a deployed rate default. The evidence
points the other way: the constraint is the inference loop, not the bus.
