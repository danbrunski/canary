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

- [x] **W08.1 ROM map definition as vehicle data.**
  - *Done 2026-10-04:* `src/analysis/rom_map.py` (`RomMap`, `load_rom_map`)
    and `data/nissan-xterra-2008/rom_map.yaml`: 19 regions, 16 erase sectors
    (sectors 8 and 15 verified from the 2026-09-20 flash, the rest inferred
    CRC partitions), 5 map slots with two slot spans (timing-like cells
    +0x09C-0x0BF, Fuel Compensation span +0x670-0x761, both inferred), 7
    tables (Fuel Targets Map 1 verified; Maps 2-5 and the 0x00A169 copy
    inferred; both Fuel Targets axes verified; Fuel Comp rpm axis and K
    inferred, K raw only). `evidence`, `confidence` and `added_by_uprev` are
    required; regions may nest but not partly overlap. `load_rom_map` refuses
    any drift from `rom_targets.yaml` by name. `tests/test_rom_map.py`
    (22 passed): missing/empty evidence (region, table, slot span, erase
    sector), unknown confidence, overlapping tables in one slot (and absolute
    ones), an address beyond the image (6 cases), axis references, and
    `test_shipped_map_loads_and_matches_every_verified_rom_targets_fact` plus
    `test_drift_from_rom_targets_is_refused_by_name`. Populated from the
    doc's 2026-10-04 section; the published artifact itself is not in the
    repo, so it was not compared row by row. One correction from the bytes:
    0x006000-0x007FFF is not wholly erased (48 non-0xFF bytes at
    0x006EFE-0x006F2D, recorded as an `unknown` region).
  - *Follow-up 2026-10-04, slot tables from the firmware*
    (`docs/ecu-firmware-analysis.md`, "Map-slot tables"): the two slot spans
    became tables. Timing trims A (+0x020) and B (+0x120), each 16 × 16 u8,
    rows = rpm axis 0xCC93 (raw × 50), cols = BFS-X axis 0xCC83 (raw), cells
    `(raw & 0x7F)` counts. Fuel Compensation (+0x660), 16 × 16 u8,
    `raw × 0.78125` %. Load Point Scaler at 0x9416 (BE16, read by the code;
    `raw × 0.00049` = BFS at load point 128 in ms) and its unread slot copy
    at +0x760. K (+0x762) is now `verified`, still raw. Each slot table is
    `verified` in Map 1 and `inferred` in Maps 2-5, because no map switch has
    been seen live. +0x260-0x45F and +0x460-0x65F are `unknown` spans.
    `Decode` gained `mask`, applied before the scale, because the trims carry
    a flag in bit 7 on 143 of 256 cells. **The Fuel Comp axes are
    `inferred`:** the ECM's code searches 0xCACD with engine speed and 0xCB29
    with `0xFFFF118A`, while UpRev's editor labels them the other way round.
    Known answers on the operator's files (`slow` tier): Map1Change is one
    Fuel Comp cell, Map 1 row 15 col 15, 132 → 131, which decodes to 103.13
    → 102.34 as UpRev displayed. The trims differ in 9 cells per slot in the
    base, and in 19 in Map 1 of k106 and 20260920. k106's "added spark"
    edit lands in rows 7-9 (2,800-3,600 rpm) × the four highest load
    columns. The scaler is 44,019 in all three images and all five slots,
    and `raw × 44019/128 × 0.00049` reproduces every BFS breakpoint in
    `afr_calibration.yaml`. Tests: `tests/test_rom_map.py` (29 passed) and
    `tests/test_rom_image.py` with `-m ""` (28 passed, the operator tests
    included).
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

