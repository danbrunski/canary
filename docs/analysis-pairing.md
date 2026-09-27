# Pairing observations for comparison (roadmap C01)

How `src.analysis.compare.compare_channels` decides that two observations
were taken "at the same instant", why the previous answer was wrong, and what
the current one can and cannot claim.

## The defect

Until 2026-09 `align_pairs()` read both channels on their *stored* stamp,
resampled each side onto a grid (default one second) with `.mean()`, and
inner-joined the bins. That is an aggregate comparison wearing a pairing's
clothes — the same class of error as every retracted finding in AGENTS.md.

Counterexample (`tests/test_analysis_compare.py::_counterexample`): two
samples per second for 60 s; A = `1000 + s + 50` then `1000 + s - 50` inside
each second, B the reverse. Every same-instant residual is exactly 100.

| path | n_pairs | r² | slope | offset | verdict |
|---|---|---|---|---|---|
| before (1 s mean bins) | 60 | 1.00000 | 1.000 | 1.8e-13 | stable |
| after (acquisition pairing, any tolerance) | 120 | 0.617 | -0.786 | 1838 | stable* |

\* The scale verdict is still "stable" because the *wrong* slope is
consistent across sub-windows; the 1:1 claim fails on slope and r², and
`residual_spread` (32.7 in B's units) says how far apart the sides are.
`binned_summary()` reproduces the old bin means (`a_mean == b_mean` in all 60
bins) and is documented as a picture, never evidence.

## The pairing contract (`src/signals/pairing.py`)

`PairingPolicy` is a frozen dataclass and is **required** at every
comparison call site — there is no default tolerance.

| field | meaning |
|---|---|
| `max_separation: pd.Timedelta` | largest acquisition-time gap two observations may have and still be one pair; inclusive |
| `reference: "a" / "b" / "auto"` | whose samples are walked; `auto` = the sparser side (`a` on a tie) |
| `tie_break: "earlier" / "later"` | which candidate wins when one before and one after the reference sample are exactly equidistant; equal-time duplicates always go lowest-index first |

`match_ordered(times_a, times_b, policy)`: sort both sides (stable), walk the
reference side in acquisition order, and give each reference sample its
nearest *not yet used* partner within tolerance. "Nearest unused" is answered
by two union-find structures (next free slot at/after, previous free slot
at/before the insertion point), so consuming a sample never forces a rescan.
Complexity is `O((n+m) log(n+m))` for the sorts plus a near-linear sweep. The
result is index pairs into the inputs, per-pair `|dt|`, and unmatched counts
per side. No sample is reused; no value is averaged, forward-filled or
interpolated; `n_pairs` is the number of accepted pairs and nothing else.

`align_pairs()` returns an `AlignedPairs` whose `pairs` frame carries
`timestamp_a/b`, `a/b` values, `index_a/b` (row labels of the
`read_channels(..., acquired_time=True)` long frame) and `separation_s`, so
every pair is traceable to two rows.
`test_every_reported_pair_is_two_identifiable_observations_within_tolerance`
asserts exactly that (C01.13).

`ChannelComparison` now carries `policy`, `reference`, `timestamp_basis_a/b`,
`unmatched_a/b`, `separation_median/p95/max_s`, `residual_bias` and
`residual_spread` (mean/std of `b - (a*slope + offset)` in b's units), and
`difference_bias` / `difference_spread` (mean/std of `b - a`: the residual of
the *1:1 hypothesis*), plus `as_dict()` for JSON emitters. The two residual
pairs answer different questions. The post-fit residual says how well *some*
line fits; its mean is zero by construction and a wrong scale or offset is
absorbed by the fit, so it cannot detect either. `b - a` is what a
unit-agreement claim rests on. MIN_PAIRS, the motion check and the scale
stability verdict are unchanged.

## Timestamp provenance (`timestamp_basis`)

`read_channels(..., acquired_time=True)` emits a `timestamp_basis` column per
row; `read_acquisition_frame()` exposes the same on the raw frame for
consumers that decode bytes themselves (the broadcast sweep).

| basis | how it is decided | clock floor |
|---|---|---|
| `acquired` | `metadata_json.acquired_at` present; the timestamp is replaced by it | none |
| `stored` | `metadata_json` present, no `acquired_at`, not a round-stamped capture. The stored stamp is the decode-time clock read (broadcast `can:` rows always) | none — decode latency is unmeasured but millisecond-order; the basis is reported so a reader can see it |
| `round` | no `acquired_at` **and** the session's `capture_profile.json` says `round_timestamps: true`; OBD/DID rows only | the round period, estimated as the median gap between distinct stamps on that side |
| `unknown` | frame has no `metadata_json` column at all | 1 s (`UNKNOWN_BASIS_FLOOR_S`) |

`check_clock_support()` raises `ComparisonRefusedError` naming the side, the
basis and the floor when `max_separation` is below the floor. A side mixing
bases is judged on its weakest row.

What cannot be inferred: a stamp shared by several OBD channels is **not**
evidence of a round stamp. A batched Mode 01 response legitimately stamps
every PID it carried with one decode instant (the ground-truth fixture has
201 of 350 OBD stamps shared this way, all from batching), and that is
indistinguishable from a round stamp without `acquired_at`. Manifest
evidence, or a `round_id` on the row itself with no `acquired_at` beside it
(C02), is what marks a stamp as `round`. Since C02 the Canable collector
persists `acquired_at` on **every** reading (`ts_schema: 2`, with
`acquired_clock`, `generation` and `round_id` — see `docs/capture-rounds.md`),
so a `round` row should only arise from data written before that field
existed. `acquired_at` is float Unix seconds in JSON and round-trips to the
microsecond.

## `grid=` migration

`align_pairs`, `compare_channels` and `broadcast_discovery.sweep_session`
raise `TypeError` naming the migration if `grid=` is passed; a bin width was
never a statement about acquisition mismatch, so it is refused rather than
reinterpreted as a one-second tolerance. `read_aligned` keeps `grid`: it is
the cell-based *aligned frame* API for band averages, gating and plots, and
its docstring now says that its output is not pairing evidence. No CLI, HTTP
or MCP path exposed `ChannelComparison`.

## Consumers audited (C01.7)

| consumer | before | now |
|---|---|---|
| `broadcast_discovery.sweep_session` | own `.resample(grid).mean()` on both sides | routed through `match_ordered` under the caller's policy, paired once per (CAN id, reference), every probe fitted over the same pairs; `n_pairs` is real pairs. Ground-truth rediscovery still ranks `0x1F9` bytes 2-3 first at scale 0.1247, `stable`; the noise sweep still returns nothing credible. Fixture runtime 0.10 s for one id, 0.15 s for all ids x 3 references |
| `cross_reference.pair_by_timestamp` (record.py `frames.log`/`pids.jsonl` tools; also `broadcast_correlate`) | nearest-within-tolerance forward sweep, no reuse, no averaging | already compliant in substance; left as is, different storage format |
| `tuning.delivery_error` (MCP `get_delivery_error`, `tuning_report.py delivery-error`, the tuning digest, the workbench Analysis page) | `read_aligned(grid="1s", cell_agg="last")` | R05.16: acquisition-paired. Samples are `lambda_delivery.delivery_samples` rows (each target read meets at most one measured read within the declaration's `acquisition.max_separation_s` via `match_ordered`; steady-target, post-fuel-cut and regime states named); rpm, `FUEL_STATUS` and load attach by the same one-to-one pairing; the clock basis is checked (`check_clock_support`). Payloads carry `engine_version: 2` and a `pairing` block |
| `tuning.compare_sessions` | `read_aligned(grid="1s", cell_agg="last")` | intentional aligned-frame consumer (per-cell means on a matched grid), not a comparison conclusion; documented on `read_aligned` |
| `tuning_frame` | `read_channels(acquired_time=True)` | unchanged; gains the basis column, unused |
| `test_exact_byte_relationships_still_hold` | `pd.merge_asof(nearest, 100 ms)` over raw bytes | left as is: below the decode layer, asserts median/max/exact-fraction rather than a fit; note it allows right-side reuse |

## Ground-truth fixture (C01.11)

Policy: `PairingPolicy.within("250ms")`. Measured on the slice: broadcast
frames every 102 ms median / 170 ms p95 / 0.5-0.7 s max (10:1 decimation
gaps); Mode 01 PIDs every 1.11 s median. Nearest-frame separations for the
accepted pairs: 26-31 ms median, 48-50 ms p95, 70-144 ms max. 100 ms left
one wheel-speed and one coolant reading unpaired (max 144 ms); 250 ms admits
every honest nearest-frame pairing and is under a quarter of the PID cadence.
Results are identical at 250 ms and 500 ms.

Every `unit_agreement` case now asserts the 1:1 residual `b - a` (bias and
spread, in the reference's units) **before** the slope. Bound rule, written
into `expectations.yaml`: `max_residual_bias = |measured| + max(3*spread/sqrt(n),
half the PID's resolution)`, `max_residual_spread = measured * 1.25`, rounded
up.

| case | slope err % old (1 s bins) | slope err % new | offset new | b-a bias | b-a spread | n | slope bound | bias / spread bound |
|---|---|---|---|---|---|---|---|---|
| can:ECM_RPM ~ obd:RPM | 0.956 | 0.255 | +1.69 rpm | -4.08 | 24.46 | 239 | 1.6 | 9.0 / 31 |
| can:ECM_RPM_23D ~ obd:RPM | 0.830 | 0.112 | -1.05 | -3.58 | 24.58 | 239 | 1.5 | 8.5 / 31 |
| can:WHEEL_SPEED_6F7_A ~ obd:SPEED | 1.453 | 1.356 | -0.85 km/h | +0.009 | 0.876 | 212 | 2.1 | 0.55 / 1.1 |
| can:WHEEL_SPEED_6F7_B ~ obd:SPEED | 1.637 | 1.400 | -0.92 | -0.027 | 0.856 | 212 | 2.3 | 0.55 / 1.1 |
| can:WHEEL_SPEED_6F7_C ~ obd:SPEED | 0.945 | 0.697 | -0.80 | -0.350 | 0.695 | 212 | 1.6 | 0.85 / 0.9 |
| can:ECM_COOLANT_TEMP ~ obd:COOLANT_TEMP | 0.289 | 0.285 | +0.25 degC | 0.000 | 0.227 | 78 | 0.9 | 0.5 / 0.3 |
| can:ECM_COOLANT_TEMP_233 ~ obd:COOLANT_TEMP | 0.138 | 1.357 | +1.20 | -0.013 | 0.196 | 78 | **1.9 (was 0.8)** | 0.55 / 0.25 |
| can:ECM_COOLANT_TEMP_23D ~ obd:COOLANT_TEMP | 0.246 | 0.840 | +0.75 | 0.000 | 0.160 | 78 | **1.4 (was 0.9)** | 0.5 / 0.21 |

The fast signals got markedly *better* under acquisition pairing (a 1 s bin
against a 1 Hz PID was smearing rpm: offset -28.7 rpm -> +1.7 rpm). The
coolant slopes got worse, and `0x233:0` failed its old 0.8 % bound. The 78
pairs agree with the PID exactly in 75 and differ by one count in 3 (2 at
-1, 1 at +1). Coolant spans 85-92 degC on this slice (var 4.6, integer on
both sides), so one one-count disagreement near the end of the range moves
the OLS slope by ~0.5-0.7 %; three account for the full 1.36 %. `0x23D:7` is
bit-identical to `0x233:0` on the wire and lands at 0.84 % only because its
frames arrive at different instants and pair to different PID readings. The
binned path passed at 0.138 % because averaging ~10 frames per bin removed
the quantisation on the regressor (errors-in-variables attenuation with
U(-1/2,1/2) noise on var 4.6 is ~1.8 %, the observed order).

**Decision (orchestrator, 2026-09-06):** the yaml's own rule is "measured
baseline + ~0.5 % headroom", and those baselines were measured under the
binned estimator. The three coolant slope bounds were re-measured under the
corrected estimator with the same rule (0x551 0.285 % -> stays 0.9;
0x233 1.357 % -> 1.9; 0x23D 0.840 % -> 1.4). Every other slope bound, every
`min_r_squared`, every `max_offset`, all `catalog_pins` and all
`byte_relationships` are unchanged. The change is a re-measurement, not an
accommodation, and it is paired with the stronger residual check above: on a
quantised signal over a narrow range the slope is the weak statistic and
`b - a` (96 % exact, spread 0.19 degC) is the evidence of 1:1 agreement.

Sanity check that the suite still catches a wrong catalog entry — a scratch
copy of `data/bindings.yaml` with `ECM_COOLANT_TEMP_233` perturbed, run
through the same assertions (nothing committed):

| perturbation | b-a bias | b-a spread | slope err | offset | caught by |
|---|---|---|---|---|---|
| none | -0.013 | 0.196 | 1.36 % | +1.20 | passes |
| scale 1.0 -> 1.05 | -6.97 degC | 0.237 | 6.05 % | -1.15 | residual bias (bound 0.55), slope (bound 1.9) |
| offset -50 -> -48 | -2.01 degC | 0.196 | 1.36 % | -0.78 | residual bias only — the slope and post-fit residual cannot see it |
| both | -8.97 | 0.237 | 6.05 % | -3.03 | residual bias, slope, offset |
| byte_start 0 -> 1 | +34.4 | 90.1 | 99.9 % | +89 | everything |

The offset row is the point: under the old file that edit would have passed
(slope unchanged, offset -0.78 inside 2.0). The residual bias is the check
that sees it.

## Complexity and memory (C01.8)

Measured 2026-09-06 on the laptop, best of three, `tracemalloc` peak:

| case | time | peak |
|---|---|---|
| fixture `align_pairs(can:ECM_RPM, obd:RPM)` incl. read + decode (3825 vs 239 rows) | 493 ms (matching itself well under 1 ms) | 3.7 MB |
| synthetic 30 min: 100 Hz (180k) vs 15 Hz (27k), `reference=auto` | 233 ms | 22 MB |
| same, `reference="a"` forced onto the dense side (worst case) | 606 ms | 22 MB |
| synthetic 60 min (2x rows), `reference=auto` | 495 ms | 44 MB |
| synthetic 60 min, `reference="a"` | 1240 ms | 44 MB |

Doubling the rows doubles time and memory: linear in practice, `n log n` at
worst. Memory is dominated by the two Python union-find lists (~100 B per
sample on the walked side). No all-pairs distance matrix is ever formed.

## Which kind of empty

A side with zero observations is refused by name (`<spec>: no observations in
this session`) before any clock check runs, in both `align_pairs` and
`sweep_session`. Falling through to `check_clock_support` used to blame
`metadata_json` for a PID the profile never polled — the `delivery_error` /
`tuning-did` shape from AGENTS.md — and "the input was never captured" and
"the clock cannot support this tolerance" demand opposite responses.

## Unresolved timestamp limitations

- `stored` stamps trail wire arrival by an unmeasured decode latency
  (millisecond order). The basis is reported but no floor is applied.
- A `round` basis can only be recognised from a `capture_profile.json`
  sidecar; a DataFrame without a `session_path` column, or a session whose
  sidecar is missing, reads as `stored`.
- The round period for the `round` floor is estimated from the data (median
  gap between distinct stamps), not read from the profile.
- Timestamps are compared as float seconds. The conversion keeps the
  sub-second fraction exact so an observation exactly at the tolerance
  pairs, but resolution at Unix-epoch magnitude is ~0.24 us.
- The greedy in-order assignment is deterministic but not globally optimal;
  a different reference side can produce a slightly different pair set.
- On a quantised signal over a narrow range the OLS slope is ill-conditioned
  (see the coolant numbers above). Read `difference_bias`/`difference_spread`
  for a 1:1 claim; use the slope for scale discovery, where the range is
  wide by construction.
- C02 owns the writer-side round/acquisition schema; nothing persisted was
  changed here.
