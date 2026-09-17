# D02.9 — where the replay's extra rows and the 7.7 GB actually came from

Investigated 2026-09-17, read-only: no replay was run, no daemon touched. Code
reading plus DuckDB aggregates over the preserved output and its source.

D02.9 says: *"It wrote 5,860,000 rows in 586 chunks while covering 32 % of a
source session holding 4,544,560 rows in 511 chunks — more rows than its input
contains, over a third of the span. Something amplifies rows and memory grows
with it."*

**Both halves of that framing are wrong.** Nothing grows without bound, memory
did not grow with rows, and the replay is not amplifying 4×. There are two
independent defects and one arithmetic artefact.

## The measurement

`_replay-2026-09-11T164903` against its source `2026-09-10T142711`, counted per
`source` column (not estimated from chunk counts):

| source | source rows | replay rows | ratio |
|---|---:|---:|---:|
| `can` | 3,133,682 | 3,110,773 | **0.993** |
| `obd` | 36,715 | 36,461 | **0.993** |
| `decoded` | 1,319,681 | 2,620,062 | **1.985** |
| `derived` | 19,248 | 34,624 | 1.799 |
| `gps` | 35,234 | 58,080 | 1.648 |
| **total** | **4,544,560** | **5,860,000** | **1.289** |

`decoded/can` is 0.4211 in the source and 0.8423 in the replay — exactly double.

## 1. The replay consumed ~99 % of the source, not 32 %

`can` and `obd` both land at 0.993×, so the replay read essentially the whole
session; the missing 0.7 % is the chunk that was in flight when the process
died. **"32 % of the span" was a wall-time fraction** (1,910 s of wall clock
against a 5,889 s recording) **multiplied against a row count**, and row density
over that recording is nowhere near uniform.

Why a "1x" replay outruns its own recording: `src/collection/replay.py:46`
sets `_MAX_GAP_SECONDS = 0.5` and the pacing at `replay.py:160-167` clamps the
*sleep*, not the timeline —

```python
target = wall_start + (orig_epoch - first_orig) / speed
delay = min(_MAX_GAP_SECONDS, max(0.0, target - time.monotonic()))
```

so every row costs at most 0.5 s of wall time however long the recorded gap
before it was. A gappy session therefore replays strictly faster than 1x while
the API, the status payload and the log line all still say `speed: 1.0`. Measured
on sessions already pulled to the laptop: `2026-05-20T033820` (3.55 M rows,
47,838 s span) replays in 2,536 s = **18.9× effective**; two dense 2026-08
drives come out at 1.01× and 1.14×. This source needed only 3.08×.

This is the repo's own "never compare aggregates" rule in a new costume: a
wall-time fraction and a row count, combined as if the second were uniform in
the first.

## 2. Real row amplification: `decoded` is written twice, at the emit path

`_iter_rows` (`replay.py:101-126`) re-emits **every** stored row, including the
`decoded` rows the source session already contains, and replayed `can` rows keep
their payload (`replay.py:75-79`). Those records join the same ingest queue as
live collectors, and the storage sink then decodes the frames again:
`drain.py:212-217` (`to_write = record + decode(record)`), wired unconditionally
at `runtime.py:218-264`, firing on any `source == "can"` row with a payload
(`frame_decoder.py:88-118`). So each decoded signal is persisted twice — once
replayed, once freshly re-derived. `derived` doubles the same way via
`inference.py:230-290`.

The two copies are **not** byte-identical rows — a grouped count over
`(timestamp, channel, value_number)` finds no duplicate pairs — because replay
rebases timestamps to wall clock and the replayed `decoded` row and its
`can` parent are emitted at different moments. The doubling is visible in the
ratio, not in duplicate detection, which is worth knowing before anyone tries to
de-duplicate after the fact.

`frame_decoder.py:20-22` already documents that the *batch* read path "can
safely skip channels that were already persisted". The storage sink has no
equivalent skip and the emit path has no equivalent filter.

