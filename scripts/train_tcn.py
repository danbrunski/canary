#!/usr/bin/env python3
"""TCN-backbone shim over the unified trainer CLI (Task 2.1).

Kept as a stable command name (docs and habits call it by path). It is
``scripts/train_sequence.py --backbone tcn``: the CLI and its backbone-bound
``train``/``build_dataset``/``evaluate``/``main``, whose training
contract is ``src.models.training_entry`` (R06.4).

``train_sequence.py`` is executed from its path with ``runpy`` rather than
imported: scripts never import each other (R06.4), because a sibling import
binds the file under a second module identity (``train_sequence`` beside
``scripts.train_sequence``) and a test patching one misses the other.
``runpy`` registers no module, so there is no second identity to patch.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

_entry = runpy.run_path(
    str(Path(__file__).resolve().with_name("train_sequence.py")), run_name="_train_tcn_entry"
)
_bound = _entry["bind_backbone"]("tcn")
build_dataset = _bound.build_dataset
train = _bound.train
evaluate = _bound.evaluate
parse_args = _bound.parse_args
main = _bound.main
_resolve_training_sessions = _entry["_resolve_training_sessions"]

__all__ = ["_resolve_training_sessions", "build_dataset", "evaluate", "main", "parse_args", "train"]

if __name__ == "__main__":
    sys.exit(main())
