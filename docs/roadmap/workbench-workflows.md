# Roadmap — workbench navigation by what the operator is doing

Status: **planned** (2026-09-21). Straight to `main`, no feature branches.
Supersedes the grouping in `archive/dashboard-ux.md`, whose ten tasks all landed
(2026-07) — this regroups the pages that work produced.

## Why this exists

The workbench has 28 pages in five groups — Live, Diagnostics, Models, Data,
Control — and every group is a *kind of thing*. The operator's work cuts across
them. One tuning drive on 2026-09-21 touched Control › Capture Profiles (pick a
profile), Control › Power/UPS (arm the keepalive), Control › Control Plane
(start it), Live › ROM Maps (watch), Data › Sessions (find it afterwards), and
then had nowhere to go for analysis, because engine analysis has no page at
all.

Two specific confusions are worth naming:

- **`Models › Tuning` is not engine tuning.** It tunes anomaly-detector
  thresholds and suppression (Task 6.25). Engine tuning — the thing the word
  means to anyone who owns the vehicle — has no home.
- **Nothing reports readiness.** Every hazard on the 2026-09-21 drive was
  knowable beforehand: the UPS sat at 28 % SOC, the truck had been reflashed
  the night before (and the daemon then withheld every RAM channel on the
  revision change — a gate since removed), and the capture
  profile's continuous-with-memory path had never run. None of it was visible
  anywhere an operator looks.

## The five groups

| Group | The question it answers | Pages |
|---|---|---|
| **Observe** | What is the truck doing right now? | Charts, Trip, Alarms, Builder |
| **Engine Tuning** | How is it fuelled and sparked, and what should change? | Logging, Logs, Analysis, ROM Maps, Calibration — see below |
| **Diagnose** | Is something wrong? | Anomalies, Trouble Codes, Monitors, Drift Trends, TPMS, LLM Assist |
| **Model** | How are the detectors doing? | Overview, Behavior, Training, **Alert Thresholds**, Drift / Health, Dataset Health |
| **System** | Plumbing and reusable configuration | Data Sources, Sessions, Export, Custom PIDs, Vehicle Profiles, Capture Profiles, Power / UPS, Control Plane, System |

`Models › Tuning` is renamed **Alert Thresholds** so the word *tuning* means
one thing in this product.

## Engine Tuning: pages, not steps

**These are independent pages, reachable in any order, and none requires having
visited another.** An earlier draft of this document laid them out as a
Prepare → Drive → Review sequence. That was wrong: an operator may only want
to log this drive, only want to look at last week's log, or only want to watch
cell traces against a ROM with no capture running at all. A wizard makes each
of those awkward in order to make one path tidy.

What holds the pages together is **shared context, not sequence**.

### The context bar

One strip across the top of every Engine Tuning page:

```text
2008 Xterra · running ZS44A-20260920 · capture: tuning 00:12:41 · log: <selected> · ROM: <loaded>
```

- **Running calibration** comes from the ECU, not from the operator: service
  `0x21 10` returns the calibration id as ASCII (`1ZS44A00000`). Shown beside
  the registry's newest flash, with a mismatch called out.
- **Selected log** and **loaded ROM** persist across the group's pages, so
  Logs → Analysis → ROM Maps keeps the same subject without re-picking it.
- **Readiness lives here as status, not as a gate**: UPS SOC, storage
  headroom, memory-channel verify results, capture-profile rates
  achieved. Red items say what fixes them. Nothing blocks navigation.

### Pages

**Logging** — start and stop a capture. Profile picker with each profile's
channel list and requested rates; once running, per-channel achieved rate,
timeouts and NRCs.
The daemon records with the configurable `default` profile between captures.
Capture Profiles under System owns profile creation and editing; Logging only
selects and runs a saved profile. Stopping a capture closes its isolated log
and restores `default` collection.
Tap-to-tag for physical actions (pull start/end, steady hold). Arming the UPS
keepalive is a control on this page rather than a trip to Power / UPS.
A capture ending for any reason other than the operator stopping it — a daemon
restart, above all — is announced here, since it currently is not announced
anywhere.

**Logs** — browse captures and open one. Two presentations of the same data,
toggled, never two pages:

- *Table* — channels as columns on one clock, sortable, filterable by regime
  (closed loop / enrichment / fuel cut / idle).
- *Visual* — time-series strips, and scatter or heat map of any channel
  against any two axes.

Each log shows its tune revision and says when a revision boundary falls inside
it.

**Analysis** — derived results, which unlike a log view may pool many sessions.
Delivery error by rpm band with its noise floor drawn in, the closed-loop trim
surface (STFT × LTFT) by rpm × airflow, final timing per cell, and pull-vs-pull
comparison across tune revisions. Every figure carries its sample count and its
gate report, and an unavailable result says *which kind* of unavailable — "the
gates removed everything" and "the channel was never captured" are different
answers.

**ROM Maps** — load a ROM, view its tables, and watch the live cell trace. This
page exists today under Live › ROM Maps and moves here. Two changes:

- The trace is currently an *inferred geometric lookup* from RPM and BFS. The
  ECM's own coordinates are now readable — `ECM_FUEL_TARGET_MAP_X_TRACE`,
  `ECM_CAM_MAP_RPM_TRACE`, `ECM_THROTTLE_MAP_X/Y_TRACE`, and rpm from
  `ECM_ENGINE_SPEED_RAM` — so the trace should show the cell the ECM says it
  is reading, with the inferred lookup kept as a cross-check and the
  disagreement displayed when they differ.
- Tracing needs those channels polled. When they are not, the page says so and
  offers the one action that fixes it (start a capture that includes them)
  instead of showing a trace that silently is not moving.

