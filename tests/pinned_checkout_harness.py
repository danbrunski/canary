"""``scripts/evaluate_detectors.py`` with the shared checkout's key inputs pinned
(T01.20).

The harness keys its score cache on ``code`` (every ``src/**/*.py``), the
config file, the bindings, the behavior models and ``data`` (the repo
catalogs): files of the *working tree*, which other sessions edit while a test
runs. A test that asserts a second run reads the first run's scores back was
deciding on whether anyone saved a file under ``src/`` or ``data/`` in that
minute: the cache then (correctly) keyed the second run anew, or discarded
the first run's entries. That is the flake, not a shared cache root (each test
has its own under ``tmp_path``).

This script runs the real harness (``main`` and everything under it), and
holds those five key inputs at the values the test's first harness run hashed,
recorded in the JSON file named by :data:`PIN_ENV`. What the test owns, the
artifacts and its sessions' frames, is still hashed live, so a test still sees
a rescore when its own inputs change. Scoring workers (``spawn``) import this
file as ``__mp_main__``, so they re-check against the same pins.

Use: ``monkeypatch.setattr(retrain, "EVALUATE_DETECTORS_SCRIPT", PINNED_HARNESS)``
and ``monkeypatch.setenv(PIN_ENV, str(tmp_path / "key-pins.json"))``.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

#: The JSON file holding the pinned values (first-seen wins).
PIN_ENV = "CANARY_TEST_SCORE_KEY_PINS"

#: The key inputs hashed from the shared working tree, not from the test's files.
CHECKOUT_KEY_INPUTS = ("code", "config_file", "bindings", "behavior_models", "data")

PINNED_HARNESS = Path(__file__).resolve()


def pin(values: Mapping[str, str | None]) -> dict[str, str | None]:
    """The checkout key inputs among ``values``, as first recorded: a name not
    yet recorded is recorded with its value here."""
    path = Path(os.environ[PIN_ENV])
    pins: dict[str, str | None] = json.loads(path.read_text()) if path.exists() else {}
    fresh = {k: values[k] for k in CHECKOUT_KEY_INPUTS if k in values and k not in pins}
    if fresh:
        pins.update(fresh)
        staged = path.with_suffix(".tmp")
        staged.write_text(json.dumps(pins, sort_keys=True))
        os.replace(staged, path)
    return {k: pins[k] for k in CHECKOUT_KEY_INPUTS if k in values}


def install() -> Any:
    """Patch the harness in *this* process (the script's own, or a scoring
    worker's); never called on import, so a test process importing the
    constants above leaves the harness module alone. Returns the module."""
    import scripts.evaluate_detectors as harness

    real_key_inputs = harness.key_inputs
    real_evaluate = harness._evaluate

    def pinned_key_inputs(sources: Any) -> dict[str, str | None]:
        now = real_key_inputs(sources)
        return {**now, **pin(now)}

    def pinned_evaluate(args: Any, cleanup: Any, early: Mapping[str, str | None]) -> int:
        # ``main`` hashes the code and the config file first ("early") and
        # lets them win over ``key_inputs``: pin those too.
        return real_evaluate(args, cleanup, {**early, **pin(early)})

    harness.key_inputs = pinned_key_inputs
    harness._evaluate = pinned_evaluate
    return harness


# A spawned scoring worker runs this file as ``__mp_main__``.
if __name__ in ("__main__", "__mp_main__"):
    _harness = install()

if __name__ == "__main__":
    sys.exit(_harness.main(sys.argv[1:]))
