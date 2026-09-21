"""Offline export/patch CLI; outputs go into a new private review directory."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.analysis.rom_targets import Patch, TargetEditor


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["export", "apply"])
    parser.add_argument("rom", type=Path)
    parser.add_argument("--definition", type=Path, required=True)
    parser.add_argument("--patch", type=Path)
    parser.add_argument("--output-dir", type=Path, help="New directory outside this repository")
    parser.add_argument(
        "--expect-rom", type=Path, help="Require byte equality to an UpRev-saved ROM"
    )
    args = parser.parse_args()
    try:
        editor = TargetEditor.load(args.rom, args.definition)
        if args.action == "export":
            if args.patch or args.output_dir or args.expect_rom:
                raise ValueError(
                    "export accepts only ROM and definition; JSON is printed to stdout"
                )
            print(json.dumps(editor.export(), indent=2))
            return 0
        if args.patch is None:
            raise ValueError("apply requires --patch")
        patch = Patch.model_validate_json(args.patch.read_text())
        candidate, report = editor.apply(patch)
        if args.expect_rom:
            if candidate != args.expect_rom.read_bytes():
                raise ValueError("Candidate does not match the expected UpRev ROM byte-for-byte")
            report["expected_rom_byte_exact"] = True
        if args.output_dir:
            output = args.output_dir.resolve()
            if output.is_relative_to(Path(__file__).resolve().parents[1]):
                raise ValueError("ROM outputs must remain outside the repository")
            # Exclusive directory creation prevents overwriting any existing file,
            # including inputs; no partially built candidate is ever published.
            output.mkdir(parents=True, exist_ok=False)
            with (output / "review.json").open("x") as stream:
                stream.write(json.dumps(report, indent=2) + "\n")
            with (output / "patch.json").open("x") as stream:
                stream.write(patch.model_dump_json(indent=2) + "\n")
            # Publish the candidate only after its report and complete bytes
            # exist. A failed write leaves a clearly temporary file, not a ROM.
            pending = output / ".candidate.tmp"
            with pending.open("xb") as stream:
                stream.write(candidate)
            pending.rename(output / "candidate.OsirisROM")
            report["output_dir"] = str(output)
        else:
            report["dry_run"] = True
        print(json.dumps(report, indent=2))
        return 0
    except (ValueError, OSError) as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
