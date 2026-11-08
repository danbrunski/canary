# B — behavior-model accuracy and consolidation

Written 2026-09-21 from the
[accuracy review](../../../data/analyses/2026-09-21-behavior-model-review.md).
These are implementation checklists; the review holds the measurements and
their limits. All work starts unchecked. Mark `[x]` only with linked evidence;
use `[~]` when code is complete but named validation or deployment remains.

| File | Scope | Dependencies | Vehicle/Pi work |
|---|---|---|---|
| [B01](archive/B01-behavior-serving-contracts.md) ✅ archived | Derivative ordering and shared eligibility | None | Final serving check in B03 |
| [B02](archive/B02-behavior-validation.md) | Reproducible evaluation, grouped holdouts, publication gates | Evaluator can start now; parity after B01 | New data only where coverage is missing |
| [B03](archive/B03-behavior-lifecycle-and-refit.md) | Demotions, compatibility, baseline refit, rollout | B01/B02 for publication; inventory now | Deployment and controlled replay |
| [B04](archive/B04-timing-and-airflow.md) | Useful timing resolution and airflow consolidation | B01/B02; B03 artifact policy | Matched operating-condition evidence |
| [B05](B05-trim-cam-and-tps.md) | Activate existing trim, bank-balance, cam and TPS recipes | B01/B02; B03 artifact policy | Missing captures and final live check |
| [B06](B06-lambda-delivery-error.md) | Target-versus-measured lambda residual | B01/B02; B03 artifact policy | Steady enrichment/lag evidence if absent |
| [B07](B07-warmup-and-idle.md) | Validate warmup and retain useful idle baseline | B01/B02; B03 artifact policy | Independent cold starts/accessory states |
| [B08](B08-firmware-informed-behavior.md) | Firmware-derived context, throttle-fill/MAF comparison, timing and Fuel Comp intervention | B03–B06; E02/E05 computations; E01/E04 evidence and Pi runtime | Independent post-intervention drives and Pi verification |

**2026-10-04 update:** decoded application logic supplies a throttle-derived
fill coordinate distinct from the MAF path, corrected Fuel Comp/timing axes
and explicit ECU branch/slot state. B08 records their proposed use in the
existing recipes and second brain, with acceptance before changing model
status. The October 4 Fuel Comp restoration supplies a pre-recorded
intervention prediction; its outcome remains to be evaluated.

## Implementation order and decisions

Start B01 and the B02 evaluator; inventory artifacts/consumers under B03.
Land enforceable serving and validation contracts before publishing new fits.
Then demote the unsupported alerts and publish the qualified baseline under
B03. B04–B07 can subsequently deliver individual qualified improvements;
each uses B03's rollout checklist. Do not wait for every optional model to
qualify before fixing the current defects. No task here changes queue order
in `OVERNIGHT-QUEUE.md` or initiates ECU programming.

Carry the review's decisions forward:

- Keep air charge and warm idle, subject to explicit validation scope.
- Remove RPM/IAT timing from standalone alerting; keep IAT as possible context
  for a load-aware timing model. Quarantine the current catalyst artifact.
- Keep RPM/load timing available for exploration while improving resolution.
- Replace STFT-only diagnosis with validated total trim and bank balance.
- Air-mass retirement is **conditional** on same-row ablation and fault
  sensitivity; one quieter high-altitude drive is not sufficient evidence.
- Existing cam/TPS/trim/warmup recipes are starting points, not already
  validated models. Lambda delivery error reuses the existing analysis path.

## Shared acceptance rules

- Use vehicle-profile data for channel identities, units, calibration
  boundaries, thresholds and measured noise floors. Never hard-code the
  development vehicle into shared model code.
- Reproduce a known answer before a novel result. Raw signal comparisons use
  `compare_channels` with acquisition-time pairing and per-sample ratio checks
  where meaningful. Model residual evaluation must identify its sampling and
  eligibility rules; resampled model rows do not prove signal identity.
- Separate input absence, stale inputs, unknown context, off-map coverage,
  insufficient independent evidence, failed validation and actual deviation.
  An ineligible observation is not an `ok` measurement or a zero residual.
- Per-row band exceedance is not an emitted event or a false positive. Report
  event persistence/deduplication separately; reserve precision/recall claims
  for labelled or explicitly synthetic faults. Do not widen bands just to pass.
- Keep physical-drive independence and tune provenance. Use
  `session_started_at`/`latest_session`, never directory-name ordering.
  Model applicability across a calibration change needs evidence; RAM-address
  verification provenance is a separate issue and must not withhold reads.
- Run analysis on the laptop. Isolate candidate outputs and replay data under
  `_` paths; preserve capture recipe allowlists and historical artifacts.
- Before implementation commits run the whole-repo venv ruff lint/format,
  mypy and full pytest gate. UI changes also require dashboard `pnpm test`
  and `pnpm typecheck`. Targeted tests are sufficient during iteration.
  Work on `main`, preserve concurrent edits, commit explicit paths only.
- Read `/status.vehicle_state` before interpreting live results. Use known
  recordings for controlled replay and `scripts/restart_canary.sh` for any
  required restart; do not terminate an unrelated capture.

## Existing task ownership

| Existing item | Relationship |
|---|---|
| D03.22 | Regenerated mode labels are a prerequisite for the B03 corpus; do not duplicate the relabel implementation. |
| D02.5 / D03.25 | Coordinate IF/sequence feature compatibility and their retrain with behavior changes; B-series completion does not close unrelated detector debt. |
| D03.20 | B04 owns behavior-surface altitude conditioning; D03 retains any IF/sequence altitude work. |
| W02 / W06 | Reuse calibration/readiness and provenance surfaces where available; B-series does not depend on completing dashboard navigation. |

Update the relevant D03 checkboxes only when their own acceptance conditions
are met, linking B-series evidence. Historical review results are evidence for
the need for these tasks, not completion of them.
