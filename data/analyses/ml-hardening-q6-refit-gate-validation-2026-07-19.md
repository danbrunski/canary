# ml-hardening Q6 — publish-gate validation against real sessions (2026-07-19)

Per `OVERNIGHT-QUEUE.md` Q6's own instruction ("Refit locally against
stored sessions to validate the gates"): ran
`scripts/refit_behavior_models.py --all --dry-run` (no write) against
all 14 usable local sessions after Phase 4 (4.1-4.7) landed.

## Result

```
fitted (5): timing_advance_vs_rpm_load, timing_advance_vs_rpm_iat,
            fuel_trim_vs_rpm_load, air_mass_per_cycle_vs_rpm_load,
            cat_warmup (r²=0.755)

skipped (9):
  coolant_warmup           r_squared=0.252 below min_r_squared=0.6
  idle_rpm_vs_coolant      r_squared=0.003 below min_r_squared=0.6
  stft_bank_balance_vs_rpm r_squared=0.041 below min_r_squared=0.6
  ltft_bank_balance_vs_rpm r_squared=0.003 below min_r_squared=0.6
  injector_pw_vs_rpm_load  missing columns ['did:ECM_INJECTOR_PW_B1']
  injector_pw_warmup       missing columns ['did:ECM_INJECTOR_PW_B1']
  afr_bank_balance_vs_rpm  missing columns ['derived:AFR_BANK_BALANCE']
  afr_voltage_vs_rpm_load  missing columns ['did:ECM_AF_SENSOR_V_B1']
  tps_voltage_per_pct      missing columns ['derived:TPS_VOLTAGE_PER_PCT']
```

## Confirms 4.1's publish gate works on real data, not just synthetic tests

Four recipes (`coolant_warmup`, `idle_rpm_vs_coolant`,
`stft_bank_balance_vs_rpm`, `ltft_bank_balance_vs_rpm`) fit against this
thin 14-session corpus but landed genuinely poor R² (0.003-0.252, well
under the 0.6 floor) — before 4.1, these would have published silently
as live behavior surfaces, generating deviation scores from a fit that
explains essentially none of the underlying variance. The publish gate
refused all four with a clear reason, visible in both the log and the
CLI's skip table. This is 4.1's exact target failure mode, caught for
real.

The other 5 skips are missing-column, not quality — this laptop's local
corpus doesn't poll the injector-PW/AFR/TPS-voltage DIDs `data/dids.yaml`
defines (see `collection.obd_dids` in `canary.yaml`), so those recipes
have nothing to fit against. Unrelated to Phase 4, expected.

No write performed (`--dry-run`) — the real shipped
`data/nissan-xterra-2008/behavior_models.yaml` (9 prior models) was not
touched. A real refit + deploy is a separate, deliberate operator
action, not run here.
