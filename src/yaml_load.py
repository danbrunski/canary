"""The one YAML loader: PyYAML's safe loader, parsed by libyaml when built in.

``yaml.safe_load`` parses in pure Python, 8-10x slower than libyaml on this
repo's catalogs (``data/<vehicle>/ecu_application_model.yaml`` 130 ms against
15 ms, ``data/dids.yaml`` 103 ms against 10 ms; 2026-10-23). The push gate
spent over a third of its profiled test time inside it, re-parsing the same
catalogs. Only the parser differs: the constructor and resolver are the same
Python ``SafeConstructor``, so a document loads to the same values either way.
``tests/test_yaml_load.py`` refuses any other YAML load in the repo.
"""

from __future__ import annotations

from typing import IO, Any

import yaml

#: libyaml's parser under the safe constructor, or the pure-Python one on a
#: PyYAML built without libyaml.
SAFE_LOADER: Any = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def load_yaml(stream: str | bytes | IO[str] | IO[bytes]) -> Any:
    """``yaml.safe_load``, through :data:`SAFE_LOADER`."""
    return yaml.load(stream, Loader=SAFE_LOADER)  # noqa: S506 - a safe loader
