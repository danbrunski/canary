# Pi as a self-hosted WiFi access point

For field / in-vehicle use away from any known network: the Pi broadcasts
its own WiFi AP, your laptop or phone joins it, and CANary is reachable at a
fixed address with no router in the loop.

Raspberry Pi OS **Bookworm** uses **NetworkManager** (it replaced `dhcpcd`),
so this is a single `nmcli` profile — no manual `hostapd` / `dnsmasq` /
`dhcpcd` stack. `ipv4.method shared` runs the client DHCP server for you and
pins the Pi at **`10.42.0.1`**.

> The Pi 5 has a **single WiFi radio**: it is either an AP *or* a client,
> not both at once. AP mode therefore has **no internet** — do `apt`,
> updates and time-sync at home (Ethernet or home WiFi), then take it to the
> field. `can0` is independent of WiFi and unaffected either way.

## 1. Set the WiFi country

Skipping this is the most common reason the AP silently never starts:

```bash
sudo raspi-config nonint do_wifi_country US     # your real regulatory domain
```

## 2. Create the hotspot profile

```bash
sudo nmcli connection add type wifi ifname wlan0 con-name canary-ap \
    autoconnect yes ssid CANary
sudo nmcli connection modify canary-ap \
    802-11-wireless.mode ap \
    802-11-wireless.band bg \
    ipv4.method shared \
    wifi-sec.key-mgmt wpa-psk \
    wifi-sec.psk 'choose-a-strong-passphrase'
sudo nmcli connection up canary-ap
```

`band bg` = 2.4 GHz (best range/compatibility in a vehicle; use `band a`
for 5 GHz to trade range for throughput). `ipv4.method shared` →
Pi = `10.42.0.1/24`, clients get `10.42.0.x` from NetworkManager's DHCP.

## 3. Autostart strategy

**A — Always-on AP (recommended for an in-vehicle box).** Leave step 2 as
is and keep no home-WiFi client profile; do home-side dev/updates over
**Ethernet**. Deterministic: the Pi is simply "the CANary box you join".

**B — Home WiFi when in range, AP otherwise.** Matches "only when away":

```bash
sudo nmcli connection modify "<your-home-ssid>" \
    connection.autoconnect yes connection.autoconnect-priority 100
sudo nmcli connection modify canary-ap connection.autoconnect-priority 0
```

Boots onto home WiFi if present, else the AP. Single-radio caveat:
NetworkManager won't preempt an *active* AP when you get home — bring the
Pi back with `sudo nmcli connection up "<your-home-ssid>"` or a reboot.

Verify after a reboot:

```bash
nmcli -t -f NAME,DEVICE,STATE connection show --active
# want: canary-ap:wlan0:activated
```

## 4. Connect from the laptop / phone

1. Join WiFi **CANary** with the passphrase from step 2.
2. Dashboard: `http://10.42.0.1:8000/` (or `http://canary.local:8000/` if
   mDNS resolves; the IP is the reliable fallback).
3. The **control token is still required over the AP** — an AP client is
   not loopback, so the control-plane auth still enforces it here by design
   (see `docs/headless-api.md`). Fetch it once and persist it for
   `canaryctl` exactly as in `docs/truck-test.md`, using `10.42.0.1` as the
   host:

   ```bash
   ssh canary@10.42.0.1 sudo cat /etc/canary/control.token
   ```

   Paste it once into the dashboard too (it persists per-origin in the
   browser).

## The `10.42.0.0/24` anchor

`ipv4.method shared` always uses `10.42.0.0/24` with the Pi at
`10.42.0.1`. That stable subnet is the intended anchor for a future
**trusted-network auto-unlock**: clients on `10.42.0.0/24` (the Pi's own
AP) could be auto-issued the control token so the in-vehicle workflow
needs no hand-pasted token, while off-net access stays token-gated. Until
that lands, the one-time per-device paste above is the workflow. See the
control-auth model in `docs/headless-api.md` (loopback is already
token-free for dev/sim).
