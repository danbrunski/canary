#!/usr/bin/env python3
"""TCN-backbone shim over the unified trainer (Task 2.1).

``scripts/retrain.py`` and ``src/control/jobs.py``'s job allow-list import
this module directly (``train``/``build_dataset``/``evaluate``/``main``) —
see ``scripts/train_sequence.py`` for the shared implementation.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from train_sequence import _resolve_training_sessions, bind_backbone  # noqa: E402

_bound = bind_backbone("tcn")
build_dataset = _bound.build_dataset
train = _bound.train
evaluate = _bound.evaluate
parse_args = _bound.parse_args
main = _bound.main

__all__ = ["_resolve_training_sessions", "build_dataset", "evaluate", "main", "parse_args", "train"]

if __name__ == "__main__":
    sys.exit(main())
