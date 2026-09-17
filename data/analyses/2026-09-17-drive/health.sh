#!/bin/bash
# Summarize last N minutes (default 10) of status.jsonl
MIN=${1:-10}
F=/tmp/claude-1000/-home-dan-code-canary/7909e25d-7a25-47af-a74f-b6ba687a7af9/scratchpad/status.jsonl
SINCE=$(date -u -d "-$MIN min" +%Y-%m-%dT%H:%M:%SZ)
jq -s -c --arg since "$SINCE" '
  map(select(._ts >= $since)) as $w
  | ($w|map(select(._err))|length) as $unreach
  | ($w|map(select(._err|not))) as $ok
  | if ($ok|length)==0 then {window_min:'"$MIN"', samples:0, unreachable:$unreach} else
  ($ok[0]) as $a | ($ok[-1]) as $b
  | {
    now: $b._ts[11:19],
    samples: ($ok|length), unreachable: $unreach,
    states: ($ok|map(.vehicle_state.state)|group_by(.)|map({(.[0]):length})|add),
    rpm: $b.vehicle_state.rpm,
    storage_q_max: ($ok|map(.health.telemetry_sinks.storage.queue_depth)|max),
    dropped_total: ($b.health.telemetry_sinks|[.[]|.dropped_records // 0]|add),
    dropped_delta: (($b.health.telemetry_sinks|[.[]|.dropped_records // 0]|add) - ($a.health.telemetry_sinks|[.[]|.dropped_records // 0]|add)),
    can0_since_start: $b.health.can0.since_start,
    missed_ticks_delta: ($b.detectors.inference.missed_ticks - $a.detectors.inference.missed_ticks),
    ticks_delta: ($b.detectors.inference.ticks_scheduled - $a.detectors.inference.ticks_scheduled),
    anomalies_emitted_delta: ($b.emitter_counters.emitted - $a.emitter_counters.emitted),
    obd_resp_rate_window: ((([$b.health.obd_scheduler.by_tier[].responses]|add) - ([$a.health.obd_scheduler.by_tier[].responses]|add)) as $r | (([$b.health.obd_scheduler.by_tier[].requests_sent]|add) - ([$a.health.obd_scheduler.by_tier[].requests_sent]|add)) as $s | if $s>0 then ($r/$s*1000|round/1000) else null end),
    obd_req_per_s: ((($b.health.obd_scheduler.requests_sent - $a.health.obd_scheduler.requests_sent)) as $s | ((($b._ts|fromdate)-($a._ts|fromdate))) as $dt | if $dt>0 then ($s/$dt*100|round/100) else null end),
    obd_timeouts_delta: ($b.health.obd_scheduler.request_timeouts - $a.health.obd_scheduler.request_timeouts),
    profile: $b.capture_profile.name,
    counters_reset: (($b.detectors.inference.ticks_scheduled < $a.detectors.inference.ticks_scheduled) or ($b.health.obd_scheduler.requests_sent < $a.health.obd_scheduler.requests_sent)),
    load_per_cpu: $b.health.system.load_per_cpu, cpu_temp: $b.health.system.cpu_temp_c, mem: $b.health.system.mem_used_fraction
  } end' "$F"
