# Test-session profiles

Each `*.yaml` (or `*.yml`) file here is one **test profile** offered by the
Test panel (Workbench → Control → Test) and the headless control API
(`GET /test-profiles`). The directory is **scanned per request** — drop in a
new file, refresh the panel, and it appears. No code change, no restart.

A profile is *not* an ordered checklist. It is a capture preset plus an
**unordered palette of event buttons**: on a drive you tap whichever event is
happening; each tap writes one tag (at the click time) to the session's
`tags.jsonl`.

## Schema

```yaml
id: my_profile            # unique across the directory (dupes are skipped)
label: "Human name"       # shown on the picker card
description: "One line."   # optional
protocol: drive            # echoed onto the capture (free-form)
vehicle_state: driving     # cold_start | engine_running_idle | driving
purpose: "what this is"   # default capture purpose (operator can edit)
order: 10                  # optional sort key for the picker (lower = first)
events:                    # at least one; order is presentation only
  - label: "Hard accel"            # button caption
    tag: hard_accel                 # what gets written to tags.jsonl
    kind: event                     # event (instant) | state (phase boundary)
    hint: "Firm accel from a stop"  # optional one-liner under the caption
    advisory_duration_s: 30         # optional; only an ordered walk uses it
```

The schema is strict — an unknown key or a missing required field makes that
**one** file fail to load (it is skipped and the reason is reported in the
panel); every other profile still loads, so a bad hand-edit can't blank the
picker mid-drive.

## Tag vocabulary

Reuse the established drive tags (`cold_start`, `warmup_complete`,
`city_stop_go`, `highway_cruise`, `coasting_dfco`, `wot_pull`,
`grid_2k_light`, …) where they fit, so the existing window/segmentation and
behavior-model tooling keeps working. A new tag string is fine for a genuinely
new event — just keep it stable once analyses reference it.