A coverage overlay — which cells this log visited, and how many samples each
holds — belongs here too, against either the live capture or the selected log.

**Calibration** — the flash history (`tune_revisions.yaml`), which revision is
running, and the evidence behind every memory channel: its last known-answer
verify and the revision it ran on. Channels are not gated on the revision (RAM
survives calibration flashes); this is where a wrong address shows up. Hosts
the re-verify action (`tools.verify_memory_channels --record-revision`).

### Reserved: ROM Editor and Flash

Two further pages are anticipated and the navigation should leave room for
them, shown as *planned* rather than hidden:

- **ROM Editor** — edit table cells in the loaded ROM and export a modified
  file. The offline Fuel Targets tooling (`docs/offline-rom-targets.md`) is the
  start of this.
- **Flash** — program a ROM into the ECU.

**The policy for Flash is settled (operator decision 2026-09-21), and the page
is where it becomes impossible to get wrong rather than merely documented.**
AGENTS.md sets two conditions on any write to the ECU's non-volatile memory,
and the Flash page enforces both itself instead of relying on whoever is
driving it:

- **Explicit permission, per operation.** A flash is started by a deliberate
  operator action on this page that names the ROM file and its hash. There is
  no API shortcut that skips the confirmation, no "flash on save" from the ROM
  Editor, and no agent-initiated path: an agent may prepare a ROM and stop.
- **Key on, engine off, stationary.** The control is disabled unless
  `vehicle_state == ignition_only`, and says why when it is not. The check is
  repeated server-side at the moment of the write, because the state that
  matters is the state when the first block is sent, not when the page
  loaded. `engine_running` refuses; so does any state that cannot be
  classified.

Around those: refuse on low supply voltage and on a UPS that could not carry
the Pi through the whole operation (28 % SOC on 2026-09-21 would have
refused); verify the image hash before starting; read back and compare
afterwards; record the result in `tune_revisions.yaml` so the running
calibration and the memory-channel stamps follow automatically. The ROM
Editor's output stays a file — producing a ROM and flashing one are separate
acts on separate pages.

## Principles

- **Pages stand alone.** No page assumes another was visited first.
- **Context is shared, not sequenced.** Selections persist across a group.
- **An empty state names its cause and offers its fix.** "Traces are not being
  polled — start a capture with a profile that carries them" beats a blank panel.
- **Readiness is status, never a gate.** The operator may always proceed.
- **One word, one meaning.** *Tuning* is the engine. Detectors have
  *thresholds*.

## Tasks

Each task below has a detailed implementation checklist under
[`tasks/`](tasks/W00-workbench-README.md) — files, symbols, tests, empty states
and acceptance. The checkboxes *there* are task-level truth; the list here is
the index.


- [x] **[W1](tasks/archive/W01-navigation-regroup.md) — Regroup the navigation.** Five groups as tabled above; rename
  `models-tuning` → Alert Thresholds; move `live-rom-maps` into Engine Tuning.
  Keep the old route ids resolving (redirect) so bookmarks and docs survive.
- [~] **[W2](tasks/archive/W02-tuning-context-bar.md) — Context bar.** Running calibration from `0x21 10` beside the
  registry's newest revision; selected log and loaded ROM held in group state;
  readiness chips (UPS SOC, storage, memory channels, achieved rates).
- [x] **[W3](tasks/archive/W03-logging-page.md) — Logging page.** Profile picker, start/stop, keepalive, per-channel
  achieved rate / timeouts / NRC, tap-to-tag.
- [x] **[W4](tasks/archive/W03-logging-page.md) — Announce an ended capture.** Detect "a capture was active and now
  is not, and the operator did not stop it" (daemon restart included) and say
  so on Logging and in the status strip. Needs a small backend fact: the last
  capture's end reason.
- [x] **[W5](tasks/archive/W04-logs-and-analysis.md) — Logs page.** Capture browser; table and visual presentations of
  one log; regime filter; tune-revision boundary marker.
- [x] **[W6](tasks/archive/W04-logs-and-analysis.md) — Analysis page.** Delivery error, trim surface, timing per cell,
  pull comparison — each with sample counts, gate report, and a named reason
  when unavailable. Backed by the existing `src/analysis` functions, which
  already take a session directory.
- [~] **[W7](tasks/W05-rom-maps-trace-coverage.md) — ROM Maps: trace from the ECM's own coordinates.** Use the map-trace
  memory channels; keep the inferred lookup as a displayed cross-check; name
  the fix when the channels are not being polled.
- [~] **[W8](tasks/W05-rom-maps-trace-coverage.md) — ROM Maps: coverage overlay.** Cells visited and samples per cell,
  for the live capture or the selected log.
- [x] **[W9](tasks/archive/W06-calibration-page.md) — Calibration page.** Flash history, running revision, per-channel
  known-answer verify state, and the re-verify action.
- [x] **W10 — Reserve ROM Editor and Flash** as planned entries.
- [x] **[W11](tasks/archive/W07-flash-interlocks.md) — Flash interlocks before any flash capability.** The two AGENTS.md
  conditions as code, server-side: a per-operation confirmation that names the
  image hash, and a `vehicle_state == ignition_only` check at the moment of
  the write. Plus supply-voltage and UPS-SOC floors. These land, with tests,
  *before* anything that can program a module, so the capability never exists
  without them.

Backend facts these need that do not exist yet: the calibration id read
(`0x21 10`) surfaced in `/status` (W2, W9); the last capture's end reason (W4);
per-cell residency from a log (W8). Everything else is already served.
