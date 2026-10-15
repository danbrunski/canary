# Coprocessor link — Pi ↔ STM32U575 protocol, v1

The contract between the Pi and the HAT rev 3.0 coprocessor (H09.1). Both
implementations follow this document: the Python codec (H09.2) and the
firmware framing (H09.10). Both must pass the golden vectors in
`tests/fixtures/coprocessor_link/vectors.yaml`. Where this document and code
disagree, the code is wrong. Design background is in
`docs/CANary-HAT-Revisions.md` §13.3. The decisions this spec builds on are
listed in `docs/roadmap/tasks/H09-coprocessor-link.md`.

**Status:** draft v1, 2026-10-15, awaiting operator review.

## 1. Principles

- **The Pi is always the master.** The MCU never starts a transaction. It
  raises DATA_RDY and waits.
- **Commands are reliable and frames are accounted for.** Pi→MCU frames are
  acknowledged, retried and de-duplicated. MCU→Pi frames are sent once,
  numbered and checked, so a loss is counted and named but not resent. Data
  that must not be lost is pulled again on request: boot-gap blocks by index,
  the K-line result by command number, configuration by readback.
- **The firmware reports raw data.** It forwards raw IMU FIFO words, raw
  ADC codes and raw CAN payloads, and never adjusts its own clock. All
  decoding, calibration and time mapping happen on the Pi, where pytest
  reaches them.
- **Configuration is data the Pi sends.** Nothing vehicle-specific is a
  firmware constant. What the MCU needs before the Pi is up (CAN bitrate, the
  boot-gap ADC plan) is persisted from the last configuration the Pi pushed.
- **Little-endian throughout.** Every multi-byte integer is LE.

## 2. Physical layer

| Item | Value |
|---|---|
| Bus | SPI0, the MCU on **CE1** (GPIO7). It shares the bus with the MCP2518FD on CE0. |
| Mode | SPI mode 0 (CPOL 0, CPHA 0), MSB first, 8-bit words |
| Clock | **10 MHz** at bring-up, raised to 20 MHz once H09.14 shows clean frames. The U575's slave full-duplex limit is 38.5 MHz (DS13737 Table 148). |
| MISO | Through a single-gate tri-state buffer enabled by CE1 (§13.3.0). **The buffer's input has a pull-down**, so a dead or resetting MCU reads as `00` bytes, which never form a valid header. |
| DATA_RDY | GPIO23, MCU → Pi, active high, external pull-down (§13.3.6) |
| Max transaction | 1052 bytes (§4) |
| Gap between transactions | CS high for **≥ 20 µs**. The MCU re-arms its DMA within that time. |

**Shared-bus budget.** The MCP2518FD has to wait for a link transaction to
finish. A 1052-byte transaction takes ~0.85 ms at 10 MHz, which is ~5 frames
at the Xterra's ~1,556 frames/s, well inside the controller's RX FIFOs.
H09.14 measures the effect on `can0` latency, as H08.9 does for rev 2.1's
ADC.

## 3. Transactions

One transaction is one CS-low period. Both directions run at once
(full duplex):

```
 offset 0            16                         16+frame_len         L
 MOSI:  [ Pi header ][ Pi frame (optional) ][ zero padding .......... ]
 MISO:  [ MCU header][ MCU frame (optional)][ zero padding .......... ]
```

### 3.1 The MCU header (MISO bytes 0–15)

| Off | Size | Field | Meaning |
|---|---|---|---|
| 0 | 2 | magic | bytes `43 4E` ("CN") |
| 2 | 1 | version | protocol major, `1` |
| 3 | 1 | flags | §3.3 |
| 4 | 2 | staged_len | total length of the MCU frame at offset 16 of **this** transaction (0 = none) |
| 6 | 2 | next_len | length of the frame the MCU will stage once this one is delivered (0 = none) |
| 8 | 2 | pi_ack_seq | `seq` of the last Pi frame accepted (0 = none since boot) |
| 10 | 2 | boot_id | random per MCU boot (from the RNG); a change means the MCU reset |
| 12 | 1 | rx_rejects | Pi frames rejected since boot, mod 256 |
| 13 | 1 | queued | MCU frames waiting behind the staged one, saturating at 255 |
| 14 | 2 | crc | CRC-16/CCITT-FALSE over bytes 0–13 |

The MCU preloads this header and its staged frame into the TX DMA before CS
falls. They are a consistent snapshot taken while CS was high.

### 3.2 The Pi header (MOSI bytes 0–15)

