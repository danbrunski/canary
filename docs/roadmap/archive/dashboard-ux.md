# Roadmap — dashboard UX: surface what's already built

Status: **complete — archived 2026-09-21** (all 10 tasks landed 2026-07; planned 2026-07-19). Straight to `main`, no feature branches.

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

- [x] **1.1 — `vehicle_state` badge.** Landed: new
  `VehicleStateBadge.tsx` beside `OperatingModeIndicator`, distinct
  alert styling (real CSS class, not just text) for
  `no_data`/`unknown`/absent, all 6 states + absent covered by tests.
- [x] **1.2 — Replay banner.** Landed: new `ReplayBanner.tsx` mounted
  once in `DashboardShell` above `<main>` — verified genuinely shared
  by both drive and workbench modes (a single shell instance swaps
  only `children`, not the banner's mount point), so this holds the
  doc's own explicit safety property: the operator cannot land in a
  mode where an active replay goes unbannered. StatusStrip source
  label switches too. Tests cover active/inactive/absent.

## Phase 2 — Replay control from the UI

**Goal.** Start/stop/inspect runtime replay without an MCP session.

- [x] **2.1 — API wrappers.** Landed: `getReplay`/`startReplay`/
  `stopReplay` in `api/http.ts` over `/control/replay`, token-gated
  like the other control calls.
- [x] **2.2 — `ReplayCard`.** Landed: session picker, start/stop,
  `_replay-*` output path, the 409 refuse-while-driving reason
  rendered verbatim (bypasses the generic error bucket specifically so
  the exact reason text reaches the operator, verified against a real
  409 payload shape). Lives directly in the Sessions panel.

## Phase 3 — Sessions panel (make placeholder 6.13 real)

**Goal.** Session/trip work gets its own home instead of hiding in the
control hub.

- [x] **3.1 — Extract `SessionsPanel.tsx`.** Landed: promoted the
  already-extracted `SessionsCard` (refactoring 3.2, landed earlier
  tonight) to its own top-level panel; `ControlPanel.tsx` no longer
  references it (verified zero duplicate rendering). `nav.ts`'s
  `data-sessions` `placeholder: true` dropped, `Workbench.tsx` wired to
  the real panel.
- [x] **3.2 — Host `ReplayCard` here.** Landed: mounted inside
  `SessionsPanel`, prefilled from the shared session-selection state.

## Phase 4 — Close the anomaly-triage loop

**Goal.** "Why did this fire?" answered inside the dashboard.

- [x] **4.1 — Re-score button.** Landed: new `RescoreControl.tsx` on
  `ModelEvidencePanel`, submits `replay_window` with params verified to
  match `src/mcp_server/tools/replay.py`'s mapping exactly, polls via
  the existing jobs plumbing, renders per-detector fired/score-vs-
  threshold. New `GET /control/jobs/{id}/result` (both dashboard and
  companion, verified reading the identical `<job_id>.replay.json`
  artifact the pre-existing MCP tool already resolves — genuine
  parity, not a divergent result schema).

## Phase 5 — One token entry, not five

**Goal.** Enter the control token once, in the shell.

- [x] **5.1 — Shell-level token.** Landed: new `TokenGate.tsx` mounted
  in the shell header, four-state vocabulary (not needed/required/
  rejected/unlocked). Per-panel `TokenField` removed from
  `ControlPanel.tsx`/`TestSessionPanel.tsx` — verified both still read
  the SAME shared `useControlToken()` instance passed down from
  `App.tsx`, no gating regression.

## Phase 6 — Cleanup

- [x] **6.1 — Delete orphaned `DiagnosticsPanel.tsx`.** Landed:
  confirmed zero references anywhere in `src/dashboard/static/src`
  before deleting (no test file existed).
- [x] **6.2 — README dashboard-section refresh.** Landed: rewritten to
  describe the actual shipped shell and panel set, spot-checked
  against the real `panels/` directory contents.