- [x] **W08.2 Image analysis core.** Given bytes, return a description.
  - *Done 2026-10-04:* `src/analysis/rom_image.py` (`load_image`,
    `describe`, vectorised `checksum_values` pinned equal to
    `Checksums.values`). `tune_revisions.yaml` gains `image_sha256` for k106
    (`e01cc0cb…f18e`) and 20260920 (`a46baa24…be50`); the UpRev base
    (`82012080…c203`) is a new `reference_images` entry, not a revision,
    because it was never flashed as-is and a revision is a flash boundary
    (match status `reference`). `tests/test_rom_image.py` (23 passed, 6 of
    them `slow`, run on the operator's files): 20260920 decoded sha256 ==
    the 2026-10-04 ECU read's `a46baa24…be50`, CRC32 `95b993c5`, checksums
    pass, revision ZS44A-20260920; k106 matched, base `reference`; a flipped
    byte reports `checksum.ok: false` with the description intact; ZS04C cal
    ID gives `map: null` + "no map definition for calibration 1ZS04C00000";
    encrypted backup refused by name (synthetic, and all 3 operator `.bin`
    files); other sizes refused with the size. `tests/test_tune_revision_images.py`
    (5 passed) covers the registry fields and their merge.
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

- [x] **W08.3 Library storage and routes** (control-token auth, like
  `/control/rom/inspect`).
  - *Done 2026-10-04:* `src/control/rom_library.py`,
    `src/dashboard/routes/rom_library.py`; new config path
    `daemon.rom_library_dir` (`control/rom_library` under `state_dir`,
    scoped per vehicle by `scope_config`). Routes in `docs/headless-api.md`
    ("ROM library and ECU backup (W08)") and
    `tests/fixtures/route_table_snapshot.json` (11 rows added; no MCP parity
    test covers these routes). `tests/test_rom_library_api.py` (11 passed):
    auth on all 11 routes, plain then `.OsirisROM` upload of one image keeps
    one entry with both sources, encrypted backup 422 by name, 413 over
    2 MiB, 415, missing filename, list newest first, describe, download
    bytes + filename, label-only PATCH, DELETE, 404 on unknown/malformed sha,
    tables and compare routes, `test_nothing_is_written_under_the_repo`
    (`repo_guard` snapshot diff empty), 409 without a map definition.
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

- [x] **W08.4 Table values.** `GET /control/rom/library/{sha}/tables`
  returns each W08.1 table for each slot:
  - *Done 2026-10-04:* `rom_image.table_values` / `tables_payload`;
    `/control/rom/inspect` unchanged (`tests/test_rom_maps_api.py` passes).
    `test_tables_known_answer_20260920_fuel_targets_cell_for_cell` (operator
    file): Map 1 raw and AFR equal the `afr_calibration.yaml` 20260920 map
    cell for cell, rpm axis decoded 1200-5200, BFS axis raw (no scale
    declared). `test_tables_known_answer_k`: K slot 1 = 28,715 on k106 and
    20260920, 27,089 on the UpRev base, `confidence: inferred`, `decoded:
    null`. Synthetic equivalents always run.
  - raw values, plus decoded values where a decode is declared;
  - axes, confidence and evidence.
  - The existing Fuel Targets payload in `/control/rom/inspect` stays as it
    is (RomMapsPanel uses it).
  - **Accept:** Fuel Targets slot 1 from the 20260920 image equals the
    `afr_calibration.yaml` 20260920 map cell for cell. K (slot +0x762,
    `be16`) reads 28,715 on k106/20260920 and 27,089 on the UpRev base,
    labelled inferred.

- [x] **W08.5 Compare.** `GET /control/rom/compare?a=<sha>&b=<sha>` returns
  the changed byte ranges (merged with a 16-byte gap), each labelled with
  the regions and tables it overlaps. Table ranges also carry per-cell old
  and new values, decoded where declared.
  - *Done 2026-10-04:* `rom_image.compare_images`.
    `test_compare_known_answer_base_to_k106` (operator files): 248 bytes,
    9 ranges, labelled checksum words, Fuel Compensation rpm axis, Map 1
    Ignition timing (probable), Fuel Targets (Map 1) with 10 cells, Map 1
    Fuel Compensation span + K, and K in Maps 2-5 (27,089 -> 28,715).
    `test_compare_known_answer_k106_to_20260920`: 3 bytes, the checksum words
    and one Fuel Targets cell 11.61 -> 11.54 AFR, at **row 7, column 4**
    (zero-based; 0x0E025C - 0x0E0220 = 60, as `afr_calibration.yaml` also
    says). The "col 6" below is a transcription slip. Synthetic
    `test_compare_synthetic_labels_and_cells` and
    `test_changed_ranges_merge_within_a_16_byte_gap` always run.
  - **Accept:**
    - **UpRev base → k106:** 248 bytes in 9 ranges, labelled checksums,
      Fuel Comp rpm axis, slot-1 timing-like cells, Fuel Targets (10 cells),
      Fuel Comp + K, and K in slots 2–5.
    - **k106 → 20260920:** 3 bytes, the checksums and Fuel Targets cell
      (row 7, col 6: 11.61 → 11.54 AFR).
    - These run as tests when the operator's ROM files are present and skip
      otherwise, plus a synthetic-image test that always runs.

- [x] **W08.6 Dashboard page: ROM Library.** A new Engine Tuning page
  alongside ROM Maps (`#wb/rom-library`). It stands alone, and its empty
  state names its cause and offers the fix (upload, or back up the ECU).
  - *Done 2026-10-04:* `panels/tuning/RomLibraryPage.tsx` (nav item
    `tuning-rom-library`, alias `rom-library`) with `RomMemoryMap.tsx`,
    `RomTablesView.tsx`, `RomCompare.tsx`; payload types in
    `types/romLibrary.ts`, routes in `api/workbench.ts`. Evidence:
    `RomLibraryPage.test.tsx` (22 tests: direct load of `#wb/rom-library` with
    an empty library names the cause and offers "Upload an image" / "Back up
    the ECU"; the encrypted-backup 422 and a 413 shown verbatim; 256-cell grid
    readout on focus/hover/tap with `aria-live`, arrow keys; region chips and
    evidence; Fuel Targets through `MapTable`, K raw labelled Inferred; no-map
    reason; rename/delete-with-confirmation/download), `RomCompare.test.tsx`
    (6: k106 → 20260920 ranges, labels, cell 11.61 → 11.54 AFR),
    `lib/romLibrary.test.ts` (19). `pnpm typecheck`, `pnpm lint`,
    `pnpm format:check` clean; `pnpm test` 70 files / 549 tests. Routes are
    mocked; the types follow the backend's 2026-10-04 draft.
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

- [x] **W08.7 ECU backup job** (daemon side).
  - *Done 2026-10-04 (offline; the truck run is W08.9):*
    `src/control/rom_backup.py` (`RomBackupJob`, recipe schema) and
    `data/nissan-xterra-2008/rom_backup.yaml` (0x7E0/0x7E8, `10 85` once,
    `10 FB` per read, exit `10 81`, 0x000000-0x0FFFFF in 63-byte reads with
    the 4-byte tail, 35 ms minimum cycle, 0.5 s timeout, 3 retries, 64-block
    re-read; provenance the capture doc). The claim is a new `tx_gate` holder
    `rom_backup` (the OBD loop pauses on it as on any claim), and the job
    transmits through `CanObdClient(tx_holder=...)`, which `tx_gate.send`
    lets out only while that claim is the only one. The read's ids are held
    in `src/collection/private_traffic.py`, which both raw-capture taps
    (`can_obd_collector`, `can_collector.CanReader`) consult. The K-line
    reader waits while the backup holds the session. Start/end events go to
    the control-event log (`rom_backup.start` / `rom_backup.end`).
    `tests/test_rom_backup.py` (37 passed, fake ECU on a virtual bus): each
    refusal by name (engine running, engine off, no engine watch, passive,
    foreign claim, foreign tester heard, K-line session, capture, ELM327
    source, no recipe, backup already running); claim and hold released after
    success, timeout, NRC, cancel, bus-open failure and an unexpected
    exception (`_assert_released`); engine start, lost engine watch, passive,
    foreign claim, K-line tester, a CAN frame on 0x7DF and a capture start
    mid-read each abort; a dropped read retries 3 times then aborts "read at
    0x0001B9 (63 bytes) failed after 3 attempts"; NRC reported as `0x22`;
    no 0x7E0/0x7E8 rows from `CanReader` or `collect_can_obd_from_config`
    while broadcast rows continue; checksum failure, cal-ID mismatch and a
    re-read mismatch each keep the image `verified: false`; the stored image
    byte-matches the fake ECU's; `10 81` is sent last; polling-resume
    detection and its timeout. The pause of the real OBD loop and the
    resume within a minute are W08.9's to confirm on the truck.
  - *Verifier fixes, 2026-10-04 (second pass):*
    - **Telemetry race (was ~15 % flaky):** `private_traffic` now records each
      hold as a wall-clock window and `exclude(msg)` decides by the frame's
      own acquisition time (`frame_arrival`), so a tap that drains late still
      drops the `10 81`/`50 81` tail. `test_the_reads_traffic_is_not_written_as_telemetry`
      adds a second tap that reads nothing until the hold has ended and
      asserts the exact dropped count (2 × 568 frames);
      `test_privacy_is_decided_by_the_frames_own_timestamp`. 50 serial runs:
      50 passed. A mutation that judges by processing time fails both.
    - **Engine-start blind window:** a fresh broadcast rpm >= 400
      (`ENGINE_RUNNING_MIN_RPM`) refuses the start and aborts mid-read
      whatever `vehicle_state` says (it prefers a fresh OBD 0 for up to 10 s).
      `test_vehicle_reading_from_a_live_source`,
      `test_a_running_engine_refuses_while_obd_still_reads_a_fresh_zero`,
      `test_an_engine_start_inside_the_obd_freshness_window_aborts`, all
      through `vehicle_reading` with a live source.
    - **Store can no longer strand the job:** nothing that can raise runs
      before `add_image` (integrity wrapped; `RomLibrary.revision_match`
      reads a broken flash history as `status: "unknown"` with the reason);
      the post-read phase ends `failed` on any error and always emits the end
      event. New `stored` flag; `sha256` only when stored.
      `test_a_store_failure_keeps_nothing_and_says_so`,
      `test_a_broken_flash_history_does_not_lose_the_image`,
      `test_an_integrity_check_error_keeps_the_image_unverified`,
      `test_any_error_after_the_read_ends_the_job_as_failed`.
    - **Shutdown/restart mid-read:** the app lifespan calls
      `RomBackupJob.shutdown()` (cancel, `10 81`, release, end event; no new
      start). `scripts/restart_canary.sh` refuses while `rom_backup` is in
      `/status.passive.ecm_session_claims` (`--force` ends it and says so).
      `test_daemon_shutdown_cancels_the_read_cleanly`,
      `test_the_app_lifespan_shuts_the_job_down`, and 3 new tests in
      `tests/test_restart_canary_script.py` (8 passed).
    - **Send error vs gate:** `RawReply.send_error` carries a `can.CanError`;
      the job retries it as a failed attempt named "CAN send failed (...)",
      counted in `send_errors`. `test_a_transient_send_error_is_retried`,
      `test_a_persistent_send_error_is_named_as_one`.
    - **K-line race:** `_session` re-checks the backup claim after
      `_wants_command`. `test_the_ecu_backup_holds_the_ecm_so_no_kline_session_starts`,
      `test_a_backup_that_starts_after_the_plan_resolves_still_wins` (fails
      when the re-check is removed).
    - `tests/test_rom_backup.py` now 50 passed.
  - *Verifier fixes, 2026-10-04 (third pass):*
    - **Resume wait honours shutdown:** `_wait_for_polling` checks the
      shutdown flag on every pass and sleeps on that event, not
      `time.sleep` (the 35 ms pacing pause waits on the cancel event too).
      `test_shutdown_during_the_resume_wait_still_records_the_end`: the join
      returns in under 2 s with a 30 s resume window, state `done`/stored,
      note "not checked: the daemon is shutting down", end event recorded.
      The old sleep fails it (join times out).
    - **`canary-update` wording:** `scripts/update_canary.sh` help (`--force`,
      exit 3) and summary now say "capture or ECU backup active".
      `tests/test_update_canary_script.py::test_refuses_while_an_ecu_backup_is_running`
      (real `restart_canary.sh`, `rom_backup` claim in `/status`): exit 3
      before the fetch, no restart. The usage test now matches the new phrase.
    - **K-line scope, behaviour:** the backup holds only the ECM's diagnostic
      session, so only K-line work on the ECM (`profile.ecm_address`) waits
      for it. `_line_is_theirs()` is back to passive/foreign only. The new
      `_ecm_held_by_backup()` guards `_session` and `_run_action`, and
      `_wants_command` uses a cached `_targets_ecm(entries)`. An ECM function
      is refused with "refused: the ECU backup holds the ECM session"; an SRS
      (0x58) code clear or an ABS read runs during a backup.
      `test_a_module_off_the_ecm_runs_during_an_ecu_backup`,
      `test_an_ecm_function_is_refused_while_the_ecu_backup_holds_it`
      (`tests/test_kline_actions.py`),
      `test_an_abs_read_is_not_held_up_by_the_ecu_backup`
      (`tests/test_kline_collector.py`).
    - **Optional, done:** for a held frame, `private_traffic` believes the
      driver stamp up to `RETAIN_SECONDS` (60 s) old instead of the shared
      policy's 5 s, so a tap stalled up to a minute still drops it. Beyond
      that it falls back to processing time; the limit is documented on the
      constant. Covered in `test_privacy_is_decided_by_the_frames_own_timestamp`
      (a 20 s stall is dropped; a stall past retention is the documented
      fallback).
    - `tests/test_rom_backup.py` 51 passed; `test_kline_collector.py` +
      `test_kline_actions.py` 40 passed.
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

- [x] **W08.8 Dashboard: Back up ECU.** On the ROM Library page:
  - show the preconditions as live chips (vehicle state, passive, foreign
    tester, capture), with Start disabled and the reason shown while any
    blocks it;
  - an in-page confirmation stating it takes about 10 minutes, key on,
    engine off;
  - progress (blocks, percentage, ETA), Cancel, and the result linked to the
    new library entry, with its revision match;
  - a failure shows the named reason.
  - **Accept:** vitest for each state, with the route mocked.
  - *Done 2026-10-04:* `panels/tuning/EcuBackupCard.tsx`. The chips read
    `/status` (`vehicle_state`, `passive.enabled`, a `foreign` ECM claim,
    `capture_profile.active`) and merge the daemon's own `preconditions` from
    `GET /control/rom/backup`, so no chip reads green where Start would be
    refused; an unreported field blocks. Evidence: `EcuBackupCard.test.tsx`
    (21 tests): idle, refused (job state and a 409 shown verbatim), running
    (blocks 4,161 of 16,645, 25.0 %, ETA 07:48, retries, Cancel posts
    `/control/rom/backup/cancel`), done (opens the new entry, revision chip),
    failed (named reason; an unverified image kept and linked), cancelled;
    in-page confirmation (about 10 minutes, key on/engine off, read-only), and
    a spy shows `window.confirm` is never called.
  - *Second pass, 2026-10-04 (backend agent):* the card links a result only
    when the job says `stored: true` (`storedSha`), so a read that could not
    be stored no longer reads "kept, marked unverified"; `RevisionChip` shows
    `unknown` (unreadable flash history) with its reason. `types/romLibrary.ts`
    declares `stored`, `send_errors`, `integrity`, the description tables'
    `decode`/`axes`, and revision status `unknown`. The table fixtures now
    have the backend's real shapes (`k_constant`, BFS axis raw with no
    decode). `EcuBackupCard.test.tsx` 23 tests (new: nothing kept on a store
    failure; a `sha256` without `stored` is never shown as kept). `pnpm
    typecheck`, `pnpm lint`, `pnpm format:check` clean; `pnpm test` 71 files /
    557 tests passed.