| Off | Size | Field | Meaning |
|---|---|---|---|
| 0 | 2 | magic | bytes `50 49` ("PI") |
| 2 | 1 | version | `1` |
| 3 | 1 | flags | 0 in v1 |
| 4 | 2 | pi_frame_len | total length of the Pi frame at offset 16 (0 = none) |
| 6 | 2 | mcu_last_seq | `seq` of the last MCU frame the Pi decoded (diagnostic only) |
| 8 | 6 | reserved | 0; the MCU ignores it in v1 |
| 14 | 2 | crc | CRC-16/CCITT-FALSE over bytes 0–13 |

### 3.3 MCU header flags

| Bit | Name | Set while |
|---|---|---|
| 0 | DATA_PENDING | frames are queued beyond the staged one; mirrors DATA_RDY |
| 1 | CONFIG_VALID | sections 1 and 2 (§6) are active, either persisted or pushed this boot |
| 2 | BOOTGAP_RECORDING | the boot-gap buffer is recording |
| 3 | BOOTGAP_FROZEN | the buffer is stopped and readable (§7) |
| 4 | KLINE_COMMAND | K-line is in command mode |
| 5 | DROPS | frames were dropped since the last STATUS frame (§8) |
| 6 | FRESH_BOOT | from reset until the first Pi frame is accepted |
| 7 | — | 0 |

### 3.4 Sizing a transaction

The Pi chooses the length `L` before each transaction:

- `S` is the length of the frame the MCU has staged for this transaction:
  - the previous header's `next_len`, if the previous transaction clocked at
    least `16 + staged_len`;
  - otherwise the previous header's `staged_len`;
  - 0 before the first valid header.
- `P` is the length of the Pi frame being sent (0 if none).
- **`L = 16 + max(S, P)`, rounded up to a multiple of 4.**
- `L = 16` is a **poll**: headers only.

The Pi verifies the MCU header's magic, version and CRC before using any
field. If `staged_len` differs from `S` (the queue changed between snapshots),
the Pi drops any partial frame and sizes the next transaction from this
header.

### 3.5 Delivery rules

**MCU → Pi:**
- **Delivery is decided by the clocked length.** The MCU retires its staged
  frame when the transaction clocked at least `16 + staged_len` bytes,
  whether or not the Pi then validated it. Otherwise the frame stays staged
  for the next transaction.
- **The Pi checks every frame.** It verifies the frame CRC and `seq`. A CRC
  failure or a `seq` gap is counted as lost frames, per stream, and is never
  resent at the link layer.

**Pi → MCU:**
- **When the MCU accepts a Pi frame:** the Pi header is valid, the
  transaction clocked at least `16 + pi_frame_len`, and the frame's CRC and
  type are valid.
- **Acknowledgement:** the MCU sets `pi_ack_seq` in its next header. It
  processes the received frame before preloading that header, so the ack
  always arrives one transaction later.
- **Duplicates:** a frame whose `seq` equals the last accepted one is
  acknowledged again and not executed. That makes retries safe.
- **A full command queue:** the MCU does not accept the frame, so no ack goes
  out and the Pi retries.
- **Retries:** the Pi resends the same frame, with the same `seq`, until the
  ack arrives, at most **5 times**. Then it reports "coprocessor not
  accepting commands". Pi→MCU frames are stop-and-wait: one unacknowledged
  frame at a time.

**Sequence numbers:** each direction counts 1…65535 and then wraps to 1; 0
means "none". The MCU restarts at 1 on every boot. The Pi restarts at 1 when
it sees a new `boot_id`.

### 3.6 Cadence and DATA_RDY

- **DATA_RDY** is high while a staged frame is undelivered or more frames are
  queued. The Pi responds within 1 ms (an edge event or polling) and keeps
  transacting while the line stays high.
- **Heartbeat:** the Pi transacts at least every **100 ms**, a poll if it has
  nothing else to send. That is the heartbeat in both directions.
- **K-line fail-safe:** if the MCU sees no valid Pi header for the K-line
  `command_timeout_ms` (§6, section 5) while in command mode, it drops EN and
  the pull-up in the safe order, falls back to sniff, and logs it (§13.3.4).

### 3.7 Failure handling on the Pi

Each failure names its kind (AGENTS.md):

