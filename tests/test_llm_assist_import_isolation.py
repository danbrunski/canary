"""Task S.4 — the LLM-assist tier is import-isolated from the capture path.

This is the single load-bearing test in the llm-assist seams work: the
roadmap's Status section ("no torch/LLM import on the capture hot path, no
RAM reservation, no daemon dependency when disabled") is the whole reason
Sequencing step 1 builds an isolated package instead of bolting cards
straight onto existing modules.

Two checks, split because they guard different things:

* :func:`test_llm_assist_package_imports_no_torch` runs in a fresh
  subprocess that imports *only* ``src.analysis.llm_assist`` — nothing else
  from this repo. A subprocess (not an in-process ``sys.modules`` check) is
  required because torch is already a mandatory dependency the model layer
  (``src.models``, imported transitively by nearly everything else) loads
  eagerly at package-import time; an in-process check after other tests
  have run would pass or fail for reasons unrelated to this package. Import
  it alone in a clean interpreter and it's the only honest signal.
* :func:`test_capture_path_modules_do_not_import_llm_assist` is a static
  check (no subprocess needed) that the daemon's actual capture/detection
  modules never reference ``src.analysis.llm_assist`` at all — "no daemon
  dependency when disabled" means the always-on loop doesn't know this
  package exists, not merely that importing it is cheap.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The daemon's actual capture/detection modules — NOT the dashboard, which
#: is where S.2's feature-flagged card endpoint deliberately does depend on
#: this package. "capture hot path" / "daemon dependency" in the roadmap's
#: Status section means the always-on collection + detection loop, not the
#: read-only dashboard API surface. Covers the *entire* daemon and
#: collection packages (every module, not a curated subset — the always-on
#: loop's entrypoint (``src/daemon/runtime.py``) and its imports all belong
#: here just as much as the handful of modules an earlier pass singled out)
#: plus the alert emitter, which is the one file outside those two packages
#: that sits on the same always-on emit path.
CAPTURE_PATH_ROOTS = (
    REPO_ROOT / "src" / "daemon",
    REPO_ROOT / "src" / "collection",
)
CAPTURE_PATH_EXTRA_FILES = ("src/alerts/emitter.py",)


def _capture_path_modules() -> list[str]:
    rel_paths = set(CAPTURE_PATH_EXTRA_FILES)
    for root in CAPTURE_PATH_ROOTS:
        for path in root.rglob("*.py"):
            rel_paths.add(str(path.relative_to(REPO_ROOT)))
    return sorted(rel_paths)


def test_llm_assist_package_imports_no_torch() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "import src.analysis.llm_assist\n"
            "assert 'torch' not in sys.modules, sorted(sys.modules)\n"
            "print('OK')\n",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def _imported_module_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_capture_path_modules_do_not_import_llm_assist() -> None:
    offenders = {}
    for rel_path in _capture_path_modules():
        path = REPO_ROOT / rel_path
        imported = _imported_module_names(path)
        hit = {name for name in imported if name.startswith("src.analysis.llm_assist")}
        if hit:
            offenders[rel_path] = hit
    assert offenders == {}


def test_llm_assist_disabled_by_default() -> None:
    from src.config import CanaryConfig

    config = CanaryConfig.from_yaml(REPO_ROOT / "canary.example.yaml")
    assert config.llm_assist.enabled is False
