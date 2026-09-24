# Clock robustness and GPS time sync

Telemetry is only as trustworthy as its timestamps. This doc covers why the
Pi's clock was wrong, and the gpsd + chrony stack that fixes it.

## The problem

The Pi 5 has an RTC, but it only keeps time across a power cut if a coin cell
is fitted to the RTC header. The X1202 UPS does **not** back it up — it's a
*system* UPS that holds the 5V rail up during input loss, wired nowhere near
the RTC.

With no cell, a cold boot goes like this (real journal, 2026-07-19):

```
14:26:52  kernel: rpi-rtc ... setting system clock to 1970-01-01T00:00:14 UTC
14:27:04  systemd: Started canary.service          ← recording begins
15:51:06  systemd-timesyncd: Initial clock synchronization
```

1. The RTC reads back epoch (1970), because nothing kept it running.
2. systemd bumps the clock to the mtime of
   `/var/lib/systemd/timesync/clock` — the last known good time. That is
   *stale by however long the Pi was off*: 65 minutes in the case above, but
   unbounded in principle. (Note this is systemd's own seed file; `fake-hwclock`
   is not installed on this image.)
3. canary starts 12 seconds later and records against that stale clock.
4. When the network reappears at home, NTP steps the clock forward — **mid
   session**. One drive ends up fractured across a discontinuity and misdated.

The tell that a jump happened: `uptime -s` disagrees with the timestamps on
early-boot journal entries, because the realtime↔monotonic offset moved
underneath them.

Away from home there is no network at all, so without an offline time source
this repeats on every drive.

## The fix: gpsd owns the receiver, chrony disciplines the clock

Satellites are an atomic clock in the sky, and we already have a receiver on
the vehicle. The standard Linux stack turns that into system time with no
custom code:

```
GPS receiver ──serial──> gpsd ──SHM unit 0──> chrony ──> system clock
                          └────socket:2947───> canary (gps:* telemetry)
```

- **gpsd** owns the serial port and publishes GPS time into a shared-memory
  segment.
- **chrony** consumes that SHM segment as a refclock, so the clock is correct
  within a minute or two of boot, with no network.
- **canary** reads fixes from gpsd's JSON socket instead of the serial port.

`scripts/install_pi.sh` installs and configures all of this. Set
`CANARY_INSTALL_GPS_STACK=0` to skip it, or `CANARY_GPS_DEVICE=/dev/serial/by-id/...`
to name the receiver when autodetection can't (more than one serial device).
On a CANary-HAT (the firmware reports it in `/proc/device-tree/hat/product`) the
board's own MAX-M10M on `/dev/ttyAMA2` is chosen automatically, and the udev rule
matches it by kernel name — a device-tree UART has no `by-id` symlink. Its
TIMEPULSE is `pps-gpio` on GPIO18 (a `/dev/ppsN` named `canary-pps`); feeding
that to chrony as a PPS refclock is not done yet.

Re-running the installer (every `canary-update` that touches it does) restarts
gpsd only when `/etc/default/gpsd`, `70-canary-gps.rules` or
`canary-gpsd@.service` changed, and chrony only when its drop-in changed or the `confdir` include was added —
otherwise it prints `unchanged: gpsd not restarted`, so an update no longer
costs the daemon a GPS reconnect. It reads `hardware.gps.source` from the
config through the project's own loader and prints the "set
`hardware.gps.source: gpsd`" hint only when the config says something else.

### Why canary had to change

A serial device has exactly one owner. Once gpsd holds the port, canary cannot
read it — so the GPS backend and offline time are the same switch. Hence:

```yaml
hardware:
  gps:
    enabled: true
    source: gpsd     # or `serial` for the direct-NMEA path
```

`source: serial` remains first-class (it's what WSL dev and any GPS-without-gpsd
setup uses) and defaults on, so existing configs are unaffected. Both backends
build the same `GpsReading` and share `reading_to_records`, so the emitted
`gps:*` channels are identical by construction rather than by convention.

Two mappings in `gps_gpsd.update_reading_gpsd` are deliberately *not*
passthroughs, and both would be silent bugs if simplified:

- **`mode` → `fix_quality`.** gpsd's `mode` is `0/1 = no fix, 2 = 2D, 3 = 3D`.
  The NMEA path takes `fix_quality` from GGA, where `0 = no fix` and anything
  positive is a fix — and `gps_tracker` derives `has_fix` from `fix_quality > 0`.
  Copying `mode` across would make an unfixed receiver (`mode=1`) display a fix.
  We collapse to the GGA convention instead.
- **`uSat`, not `nSat`.** `num_satellites` must mean satellites *used in the
  fix*, matching GGA. The sensor-committee gate (`gps:num_satellites >= 7`) and
  the speed-trust check are tuned against that meaning; seen-count runs higher
  on a marginal fix and would re-open the parked-truck speed-jitter leak those
  gates exist to close.

Also note `SKY` (fix health) and `TPV` (position/velocity) arrive on independent
cadences, so satellite count and HDOP persist across TPV epochs rather than
resetting — they gate whether `gps:speed` is emitted at all.

