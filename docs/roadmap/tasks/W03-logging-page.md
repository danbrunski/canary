# W03 — the Logging page

Roadmap tasks W3 and W4. Depends on W01, W02.

Start and stop a capture from one place, and find out when one ended without
being asked to. On 2026-09-21 a capture died four minutes into a drive because
another agent restarted the daemon; nothing anywhere said so, and the drive
could not be repeated that day.

**Files:** new `panels/tuning/LoggingPage.tsx`; reuses
`panels/CaptureProfileBanner.tsx`, `hooks/useControlResource.ts`,
`hooks/useTagQueue.ts`, `hooks/useStatus.ts`. Backend:
`src/dashboard/routes/control.py` (`/control/capture`, `/control/capture/stop`,
`/control/captures/current/tags`, `/control/power/keepalive`,
`/control/capture/quality`), `src/control/capture_manifest.py`,
`src/control/capture_finalize.py`.

## Backend: why a capture ended

- [ ] **W03.1 — Record an end reason on every capture.** Add `end_reason` to
  the capture manifest: `operator_stop`, `profile_switch`, `daemon_shutdown`
  (clean SIGTERM — write it from the shutdown path), or `interrupted`. The
  dying process cannot write the last one, so:
- [ ] **W03.2 — Detect an interrupted capture at start-up.** When the daemon
  starts, find the newest `_capture-*` manifest with a start and no end record,
  stamp it `end_reason: interrupted` with `detected_at`, and run the normal
  finalization on it (it currently never gets a quality verdict). Use
  `src.control.sessions.session_started_at` to order — never the directory
  name. *Test:* a manifest with no end record is stamped exactly once across
  two start-ups.
- [ ] **W03.3 — `/status.last_capture`.** `{name, session_dir, started_at,
  ended_at, end_reason, quality_verdict}` for the most recent capture, present
  even when none is active.

## Frontend

- [ ] **W03.4 — Profile picker.** Every profile from
  `GET /control/capture/profiles`, each showing description, `notes`,
  `experimental`, channel counts (Mode 01 / Mode 22 / memory), `target_rate_hz`
  and `expected_total_demand_hz`. Read-only here — editing stays on
  System › Capture Profiles, linked.
- [ ] **W03.5 — Start / stop / pause** against the existing routes, with the
  optional `tune_revision` field pre-filled from the context bar's running
  calibration.
- [ ] **W03.6 — Keepalive on this page.** A minutes input and an arm button
  against `POST /control/power/keepalive`, with remaining time shown. Offer it,
  pre-filled, when a capture starts with UPS SOC under the readiness floor.
- [ ] **W03.7 — Live per-channel table** while a capture runs: channel, kind,
  requested Hz, achieved Hz, sent, responses, timeouts, NRCs — the rows the
  scheduler already reports per request. A channel achieving under
  `min_achieved_rate_hz` is amber; `requests_sent == 0` after 10 s is red. That
  last state is exactly how the windowed-memory bug presented on 2026-09-20
  (`capture_selected: true, requests_sent: 0`), and it took a manual `/status`
  dump to see.
- [x] **W03.8 — Withheld channels, in the open.** *Obsolete 2026-09-21:*
  memory channels are no longer withheld on a tune-revision change (`6ef51b4`,
  operator: the RAM layout survives calibration flashes), and the
  `memory_withheld` payload field is gone. A channel that is not delivering
  shows up in W03.7's per-channel `requests_sent`/achieved rate like any other.
- [ ] **W03.9 — Tap-to-tag.** Buttons for the physical actions the project
  tags: pull start / pull end, steady hold start / end, decel, idle. Through
  `useTagQueue` so a tap during a connectivity gap is queued, not lost. Tag the
  physical action, never a UI selection (AGENTS.md).
- [ ] **W03.10 — Announce an ended capture.** When `last_capture.end_reason` is
  anything other than `operator_stop` and the operator has not acknowledged it,
  show a persistent banner on this page **and** in `StatusStrip.tsx`:
  "`tuning` ended after 4 min: daemon restarted". Acknowledgement is per
  capture, stored with `useLocalStorage`.

## Empty states

| condition | the page says |
|---|---|
| no profiles returned | "No capture profiles — the catalog failed to load", with the error |
| engine off | start stays enabled; a note says DIDs will answer but nothing will move |
| Pi storage under the floor | amber warning beside Start, with free space |
| capture active under another profile | the active profile, its elapsed time, and Stop |

## Done when

A capture can be started, tagged, kept alive and stopped without leaving the
page; an interrupted capture is announced on the next page load after the
daemon comes back; a channel that is selected but never sent is red within ten
seconds. *Truck check:* one real capture, start to stop, from this page.
