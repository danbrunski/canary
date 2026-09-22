# Behavior artifact inventory (B03.1) — 2026-09-21

Offline inventory of the served behavior-model set, the policy that now
governs it, and everything that depends on it. Regenerate with:

```
.venv/bin/python tools/inventory_behavior_artifacts.py \
    --config canary.dev.example.yaml --profile nissan-xterra-2008 [--json]
```

## Deployed identity: unknown

The Pi was not contacted (offline session; the tool never contacts it), so
`/status.vehicle_state` was not read and the deployed artifact is **unknown**.
It is not assumed equal to the local one — the 2026-06 overhaul deploy left the Pi on
stale binaries once already. When the Pi is reachable, read
`/status.vehicle_state` and any active capture first, save
`GET /models/behavior` and pass it as `--deployed-snapshot`: models are
compared by `artifact.fingerprint`, or by `version`/`fit.fitted_at` for a
daemon older than B03.8 (which is labelled as such). Owed under B03.9.

## Local artifact

| Item | Identity |
|---|---|
| Artifact | `data/nissan-xterra-2008/behavior_models.yaml`, sha256 `9cd174ce0388d73f…`, schema 1, last changed `b9356bd` (2026-09-18, DID renames), tree `39139df` |
| Policy | `data/nissan-xterra-2008/behavior_policy.yaml` (new, B03.2/B03.3), sha256 `fb72e9478113cf4f…` |
| Validation criteria | `data/nissan-xterra-2008/behavior_validation.yaml`, sha256 `eadf6f92777557fc…` |
| Calibration in force | `ZS44A-20260920` per `tune_revisions.yaml` (sha256 `63154293bc5c995d…`). **No shipped model records a `tune_revision`** — all eight predate the tune registry, so none has calibration provenance. |
| Accepted-baseline overrides | none (`accepted_baselines.yaml` has zero overrides) |
| Config | Pi runs `canary.pi.example.yaml` vocabulary (`approved_operating_modes` in the validation file); the inventory records whichever `--config` it was run with |

| Model | v | Fitted | Lifecycle | Permitted uses | Evidence |
|---|---|---|---|---|---|
| `air_charge_vs_rpm_throttle_did` | 1 | 2026-08-07 | unvalidated | display, learned_feature | held-out rows, no B02 gate evidence |
| `warm_idle_rpm_baseline` | 2 | 2026-07-27 | unvalidated | display, learned_feature | held-out rows, no B02 gate evidence |
| `timing_advance_vs_rpm_load` | 2 | 2026-07-27 | **exploratory** | display | held-out rows, no B02 gate evidence |
| `timing_advance_vs_rpm_iat` | 2 | 2026-07-27 | **demoted** | display | held-out rows, no B02 gate evidence |
| `fuel_trim_vs_rpm_load` | 2 | 2026-07-27 | unvalidated | display, learned_feature | held-out rows, no B02 gate evidence |
| `air_mass_per_cycle_vs_rpm_load` | 2 | 2026-07-27 | unvalidated | display, learned_feature | held-out rows, no B02 gate evidence |
| `ve_vs_rpm_load` | 2 | 2026-07-27 | unvalidated | display, learned_feature | held-out rows, no B02 gate evidence |
| `cat_warmup` | 4 | 2026-05-25 | **quarantined** (artifact `sha256:f14450ab…`) | display | none, no held-out rows |

All eight are schema 1 without a serving contract, so none alerts regardless of
the policy (B01.6); the policy makes the three review decisions hold after they
are refit.

## Dependent detectors

| Manifest | Version | Features | `deviation:*` consumed |
|---|---|---|---|
| `models/nissan-xterra-2008/isolation_forest/model.manifest.json` | `isolation_forest_2026-09-20T054703` | 295 | none (`behavior_model_columns: []`) |
| `models/nissan-xterra-2008/lstm_autoencoder/model.manifest.json` | `lstm_autoencoder_2026-09-20T064628` | 361 | none (sequence selection drops the whole family) |
| `models/_bakeoff/*`, `models/_overnight-1.6-*` (unscoped, not served) | 2026-05/07 | 224–296 | none |

No tracked IF/sequence artifact consumes a behavior deviation column, so the
demotions strand nothing (pinned by
`tests/test_behavior_lifecycle.py::test_tracked_manifests_consume_no_demoted_behavior_column`).
Manifests do not record which behavior artifact produced a consumed deviation
column, so a future consumer's column meaning can only be checked by
`behavior_feature_conflicts` (policy) and producibility (presence), not by
artifact identity — coordinate any such retrain with D02.5/D03.25.

## Consumers of the behavior artifact

Every Python consumer loads through `src.decode.behavior_models.load_behavior_models`,
which now applies the profile policy — no caller can read the artifact without it.

- **Daemon:** `src/daemon/inference.py` (start-up load, `_observe_behavior`,
  `BehaviorDetector` — emits/fuses only `alert`-permitted models),
  `src/daemon/polling.py::poll_behavior_models` (hot reload; policy file is part
  of the reload stamp), `src/daemon/components.py`/`server.py` (wiring),
  `src/preprocessing/live.py` (live operating point).
- **Preprocessing / training:** `feature_pipeline.build_feature_frame` and
  `featurize_wide_frame` → `behavior_decoder.append_deviation_columns` (produces
  every `deviation:*` column), `scripts/train_anomaly_model.py` (IF selection
  drops `learned_feature`-withheld columns, recorded as
  `behavior_learned_feature_withheld` on the spec), sequence trainers (drop all
  deviations), `src/models/spec_producibility.py` +
  `tools/check_manifest_producibility.py` (presence + policy conflicts),
  `src/collection/capture_profiles.py` (required input channels).
- **Analysis:** `src/analysis/refit.py` + `scripts/refit_behavior_models.py`
  (carry-over, report, prior preservation), `src/analysis/behavior_validation.py`
  + `scripts/evaluate_behavior_models.py`, `src/analysis/session_deviations.py`
  + `scripts/analyze_session.py`, `src/analysis/fingerprint.py` (deviation
  trends), `scripts/replay.py`.
- **HTTP API:** `GET /models/behavior` (`src/dashboard/readers.py`),
  `GET /models/behavior/live` (`src/dashboard/behavior_live.py`), both via
  `src/dashboard/routes/public.py`.
- **MCP:** `get_behavior_models`, `get_behavior_live` (daemon passthrough),
  `get_behavior_deviations` (local, `session_deviations`), resource
  `canary://behavior-models` (`src/mcp_server/resources.py`).
- **Dashboard:** `hooks/useBehaviorModels.ts`, `hooks/useBehaviorLive.ts`,
  `panels/BehaviorModelsPanel.tsx` (Model › Behavior),
  `panels/OperatingModeIndicator.tsx`.