| Observation | Kind reported | Action |
|---|---|---|
| MISO header all `00` | `no coprocessor (MISO idle)` | Poll every 100 ms. After 3 in a row, `/status` shows the coprocessor missing. |
| Bad magic or CRC, but not all `00` | `link header error` | Count it and poll again. Three in a row is reported as a link fault. |
| `version` ≠ 1 | `coprocessor protocol vN, expected v1` | Refuse to proceed. |
| `boot_id` changed | `coprocessor reset` (cause from STATUS) | Restart the Pi `seq`, read STATUS, re-push configuration, re-read the boot-gap state. |
| Frame CRC failure | `link frame CRC` | Count it, per stream. |
| `seq` gap of n > 1 | `link frames lost` | Count n−1, per stream. |
| No ack after 5 retries | `coprocessor not accepting commands` | Surface it, and keep polling. |

## 4. Frames

Both directions use the same format:

| Off | Size | Field |
|---|---|---|
| 0 | 1 | type (§5) |
| 1 | 1 | flags (0 in v1) |
| 2 | 2 | seq |
| 4 | 2 | payload_len, N ≤ 1024 |
| 6 | N | payload |
| 6+N | 4 | CRC-32/ISO-HDLC over bytes 0 … 5+N |

**Frame length = N + 10, at most 1034.** The largest transaction is
16 + 1034 = 1050, padded to **1052**.

### 4.1 CRCs

| Name | Poly | Init | RefIn/RefOut | XorOut | Check ("123456789") | Python |
|---|---|---|---|---|---|---|
| CRC-16/CCITT-FALSE | 0x1021 | 0xFFFF | no/no | 0x0000 | 0x29B1 | `binascii.crc_hqx(b, 0xFFFF)` |
| CRC-32/ISO-HDLC | 0x04C11DB7 | 0xFFFFFFFF | yes/yes | 0xFFFFFFFF | 0xCBF43926 | `zlib.crc32(b)` |

Both are stored LE. The U575's CRC unit computes either one in hardware.

## 5. Messages

**Timestamps.** `mcu_us` is microseconds since MCU reset, as a u64: a 32-bit
1 MHz timer extended in software. The firmware never steps or slews it.
Fields named `*_us` are absolute `mcu_us` (u64). Fields named `delta_us` are
u32 offsets from the enclosing block's base. The Pi maps `mcu_us` to its own
clock with PPS_EDGE and CAN_WITNESS (§13.3.3c).

**Unknown types** are skipped and counted, which leaves room to add
messages. A changed layout is a new `version`.

### 5.1 Pi → MCU (0x01–0x3F)

| Type | Name | Payload | Reply |
|---|---|---|---|
| 0x01 | GET_STATUS | — | STATUS |
| 0x02 | SET_CONFIG | `section u8, persist u8, config_version u16, body…` (§6) | CMD_RESULT |
| 0x03 | GET_CONFIG | `section u8` | CONFIG_REPORT |
| 0x04 | BOOTGAP_STOP | — | CMD_RESULT |
| 0x05 | BOOTGAP_READ | `slot u16, offset u16, length u16` (length ≤ 1000) | BOOTGAP_CHUNK |
| 0x06 | BOOTGAP_RELEASE | — | CMD_RESULT |
| 0x07 | RESET | `magic u32 = 0x54455352` (bytes `52 53 45 54`, "RSET") | none (the MCU resets) |
| 0x10 | KLINE_SET_MODE | `mode u8` (0 off, 1 sniff, 2 command) | KLINE_RESULT |
| 0x11 | KLINE_WAKE | `kind u8` (1 fast, 2 five-baud), `address u8`, `idle_us u32, low_us u32, high_us u32` | KLINE_RESULT |
| 0x12 | KLINE_TRANSACT | `p4_us u16, reply_timeout_ms u16, end_silence_us u16, max_reply u16, tx_len u16, tx[tx_len]` | KLINE_RESULT |
| 0x13 | KLINE_RECEIVE | `reply_timeout_ms u16, end_silence_us u16, max_reply u16` | KLINE_RESULT |
| 0x14 | KLINE_GET_RESULT | `cmd_seq u16` | KLINE_RESULT (the retained last result) |

**K-line command details (§13.3.4):**
- **KLINE_WAKE** uses `idle_us` as ISO's W5: the line must have been idle that
  long first. Fast init drives `low_us` low and then `high_us` high. Five-baud
  sends `address` at 5 baud and ignores `low_us`/`high_us`.
- **KLINE_TRANSACT** sends `tx`, waits `p4_us` between bytes, and checks every
  echoed byte. The pull-up is on only for that exchange. It then collects the
  reply until the line has been silent for `end_silence_us`, or until
  `reply_timeout_ms` passes with no first byte.
