# Windows ROM codec follow-up, 2026-09-20

Payload decode remains incomplete. This supplements
`uprev-rom-container-research.md` and does not change its conclusions.

## Reproduced and additional differential evidence

Compared the original and Map1Change ROMData payloads, starting at file
offset 0x78, length 1,048,576 bytes. The known input hashes and single-cell
edit are recorded in the parent research note.

- Reproduced 5,413 unique aligned 32-bit XOR differences.
- For **all 8,192 blocks of 32 words (128 bytes)**, adjacent XOR differences
  match at word pairs (1,2), (9,10), (17,18), (25,26), using zero-based
  indices within each block. Those are the only equal adjacent pairs in the
  complete payload: 32,768 matches total.
- Equivalently, for every block and those pairs, the original payload's
  within-pair XOR equals the edited payload's within-pair XOR. This is a
  concrete invariant useful for testing codec hypotheses, not a plaintext
  recovery or proof of any particular cipher.
- Aligned little-endian subtraction differences have 260,253 unique values;
  big-endian subtraction has 260,106. The repeated structure is much stronger
  under XOR than under integer subtraction.
- The binary linear span of the 32-bit XOR differences has rank 25. Rank
  is invariant under byte swapping. This alone does not identify a codec.

The reproducible read-only differential script is outside the repository:
`C:\Users\thebo\Documents\Codex\2026-09-20\i-hav\work\analyze-rom-difference.py`.
The two local ROM copies remain outside Git.

## Windows access and prepared helper

An unrestricted agent shell is still a non-administrator Windows token:
`IsUserAnAdmin()` returned 0. Opening the running ROMeditor PID 19792 with
query-information and VM-read rights (0x410) returned error 5. Thus sandbox
permission and Windows UAC elevation remain distinct requirements.

Prepared local helper:
`C:\Users\thebo\Documents\Codex\2026-09-20\i-hav\work\inspect-romeditor.ps1`.

It requests only query/read process rights, reads the main executable's
loaded image in 4 KiB pages, and searches ASCII/UTF-16 section labels. It
does not enumerate or copy arbitrary process heaps, patch code, attach a
debugger, change licensing, or contact the ECU. Unreadable pages are left
zero-filled and the count of readable pages is reported. The output is a
memory-layout image, not a reconstructed runnable PE.

Output remains outside Git in `work\rom-codec-local\`:

- `inspection.json`: process/image metadata and section-label addresses.
- `ROMeditor-loaded-image.bin`: scoped main-module bytes for local analysis.
- `error.txt`: failure information, if inspection fails.

Launched through Windows `Start-Process -Verb RunAs -WindowStyle Hidden`.
Windows ultimately reported `The operation was canceled by the user`.
The helper did not run and no process-memory output was produced. No automatic
retry was made. To continue, the operator must authorize a new elevated
launch or run the helper from an administrator PowerShell. No further map
edits or USB captures are needed for this step.