**The layer question is now answered: the emit path.** The fix depends on
whether the detectors need the replayed `decoded` rows, and they do not.
`LiveFeatureSource.observe` (`live.py:256+`) handles `source == "can"` by
calling `_apply_can_decode`, which writes `decoded:<SIGNAL>` into
`_current_values` itself (`live.py:335-344`). The live/inference path therefore
derives its decoded features from the raw frames, independently of the storage
sink, and a replay that emitted only captured sources would feed the detectors
exactly what a live drive does.

**And today's behaviour is worse than redundant — it is ambiguous.** A replayed
`decoded` row falls through `observe`'s catch-all and writes *the same*
`decoded:<channel>` key that `_apply_can_decode` writes. So during a replay the
value the detectors see is whichever arrived last: the stored one, or the one
freshly decoded from the frame. Those differ precisely when `bindings.yaml`
changed since the recording — i.e. in the situation where a replay is being used
to check a binding change. Skipping re-derived sources removes the ambiguity as
well as the duplicate rows.

**One case where skipping would lose data**, and it has to be decided per
session rather than per row: raw CAN payload persistence only landed 2026-07-29,
so in an older session the `can` rows have no payload and nothing downstream can
re-derive anything. There, the stored `decoded` rows are the only decoded
signals available and must still be emitted — with the reason reported, not
silently.

`tests/test_replay.py:56-68` pins row-for-row emission at the queue for a
fixture of `can`/`obd`/`can`, so row-for-row *is* the intended contract for
captured sources and it holds where it is tested; the duplication is introduced
downstream of that assertion.

## 3. Live GPS contaminates a replay's output — a new finding

`gps` came out at **1.648×** (58,080 vs 35,234): about 22,800 rows that are not
in the source at all. The Pi's own GPS receiver kept emitting into the active
session directory — which, during a replay, is the `_replay-*` directory — at
roughly 12 rows/s over the 1,910 s run. `can` and `obd` show no such inflation
because that replay ran parked: no bus traffic, no ECM answering.

So **a replay output is not a pure re-emission; it is the replay plus whatever
the live sensors produced meanwhile.** Anything comparing a replay's GPS or
derived-speed channels against the source is comparing two different drives.
This is independent of the decoded doubling and was not previously recorded.

## 4. The 7.7 GB is not the replay at all — it is the trip-end digest

The only full-session load reachable inside the daemon during a replay:

```
drain.py:349-358   gps_tracker.observe(record) -> True on the trip-end edge
drain.py:556-558   on_trip_ended(active_session_path())     # the _replay-* dir
runtime.py:249     _run_trip_end_hook(...)                  # SYNCHRONOUS, on the loop
control/trip_end.py:60   get_or_compute_digest(...)
analysis/digest.py:258   classify_session(...)  -> resample_and_segment
analysis/digest.py:265   compute_fingerprint(...) -> resample_and_segment   # a second full load
loader.py:203-205  long = read_sessions(...) ; wide = to_wide(long, ...)
```

`read_session` (`loader.py:50-88`) reads every chunk with every column including
payload bytes and `metadata_json`; `to_wide` (`loader.py:101-136`) then runs
`pivot_table` on `(session, timestamp)` at **raw microsecond resolution** with
`dropna=False`, and only afterwards does `resample_wide` downsample to 1 Hz. The
final frame for a 1,910 s session is ~1,910 × 100 cells; the intermediate pivot
is ~4 M × 100.

**Measured directly at scale afterwards, on the laptop** (`digest_mem.py`,
against the pulled `2026-08-08T214218`, 2,622,370 rows / 1,141 s / 2,298 rows
per second):

| stage | result | peak RSS |
|---|---|---:|
| baseline | — | 121 MB |
| `read_session` | 2,622,370 rows × 13 cols | **1,879 MB** |
| `to_wide` | 1,824,090 rows × 102 cols | **3,440 MB** |
| `resample_and_segment` (the actual digest call) | **1,143 rows × 203 cols** | **5,483 MB** |

**5.5 GB of peak memory to produce 1,143 rows — 0.044 % of the rows it loaded.**
That is the whole defect in one line. Distinct timestamps are 0.696 of rows, so
the pivot's row count is set by the raw timestamp resolution, exactly as the
code structure implies.