- **The firmware parses no K-line protocol.** Checksums, lengths, NRC 0x78
  and the allowlist are the Pi's job.

### 5.2 MCU → Pi (0x80–0xBF)

| Type | Name | Payload |
|---|---|---|
| 0x80 | STATUS | §5.3 |
| 0x81 | CMD_RESULT | `cmd_seq u16, cmd_type u8, code u8, detail u16` |
| 0x82 | CONFIG_REPORT | `section u8, persisted u8, config_version u16, body…` (the bytes active now) |
| 0x83 | ADC_BLOCK | `t0_us u64, period_us u32, step_ns u16, channel_mask u8, reserved u8, n_sets u16, codes u16[n_sets × k]` |
| 0x84 | IMU_BLOCK | `before_us u64, chip_ts u32, after_delta_us u32, n_words u16, words[7 × n_words]` |
| 0x85 | CAN_WITNESS | `base_us u64, n u8, entries[n]` of `delta_us u32, id u32, dlc u8, data[8]` (17 bytes each) |
| 0x86 | BOOTGAP_CHUNK | `slot u16, offset u16, status u8, reserved u8, data…` |
| 0x87 | PPS_EDGE | `edge_us u64, edge_count u32` |
| 0x88 | KLINE_RESULT | `cmd_seq u16, cmd_type u8, status u8, mode u8, reserved u8, m1 u32, m2 u32, m3 u32, base_us u64, n_rx u16, rx[n_rx]` of `byte u8, delta_us u32` |
| 0x89 | KLINE_EVENTS | `base_us u64, n u16, events[n]` of `kind u8, value u8, delta_us u32, duration_us u32` |
| 0x8A | LOG | `level u8` (0 debug … 3 error), `text` (UTF-8, the rest of the payload) |

**Field details:**

- **ADC_BLOCK.** `k` is popcount(`channel_mask`). Codes run in ascending
  channel order within each set.
  - Set `j` starts at `t0_us + j × period_us`.
  - Within a set, the i-th converted channel is sampled `i × step_ns` after
    the set's start. The ADS8688A converts one channel at a time, and the
    block says so instead of pretending the set is simultaneous.
  - The codes are raw 16-bit ADS8688A conversions. Ranges and calibration are
    `data/hat_adc_*.yaml`, applied on the Pi.
- **IMU_BLOCK.**
  - `words` are raw LSM6DSOX FIFO words (tag byte + 6 data bytes) in read
    order, decoded on the Pi by `src/collection/lsm6dsox.py`.
  - `chip_ts` is the chip's live TIMESTAMP register, read between two MCU
    clock reads. The second read is at `before_us + after_delta_us`. That is
    the bracketed correlation the Pi-side driver already uses, moved onto the
    MCU's clock.
- **CAN_WITNESS.**
  - `id` bit 31 marks an extended (29-bit) ID, and bits 28–0 are the ID.
  - Data is zero-padded to 8 bytes; `dlc` gives the real length.
  - The entry time is the FDCAN RX timestamp, extended to `mcu_us`.
- **BOOTGAP_CHUNK `status`:**

  | Code | Meaning |
  |---|---|
  | 0 | ok |
  | 1 | not frozen |
  | 2 | slot out of range |
  | 3 | slot empty |
  | 4 | offset or length out of range |

- **PPS_EDGE:** one per TIMEPULSE rising edge, timer-captured.

### 5.3 STATUS payload (100 bytes)

| Off | Size | Field |
|---|---|---|
| 0 | 3 | fw_major u8, fw_minor u8, fw_patch u8 |
| 3 | 1 | reset_cause: 0 power-on, 1 pin (NRST), 2 IWDG, 3 software, 4 brown-out, 255 other |
| 4 | 4 | git_hash u32 (the first 8 hex digits of the build commit) |
| 8 | 2 | boot_id u16 |
| 10 | 1 | flags, as in the header (§3.3) |
| 11 | 1 | kline_mode u8 |
| 12 | 8 | uptime_us u64 |
| 20 | 4 × 15 | counters u32, in this order: `can_rx`, `can_fifo_lost`, `can_bus_errors`, `witness_sent`, `adc_sets`, `adc_blocks_dropped`, `imu_words`, `imu_blocks_dropped`, `kline_events_dropped`, `queue_frames_dropped`, `pi_frames_rejected`, `pps_edges`, `bootgap_frames`, `bootgap_blocks_overwritten`, `kline_failsafe_trips` |
| 80 | 1 | bootgap_state: 0 idle, 1 recording, 2 frozen, 3 released |
| 81 | 1 | bootgap_encoding (1, §7) |
| 82 | 2 | bootgap_block_size u16 (8192) |
| 84 | 2 | bootgap_slots u16 (64) |
| 86 | 2 | bootgap_head_slots u16 (32) |
| 88 | 4 | bootgap_blocks_written u32 |
| 92 | 2 | kline_last_result_seq u16 |
| 94 | 2 | v12_mv u16 (the last 12 V reading, 0 if none) |
| 96 | 4 | reserved, 0 |

