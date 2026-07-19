# Roadmap — dashboard UX: surface what's already built

Status: **planned** (2026-07-19). Straight to `main`, no feature branches.

## Why this exists

A UX review (2026-07-19) compared the documented purpose — a diagnostic
*workbench*, not just a live monitor — against what the dashboard
actually surfaces, and found the gaps are wiring, not new subsystems.
Everything below has a finished backend and zero (or misplaced) UI:

1. **Replay is invisible.** The runtime replay API is complete
   (`/control/replay` start/stop/status in
   `src/dashboard/routes/control.py:224-260`, a `replay` block in
   `/status`, `src/dashboard/routes/public.py:58`) and AGENTS.md calls
   replay the canonical way to evaluate detectors — but the frontend
   never calls or reads any of it. Worse, nothing warns when replay is
   active, so re-emitted session data is indistinguishable from the
   live truck on every panel.
2. **`vehicle_state` isn't shown.** The derived classification
   (`classify_vehicle_state`, `src/dashboard/status.py:73`, in every
   `/status` payload) is the field agents are told to read *first* —
   yet the UI only uses `vehicle_state` as free-text capture metadata.
   The `OperatingModeIndicator` shows the ML operating mode, which is a
   different thing. "Detectors quiet" reads very differently under
   `engine_off` vs `engine_running`.
3. **The Sessions panel is a placeholder while its features hide in
   ControlPanel.** Metadata edit, pin/unpin, export zip, delete, and
   companion sync all ship today, buried in the ~580-line
   `ControlPanel.tsx`. Task 6.13's panel is mostly a relocation job.
4. **Anomaly triage doesn't close the loop in the UI.** `AnomalyPanel`
   inspects evidence and records verdicts, but the "why did/didn't this
   fire?" step (`replay_window`) is MCP/CLI-only — it's just a
   `/control/jobs` submission (`kind="replay_window"`, param mapping in
   `src/mcp_server/tools/replay.py:87`).
5. **Token entry is duplicated per panel** with inconsistent messaging
   (TestSessionPanel has a four-state model the other gated panels
   lack).
6. **Dead code + stale docs.** `DiagnosticsPanel.tsx` is defined but
   never imported (orphaned by the grouped-Workbench refactor), and the
   README still describes the old flat-tab layout.

## Guiding constraints

- Whole-repo gate before every commit, plus `pnpm test` and
  `pnpm typecheck` in `src/dashboard/static` for every task here
  (re-run `TuningFeedback.test.ts` once on flake).
- `nav.ts` stays the single source of truth for workbench navigation.
- Keep the app's conventions: honest empty/loading/error states,
  `styles.ts` classes, hand-rolled canvas charts, no new FE deps.
- Replay start must surface the refuse-while-driving 409 (ml-hardening
  7.2 pre-made decision) verbatim — no client-side second-guessing.
- All of this is testable against fixture payloads / the local daemon;
  Pi verification is a morning task, not a blocker.

## Sequencing rationale

Phase 1 first: the truth indicators are tiny, shell-level, and every
later phase (and every debugging session) benefits from them. Phase 2
(replay control) needs a home, which Phase 3 (Sessions panel) provides —
but the API wrapper lands first so the banner and card share types.
Phase 4 rides on the existing jobs plumbing. Phase 5 should merge with
refactoring Phase 3's ResourceGate/TokenField work rather than duplicate
it. Phase 6 is independent cleanup, last because it's cheap.

---

## Phase 1 — Truth indicators (vehicle_state + replay visibility)

**Goal.** The shell always answers "what am I actually looking at?"

- [ ] **1.1 — `vehicle_state` badge.** Read `status.vehicle_state` from
  the existing 5 s `useStatus` poll and render a badge in the header
  beside `OperatingModeIndicator` (states: `engine_running`,
  `ignition_only`, `engine_off`, `no_data`, `replay`, `unknown`;
  distinct alert styling for `no_data`/`unknown`). Add the field to the
  status types in `api/http.ts`. FE tests over fixture payloads for
  each state.