### chrony configuration

Config lands in `/etc/chrony/conf.d/canary-gps.conf` rather than by rewriting
`chrony.conf`, which belongs to the admin:

```
refclock SHM 0 refid GPS precision 5e-1 offset 0.0
makestep 1 -1
```

`precision 5e-1` is deliberately loose. NMEA time has no PPS pulse to align to,
so it arrives a few hundred milliseconds late — measured ~355 ms on this
receiver at 9600 baud. An optimistic `1e-1` asserts an accuracy the source
can't meet, and chrony responds by comparing it against network NTP, finding it
inconsistent, and marking GPS a falseticker (`#x` in `chronyc sources`) — which
disables precisely the source we need when there is no network. At `5e-1`
chrony accepts it (`#-`: valid, unselected only because network NTP is
tighter), and half a second is still three orders of magnitude better than the
drift being corrected.

`makestep 1 -1` steps the clock by any offset at any time, rather than slewing.
Slewing a 65-minute error would take days, and the failure being fixed *is* a
large correction landing mid-drive. Nothing here is safety-critical or
ordering-sensitive with respect to wall clock, so stepping is both correct and
the safer choice.

`GPSD_OPTIONS="-n"` in `/etc/default/gpsd` is load-bearing: without it gpsd
won't open the receiver until a client connects, so chrony would have no time
source during early boot — precisely when the clock is wrong.

## Plug order doesn't matter (and why `DEVICES` is empty)

gpsd opens its device list once at startup. If the receiver isn't plugged in
yet it logs `device activation failed, freeing device` and *never retries* — so
a receiver attached after boot used to stay dark until someone restarted gpsd
by hand, with the dashboard reporting "no GPS configured."

Attachment is therefore driven by udev, not by static config:

- `/etc/udev/rules.d/70-canary-gps.rules` matches this one receiver (by its
  by-id symlink) and pulls in `canary-gpsd@<kernel-name>.service`.
- `systemd/canary-gpsd@.service` runs `gpsdctl add` on start and
  `gpsdctl remove` on stop, and is `BindsTo` the device — so unplug releases it
  and re-plug re-attaches under the new kernel name.
- At boot, `systemd-udev-trigger` replays add events for already-present
  devices, so the plugged-in-at-boot case takes the same path.

**`DEVICES=""` is deliberate — do not "fix" it by listing the receiver there.**
gpsd tracks devices by the literal string it was handed, so a static by-id
entry plus a hotplug `/dev/ttyUSBn` entry are two identities for one port. Both
try to `open()` it and the loser dies with `EBUSY`:

```
SER: device open of /dev/ttyUSB0 failed: Device or resource busy(16)
/dev/ttyUSB0: device activation failed, freeing device.
```

Which side lost varied per plug event, so GPS came up intermittently. One
device, one owner. (`USBAUTO` stays `false` for a related reason: it's a global
"grab every matching USB serial device" switch, and `/dev/ttyUSB*` is shared
with the OBD adapter here. Debian also comments the CP210x match out of its
stock `60-gpsd.rules` — "matches too many other devices" — so the stock rule
wouldn't fire for this receiver anyway, and Debian's `gpsdctl@.service` gates
itself on `USBAUTO=true`, which is why canary ships its own ungated copy.)

## Verifying

```bash
gpspipe -w -n 5                 # gpsd is talking to the receiver
gpspipe -w -n 2 | grep DEVICES  # expect exactly ONE entry (see above)
systemctl list-units 'canary-gpsd@*'   # the attachment unit for the live node
chronyc sources                 # a 'GPS' refclock should appear and reach sync
chronyc tracking                # check Reference ID / Leap status
timedatectl                     # System clock synchronized: yes
```

Offline power-cycle test (network off, to mimic a park):
shut down, wait a minute, boot. Expect the clock to correct from GPS within a
minute or two, and no session file to contain an internal multi-second jump.

Note that a cold GPS fix takes longer than a warm one — a receiver that has
been off for days may need several minutes of clear sky before chrony has
anything to sync to.

## Still outstanding

- **RTC coin cell.** Fitting one to the Pi 5's RTC header gives a
  roughly-correct clock the instant it boots, before GPS has a fix. Add
  `dtparam=rtc_bbat_vchg=3000000` to `/boot/firmware/config.txt`, reboot, then
  `hwclock -w` (note: `hwclock` is not installed by default on this image —
  `apt-get install util-linux-extra`). This *complements* GPS time; it does not
  replace it, since a coin cell drifts and GPS does not.
- **Gating recording on a synced clock.** Ordering `canary.service` after
  `time-sync.target` stops the daemon from ever stamping data with an un-synced
  clock. Not yet applied — see the caveat in the repo history: with chrony in
  place the gate becomes satisfiable offline, but `systemd-time-wait-sync` has
  `TimeoutStartSec=infinity`, so the failure mode of a GPS that never gets a fix
  is "canary never starts."
- **Clock-jump session roll.** Even with all of the above, a step mid-session
  should roll to a new session file at the discontinuity rather than leaving one
  file spanning a jump.