**Where the cliff actually is, bracketed on the Pi itself** (the earlier
1.49 KB/row extrapolation was too crude to site it):
- `2026-09-10T142711`, **4,544,560 rows — has a `digest.json`**, so it completed
  on the 8 GB Pi.
- `_replay-2026-09-11T164903`, **5,860,000 rows — no digest**, and the daemon
  was OOM-killed computing it.
So the limit is somewhere between **~4.5 M and ~5.9 M rows**, and it is not a
linear extrapolation from the laptop's numbers (the laptop peaks higher per row
than the Pi's success at 4.5 M would imply — allocator and fragmentation
differences, so trust the bracket over the slope).

The earlier small-session measurement, for the record: on a 397,385-row session
`read_session` +359 MB,
`to_wide` +233 MB, **592 MB peak = 1.49 KB of peak RSS per row**. At 5.86 M rows
that extrapolates to **~8.7 GB**, against the kernel's reported 7.7 GB anon-rss
— consistent with being killed part-way through the allocation. It is a
**one-shot spike, not a leak**, and it blocks the event loop while it runs, which
is why the output stops at 586 full chunks with no partial one.

**Confirmed here:** the preserved replay directory contains `dtc.json` and 586
parquet chunks and **no `digest.json`, `classification.json` or
`fingerprint.json`** — the hook fired and was killed mid-computation. Timing
agrees: `DEFAULT_TRIP_END_STATIONARY_SECONDS = 300.0`
(`analysis/gps_tracker.py:42`) after a drive that "completed before the kill"
lands in the minute the kill happened.

Two things make this its own defect rather than part of D02.9:

1. **The guard exists and this caller bypasses it.** `GET` for the same digest
   refuses `_replay-*` names, because `control/digest.py:30` goes through
   `control/sessions.py:166-178` whose regex admits only live and `_capture-*`
   sessions. The trip-end hook calls `get_or_compute_digest` directly. That is
   the AGENTS.md shape exactly — "a fix that every caller must remember is at the
   wrong layer" — and the fix is to push the refusal into the shared function.
2. **The guard alone only moves the cliff.** Peak memory is O(rows × channels)
   with the pivot before the resample, so a long *live* drive OOMs the same way:
   2.6 M rows already needs ~3.9 GB on an 8 GB Pi. The principled fix is the
   order of operations — select only the needed columns and reduce each channel
   onto the resample grid *before* the pivot (~1,900 × 100 instead of ~4 M × 100)
   — and run it off the event loop. That is what makes long drives safe, not just
   long replays.

Every real drive has a `digest.json`, so this has never fired live; the replay
was simply the first session big enough.

## What the sustained-validation harness cannot see

`src/validation/harness.py:617-690` wires replay through the storage decode, so
it *should* catch the doubling — but its fixture
`tests/fixtures/ground_truth/drive-slice` holds `gps 1682 / can 19122 /
obd 953` and **no `decoded` rows at all**, so the expansion it records
(`decoded_rows: 3080` beside `can: 2189`) is a correct one-to-many for that
fixture and structurally incapable of showing a doubling. A regression test needs
a fixture whose source already contains `decoded:` rows. The harness does
already measure `max_rss_mb`, so the instrument for the memory half exists and
only needs pointing at a big enough session, off the vehicle.

## Evidence preserved

`replay_retention_days: 7` would have swept the replay output around 2026-09-18.
Both sides are now safe:
- source `2026-09-10T142711` **pinned** on the Pi (retention skips it);
- both directories copied to `~/canary-data/_d029-evidence/` on the laptop
  (`_replay-2026-09-11T164903`, 56 MB / 586 chunks; `2026-09-10T142711`,
  47 MB / 511 chunks). The `_`-prefix keeps them out of training discovery.

## Still unmeasured

- Whether the re-derived `decoded` rows reach the detectors or only the writer —
  the question that decides where the row fix belongs (§2).
- The digest cost at 2.6 M rows, measured rather than extrapolated. Safe on the
  laptop against the pulled session; **must never be run on the Pi.**
- The journal around 2026-09-11 17:00–17:30 UTC, to pin the trip-end line
  against the OOM ordering.