- [ ] **1.2 — Replay banner.** Whenever `status.replay` reports an
  active replay, show a prominent full-width banner in **both** drive
  and workbench modes ("REPLAY — session <id>", elapsed/progress if the
  payload carries it) and switch the StatusStrip source label. The
  operator must never mistake re-emitted data for the live truck. FE
  tests: active, inactive, and replay-block-absent payloads.

## Phase 2 — Replay control from the UI

**Goal.** Start/stop/inspect runtime replay without an MCP session.

- [ ] **2.1 — API wrappers.** `getReplay` / `startReplay` /
  `stopReplay` in `api/http.ts` over `/control/replay` (+ types).
  Token-gated like the other control calls.
- [ ] **2.2 — `ReplayCard`.** Session picker (reuse the session-list
  fetch ControlPanel already does), start/stop buttons, live status
  incl. the isolated `_replay-*` output path, and the 409
  refuse-while-driving reason rendered verbatim. Lives in the Sessions
  panel (Phase 3); if 3.1 hasn't landed yet, mount it temporarily in
  ControlPanel and move it in 3.1. FE tests incl. the 409 path.

## Phase 3 — Sessions panel (make placeholder 6.13 real)

**Goal.** Session/trip work gets its own home instead of hiding in the
control hub.

- [ ] **3.1 — Extract `SessionsPanel.tsx`.** Move the session
  operations out of `ControlPanel.tsx` — list, metadata edit, pin/unpin
  (+reason), export zip, delete (confirm), companion sync — into a new
  panel; ControlPanel keeps capture, jobs, detector switch, artifact
  upload, preflight, companion config. Drop `placeholder: true` at
  `nav.ts` `data-sessions` and replace the `PlaceholderPanel` mapping in
  `Workbench.tsx`. Coordinate with refactoring 3.2 (god-panel splits) —
  if that has landed first, this is a move of already-extracted
  components. FE tests move with the code.
- [ ] **3.2 — Host `ReplayCard` here.** Replay is a per-session action;
  the card (2.2) renders beside the session list, prefilled from the
  selected session.

## Phase 4 — Close the anomaly-triage loop

**Goal.** "Why did this fire?" answered inside the dashboard.

- [ ] **4.1 — Re-score button.** On `ModelEvidencePanel` (reached from
  `AnomalyPanel` inspect), add a token-gated "re-score this window"
  action: submit `/control/jobs` with `kind="replay_window"` and the
  event's window params (mirror the param mapping in
  `src/mcp_server/tools/replay.py:87`), poll the job via the existing
  jobs plumbing, and render the returned report (per-detector
  fired/score vs threshold) inline with honest running/failed states.
  FE tests over fixture job lifecycles.

## Phase 5 — One token entry, not five

**Goal.** Enter the control token once, in the shell.

- [ ] **5.1 — Shell-level token.** Lift token entry out of the
  individual panels into the shell header (lock icon → entry →
  unlocked), backed by the existing `useControlToken` storage so all
  gated panels share state. Adopt TestSessionPanel's four-state
  messaging (not needed / required / rejected / unlocked) as the single
  vocabulary. **Merge with, don't duplicate,** refactoring Phase 3's
  `ResourceGate`/`TokenField` task — if that shipped first, this is
  "mount TokenField in the header and delete the per-panel entries."
  FE tests for the four states.

## Phase 6 — Cleanup

- [ ] **6.1 — Delete orphaned `DiagnosticsPanel.tsx`.** It takes a
  `mode` prop and is imported nowhere (`Workbench.tsx` maps all
  `diag-*` nav ids to other panels). Delete it and its test if any;
  git history keeps it if a diagnostics summary panel is ever wanted.
- [ ] **6.2 — README dashboard-section refresh.** The README still
  describes the pre-Workbench flat-tab layout and documents
  DiagnosticsPanel as the parked-only panel. Rewrite that section to
  match the shipped shell (drive/workbench modes, grouped sidebar per
  `nav.ts`, StatusStrip) and the panels added by this roadmap.
