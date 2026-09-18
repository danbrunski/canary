# Deploy plan for the 2026-09-17 work

The Pi was unplugged and powered off at ~00:15 UTC 2026-09-18 with **one change
already applied to it** and **six code commits pending**. Everything here is
pushed to `main`.

## Already on the Pi (applied 2026-09-18 ~00:05 UTC, survives the power cycle)

- `/etc/systemd/system/canary.service` carries **`MemoryMax=6G`** and
  **`MemorySwapMax=0`** (`2dfa0aa`). Verified live: `MemoryMax=6442450944`,
  `MemorySwapMax=0`, daemon healthy at 515 MB. Nothing to redo — but note the
  Pi's unit is now ahead of what `install_pi.sh` last wrote, and re-running
  the installer is what keeps them in step (the commit is in the repo, so a
  pull plus the installer, or the installer alone, is consistent).
- Four pinned sessions for D02.10 (`2026-07-26T234833` + the three
  `calibration_sessions`) and the pinned D02.9 source `2026-09-10T142711`.
  Pins are sidecars in the session dirs, so they survive too.

## Pending deploy, in dependency order

The Pi has no GitHub deploy key (D03.5), so deploys go over a bundle:

```bash
# on the laptop
git bundle create /tmp/deploy.bundle <pi-HEAD>..main main
scp /tmp/deploy.bundle canary@192.168.137.2:/tmp/
# on the Pi
cd ~/canary && git pull --ff-only /tmp/deploy.bundle main && git log --oneline -1
sudo systemctl restart canary
```
`<pi-HEAD>` was `3c2391d` when the Pi went down — check it rather than assume.
**Verify the hash after the pull**: a failed pull looked like a successful
deploy earlier today.

| commit | what it changes | risk |
|---|---|---|
| `7ebfa85` | `metadata_json` parsed once per row, not six times. Capture-quality recompute 38.8 s → 7.1 s | low — proven bit-identical on four real sessions + the ground-truth suite |
| `c15f66a` | replay emits only captured sources; replay status publishes `rows_emitted`/`source_rows_total` | low — no replay runs unattended; behaviour only differs when a replay runs |
| `3d45680` | `GET /status?view=compact`; capture tag/stop 409 instead of a silent null | low, but **the tag/stop refusal is a behaviour change** any script that ignored the null body will now see as an error. That is the point |
| `2dfa0aa` | already applied (above) | — |
| `dcde580` | trip-end digest off-loop, address-space-capped, row-budgeted, refusals recorded | **read the behaviour change below** |

## The behaviour change to expect after `dcde580`

Sessions larger than the derived row budget (~1.95 M rows on the 8 GB Pi: a
4 GiB child ceiling ÷ the measured 2,200 bytes/row) **no longer get a digest**.
They get `digest_skipped.json` naming the row count and the budget, and
`GET /control/sessions/{id}/digest` answers **409** with the reason.

That is a deliberate trade, and it is smaller than it looks: the sessions this
refuses are the ones that were *already* failing. Today's 638-chunk capture
wedged the daemon for 38 minutes rather than producing a digest, and of the
eleven sessions this drive produced, only the three smallest ones have a
`digest.json` at all. A refusal that says why is strictly better than a hang.

**It is still a regression against the ideal**, and the fix that removes it is
D03.6's second half: `resample_and_segment` pivots at raw microsecond
resolution *before* resampling to 1 Hz, which is why peak memory is
O(rows × channels) — 5,483 MB to emit 1,143 rows. Reduce each channel onto the
resample grid first and the cost becomes O(grid × channels); then raise the
budget (one constant, `DIGEST_PEAK_BYTES_PER_ROW`, deliberately the only place
the number lives). That change touches `src/preprocessing/loader.py`, which
training preprocessing shares, so it needs the parity treatment D01.8 got —
not an unattended landing.

## Verify after deploying

1. `/status` healthy within 3 min: `vehicle_state`, 0 dropped records, inference
   ticking. `GET /status?view=compact` should answer in **well under** the full
   payload's 1.3–2.6 s — that number is the whole point of the change, so
   measure both and record them.
2. `POST /control/captures/current/tags` with no capture active → **409**, not
   `{"tag": null}`.
3. Park-and-idle for 6 minutes with a *small* session open, then check that the
   session got its `digest.json` (the trip-end path still works end to end).
4. `journalctl -u canary | grep -i digest` — a refusal should log a warning
   naming the row count, and there should be no `MemoryError` on a small
   session.
5. `systemctl show canary -p MemoryMax -p MemorySwapMax` still reports
   `6442450944` / `0` after the restart.

## Then, in this order

1. **D02.10's replay comparison** — now unblocked and safe: replay one of the
   pinned *calibration* sessions (700 k–2.5 M rows, so well inside the budget)
   and compare per-detector event rates against D02.6's 3.27/min. Needs
   `c15f66a` deployed first or the output is inflated 1.29×. Don't run it while
   the truck is collecting: a replay replaces live collection.
2. **D03.6's cost fix** (above). It lifts the digest budget and also makes long
   *live* drives safe, which is the part that still isn't.
3. **The two operator decisions from the drive** that nothing else can settle:
   catalogue **0x1134** and pair it with Mode 01 PID 0x44 in one capture (the
   best target-AFR candidate the project has had), and re-fit the injector
   constant from this drive's open-loop samples (it is ~15 % low at WOT).

Full drive context: `data/analyses/2026-09-17-drive-log.md`. D02.9/D03.6
evidence: `data/analyses/2026-09-17-d029-replay-amplification.md`.
