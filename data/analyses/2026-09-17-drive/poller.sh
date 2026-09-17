#!/bin/bash
# /status every 20 s -> JSONL; unreachable samples recorded as {"_ts":..,"_err":..}
source ~/.canary/env 2>/dev/null
OUT=/tmp/claude-1000/-home-dan-code-canary/7909e25d-7a25-47af-a74f-b6ba687a7af9/scratchpad/status.jsonl
while true; do
  TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  R=$(curl -s -m 10 -H "Authorization: Bearer $CANARY_CONTROL_TOKEN" http://192.168.137.2:8000/status)
  if echo "$R" | jq -e .vehicle_state >/dev/null 2>&1; then
    echo "$R" | jq -c --arg ts "$TS" '. + {_ts:$ts}' >> "$OUT"
  else
    echo "{\"_ts\":\"$TS\",\"_err\":\"unreachable\"}" >> "$OUT"
  fi
  sleep 20
done