The MCU queues a STATUS frame once a second and in reply to GET_STATUS.

### 5.4 Codes

**CMD_RESULT `code`:**

| Code | Meaning |
|---|---|
| 0 | ok |
| 1 | bad section |
| 2 | bad length |
| 3 | bad value (`detail` = byte offset) |
| 4 | persist failed |
| 5 | wrong state |
| 6 | unknown type |

**KLINE_RESULT `status`.** `m1`–`m3` mean different things for each status:

| Code | Status | m1 | m2 | m3 |
|---|---|---|---|---|
| 0 | OK | WAKE: measured low µs. TRANSACT: echo bytes checked | WAKE: measured high µs | WAKE: idle before µs |
| 1 | ECHO_MISMATCH | index | expected | got |
| 2 | NO_ECHO | — | — | — |
| 3 | TIMEOUT | — | — | — |
| 4 | BUS_BUSY | observed idle µs | — | — |
| 5 | WRONG_MODE | — | — | — |
| 6 | UNDERVOLTAGE | 12 V mV | — | — |
| 7 | FOREIGN_BREAK (a break arrived mid-exchange) | low µs | — | — |
| 8 | TOO_LONG | — | — | — |
| 9 | BUSY | — | — | — |
| 10 | NOT_FOUND | — | — | — |
| 255 | INTERNAL | — | — | — |

`rx` carries the bytes received after the echo: the reply, or the
five-baud sync and key bytes.

**KLINE_EVENTS `kind`:**

| Kind | Meaning | value | duration_us |
|---|---|---|---|
| 1 | byte | the byte | 0 |
| 2 | break | 0 | measured low |
| 3 | framing error | the received byte | 0 |
| 4 | mode change | the new mode | 0 |

### 5.5 Priorities and backpressure

The MCU's outbound queue is RAM-bounded (32 KB in v1). Frames are staged in
this priority order:

1. CMD_RESULT, KLINE_RESULT, CONFIG_REPORT, BOOTGAP_CHUNK (replies)
2. STATUS, PPS_EDGE, LOG
3. KLINE_EVENTS
4. CAN_WITNESS
5. IMU_BLOCK
6. ADC_BLOCK

**When the queue is full, the MCU drops the oldest frame of the
lowest-priority stream present** and counts it in that stream's STATUS
counter. Replies are never dropped, and their count stays bounded because
the Pi sends commands stop-and-wait. A frame already announced in `next_len`
is committed and is staged next, whatever arrives after it.

## 6. Configuration sections

`SET_CONFIG` carries one section. With `persist` = 1, the MCU also writes it
to a flash page (with a CRC), and applies that copy at reset before the Pi is
up. Sections 1 and 2 must persist for the boot gap to work.
`config_version` is the Pi's own counter, echoed back in CONFIG_REPORT.
**The Pi sends `persist` = 1 only when the GET_CONFIG readback differs** from
what it would push, so daemon restarts do not wear the flash.

| Section | Name | Body | Persist |
|---|---|---|---|
| 1 | CAN | `bitrate u32, sample_point_permille u16, bootgap_enable u8, reserved u8` | required |
| 2 | ADC | `period_us u32, channel_mask u8, ranges u8[8], sensor_power u8, block_sets u16, boot_period_us u32, boot_mask u8, reserved u8` | required |
| 3 | IMU | `watermark u16, n u8, writes[n]` of `reg u8, value u8` | no |
| 4 | WITNESS | `max_rate_hz u16, n u8, ids u32[n]` (bit 31 = extended) | no |
| 5 | KLINE | `baud u32, rxd_blank_us u16, command_timeout_ms u16` | no |

**What each section means:**
- **CAN.** FDCAN always runs in bus-monitoring mode. The firmware derives bit
  timing from `bitrate` and `sample_point_permille`. It starts the boot-gap
  buffer at reset when `bootgap_enable` = 1.
