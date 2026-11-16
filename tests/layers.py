"""The layer of every ``src/`` package (R30.2).

It lived in ``tests/test_layering.py`` and four boundary tests imported it from
there. ``tests/test_layering.py`` still enforces it; the table is here so a test
module is not a library. A unit missing from it fails that test, so a new
package is placed here first.
"""

from __future__ import annotations

#: Layer of every top-level package and root module under ``src/``. A unit
#: missing from this table fails the test, so a new package must be placed.
LAYERS: dict[str, int] = {
    # 0 foundation: byte -> engineering-unit decoding and the root helpers.
    "decode": 0,
    "artifact_hash": 0,
    "atomic_write": 0,
    "can_ids": 0,
    "cgroup_memory": 0,
    "coerce": 0,
    "jsonable": 0,
    "columns": 0,
    "config": 0,
    "dataframe_utils": 0,
    "file_memo": 0,
    "jsonl_store": 0,
    "log": 0,
    "lsm6dsox_settings": 0,
    "operating": 0,
    "paths": 0,
    "refusal": 0,
    "session": 0,
    "signals": 0,
    "strict_model": 0,
    "telemetry": 0,
    "timefmt": 0,
    "trainability": 0,
    "yaml_load": 0,
    # 1 persistence
    "storage": 1,
    "vehicle": 1,
    "evidence": 1,
    # 2 acquisition
    "collection": 2,
    # 3 pipeline
    "preprocessing": 3,
    # 4 detection
    "behavior": 4,
    "features": 4,
    "models": 4,
    "alerts": 4,
    "plausibility": 4,
    "datasets": 4,
    # 5 workbench
    "analysis": 5,
    # 6 application
    "control": 6,
    "vehicle_state": 6,
    "provisioning": 6,
    # 7 runtime
    "daemon": 7,
    "startup": 7,
    # 8 interface
    "dashboard": 8,
    "companion": 8,
    "mcp_server": 8,
    # 9 entry
    "validation": 9,
    "main": 9,
}
