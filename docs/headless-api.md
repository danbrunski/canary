# Headless API

The Raspberry Pi runs one CANary daemon. That daemon collects telemetry,
writes Parquet, runs model polling/inference, and serves the read-only HTTP
and WebSocket API used by the browser dashboard.

There is no authentication in v1. Bind it only to loopback, a trusted LAN, or
the vehicle hotspot.

## Browser

If the dashboard frontend has been built, open:

```bash
http://canary.local:8000/
```

If the frontend has not been built yet, the API still works.

## REST

```bash
curl http://canary.local:8000/status
curl http://canary.local:8000/config
curl 'http://canary.local:8000/telemetry/latest?n=50'
curl 'http://canary.local:8000/anomalies?since=2026-05-16T00:00:00Z'
curl http://canary.local:8000/bindings
curl http://canary.local:8000/models/behavior
```

Endpoint summary:

| Endpoint | Purpose |
| --- | --- |
| `GET /status` | storage path/free space, latest session, loaded model versions, vehicle, anomaly counters |
| `GET /config` | JSON view of the loaded `canary.yaml` |
| `GET /telemetry/latest?n=N` | newest `N` telemetry rows from disk |
| `GET /anomalies?since=...` | persisted anomaly events from JSONL |
| `GET /bindings` | decoded CAN binding metadata for display |
| `GET /models/behavior` | behavior-model surfaces for dashboard panels |

## WebSockets

Live telemetry and anomalies stream over WebSockets:

```bash
websocat ws://canary.local:8000/ws/telemetry
websocat ws://canary.local:8000/ws/anomalies
```

The dashboard uses the same streams. Slow clients drop old messages rather
than blocking collection.

## Laptop Development

For backend-only work, SSH into the Pi and use systemd:

```bash
ssh <pi-user>@canary.local
sudo journalctl -u canary -f
sudo systemctl restart canary
```

For local frontend development from a laptop, forward the Pi backend to local
port 8000 and run Vite locally:

```bash
ssh -L 8000:127.0.0.1:8000 <pi-user>@canary.local
cd src/dashboard/static
pnpm dev
```

Vite proxies `/status`, `/config`, `/telemetry`, `/anomalies`, and `/ws/*` to
`127.0.0.1:8000`, so the laptop browser talks to the Pi through the SSH tunnel.
