#!/usr/bin/env bash
# Snapshot the Pi daemon every 20s for the whole drive.
# Roadmap: PENDING-HARDWARE H1/H7/H9 + D01-daemon-throughput Phase 3.
source ~/.canary/env
H="Authorization: Bearer $CANARY_CONTROL_TOKEN"
B=http://192.168.137.2:8000
OUT="$1"; END=$((SECONDS + 5400))   # 90 minutes
while [ $SECONDS -lt $END ]; do
  TS=$(date -Is)
  BODY=$(curl -s -m 12 -H "$H" "$B/status" 2>/dev/null)
  if [ -n "$BODY" ]; then
    printf '{"t":"%s","ok":true,"status":%s}\n' "$TS" "$BODY" >> "$OUT"
  else
    printf '{"t":"%s","ok":false}\n' "$TS" >> "$OUT"
  fi
  sleep 20
done
