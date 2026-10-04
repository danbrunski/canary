# UpRev ROM backup capture: the read is plain, the file is encrypted on the PC

2026-10-04. Read-only capture of an UpRev "ROM backup" of the ECM while it
ran ZS44A-20260920. Nothing was written to the ECU. CANary held passive mode
(no transmit) for the whole window and recorded:

- USB between Osiris and the cable: Wireshark/USBPcap on Windows,
  `Desktop/Uprev/Wireshark/uprev-rom-dump-20261004.pcapng`, 1,595,798
  packets, 806.6 s, 11:24:00–11:37:26 MDT.
- CAN: a raw `candump -L can0` on the Pi, every frame, 17:23:50–17:38Z,
  `/data/canary/re/uprev-backup-20261004/`.
- K-line: the daemon's sniffer, in session `2026-10-04T051811` (capture
  window `uprev-backup-read`).

The backup Osiris saved: `Desktop/Uprev/ROM Backup/23710 -
ZS44A_VIN Not Found-20261004.bin`, 1,048,592 bytes.

## Findings

1. **The ECU hands out its flash as plain bytes, over standard UDS on CAN.**
   - Osiris sent, on 0x7E0, a DiagnosticSessionControl `10 85` (positive
     response `50 85`), a cable CAN-ID setup (`03 07E0 07E8`), and a
     ReadMemoryByAddress `23 [BE32 address] [BE16 length]`, repeating
     this cycle for each read.
   - It made 16,644 reads of 63 bytes, 3 of 16 and 1 of 4 (probes), with 3
     negatives (`7F 23 22` once, `7F AC 11` twice).
   - The 0x63 replies reassembled by address cover 0x000000–0x0FFFFF
     exactly. The image is **byte-identical to the decoded
     `Tuned ROMs/ZS44A-Tuned-20260920.OsirisROM`**: sha256 `a46baa24…be50`,
     CRC32 `95b993c5`, which is the whole-image CRC in
     `uprev-flash-20260920.md`.
   - The same traffic is in the raw CAN log: 183,080 frames on 0x7E0 and
     183,079 on 0x7E8. The first request was at 17:25:11.99Z and the last
     at 17:35:36.48Z, so about 624 s for 1 MiB (~1.7 KB/s).
2. **The encryption happens on the PC, after the read.** None of 400 sampled
   32-byte chunks of the encrypted `.bin` body appear in the USB stream in
   either direction, while the plain image does. Osiris receives plain bytes
   and encrypts them before writing the backup file.
3. **The backup encryption is deterministic and chained.**
   - All three backups (factory ZS04C ×2, and today's ZS44A) start with the
     same 16 bytes, `32bec582…8c81`.
   - The factory backup and today's backup are identical for their first
     **33,280 bytes**, then share no aligned 16-byte block anywhere after.
     33,280 = 16 + 0x8000 + 496: the plain images evidently agree through
     the 32 KiB boot region and a little beyond, and the ciphertexts agree
     exactly that far.
   - A fixed key and IV explain the shared prefix. Never re-converging, even
     where both images must hold identical erased (0xFF) pages, rules out a
     reused keystream (CTR/OFB) or ECB, and points to a chained mode such as
     CBC.
   - So XOR against today's known pair cannot recover the factory image.
     Decrypting it would need the key from UpRev's software, which means
     reverse-engineering their protection. It is not planned.
4. **Osiris also touched K-line.** The sniffer heard a tester (`FC`) addressing
   module `10` at 17:25:11Z, just before the CAN reads began.

## Consequences

- The factory ZS04C image survives only inside the encrypted 2026-07-22 and
  2026-07-26 backups. The ECU no longer holds it.
- Reading the ECU's flash needs no UpRev tooling: plain `10 85` then `23` reads
  of up to 63 bytes on 0x7E0 return it. This matches the ≤63-byte 0x23
  windows CANary already uses for RAM channels. A whole-flash read is still
  a read, so it needs neither of the non-volatile-write conditions in
  AGENTS.md.
