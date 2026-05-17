# Truck Test — Headless Control Plane

In-vehicle quickstart for driving a capture from a phone/laptop via the
control API. Assumes `sudo bash scripts/deploy_truck.sh` has been run (Pi
boots with `canary.service` + `can0.service` enabled and a control token).

Full API reference: `docs/headless-api.md`. SocketCAN details:
`docs/can-setup.md`.

## Capture paths — which one

There are two recorders; the truck test uses the **control plane**:

- **Control plane (this doc):** `canary.service` is always collecting to
  `/data/canary/sessions`. `canaryctl capture start/stop` marks **labeled
  windows** on that live session — no second process, no CAN contention.
  Drive it remotely from a phone. This is the in-vehicle path.
- **Standalone recorder** (`scripts/record.py`, `docs/capture-runbook.md`):
  a separate one-shot recording process. Use on the bench / for the RE
  workflow, **not** alongside the running daemon (they'd contend for can0).

## One-time, on the laptop/phone

Persist host + token once into `~/.canary/env` so every shell (and a
flaky-signal reconnect mid-drive) just works — `canaryctl` reads it when
the vars aren't already exported:

```bash
# Pi IP: check your router, or `ping canary.local`, or on the Pi: hostname -I
mkdir -p ~/.canary && chmod 700 ~/.canary
cat > ~/.canary/env <<EOF
CANARY_HOST=http://<pi-ip>:8000
CANARY_TOKEN=$(ssh canary@<pi-ip> sudo cat /etc/canary/control.token)
EOF
chmod 600 ~/.canary/env
scripts/canaryctl.py version          # sanity: reaches the daemon
```

(`export CANARY_HOST=... CANARY_TOKEN=...` still works and overrides the
file; an explicit `--host/--token` flag overrides both.)

(`canaryctl.py` is in the repo; clone it on the laptop or `scp` it.)

## Before pulling out — go/no-go

Plug the Canable into the OBD-II port, engine running, then:

```bash
ip -details -brief link show can0     # on the Pi: expect UP, can 500000
scripts/canaryctl.py preflight        # from the laptop
```

`preflight` is GO when `can_interface`, `control_token`, `storage`, and
`active_session_writable` are ok. `can0` missing ⇒ Canable not seated /
not enumerated; `latest_telemetry` empty right after boot is fine — it
fills once frames flow.

## The drive

```bash
scripts/canaryctl.py capture start --purpose "highway pull" \
    --protocol drive --vehicle-state driving

# Tag events live as you drive (each appends to the session's tags.jsonl):
scripts/canaryctl.py tag cold_start
scripts/canaryctl.py tag highway_cruise --field speed_target=70
scripts/canaryctl.py tag wot_pull
scripts/canaryctl.py capture current        # confirm it's still recording

scripts/canaryctl.py capture stop --reason "arrived"
```

## After

```bash
scripts/canaryctl.py sessions list
scripts/canaryctl.py sessions pin <session_id> --reason "training candidate"
scripts/canaryctl.py sessions export <session_id> --out drive.zip
```

Pin keeps a good drive from being aged out by retention; export pulls it
to the laptop for training. Map the drive backlog (cold-start, city,
highway, load-sweep …) onto `docs/data-collection-plan.md`.

## If something looks wrong

```bash
ssh canary@<pi-ip>
systemctl is-active canary                 # expect: active
sudo journalctl -u canary -f               # live daemon log
sudo scripts/canup.sh                       # force can0 back up by hand
sudo systemctl restart canary               # last resort
```

A mid-drive USB re-enumeration self-heals: `can0.service` (udev) raises the
link and the daemon's collector auto-reconnects onto it — the capture
window keeps running.