- **ADC.**
  - `ranges` are ADS8688A range-register codes, one per channel.
  - `sensor_power` = 1 drives U9's EN high while sampling.
  - `boot_period_us`/`boot_mask` are the plan used from reset until the Pi
    pushes section 2. `boot_period_us` = 0 means no boot-gap sampling.
- **IMU.** The firmware applies the register writes in order. It knows no
  LSM6DSOX register meanings beyond FIFO status and data and the timestamp
  register, so the configuration stays in `src/collection/lsm6dsox.py`.
- **WITNESS.** `max_rate_hz` caps witness entries per second across all IDs.

## 7. Boot-gap buffer, encoding 1

**Layout.** SRAM3 is 512 KB, split into **64 slots of 8192 bytes**:

- **Head:** slots 0–31 fill once, in order, and are never overwritten.
- **Tail:** slots 32–63 form a ring, and a new block overwrites the oldest
  tail block.
- **Every block is self-contained:** it carries its own time base and ID
  table, so a lost or overwritten block is a clean gap with known edges.

`BOOTGAP_STOP` closes the open block and freezes the buffer. The Pi reads a
block's 24-byte header from every slot, orders the valid blocks by
`block_seq`, reads their records, and sends `BOOTGAP_RELEASE` once ingested.
Stitching to `can0` is §13.3.3(a).

### 7.1 Block header (24 bytes)

| Off | Size | Field |
|---|---|---|
| 0 | 2 | magic: bytes `42 47` ("BG") |
| 2 | 1 | encoding, `1` |
| 3 | 1 | flags: bit 0 head slot, bit 1 tail slot, bit 2 closed, bit 3 written after the tail first wrapped |
| 4 | 4 | block_seq u32: 0, 1, 2… in write order |
| 8 | 8 | base_us u64: `mcu_us` of the block's first record |
| 16 | 2 | used u16: record bytes after the header |
| 18 | 2 | n_records u16 |
| 20 | 4 | crc u32: CRC-32/ISO-HDLC over the `used` record bytes, 0 while open |

### 7.2 Records

Every record starts with one header byte: **kind** in bits 7–6, **index** in
bits 5–0. Next comes `dt`, the µs since the previous record in this block
(the first record's is from `base_us`), as an unsigned LEB128 varint.

| Kind | Name | After `dt` | Index bits |
|---|---|---|---|
| 0 | FULL | `idw` varint, `dlc` u8, `data[dlc & 0x0F]` | 0–62 assigns this index to the ID for the rest of the block; 63 assigns none (table full) |
| 1 | DELTA | `mask` u8, then the changed bytes in ascending position | the index of an earlier FULL in this block |
| 2 | ANALOG | `mask` u8, `codes u16[popcount(mask)]` | 0 = one ADC set (§6, section 2's boot plan) |
| 3 | EVENT | depends on the event | 0 FIFO lost (`count` varint), 1 bus error (`code` u8), 2 frames dropped (`count` varint), 3 pad (the block ends here) |

**Field details:**
- **`idw`** = `id | (extended << 29)`. `dlc`'s low nibble is the length, and
  bit 4 marks RTR.
- **DELTA** repeats the previous frame of that index with the same `dlc`.
  Bit *i* of `mask` set means data byte *i* changed. The encoder writes a
  FULL whenever `dlc` changes.
- **The index table resets at each block start.** The first frame of each ID
  in a block is always FULL.

**Size, as a sanity check.** A FULL standard 8-byte frame is
1 + 2 (`dt`) + 2 (`idw`) + 1 + 8 = 14 B. A DELTA with two changed bytes is
1 + 2 + 1 + 2 = 6 B, which matches §13.3.10's estimate. H09.10 measures the
real figure on a full capture.

## 8. Accounting

The Pi keeps, per stream: frames received, CRC failures, `seq` gaps (frames
lost), and the MCU's own drop counters from STATUS. A gap in any stream is
recorded with its kind ("link CRC", "link loss", "MCU queue full", "FDCAN
FIFO lost"), never silently interpolated. `queue_frames_dropped` and the
per-stream drop counters are separate on purpose: one says the link fell
behind, the others say which data was given up.

## 9. Golden vectors

`tests/fixtures/coprocessor_link/vectors.yaml` holds one case per message
type, plus link headers, a poll, a boot-gap block, and invalid cases (bad
header CRC, bad frame CRC, all-zero MISO). Each case gives the bytes in hex
and the decoded fields. H09.2 and H09.10 must decode each one to those fields
and re-encode them to those bytes. Invalid cases must be refused with the
named kind.
