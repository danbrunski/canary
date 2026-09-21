# UpRev Osiris ROM container investigation

Historical initial investigation, 2026-09-20. **Superseded:** the Windows
agent decoded the payload; see `uprev-rom-codec-decoded.md` and the independent
WSL verification in `uprev-rom-table-verification.md`. The initial findings
below are preserved as investigation history, not the current blocker list.
All inspection is offline/read-only. No ROM was modified,
no editor state was changed, and no ECU requests were issued.

## Operator-provided ground truth

Evidence directory: `/home/dan/Uprev/ROMs/`.

The operator explicitly confirmed `ZS44A-Tuned.OsirisROM` is the current
tune. Its August filesystem date is not evidence to the contrary.

Known single-cell edit, made in UpRev and saved separately:

- Tree: `Fuel -> Map 1 -> Fuel Compensation`.
- Window: `Map 1 Fuel Compensation`.
- X axis: `Volumetric Flow Ratio`, column `50.00%`.
- Y axis: `Rotations Per Minute`, row `7200`.
- Displayed value: `103.13` -> `102.34`.
- Reverted file restores the cell to `103.13`.

This is **fuel compensation**, not the Fuel Targets table. Values are
consistent with counts 132 -> 131 and display `100 * count / 128`, rounded
to two decimals. This is a candidate storage interpretation, not an
extracted byte or established file offset.

| File | SHA-256 |
|---|---|
| `ZS44A-Tuned.OsirisROM` | `c5325776b08c3084edd49d8f68587dad4668e02b0e77c7686f7548fb6f7c8fa9` |
| `ZS44A-Tuned-NoOp1.OsirisROM` | `c5325776b08c3084edd49d8f68587dad4668e02b0e77c7686f7548fb6f7c8fa9` |
| `ZS44A-Tuned-NoOp2.OsirisROM` | `c5325776b08c3084edd49d8f68587dad4668e02b0e77c7686f7548fb6f7c8fa9` |
| `ZS44A-Tuned-Map1Change.OsirisROM` | `f8aeb980e81aca69874c8c0c7ae90588605d6a2e4dadb6b219ce25fd0674f69e` |
| `ZS44A-Tuned-Map1ChangeReverted.OsirisROM` | `c5325776b08c3084edd49d8f68587dad4668e02b0e77c7686f7548fb6f7c8fa9` |
| `ZS44A.OsirisROM` | `8468e11bedeab3d8247a7750b56c0f1b0dd475ef5c4509df1482425acfebb292` |

`cmp` independently verified original/reverted byte equality. No-op equality
alone could reflect cached encoded data; exact edit/revert strengthens the
case for reproducible content-dependent encoding, without proving every
save path deterministic. The operator declined another identical edit as
low-value; do not request it again as a prerequisite.

## Outer framing

Tuned files are 1,050,256 bytes. ASCII signature:
`UpRev Osiris ROM File Ver 1.0000`. Sections begin at file offset `0x60`.
The observed section header can be read as `struct.unpack('<8sIIII', ...)`:

| Relative offset | Observed interpretation |
|---|---|
| `+0x00` | Eight-byte section label |
| `+0x08` | Payload length |
| `+0x0c` | Total section length, including 24-byte header |
| `+0x10` | Field equal to 1 in every inspected section; semantics unknown |
| `+0x14` | Four-byte field; checksum/seed interpretation unconfirmed |
| `+0x18` | Encoded payload begins |

| Section | Header offset | Payload bytes | Total bytes | Original `+0x14` field |
|---|---|---:|---:|---|
| `ROMData ` | `0x60` | 1,048,576 | 1,048,600 | `0x155fefed` |
| `Keys    ` | `0x100078` | 12 | 36 | `0xc776adba` |
| `CELData2` | `0x10009c` | 1,476 | 1,500 | `0x9df4b6e8` |
| `Notes   ` | `0x100678` | 0 | 24 | `0x00000000` |

These sections cover the file exactly. `ZS44A.OsirisROM` has the same three
non-Notes section lengths and is 24 bytes shorter (no Notes section).
Section labels do not establish the contents' meaning; in particular,
`Keys` is not proof of a usable cipher key and `CELData2` is not proven to
contain map definitions.

## Differential result

