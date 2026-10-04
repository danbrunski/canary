# W08 — ROM library, memory map, compare and ECU backup

Requested by the operator on 2026-10-04: a dashboard page like the
"Xterra ECM Flash Map" artifact that shows information about loaded ROMs, a
backup utility that reads the ECU's flash, and loading either a plain image or
an `.OsirisROM`. It extends the existing ROM Maps panel
(`panels/RomMapsPanel.tsx`, `POST /control/rom/inspect`); it does not replace
it.

**Read-only throughout.** Nothing here writes the ECU's non-volatile memory,
and no flash path is added. The backup only reads. The AGENTS.md
non-volatile-write conditions therefore do not apply, but W08.7 keeps its own
key-on, engine-off refusal, for the reason given there.

Evidence this builds on:

- **Image layout and candidate tables:** `docs/uprev-rom-table-verification.md`,
  section "2026-10-04: image map and three candidate tables".
- **The read recipe:** `docs/skunkworks/uprev-backup-20261004.md`. UpRev read
  1 MiB with plain UDS on 0x7E0/0x7E8: `10 85` once, then `10 FB` and
  `23 [BE32 address] [BE16 0x003F]` per 63-byte block, about 35 ms per cycle
  and 624 s in all, with no SecurityAccess. The image it returned is
  byte-identical to the decoded `ZS44A-Tuned-20260920.OsirisROM` (sha256
  `a46baa24…be50`).
- **The codec and checksums:** `src/analysis/osiris.py`, `src/analysis/rom_targets.py`,
  and `data/nissan-xterra-2008/rom_targets.yaml` (`checksums`).

## Pre-made decisions (operator, 2026-10-04)

- **Inputs.** The library accepts a plain image (exactly 1,048,576 bytes,
  as the backup produces) or an `.OsirisROM`. UpRev's encrypted backup
  `.bin` (1,048,592 bytes, starting `32bec582…`) is recognised and refused
  by name: it cannot be decoded (see the backup capture doc).
- **Inspection is separate from editing.** Any image whose layout matches the
  vehicle's map definition is viewable: the calibration ID at its declared
  offset, plus a checksum verdict, which is shown whether it passes or fails.
  The known-source-hash guard in `TargetEditor` stays on the edit/export path
  and is not loosened.
- **The map is data.** Regions and tables live in the vehicle profile, with
  confidence and evidence as required fields.
- **Storage.** Images are stored outside Git and per vehicle, content-addressed
  by the plain image's sha256. Downloads are plain `.bin`; there is no
  `.OsirisROM` export in W08.

## Items

- [ ] **W08.1 ROM map definition as vehicle data.**
  - Add `data/nissan-xterra-2008/rom_map.yaml` with a validated schema
    (pydantic, required fields, refusal by name), covering:
    - image size and calibration-ID location;
    - erase sectors;
    - regions: start, end, name, kind (`boot`, `calibration`, `application`,
      `uprev`, `erased`, `unknown`), confidence (`verified`, `inferred`,
      `unknown`), required `evidence` text, and an `added_by_uprev` flag;
    - tables: offset or per-slot offset, rows × cols, dtype `u8`/`be16`,
      optional decode (e.g. `afr = 1881.6 / raw`, `rpm = raw × 50`), axes as
      references to other table entries, confidence and evidence;
    - map slots: base, stride and count.
  - Populate it from the 2026-10-04 findings. Every artifact row becomes a
    region or table entry with the same confidence.
  - Confidence is a field, never prose.
  - A table whose decode is unconfirmed is shown raw, labelled inferred.
  - **Accept:** loader tests that refuse a missing `evidence`, overlapping
    tables in one slot, an address beyond the image, and an unknown
    confidence value. A test pins that the shipped file matches every
    verified fact already in `rom_targets.yaml` (table offset, axes,
    checksum offsets), so the two files cannot drift.

