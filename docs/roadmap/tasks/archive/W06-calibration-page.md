# W06 — the Calibration page

Roadmap task W9. Depends on W01, W02.

Which ROM is running, the flash history, and the evidence behind each RAM
address. Addresses are program RAM and survive calibration flashes (operator,
2026-09-21), so the daemon no longer withholds channels on a revision change
(`6ef51b4`); `verified_tune_revision` is provenance. What this page adds is the
evidence check: re-run each channel's known-answer relation and show the result,
so a wrong or moved address (a different ECU program, or a bad find in the
first place) is one red row rather than a plausible-looking number.

**Files:** new `panels/tuning/CalibrationPage.tsx`. Backend:
`src/analysis/tune_revisions.py` (`TuneRevisionRegistry`, `load_tune_revisions`,
`.at()`), `data/<profile>/tune_revisions.yaml`, `src/decode/dids.py`
(`MemoryChannel`, `plan_memory_reads`),
`tools/verify_memory_channels.py` (`verify_channel`, `plan_restamp`, `restamp`).

## Backend

- [x] **W06.1 — `GET /control/calibration`.** The flash history (revision,
  `flashed_at`, `k`, `rom_file`, `rom_sha256`, notes), the revision the
  registry resolves as running, the ECU's own calibration id (W02.2) and whether
  they agree.
- [x] **W06.2 — `GET /control/memory-channels`.** Per catalogued channel: name,
  address, window, `verified_tune_revision`, `revision_carried_reason`, the
  declared `known_answer` relation (or none), and a computed `state`:
  `verified` (last checked on the running revision and passed), `checked
  earlier` (passed on another revision — expected after a calibration flash,
  informational, still polled), `carried` (stamped with a recorded reason and
  no check), or `unverifiable` (declares no check). None of these gates
  polling; they are evidence, and the page must not merge any two of them.
- [x] **W06.3 — Move the verifier's core out of `tools/`.** `verify_channel`,
  `plan_restamp` and `restamp` become a module under `src/` that both the CLI
  and the daemon import; the CLI keeps its interface. The daemon must not shell
  out to a tool.
- [x] **W06.4 — `POST /control/memory-channels/verify`.** Runs the known-answer
  checks through the daemon's own client, inside the Mode-22 session, and
  returns a verdict per channel (`pass` / `fail` / `unverifiable`, with counts).
  Read-only on the ECU: `0x10`, `0x23`, `0x22`. Refuses while a capture is
  active. With `record_revision`, applies `plan_restamp` + `restamp` — window-
  atomic, text edit, validated before write — and requires an explicit
  `carry_reason` to stamp anything unverifiable. The catalog reload that follows
  must not restart the daemon (see `scripts/restart_canary.sh` for why).
- [x] **W06.5 — Editing `data/dids.yaml` from the daemon is a write to a
  tracked file.** Decide where the stamped catalog lives on the Pi so a later
  `git pull` neither clobbers it nor is blocked by it. The capture-profile
  catalogs already have this problem; solve it the same way, deliberately, not
  by accident.

## Frontend

- [x] **W06.6 — Flash history timeline**, newest first, the running revision
  marked. The registry's own caveats (approximate flash times, sessions near a
  boundary possibly mislabelled) are shown, not dropped.
- [x] **W06.7 — Running calibration header.** ECU-reported id beside the
  registry's newest entry; a mismatch is the first thing on the page, with
  "the ECU reports a calibration the flash history does not contain — record
  the flash before trusting any RAM address".
- [x] **W06.8 — Memory channel table** from W06.2, grouped by window since a
  window is stamped whole or not at all. Colour by `state`; `carried` shows its
  recorded reason in full; `unverifiable` says what would make it checkable.
- [x] **W06.9 — Re-verify action.** Runs W06.4 without recording and shows the
  verdicts. Recording is a second, separate action that names the revision
  being stamped and, where any member is unverifiable, requires a typed reason.
  A failed channel blocks its window and says the address has probably moved.
- [x] **W06.10 — Record a flash.** A form that appends a revision to the
  registry (name, time, K, ROM file, SHA-256, notes). Today that is a hand edit
  of YAML made after the fact, which is how the registry and the truck drifted
  apart for a morning.

## Empty states

| condition | the page says |
|---|---|
| no registry file | "No flash history for this vehicle" + the record-a-flash form |
| ECU id unread | "The ECM has not been read yet" — not a mismatch |
| no memory channels catalogued | "No RAM channels for this vehicle" — not an error |
| capture active | re-verify disabled, with the reason |
| engine off | re-verify allowed; a note that a fast-moving quantity cannot be distinguished from a moved address while nothing is moving |

## Done when

Implementation is complete. Verification stamps and flashes recorded through
the daemon live in the profile-scoped operational control directory (seeded
from the tracked catalogs), so they neither dirty the checkout nor get
clobbered by a pull. The real-flash exercise remains a deployment validation,
not an implementation dependency, because the Pi was unreachable on both
documented addresses during this work.

After a flash is recorded, every memory channel shows `checked earlier` and
keeps polling; one re-verify and one record action return the checkable ones to
`verified`, a failing one says its address has probably moved; the carried ones
display their reason; and none of it needed a shell. *Truck check:* run it
against a real flash boundary — `ZS44A-20260920` is waiting.
