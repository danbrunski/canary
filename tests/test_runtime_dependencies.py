"""The daemon runs on the base dependencies alone.

A Pi is installed without the ``dev`` extra. On 2026-10-11 a fresh one
crash-looped at start because ``src.signals.fsm`` imported ``pdfplumber`` (dev:
FSM-PDF extraction) at module level, and the quality gate reaches that module
for ``PHYSICAL_LIMITS``. The laptop and the dev Pi both had it installed, so
nothing noticed. This imports the daemon's entry points with every dev-only
package made unimportable.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

#: Import names of packages that only the ``dev`` extra installs.
DEV_ONLY = ("pdfplumber", "pytest", "pytest_asyncio", "xdist", "mypy", "ruff", "pandas_stubs")

#: What the systemd units start, plus the modules they load lazily at runtime.
RUNTIME_MODULES = (
    "src.main",
    "src.daemon.runtime",
    "src.daemon.inference",
    "src.control.brain_jobs",
    "src.preprocessing.quality_gate",
    "src.mcp_server",
)


def test_daemon_modules_import_without_dev_packages() -> None:
    script = textwrap.dedent(
        f"""
        import importlib, sys

        class Block:
            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] in {DEV_ONLY!r}:
                    raise ModuleNotFoundError(f"No module named {{name!r}} (dev-only)")
                return None

        sys.meta_path.insert(0, Block())
        for mod in {RUNTIME_MODULES!r}:
            importlib.import_module(mod)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr[-3000:]


#: Modules the daemon must never load: the FSM PDF scraper and the PDF library
#: it needs. ``src.signals.fsm`` (``PHYSICAL_LIMITS``) is reached at serve time;
#: the scraper lives in ``src.signals.fsm_extract`` (R26.6) so that reaching the
#: limits cannot reach ``pdfplumber``. Checked on ``sys.modules`` rather than by
#: blocking the import, so a swallowed ``ImportError`` cannot hide it.
NEVER_LOADED_AT_SERVE = ("pdfplumber", "src.signals.fsm_extract")


def test_serve_time_imports_never_load_the_pdf_extractor() -> None:
    script = textwrap.dedent(
        f"""
        import importlib, sys

        for mod in ("src.signals.fsm", *{RUNTIME_MODULES!r}):
            importlib.import_module(mod)
        loaded = [m for m in {NEVER_LOADED_AT_SERVE!r} if m in sys.modules]
        assert not loaded, f"loaded at serve time: {{loaded}}"
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr[-3000:]


def test_fsm_module_imports_neither_pdfplumber_nor_the_extractor() -> None:
    """The limits module itself, in a process that imports nothing else of the
    project: its own import statements are the whole of what it can load."""
    script = textwrap.dedent(
        f"""
        import importlib, sys

        importlib.import_module("src.signals.fsm")
        loaded = [m for m in {NEVER_LOADED_AT_SERVE!r} if m in sys.modules]
        assert not loaded, f"src.signals.fsm loaded {{loaded}}"
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr[-3000:]