- [ ] **W08.2 Image analysis core.** Given bytes, return a description.
  - **Format detection:** `.OsirisROM`, decoded with `osiris.py`; plain
    1 MiB image; UpRev encrypted backup, refused by name; anything else,
    refused by name with its size.
  - **Identity:** the plain image's sha256 and CRC32, the container sha256
    if there is one, and the calibration ID read at the declared offset.
  - **Checksums:** recompute and compare with the stored words, using the
    vehicle's checksum definition.
  - **Tune registry match:** the image matches a revision when its plain
    sha256 equals that revision's. Add an optional `image_sha256` per
    revision in `tune_revisions.yaml`, filled for k106 and 20260920 and the
    UpRev base from the decoded files (`rom_sha256` stays the container's).
    Otherwise the result is "unrecognised".
  - **Block statistics:** for each 4 KiB block, the percentage of 0xFF bytes,
    entropy, and its class (erased / dense / data / sparse), plus regions
    and tables from W08.1 resolved against this image.
  - **No map:** an image with a different calibration ID (e.g. stock ZS04C)
    gets block statistics and identity but `map: null`, with the reason "no
    map definition for calibration <id>".
  - **Accept:** known answers. The decoded 20260920 file and the 2026-10-04
    ECU read (when available as a test fixture slice, or by hash) give the
    same sha256, checksums pass, and the revision is ZS44A-20260920. A
    flipped byte fails the checksum and is reported, not refused. Decoded
    ROM bytes stay outside Git: tests use synthetic images with the real
    layout, or skip when the operator's files are absent.

- [ ] **W08.3 Library storage and routes** (control-token auth, like
  `/control/rom/inspect`).
  - **Storage:** a per-vehicle directory from config (scoped like the other
    per-vehicle paths), holding `<sha256>.bin` and `<sha256>.json`. The
    metadata records:
    - the source (`upload_plain`, `upload_osirisrom` or `ecu_read`);
    - the original filename and when it was added;
    - the container sha256 for an Osiris upload;
    - a label;
    - for an ECU read, the read metadata from W08.7.
  - Uploading the same image twice keeps one entry and adds the source.
  - **Routes:**
    - `POST /control/rom/library`: octet-stream upload with a `filename`
      query parameter; 2 MiB cap; returns the entry.
    - `GET /control/rom/library`: entries newest first, each with identity,
      checksum verdict, revision match, source and label.
    - `GET /control/rom/library/{sha}`: the full W08.2 description.
    - `GET /control/rom/library/{sha}/download`: the plain image as
      `application/octet-stream` with a filename.
    - `PATCH /control/rom/library/{sha}`: label only.
    - `DELETE /control/rom/library/{sha}`.
  - Add the routes to `docs/headless-api.md`, the route-table snapshot and
    any MCP parity the route snapshot requires.
  - **Accept:** route tests for each, including the encrypted-backup refusal
    and the 2 MiB cap, and a test that nothing is written under the repo.

- [ ] **W08.4 Table values.** `GET /control/rom/library/{sha}/tables`
  returns each W08.1 table for each slot:
  - raw values, plus decoded values where a decode is declared;
  - axes, confidence and evidence.
  - The existing Fuel Targets payload in `/control/rom/inspect` stays as it
    is (RomMapsPanel uses it).
  - **Accept:** Fuel Targets slot 1 from the 20260920 image equals the
    `afr_calibration.yaml` 20260920 map cell for cell. K (slot +0x762,
    `be16`) reads 28,715 on k106/20260920 and 27,089 on the UpRev base,
    labelled inferred.

- [ ] **W08.5 Compare.** `GET /control/rom/compare?a=<sha>&b=<sha>` returns
  the changed byte ranges (merged with a 16-byte gap), each labelled with
  the regions and tables it overlaps. Table ranges also carry per-cell old
  and new values, decoded where declared.
  - **Accept:**
    - **UpRev base → k106:** 248 bytes in 9 ranges, labelled checksums,
      Fuel Comp rpm axis, slot-1 timing-like cells, Fuel Targets (10 cells),
      Fuel Comp + K, and K in slots 2–5.
    - **k106 → 20260920:** 3 bytes, the checksums and Fuel Targets cell
      (row 7, col 6: 11.61 → 11.54 AFR).
    - These run as tests when the operator's ROM files are present and skip
      otherwise, plus a synthetic-image test that always runs.

- [ ] **W08.6 Dashboard page: ROM Library.** A new Engine Tuning page
  alongside ROM Maps (`#wb/rom-library`). It stands alone, and its empty
  state names its cause and offers the fix (upload, or back up the ECU).
  - **Library list:** label, source, calibration ID, revision match chip,
    checksum chip and date. Upload (drag-and-drop or picker) accepts `.bin`
    and `.OsirisROM`; a refusal shows its reason. Rename, delete with
    in-page confirmation, and download.
  - **Memory map:** the 16 × 16 grid of 4 KiB blocks, with a readout on
    hover or tap, and the region table with Verified / Inferred / Unknown /
    UpRev chips and evidence. Same information as the artifact, in the
    dashboard's own styles.
  - **Tables:** each table with its confidence chip; Fuel Targets reuses
    `MapTable`.
  - **Compare:** pick two entries; show the change list with labels and
    per-cell values.
  - Gate: `pnpm typecheck`, `pnpm lint`, `pnpm format`, and vitest for the
    new components. Load `#wb/rom-library` directly in a test with empty
    state.

- [ ] **W08.7 ECU backup job** (daemon side).
  - **Routes:**
    - `POST /control/rom/backup` starts it;
    - `GET /control/rom/backup` returns its state: idle, refused + reason,
      running (blocks done / total, bytes, elapsed, ETA, retries), done
      (library sha), failed + reason, or cancelled;
    - `POST /control/rom/backup/cancel`.
  - **Recipe as vehicle data:** the session sequence (`10 85` once, then
    `10 FB` before each read), block size 63, range 0x000000–0x0FFFFF, the
    request IDs, and pacing, recorded with the capture doc as provenance.
    Shared code holds no Xterra constants.
  - **Refusals, checked at start and re-checked during the read**, each
    naming its cause:
    - `vehicle_state` other than `ignition_only`. Entering a programming-type
      session with the engine running is not something to find out about; on
      2026-10-04 UpRev did this only key-on, engine-off.
    - Passive mode armed.
    - A foreign tester heard recently: CAN or K-line, `foreign_tester`.
    - A capture or profile capture active. The read stops all polling for
      about 10 minutes, as `scripts/restart_canary.sh` refuses for the same
      reason.
    - Another backup running.
    - If the engine starts mid-read, abort.
  - **Bus ownership:** take the ECM session through the existing claim
    mechanism (`ecm_session_claims`), so the OBD scheduler pauses Mode 01,
    Mode 22 and RAM polling. Release it in every exit path. Afterwards,
    return the ECM to the state CANary's normal polling expects, and show
    that polling resumes.
  - **Telemetry:** the read's 16k responses are not written as telemetry
    rows. Record one start event and one end event, with the summary.
  - **Robustness:**
    - a per-request timeout and a bounded retry per block;
    - an abort when retries are exhausted, naming the address;
    - a negative response is reported with its code.
  - **Integrity:** the finished image must pass the vehicle's checksums,
    and its calibration ID must match the ECU-reported one. Re-read 64
    blocks spread across the image and compare them. A failure keeps the
    image, marked "unverified", with the reason; it is never silently stored
    as good. The result goes into the W08.3 library as `source: ecu_read`
    with the read metadata: start, end, duration, requests, retries,
    revision match.
  - **Accept:**
    - fake-ECU tests for every refusal;
    - the claim is released on success, error and cancel;
    - an engine start mid-read aborts;
    - a timeout retries, then aborts by address;
    - no telemetry rows are written;
    - a checksum failure is marked unverified;
    - the resulting image byte-matches the fake ECU's.
  - Load `vehicle-nissan-xterra-2008` and `ecu-reverse-engineering` for the
    protocol.

- [ ] **W08.8 Dashboard: Back up ECU.** On the ROM Library page:
  - show the preconditions as live chips (vehicle state, passive, foreign
    tester, capture), with Start disabled and the reason shown while any
    blocks it;
  - an in-page confirmation stating it takes about 10 minutes, key on,
    engine off;
  - progress (blocks, percentage, ETA), Cancel, and the result linked to the
    new library entry, with its revision match;
  - a failure shows the named reason.
  - **Accept:** vitest for each state, with the route mocked.

- [ ] **W08.9 Known answer on the truck** (needs the truck, key on, engine
  off).
  - Run the backup from the dashboard.
  - **Accept:** the image's sha256 equals `a46baa24…be50`, the 2026-10-04
    UpRev-read image, unless a flash was recorded since; then it must equal
    that revision's `image_sha256`. Checksums pass, and the 64-block
    re-read matches.
  - Record the duration and retries, and confirm normal polling resumed
    within a minute of the end.

## Order

W08.1 → W08.2 → W08.3/W08.4/W08.5 (backend) can proceed in parallel with
W08.6/W08.8 (frontend) against the routes above, which are the contract.
W08.7 is independent of W08.4/W08.5. W08.9 last.