- [x] **W08.9 Known answer on the truck** (needs the truck, key on, engine
  off).
  - Run the backup from the dashboard.
  - **Accept:** the image's sha256 equals `a46baa24…be50`, the 2026-10-04
    UpRev-read image, unless a flash was recorded since; then it must equal
    that revision's `image_sha256`. Checksums pass, and the 64-block
    re-read matches.
  - Record the duration and retries, and confirm normal polling resumed
    within a minute of the end.
  - *Passed 2026-10-04 (Pi `896b3f7`, ECU calibration ID read that boot:
    `1ZS44A00000`).* The run started 22:01:31Z, through
    `POST /control/rom/backup`, the same job the dashboard button drives:
    - 16,645 reads (1,048,576 bytes) in **610.3 s**, with 33,427 requests,
      **4 retries / 4 timeouts**, 0 send errors and 0 NRCs.
    - sha256 `a46baa248f16…1531be50`; `revision_match` ZS44A-20260920
      `matched`.
    - Checksum `be32_sum_xor` stored = computed (sum `0xF538FCAA`, xor
      `0x0D3BBAC0`). Calibration ID image = ECU = `1ZS44A00000`. The 64-block
      re-read had 0 mismatches.
    - Stored and verified. Polling resumed 2.1 s after the release.
  - *A first attempt at 21:58:32Z failed* at block 1,729 (0x01A97F): "session
    0xFB: no response within 0.5 s" after 4 attempts. The key had been turned
    off. Every ECM broadcast (0x23D, 0x231, 0x551, 0x7E8, …) stopped at
    21:59:36Z, while 0x2D1/0x2DE/0x280 carried on. The job failed cleanly:
    nothing stored, the session released, and the exit sequence sent.
    - Gap found: the refusal names a timeout, not the cause. While the job
      holds the ECM, `vehicle_state` reads `passive`, so the in-read re-check
      cannot see key-off.
    - A read timeout that coincides with the ECM's own broadcast frames going
      silent could be reported as "ECM went silent (key off?)". This is not
      implemented: an operator-facing message improvement, not a correctness
      issue.

## Order

W08.1 → W08.2 → W08.3/W08.4/W08.5 (backend) can proceed in parallel with
W08.6/W08.8 (frontend) against the routes above, which are the contract.
W08.7 is independent of W08.4/W08.5. W08.9 last.
