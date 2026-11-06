# The UpRev cable as a K-line interface

CANary can use the operator's UpRev cable as a K-line interface to the ECM,
without UpRev's software running. Through it CANary reads the ECM's DIDs and
RAM bytes over K-line: support-mask discovery, single reads, and dynamic
streams that sample many DIDs and RAM bytes per reply. The path is
read-only diagnostic streaming. It is not a flash path: UpRev's reflashing
runs over CAN, and nothing here can send it. AGENTS.md's policy on
non-volatile writes applies unchanged.

It remains an optional laptop-side CLI, not a daemon collector. The
CANary-HAT's own K-line (H05) reads the same channels without the cable, and
the HAT's sniffer (H06) hears another tester's K-line session.

## The cable

- USB ID `0403:ccb8`, product `Cipher USB Cable (Nissan)` (Windows calls it
  `UpRev Vehicle Interface`).
- One program owns it at a time. Quit UpRev's software first.
- From WSL, attach it with `usbipd` (`usbipd bind --busid <id> --force` in an
  elevated PowerShell, then `usbipd attach --wsl --busid <id>`) and check with
  `lsusb -d 0403:ccb8`. The bus ID changes with re-plugging. While attached to
  WSL, Windows cannot use it.
- Install the optional extra: `.venv/bin/pip install -e '.[uprev-usb]'`
  (PyUSB). The offline tests don't need it.

## The ECM session is shared

The ECM keeps one diagnostic session across both buses, so CANary's CAN
diagnostic client and a K-line logger cannot both own it. Trying both at once
gave negative responses and lost sessions. Put CANary in TTL-bounded passive
mode before the cable takes K-line. Raw broadcast CAN, the decoded broadcast
channels, GPS and storage carry on, and give a same-time reference.

## Capturing and comparing

```bash
# On the Pi control surface first: passive=true with a bounded TTL.
.venv/bin/python tools/uprev_kline_probe.py \
  --stream 0x1201:2 --stream 0x1101:1 --samples 1000 \
  --out /tmp/uprev.jsonl --quiet

.venv/bin/python tools/analyze_uprev_kline_capture.py \
  --capture /tmp/uprev.jsonl --session /path/to/simultaneous/pi/session \
  --tolerance 50ms --out /tmp/uprev-analysis.json
```

- `--scan-supported` reads every DID the ECM's support masks advertise
  (75 on the Xterra) once; `--stream-supported` streams them all.
- `--did` reads one DID once; `--stream DID:WIDTH` adds a DID to a stream;
  `--memory-byte` adds a RAM byte.
- `--stream-supported` puts at most 12 DIDs in each stream definition (the
  default `--chunk-size`). All 75 in one definition timed out; seven
  definitions of up to 12 streamed all of them cleanly.
- `tools/uprev_usb_probe.py` only checks that the cable enumerates and
  answers.

The analyzer keeps every raw observation and pairs through
`compare_channels`: `n_pairs` counts one-to-one acquisition-time pairs, and a
high r² without a stable scale is not reported as an identity.

Captures are research data. Keep them out of the training corpus, under a
`_`-prefixed directory (`/data/canary/_uprev-usb/…`).

## What it found

The DIDs and RAM channels identified this way live in the catalogs, not
here: `data/dids.yaml` (with each entry's evidence) and
`data/nissan-xterra-2008/kline.yaml`. The live road run is
`data/analyses/2026-09-20-uprev-usb-road.md`.

## Code

```text
src/collection/uprev_usb.py       USB transport
src/collection/uprev_protocol.py  framing and stream parsing
src/collection/uprev_session.py   read-only ECM session start and DID discovery
tools/uprev_usb_probe.py          cable check
tools/uprev_kline_probe.py        DID / RAM / mixed stream capture
tools/analyze_uprev_kline_capture.py  paired DID / RAM / CANary comparison
tests/fixtures/uprev_usb/         compact transcripts for offline tests
tests/test_uprev_*.py             offline protocol, transport and session tests
```

Decoded values can enter the pipeline as `TelemetryRecord.uprev(...)`, but
the current `UPREV_SOURCE` is import-only (`tools/import_uprev_log.py`).
Making live records is a contract change: sweep every source assertion,
storage and session assumption, query and report caller, and test, rather
than widening one allowlist. Whether live cable records belong in the daemon
at all is open; the CLI plus paired analysis may be the cleaner boundary.
