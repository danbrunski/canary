# GRU-D LSTM autoencoder — multi-rate / missing-data handling

## Why

The sequence autoencoder reconstructs dense per-tick windows, but canary's
inputs arrive at **very different rates**: broadcast CAN channels are ~1 Hz,
round-robin OBD PIDs refresh every ~25 s, GPS every ~1 s when it has a fix.
The naive pipeline (forward-fill, then reject any window touching a `__gap`)
collapses to **zero windows** the moment a slow channel is included, so we'd
been *dropping* slow channels — discarding real signal (RPM/throttle ended up
excluded). Forward-fill alone also trains the model to reconstruct fabricated
values.

The established fix (Che et al., *Recurrent Neural Networks for Multivariate
Time Series with Missing Values*, GRU-D) is to **feed the missingness to the
model** instead of hiding it: per channel, supply the value, an observed/stale
**mask**, and **Δt** (time since last real observation), and let the network
**decay** stale inputs and hidden state toward the channel's mean. The
reconstruction loss is **masked** so the model is never scored on fills.

This keeps *every* channel at its native rate, removes channel-dropping and
window-rejection entirely, and turns "this reading is 25 s old" into a signal
rather than a fabrication.

## Inputs (per timestep t, per channel d)

- `x[t,d]` — value, z-scored by the model scaler (so the empirical mean is 0).
  Carried forward (last observed) between observations.
- `m[t,d]` — mask: 1 if channel d was freshly observed at t, else 0.
- `δ[t,d]` — seconds since channel d's last real observation (0 when observed).
  Computed from the mask run: `δ=0` on observed ticks, else `δ_{t-1}+Δt`.

**Batch (training).** The resampler already emits `<channel>__gap`; `m = ¬gap`,
and `δ` is the cumulative time since the last `m=1`. No new capture work.

**Serve (parity — the delicate part).** `LiveFeatureSource` builds rows from
current-value snapshots and never resamples, so it has no gap flags. It must
track, per channel, the **timestamp of the last `observe()`**, and at each tick
emit `m = (now - last_obs ≤ max_gap)` and `δ = now - last_obs`. This must
mirror the resampler's `max_gap` exactly. The mask/Δt derivation is the only
serve-side addition; the model code is shared.

## GRU-D encoder (the decay cell)

Standard GRU recurrence with two learned decays (mean = 0 in scaled space):

```
γ_x[t] = exp(−max(0, w_x ⊙ δ[t] + b_x))          # per-channel input decay
x̂[t]   = m[t]·x[t] + (1−m[t])·γ_x[t]·x_last       # decay last value → 0 (mean)
γ_h[t] = exp(−max(0, W_h · δ[t] + b_h))            # hidden-state decay (vector)
ĥ[t−1] = γ_h[t] ⊙ h[t−1]
h[t]   = GRUCell([x̂[t] ‖ m[t]], ĥ[t−1])           # mask concatenated as input
```

- `w_x, b_x`: length-F vectors (diagonal input decay, per GRU-D).
- `W_h, b_h`: hidden-size decay from δ (project δ→hidden).
- `x_last`: last observed value per channel, updated where `m=1`.
- Implemented as an explicit timestep loop (PyTorch `nn.GRU` can't express
  decay); keep it `torch.jit`-friendly and CPU-fast (Pi budget: 1 Hz ticks).

**Autoencoder shape.** Encoder = GRU-D → final hidden → `latent`. Decoder =
plain GRU (or LSTM) from the repeated latent → `Linear` → reconstructed values
(F wide). Mask/δ are encoder-only inputs; the decoder reconstructs values.

## Loss & scoring

**Masked, observed cells only** — a fabricated fill never contributes.
As shipped (`src/models/lstm_autoencoder.py`): masked **MSE**
`sum(m ⊙ (x̂_recon − x)²) / sum(m)` for the training loss, masked **MAE**
for `reconstruction_error` at serve/scoring. Per-mode error thresholds
are calibrated on the masked scoring metric. (A TCN backbone with the
same `(value, mask, delta)` contract is also available — see
`src/models/tcn.py` and `models.sequence_detector` in config. **9.1
decision (operator-approved):** GRU-D LSTM is the default
`sequence_detector` — its GRU decoder reconstructs step-by-step from the
latent, materially stronger temporal-shape modeling than the TCN decoder's
single broadcast-latent reconstruction (`tcn.py`). TCN stays available and
documented as the NPU-export fallback, not an equal; no further bake-off
is needed to proceed with LSTM as the shipped default.)

## Removed (superseded)

- `feature_pipeline._dense_sequence_columns` density filter — keep all channels.
  (Since 2026-09-26, D03.34: one coverage floor shared with the IF drops a
  channel valued on under half its sessions' grid rows — polled too slowly for
  the 1 Hz grid; masking still handles ordinary staleness.)
- `sequence_dataset` window gap-rejection (`_window_has_gap`) — every window is
  valid; the mask handles staleness. Keep the NaN-on-required-value guard only
  for leading rows before a channel's first observation (impute to 0 = mean).
- The committed placeholder LSTM (52 dense channels) is replaced.

## File-by-file

- `sequence_dataset.py` — build `(value, mask, δ)` tensors per window; drop
  gap-rejection; stop excluding channels; impute leading NaN → 0.
- `lstm_autoencoder.py` — GRU-D encoder cell (decay), 3-input `forward`/
  `reconstruction_error`, masked error; architecture/manifest gains the decay
  params + input layout.
- `lstm_training.py` — masked MSE loss; persist scaler + decay params; per-mode
  thresholds on masked error.
- `feature_pipeline.sequence_feature_columns` — return all trainable
  non-deviation channels (no every-session pruning: masking handles absence;
  keep an all-NaN-everywhere drop). D03.34 (2026-09-26) adds back one floor,
  shared with the IF, for channels too sparse for the 1 Hz grid.
- `live.py` — per-channel last-observed tracking → `m`, `δ` each tick.
- manifest — input layout (F, channel order), `max_gap`, decay tensors so serve
  reconstructs the model exactly.
- tests — cell decay math (γ→1 at δ=0, →0 as δ→∞), masked-loss ignores fills,
  windows form on a multi-rate frame, serve mask/δ matches the resampler.

## Deferred

Bidirectional / attention decoders, learned per-channel means (mean=0 via the
scaler suffices here), and Δt-aware decoders.
