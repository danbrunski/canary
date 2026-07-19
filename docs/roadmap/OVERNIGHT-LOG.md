# Overnight run log

refactoring.md 1.3 — done — single-sourced numeric_value_columns + resample_and_segment in src/preprocessing/loader.py, closed the GAP_SUFFIX drift (commit 4a46509)
refactoring.md 1.4 — done — run_with_reconnect shared across all 4 collectors in src/collection/reconnect.py; GPS now fails fast on ValueError instead of retrying
ml-correctness-hardening.md 1.1+1.2 — done — bounded ffill + gap flags for slow channels in resampler.py, new slow_channel_max_gap_seconds config (commit d5051b2)
