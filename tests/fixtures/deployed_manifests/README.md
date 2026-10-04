# Deployed model manifests

A copy of each model manifest the Pi is running, so CI can check that a
catalog edit never strands a deployed model's feature spec
(`tests/test_manifest_producibility.py`, D02.5).

Models are not in git (S03, operator decision 2026-10-04): they live under
`/data/canary/models/<vehicle>/` on the Pi and travel by upload or `scp`. This
directory is the one deliberate record of what is deployed. It is a test
fixture, never written at runtime. **Refresh it whenever a model is deployed or
retrained on the Pi:**

```bash
for m in isolation_forest lstm_autoencoder; do
  scp canary@canary.local:/data/canary/models/nissan-xterra-2008/$m/model.manifest.json \
      tests/fixtures/deployed_manifests/nissan-xterra-2008/$m/
done
```

Layout mirrors the Pi's: `<vehicle>/<model_type>/model.manifest.json`.
Captured 2026-10-04: isolation forest `2026-09-20T054703`, LSTM `2026-09-24T035125`.