The one-cell edit changes 1,045,010 bytes, exclusively in inclusive file
range `0x74..0x100077`. The first 116 and last 1,560 bytes are unchanged.
The `ROMData` field at `+0x14` changes to `0xb6718783`; the one-MiB payload
begins at `0x78`. All other sections remain byte-identical.

Original and edited payload byte entropies are approximately 7.99986 and
7.99985 bits/byte. Nevertheless, their XOR difference has strong word-level
structure: only **5,413 distinct little-endian 32-bit XOR values among
262,144 aligned words**. The first 16 words have only 11 distinct XOR
values. No aligned 16-byte payload blocks remain identical.

Thus high byte entropy must not be reported as proof of standard encryption.
Whole-payload changes do not imply cryptographic diffusion. The repeated
word differences are a useful constraint for a future codec hypothesis.

Bounded negative tests, not exhaustive exclusions:

- The changing four-byte field is neither standard CRC32 nor Adler32 of
  the encoded payload (tested on original and edit).
- If decoded payloads differ at exactly one byte by XOR `0x07` (132 -> 131),
  no position in a one-MiB payload gives the observed header-field XOR as a
  standard CRC32 difference. XOR `0x01` was also tested without a match.
  This does **not** exclude CRC32 if UpRev changes other decoded bytes,
  uses another representation, or transforms the checksum.
- Simple field-seeded MSVC/ANSI/Numerical Recipes/Delphi/Borland LCG XOR
  streams and classic .NET Random variants did not reproduce the observed
  difference in the tested layouts. No algorithm family is ruled out by
  these limited tests.

## Installed editor and definitions

Installation: `/mnt/c/Program Files/UpRev/ROM Editor/`.

`ROMeditor.exe` is a 32-bit Windows PE with no exposed CLR directory in its
on-disk PE header. The relocated entry point, unusually named large
sections, and import table containing just `lstrcpy` and
`InitCommonControls` strongly suggest packing/protection. Ordinary string
inspection did not expose ROMData/CELData load/save routines. This does
not establish the language/runtime of the unpacked application.

`ECUDefs.Osiris` has signature `UpRev ROM Definitions Ver 1.0000` and uses
the same observed 96-byte prefix / 24-byte section framing:

| Label | Header offset | Payload bytes | Total bytes |
|---|---|---:|---:|
| `Script00` | `0x60` | 12,022,136 | 12,022,160 |
| `Script01` | `0xb771f0` | 12,023,256 | 12,023,280 |

These cover all 24,045,536 bytes. Shared framing is evidence of a common
container design, **not yet proof of a shared payload codec**. Decoding
these sections may provide the table-address/axis/scaling definitions we
need; their actual contents remain unknown.

The WSL Windows-process bridge worked after sandbox escalation, but
`OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ)` on the running
ROMeditor returned Windows error 5 (access denied). No process memory was
read, no process was launched/restarted, and no write/debug privileges were
requested. The sandbox bridge and Windows process permissions are separate
constraints.

## Next useful step: Windows-side read-only codec inspection

An elevated Windows agent/tool, with permission to inspect the operator's
editor process, is the next route. No additional driving, USB capture,
manual cell edits, or vehicle flashing is required for this investigation.

1. Inspect the loaded/unpacked editor modules for the container section
   labels and their read/write call sites. Prefer scoped code/module
   extraction, not an indiscriminate whole-process dump. Keep any extracted
   proprietary material local and outside Git; do not expose license data.
2. Trace how `ROMData`'s four-byte field and payload are decoded and encoded.
   Determine integrity-field semantics rather than naming it a checksum by
   assumption. Do not patch the editor or bypass licensing.
3. Test a read-only decoder against original and edited files. Locate the
   known Fuel Compensation cell using independent table definitions/UI
   values; report all decoded differences, including internal checksums.
4. Reproduce the existing full Fuel Targets transcription for the original
   file hash as a second known-answer check. A plausible map-shaped byte
   sequence alone is not sufficient.
5. Before any candidate writer: require byte-exact decode/encode round trips
   on both supplied variants and reject unsupported containers. Then verify
   candidate tables by reopening offline in UpRev. Preserve all unknown
   sections and bind every patch to the source hash and exact old values.

No candidate ROM writer or upload-ready file exists from this work.
CANary continues to collect/analyze only; the operator's UpRev workflow owns
review and any eventual flashing.
