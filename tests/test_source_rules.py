"""Source-scan guards for rules the project has already decided (R24.8).

``tests/test_yaml_load.py`` refuses a direct ``yaml.safe_load`` anywhere. This
module is the same idea as a table: each :class:`Rule` is a pattern or AST
predicate, the files it scans, an allowlist of the files where it still occurs
today, and the incident or rule it enforces. Adding a rule is adding a row.

An allowlist is a ratchet, not a permission. Per file it records how many hits
exist today and why, and it may only shrink:

* more hits than listed (or a hit in an unlisted file) fails, naming the rule,
  the ``file:line`` and the sanctioned alternative;
* fewer hits than listed fails too ("lower the count"), and zero hits fails
  with "remove it from the allowlist", so a cleanup cannot be quietly undone
  by the next copy in the same file.

Every row also carries a ``bad`` and a ``good`` snippet. ``test_the_predicate_*``
proves the predicate flags the first and not the second, and
``test_a_violation_in_a_scanned_file_is_reported`` writes the bad snippet into
a scratch tree and checks the whole scan reports it, so a rule that has stopped
matching anything cannot pass for a clean tree.

Not folded in: ``tests/test_daemon_restart_guard.py`` (R30.1) scans shell
scripts line by line with its own comment handling and carries its own tests,
so moving it here would not be a pure move.
"""

from __future__ import annotations

import ast
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

import pytest

from src.paths import REPO_ROOT

REPO = REPO_ROOT
#: Directories never walked: environments, build output, VCS and caches.
SKIPPED_DIRS = frozenset({".venv", "venv", "node_modules", "dist", ".git", "__pycache__"})

Hit = tuple[int, str]
Finder = Callable[[str, str], list[Hit]]
#: ``path -> (hits allowed today, why they are still there)``
Allowlist = Mapping[str, tuple[int, str]]


def _grant(reason: str, counts: Mapping[str, int]) -> dict[str, tuple[int, str]]:
    return {path: (count, reason) for path, count in counts.items()}


@dataclass(frozen=True)
class Rule:
    name: str
    #: The incident or rule, and the sanctioned alternative; printed on failure.
    why: str
    roots: tuple[str, ...]
    suffixes: tuple[str, ...]
    find: Finder = field(compare=False)
    allowed: Allowlist = field(compare=False)
    #: A snippet the predicate must flag, and one it must not.
    bad: str = ""
    good: str = ""
    #: Repo-relative paths the rule does not apply to (the thing's own home).
    exempt: Callable[[str], bool] = field(default=lambda rel: False, compare=False)


# --- predicates -----------------------------------------------------------


@cache
def _parse(text: str) -> ast.Module | None:
    """One parse per distinct file text, shared by every rule that reads it."""
    try:
        return ast.parse(text)
    except SyntaxError:
        return None


def _regex_finder(pattern: str, *, skip_comments: bool = False) -> Finder:
    compiled = re.compile(pattern)

    def find(rel: str, text: str) -> list[Hit]:
        hits: list[Hit] = []
        for number, line in enumerate(text.splitlines(), 1):
            if skip_comments and line.lstrip().startswith(("//", "*", "/*")):
                continue
            if compiled.search(line):
                hits.append((number, line.strip()))
        return hits

    return find


def _slow_frame_rows(rel: str, text: str) -> list[Hit]:
    """``.iterrows()`` and ``.apply(..., axis=1)``: a Python call per row."""
    if "iterrows" not in text and not ("apply" in text and "axis" in text):
        return []
    tree = _parse(text)
    hits: list[Hit] = []
    for node in ast.walk(tree) if tree else ():
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr == "iterrows":
            hits.append((node.lineno, ".iterrows()"))
        elif node.func.attr == "apply":
            axis = [k.value for k in node.keywords if k.arg == "axis"]
            axis += [arg for arg in node.args[1:2]]
            if any(isinstance(a, ast.Constant) and a.value in (1, "columns") for a in axis):
                hits.append((node.lineno, ".apply(..., axis=1)"))
    return hits


def _dir_listing(node: ast.AST) -> str | None:
    """The receiver of ``X.iterdir()`` or ``X.glob("20...")``, else ``None``."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return None
    attr = node.func.attr
    if attr == "iterdir":
        return ast.unparse(node.func.value)
    if attr in ("glob", "rglob") and node.args:
        first = node.args[0]
        if isinstance(first, ast.Constant) and str(first.value).startswith("20"):
            return ast.unparse(node.func.value)
    return None


def _listing_of(arg: ast.AST) -> bool:
    """Is ``arg`` a directory listing, bare or filtered by a comprehension?

    A comprehension that keeps only files, or only some suffix, is listing
    files, not session directories.
    """
    if _dir_listing(arg):
        return True
    if isinstance(arg, ast.GeneratorExp | ast.ListComp):
        for gen in arg.generators:
            if _dir_listing(gen.iter) is None:
                continue
            conditions = " ".join(ast.unparse(c) for c in gen.ifs)
            return "is_file" not in conditions and "suffix" not in conditions
    return False


def _sessions_ordered_by_name(rel: str, text: str) -> list[Hit]:
    """``sorted`` / ``max`` / ``min`` over a directory listing, ordered by name.

    ``key=`` that is not about the name (``publish.py``'s ``order``) is fine;
    ``key=lambda p: p.name`` is the same bug spelled longer.
    """
    if not ("iterdir" in text or re.search(r"glob\(\s*[\"']20", text)):
        return []
    tree = _parse(text)
    hits: list[Hit] = []
    for node in ast.walk(tree) if tree else ():
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("sorted", "max", "min")
            and node.args
            and _listing_of(node.args[0])
        ):
            continue
        keys = [ast.unparse(k.value) for k in node.keywords if k.arg == "key"]
        if keys and ".name" not in keys[0]:
            continue
        hits.append((node.lineno, f"{node.func.id}(<directory listing>)"))
    return hits


_TEST_IMPORT_HINTS = (
    "tests.test_",
    "import test_",
    "from tests import",
    "from . import",
    "from test_",
)


def _imports_another_test(rel: str, text: str) -> list[Hit]:
    """Each from tests.test_x import, from tests import test_x, import tests.test_x."""
    if not any(hint in text for hint in _TEST_IMPORT_HINTS):
        return []
    tree = _parse(text)
    hits: list[Hit] = []
    for node in ast.walk(tree) if tree else ():
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.startswith(("tests.test_", "test_")) or (
                module in ("tests", "") and any(a.name.startswith("test_") for a in node.names)
            ):
                hits.append((node.lineno, f"from {'.' * node.level}{module} import ..."))
        elif isinstance(node, ast.Import) and any(
            a.name.startswith(("tests.test_", "test_")) for a in node.names
        ):
            hits.append((node.lineno, "import tests.test_..."))
    return sorted(hits)


def _located(pattern: re.Pattern[str], label: str) -> Finder:
    """A finder reporting each match of a multi-line ``pattern`` at its first line."""

    def find(rel: str, text: str) -> list[Hit]:
        return [(text.count("\n", 0, m.start()) + 1, label) for m in pattern.finditer(text)]

    return find


def _is_exception_class(node: ast.ClassDef) -> bool:
    """A class with a base named ``*Error``, ``*Exception`` or ``*Refusal``."""
    for base in node.bases:
        name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
        if name.endswith(("Error", "Exception", "Refusal")):
            return True
    return False


def _hand_written_reason(rel: str, text: str) -> list[Hit]:
    """``self.reason = ...`` in an exception class: the base's job (R24.1)."""
    if "self.reason" not in text:
        return []
    tree = _parse(text)
    hits: dict[int, str] = {}
    for node in ast.walk(tree) if tree else ():
        if not (isinstance(node, ast.ClassDef) and _is_exception_class(node)):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Assign | ast.AnnAssign):
                continue
            for target in sub.targets if isinstance(sub, ast.Assign) else [sub.target]:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "reason"
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    hits[sub.lineno] = f"self.reason = ... in {node.name}"
    return sorted(hits.items())


def _is_status_attribute(node: ast.expr | None, name: str, attribute: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == attribute
        and isinstance(node.value, ast.Name)
        and node.value.id == name
    )


def _remap_only_http_exception(rel: str, text: str) -> list[Hit]:
    """``except X as exc: raise HTTPException(exc.status, exc.detail)`` and nothing else."""
    if "HTTPException" not in text:
        return []
    tree = _parse(text)
    hits: list[Hit] = []
    for node in ast.walk(tree) if tree else ():
        if not (isinstance(node, ast.ExceptHandler) and node.name and len(node.body) == 1):
            continue
        raised = node.body[0]
        if not (
            isinstance(raised, ast.Raise)
            and isinstance(raised.exc, ast.Call)
            and isinstance(raised.exc.func, ast.Name)
            and raised.exc.func.id == "HTTPException"
        ):
            continue
        call = raised.exc
        keywords = {k.arg: k.value for k in call.keywords}
        status = call.args[0] if call.args else keywords.get("status_code")
        detail = call.args[1] if len(call.args) > 1 else keywords.get("detail")
        if len(call.args) + len(keywords) == 2 and (
            _is_status_attribute(status, node.name, "status")
            and _is_status_attribute(detail, node.name, "detail")
        ):
            hits.append((node.lineno, f"except ... as {node.name}: raise HTTPException(...)"))
    return sorted(hits)


#: ``EXAMPLE_YAML = ...`` / ``EXAMPLE_YAML: Path = ...`` in column 0: module level.
_EXAMPLE_YAML_ASSIGNMENT = re.compile(r"^EXAMPLE_YAML\s*(?::[^=\n]+)?=(?!=)", re.MULTILINE)
#: ``Path(__file__)`` then any ``.resolve()`` / ``.absolute()`` calls, then ``.parents[1]``,
#: possibly spread over lines inside parentheses.
_FILE_PARENTS_ONE = re.compile(
    r"Path\(\s*__file__\s*\)(?:\s*\.\s*\w+\(\s*\))*\s*\.\s*parents\s*\[\s*1\s*\]"
)


# --- the rules ------------------------------------------------------------

RULES: tuple[Rule, ...] = (
    Rule(
        name="parquet-glob-literal",
        why=(
            "the session chunk pattern is src.storage.session_files.PARQUET_PATTERN; a literal "
            '"telemetry-*.parquet" is a second copy that a rename of the chunk files would miss '
            "(R24.8; migrate a call site by importing the constant, then drop it from the "
            "allowlist)"
        ),
        roots=("src", "scripts", "tools"),
        suffixes=(".py",),
        find=_regex_finder(r"""["']telemetry-\*\.parquet["']"""),
        exempt=lambda rel: rel == "src/storage/session_files.py",
        allowed={
            **_grant(
                "a literal copy of PARQUET_PATTERN from before the constant was adopted; not "
                "yet migrated (R24.8 does not migrate call sites)",
                {
                    "scripts/classify_sessions.py": 1,
                    "scripts/inventory_cold_starts.py": 2,
                    "src/analysis/behavior_input_inventory.py": 2,
                    "src/analysis/behavior_validation_schema.py": 1,
                    "src/analysis/broadcast_discovery.py": 1,
                    "src/analysis/delivery_evidence.py": 1,
                    "src/preprocessing/drives.py": 2,
                    "src/storage/broadcast_storage.py": 2,
                    "src/storage/sidecar.py": 1,
                    "src/validation/evidence.py": 1,
                    "tools/fuel_comp_fit.py": 2,
                    "tools/fuel_comp_samples.py": 1,
                    "tools/log_to_parquet.py": 1,
                    "tools/make_ground_truth_fixture.py": 2,
                    "tools/make_investigation_cases.py": 2,
                    "tools/make_replay_fixture.py": 2,
                    "tools/migrate_channel_names.py": 1,
                    "tools/pull_report.py": 1,
                    "tools/replay_event_diff.py": 1,
                },
            ),
        },
        bad='chunks = sorted(d.glob("telemetry-*.parquet"))\n',
        good=(
            "from src.storage.session_files import PARQUET_PATTERN\n"
            "chunks = sorted(d.glob(PARQUET_PATTERN))\n"
        ),
    ),
    Rule(
        name="per-row-dataframe-python",
        why=(
            ".iterrows() and .apply(axis=1) run Python once per row: ~100x slower than the "
            "vectorised form on a session frame. Read the columns as lists (df[name].tolist()) "
            "or use itertuples(); the last two loops, in collection/replay.py and "
            "decode/dids.py, went in R23.5 (replay 9x, DID decode 5x faster) and this has "
            "been found three times, so nothing under src/ is allowed one any more"
        ),
        roots=("src",),
        suffixes=(".py",),
        find=_slow_frame_rows,
        allowed={},
        bad=(
            "for _, row in frame.iterrows():\n    pass\n"
            "frame.apply(f, axis=1)\nframe.apply(f, axis='columns')\n"
        ),
        good="frame.apply(f)\nframe.apply(f, axis=0)\nfor row in frame.itertuples():\n    pass\n",
    ),
    Rule(
        name="sessions-ordered-by-directory-name",
        why=(
            "never order sessions by directory name: captures carry prefixes (_capture-, "
            "_tuning-), so name order sorts by prefix first and a capture a month later "
            'compares as "earlier" (AGENTS.md, re-derived four times). Use '
            "src.storage.sessions.session_started_at / latest_session / iter_sessions"
        ),
        roots=("src", "scripts", "tools"),
        suffixes=(".py",),
        find=_sessions_ordered_by_name,
        allowed={
            **_grant(
                "backfills each session independently; nothing takes a first or last",
                {
                    "scripts/backfill_rolling_circumference.py": 1,
                },
            ),
            **_grant(
                "catalog discovery: every directory is indexed independently; nothing takes a "
                "first or last",
                {
                    "scripts/catalog.py": 2,
                },
            ),
            **_grant(
                "classifies and quarantines every session independently; nothing takes a first "
                "or last",
                {
                    "scripts/classify_sessions.py": 1,
                },
            ),
            **_grant(
                "one-time migration; moves every directory independently, and the second hit "
                "lists model/anomaly directories, not sessions",
                {
                    "scripts/migrate_to_profiles.py": 2,
                },
            ),
            **_grant(
                "sibling-session search: every directory is kept or dropped by its own parsed "
                "start time (session_started_at); the name order is only for a stable list",
                {
                    "src/preprocessing/drives.py": 1,
                },
            ),
            **_grant(
                "discover_sessions, the training listing: _-prefixed captures are excluded, so "
                "the remaining names are bare timestamps and name order is time order",
                {
                    "src/preprocessing/session_reader.py": 1,
                },
            ),
            **_grant(
                "the retention tick judges each directory alone by its own parsed start "
                "(_live_started_at); nothing takes a first or last from the list",
                {
                    "src/storage/retention.py": 2,
                },
            ),
            **_grant(
                "audit visits every session; no first or last taken at the listing (downstream "
                "use not reviewed)",
                {
                    "tools/audit_artifact_lineage.py": 1,
                },
            ),
            **_grant(
                "hands the list to the change-detection report in name order; whether the "
                "report assumes time order is not verified (candidate for iter_sessions)",
                {
                    "tools/change_detection_report.py": 1,
                },
            ),
            **_grant(
                "corpus report lists sessions in name order; not time-ordered across capture "
                "prefixes (candidate for iter_sessions)",
                {
                    "tools/corpus_report.py": 1,
                },
            ),
        },
        bad=(
            "newest = max(root.iterdir())\n"
            "found = sorted(p for p in root.iterdir() if p.is_dir())\n"
            "last = sorted(root.glob('20*'))\n"
            "by = sorted(root.iterdir(), key=lambda p: p.name)\n"
        ),
        good=(
            "files = sorted(p for p in d.iterdir() if p.is_file() and p.suffix == '.bin')\n"
            "chunks = sorted(d.glob(PARQUET_PATTERN))\n"
            "by = sorted(d.iterdir(), key=order)\n"
        ),
    ),
    Rule(
        name="dashboard-setinterval",
        why=(
            "hand-rolled setInterval polling overlaps its own requests and leaks a timer per "
            "copy (R08.9 removed it; R28.3 found it regrown). Use hooks/usePolling, hooks/useNow "
            "or useControlResource; hooks/ itself (useTagQueue's flush is a queue, not a poll) "
            "and test files are exempt"
        ),
        roots=("src/dashboard/static/src",),
        suffixes=(".ts", ".tsx"),
        find=_regex_finder(r"\bsetInterval\s*\(", skip_comments=True),
        exempt=lambda rel: (
            "/hooks/" in rel
            or re.search(r"\.(test|spec)\.tsx?$", rel) is not None
            or "/__tests__/" in rel
        ),
        allowed={
            **_grant(
                "a private 1 s clock beside hooks/useNow; R28.3 replaces it with useNow",
                {
                    "src/dashboard/static/src/panels/AlarmsPanel.tsx": 1,
                    "src/dashboard/static/src/panels/NetworkPanel.tsx": 1,
                    "src/dashboard/static/src/panels/ReplayBanner.tsx": 1,
                    "src/dashboard/static/src/panels/ReplayCard.tsx": 1,
                    "src/dashboard/static/src/panels/StatusStrip.tsx": 1,
                    "src/dashboard/static/src/panels/TestSessionPanel.tsx": 1,
                    "src/dashboard/static/src/panels/tuning/TuningContextBar.tsx": 1,
                },
            ),
            **_grant(
                "5 s hand-rolled poll with no in-flight guard; R28.3 moves it to "
                "useControlResource",
                {
                    "src/dashboard/static/src/panels/ControlPanel.tsx": 1,
                    "src/dashboard/static/src/panels/SessionsPanel.tsx": 1,
                },
            ),
            **_grant(
                "job poll; R28.3 makes it a usePolling enabled while the job runs",
                {
                    "src/dashboard/static/src/panels/RescoreControl.tsx": 1,
                },
            ),
            **_grant(
                "a private copy of useNow; R28.3 replaces it with hooks/useNow",
                {
                    "src/dashboard/static/src/panels/tuning/romEditor/SimForm.tsx": 1,
                },
            ),
        },
        bad="const id = window.setInterval(() => tick(), 1000);\n",
        good="// do not use setInterval(\nconst now = useNow(1000);\n",
    ),
    Rule(
        name="tests-import-other-tests",
        why=(
            "a test module is not a library: each import re-executes the provider and a rename "
            "there breaks every importer (R07.5 closed at zero, R30.2 found 58 files). Put the "
            "builder in a plain tests/*_builders.py module (tests/configs.py, tests/builders.py)"
        ),
        roots=("tests",),
        suffixes=(".py",),
        find=_imports_another_test,
        allowed={
            **_grant(
                "imports another test module's private builders; moves to a tests/*_builders.py"
                " module in R30.2 box 2",
                {
                    "tests/test_air_charge.py": 1,
                    "tests/test_behavior_validation_boundaries.py": 1,
                    "tests/test_brain_jobs.py": 1,
                    "tests/test_brain_routes.py": 3,
                    "tests/test_brain_state.py": 3,
                    "tests/test_candidate_promotion.py": 2,
                    "tests/test_capture_catalog_independence.py": 1,
                    "tests/test_capture_request_ledger.py": 3,
                    "tests/test_config_boundaries.py": 1,
                    "tests/test_control_network.py": 1,
                    "tests/test_control_network_wifi.py": 3,
                    "tests/test_detector_evaluation_boundaries.py": 1,
                    "tests/test_dyno_golden.py": 2,
                    "tests/test_dyno_workbench.py": 1,
                    "tests/test_ecu_twin_contract.py": 3,
                    "tests/test_evidence_cards.py": 3,
                    "tests/test_evidence_interventions.py": 1,
                    "tests/test_evidence_retrieval.py": 1,
                    "tests/test_feature_pipeline_boundaries.py": 1,
                    "tests/test_fuel_comp_fit.py": 1,
                    "tests/test_hypothesis_tracking.py": 1,
                    "tests/test_ignition_timing.py": 1,
                    "tests/test_imu_lon_mean.py": 2,
                    "tests/test_imu_speed_hold.py": 2,
                    "tests/test_kline_actions.py": 1,
                    "tests/test_kline_sniff.py": 1,
                    "tests/test_memory_burst_planner.py": 1,
                    "tests/test_memory_burst_traces.py": 1,
                    "tests/test_memory_catalog_timing_chain.py": 1,
                    "tests/test_paired_refusals.py": 3,
                    "tests/test_proposals_live.py": 4,
                    "tests/test_provisioning_netsock.py": 1,
                    "tests/test_provisioning_wifi.py": 1,
                    "tests/test_residual_chain.py": 5,
                    "tests/test_rom_edit_spec.py": 1,
                    "tests/test_rom_editor.py": 2,
                    "tests/test_rom_editor_api.py": 3,
                    "tests/test_rom_editor_private_rom.py": 2,
                    "tests/test_rom_levers.py": 2,
                    "tests/test_rom_levers_parity.py": 2,
                    "tests/test_rom_sim_cli.py": 3,
                    "tests/test_rom_sim_known_answers.py": 2,
                    "tests/test_sequence_cadence.py": 1,
                    "tests/test_sim_assumptions.py": 4,
                    "tests/test_sim_cells.py": 2,
                    "tests/test_sim_design.py": 1,
                    "tests/test_sim_montecarlo.py": 3,
                    "tests/test_sim_report.py": 2,
                    "tests/test_target_phi_private_rom.py": 1,
                    "tests/test_timing_lookup.py": 3,
                    "tests/test_trip_digests_per_trip.py": 1,
                    "tests/test_twin_command_change.py": 4,
                    "tests/test_twin_command_change_private_rom.py": 2,
                    "tests/test_twin_investigation.py": 4,
                    "tests/test_twin_live.py": 7,
                    "tests/test_twin_profile.py": 1,
                    "tests/test_twin_runner.py": 5,
                    "tests/test_vehicle_power.py": 1,
                },
            ),
        },
        bad=(
            "from tests.test_a import x\nfrom tests import test_b\nimport tests.test_c\n"
            "def f():\n    from tests.test_d import y\n"
        ),
        good=(
            "from tests.configs import isolated_config\n"
            "from tests import configs\nimport tests.builders\n"
        ),
    ),
    Rule(
        name="tests-redefine-example-yaml",
        why=(
            "tests/configs.py exports EXAMPLE_YAML; 63 files defined their own in five spellings "
            "(R30.8). Import it from tests.configs, or use the example_config fixture"
        ),
        roots=("tests",),
        suffixes=(".py",),
        find=_located(_EXAMPLE_YAML_ASSIGNMENT, "EXAMPLE_YAML = ..."),
        exempt=lambda rel: rel == "tests/configs.py",
        allowed={
            **_grant(
                "defines its own EXAMPLE_YAML instead of importing tests.configs.EXAMPLE_YAML; "
                "moves to the example_config fixture (R30.8)",
                {
                    "tests/test_alert_budget.py": 1,
                    "tests/test_autotag_script.py": 1,
                    "tests/test_benchmark_live_pipeline.py": 1,
                    "tests/test_capture_profiles.py": 1,
                    "tests/test_change_only_can_storage.py": 1,
                    "tests/test_config.py": 1,
                    "tests/test_control_can_activity.py": 1,
                    "tests/test_control_classification.py": 1,
                    "tests/test_control_network.py": 1,
                    "tests/test_control_network_wifi.py": 1,
                    "tests/test_control_obd_request.py": 1,
                    "tests/test_control_passive.py": 1,
                    "tests/test_control_power.py": 1,
                    "tests/test_control_trends.py": 1,
                    "tests/test_control_trigger_poll.py": 1,
                    "tests/test_control_update_routes.py": 1,
                    "tests/test_decoded_mode_signals.py": 1,
                    "tests/test_detector_evaluation_golden.py": 1,
                    "tests/test_did_decode.py": 1,
                    "tests/test_digest.py": 1,
                    "tests/test_evaluate_detectors_default_profile.py": 1,
                    "tests/test_evaluate_detectors_script.py": 1,
                    "tests/test_evaluation_detectors.py": 1,
                    "tests/test_evaluation_summaries.py": 1,
                    "tests/test_false_positive_benchmark.py": 1,
                    "tests/test_feature_policy.py": 1,
                    "tests/test_fingerprint.py": 1,
                    "tests/test_fingerprint_script.py": 1,
                    "tests/test_kline_actions.py": 1,
                    "tests/test_label_sessions_script.py": 1,
                    "tests/test_lstm_autoencoder.py": 1,
                    "tests/test_lstm_inference.py": 1,
                    "tests/test_lstm_training.py": 1,
                    "tests/test_main.py": 1,
                    "tests/test_manifest_producibility.py": 1,
                    "tests/test_pairing_routes.py": 1,
                    "tests/test_parked_state.py": 1,
                    "tests/test_preprocessing_loader.py": 1,
                    "tests/test_preprocessing_segmenter.py": 1,
                    "tests/test_profile_capture.py": 1,
                    "tests/test_profiles.py": 1,
                    "tests/test_replay_script.py": 1,
                    "tests/test_resample_config_construction.py": 1,
                    "tests/test_retrain_script.py": 1,
                    "tests/test_route_table_snapshot.py": 1,
                    "tests/test_scripts_cli.py": 1,
                    "tests/test_sequence_autoencoder_training.py": 1,
                    "tests/test_sequence_bakeoff.py": 1,
                    "tests/test_sequence_dataset.py": 1,
                    "tests/test_sequence_detector_control.py": 1,
                    "tests/test_sequence_registry.py": 1,
                    "tests/test_session_classifier.py": 1,
                    "tests/test_storage_catalog.py": 1,
                    "tests/test_synthetic_fault_eval.py": 1,
                    "tests/test_train_lstm.py": 1,
                    "tests/test_train_tcn.py": 1,
                    "tests/test_trip_report_script.py": 1,
                    "tests/test_trip_summary_sidecar.py": 1,
                    "tests/test_twin_profile.py": 1,
                    "tests/test_vehicle_log_script.py": 1,
                },
            ),
        },
        bad="EXAMPLE_YAML = REPO / 'canary.example.yaml'\n",
        good="from tests.configs import EXAMPLE_YAML\ndef f():\n    EXAMPLE_YAML = 1\n",
    ),
    Rule(
        name="tests-recompute-repo-root",
        why=(
            "176 test files recomputed the repo root from Path(__file__) (R30.8); "
            "src.paths.REPO_ROOT exists. Import it, or use the repo_root fixture"
        ),
        roots=("tests",),
        suffixes=(".py",),
        find=_located(_FILE_PARENTS_ONE, "Path(__file__)...parents[1]"),
        allowed={
            **_grant(
                "recomputes the repo root from Path(__file__) instead of src.paths.REPO_ROOT "
                "/ a repo_root fixture (R30.8)",
                {
                    "tests/configs.py": 1,
                    "tests/ecu_twin_vectors.py": 1,
                    "tests/repo_guard.py": 1,
                    "tests/rom_fixtures.py": 1,
                    "tests/test_air_charge.py": 1,
                    "tests/test_air_charge_private_rom.py": 1,
                    "tests/test_alert_budget.py": 1,
                    "tests/test_artifact_lineage.py": 1,
                    "tests/test_autotag_script.py": 1,
                    "tests/test_behavior_lifecycle.py": 1,
                    "tests/test_behavior_models.py": 3,
                    "tests/test_behavior_serving_contracts.py": 1,
                    "tests/test_behavior_validation_boundaries.py": 1,
                    "tests/test_behavior_validation_golden.py": 1,
                    "tests/test_broadcast_correlate.py": 1,
                    "tests/test_budget_level.py": 3,
                    "tests/test_bus_tool.py": 1,
                    "tests/test_canary_token_script.py": 1,
                    "tests/test_capture_catalog_independence.py": 1,
                    "tests/test_capture_profile_preview_parity.py": 1,
                    "tests/test_capture_profiles.py": 1,
                    "tests/test_cold_start_imports.py": 1,
                    "tests/test_committee_checks.py": 1,
                    "tests/test_committee_coverage.py": 1,
                    "tests/test_committee_producers.py": 1,
                    "tests/test_committees_live.py": 1,
                    "tests/test_config.py": 1,
                    "tests/test_config_boundaries.py": 1,
                    "tests/test_config_golden.py": 1,
                    "tests/test_control_can_activity.py": 1,
                    "tests/test_control_classification.py": 1,
                    "tests/test_control_network.py": 1,
                    "tests/test_control_network_wifi.py": 1,
                    "tests/test_control_obd_request.py": 1,
                    "tests/test_control_passive.py": 1,
                    "tests/test_control_power.py": 1,
                    "tests/test_control_trends.py": 1,
                    "tests/test_control_trigger_poll.py": 1,
                    "tests/test_control_update_routes.py": 1,
                    "tests/test_control_updater.py": 1,
                    "tests/test_daemon_restart_guard.py": 1,
                    "tests/test_daemon_shutdown_deadline.py": 1,
                    "tests/test_dashboard_scheduler_fixture.py": 1,
                    "tests/test_data_drive_script.py": 1,
                    "tests/test_decode_uprev_flash.py": 1,
                    "tests/test_decoded_mode_signals.py": 1,
                    "tests/test_decoded_rows_never_stored.py": 1,
                    "tests/test_default_profile_b08_context.py": 1,
                    "tests/test_derived_inputs_declared.py": 1,
                    "tests/test_detector_evaluation_boundaries.py": 1,
                    "tests/test_detector_evaluation_golden.py": 1,
                    "tests/test_detector_roles.py": 1,
                    "tests/test_diagnostic_discovery.py": 1,
                    "tests/test_did_decode.py": 1,
                    "tests/test_did_side_effects.py": 1,
                    "tests/test_digest.py": 1,
                    "tests/test_dyno.py": 1,
                    "tests/test_dyno_boundaries.py": 1,
                    "tests/test_dyno_golden.py": 1,
                    "tests/test_dyno_inputs.py": 1,
                    "tests/test_ecu_application_model.py": 1,
                    "tests/test_ecu_calibration_id.py": 1,
                    "tests/test_ecu_overrides.py": 1,
                    "tests/test_ecu_state_models.py": 1,
                    "tests/test_ecu_twin_contract.py": 1,
                    "tests/test_ecu_twin_state_vectors.py": 1,
                    "tests/test_engine_dynamics.py": 1,
                    "tests/test_epoch_seconds_conversion.py": 1,
                    "tests/test_evaluate_detectors_default_profile.py": 1,
                    "tests/test_evaluate_detectors_script.py": 1,
                    "tests/test_evaluation_detectors.py": 1,
                    "tests/test_evaluation_summaries.py": 1,
                    "tests/test_feature_pipeline_boundaries.py": 1,
                    "tests/test_feature_pipeline_golden.py": 1,
                    "tests/test_feature_pipeline_incremental.py": 1,
                    "tests/test_feature_policy.py": 1,
                    "tests/test_fingerprint.py": 1,
                    "tests/test_fingerprint_script.py": 1,
                    "tests/test_fuel_comp_fit.py": 1,
                    "tests/test_hat_adc.py": 1,
                    "tests/test_imu_collector.py": 1,
                    "tests/test_install_pi_script.py": 1,
                    "tests/test_kline_actions.py": 1,
                    "tests/test_kline_decode.py": 1,
                    "tests/test_label_sessions_script.py": 1,
                    "tests/test_lambda_delivery_live.py": 1,
                    "tests/test_layering.py": 1,
                    "tests/test_llm_assist_import_isolation.py": 1,
                    "tests/test_lstm_autoencoder.py": 1,
                    "tests/test_main.py": 1,
                    "tests/test_manifest_producibility.py": 1,
                    "tests/test_memory_burst_planner.py": 1,
                    "tests/test_memory_catalog_injector.py": 1,
                    "tests/test_memory_catalog_knock_lambda.py": 1,
                    "tests/test_memory_catalog_timing_chain.py": 1,
                    "tests/test_memory_channels.py": 1,
                    "tests/test_memory_rate_weights.py": 1,
                    "tests/test_memory_relations.py": 1,
                    "tests/test_memory_window_burst.py": 1,
                    "tests/test_migrate_channel_names.py": 1,
                    "tests/test_operating_context.py": 1,
                    "tests/test_pairing_routes.py": 1,
                    "tests/test_paths.py": 1,
                    "tests/test_physical_drives.py": 1,
                    "tests/test_plausibility.py": 1,
                    "tests/test_plausibility_live_alerts.py": 1,
                    "tests/test_prepare_image.py": 1,
                    "tests/test_preprocessing_boundaries.py": 1,
                    "tests/test_preprocessing_loader.py": 1,
                    "tests/test_preprocessing_segmenter.py": 1,
                    "tests/test_process_state_reset.py": 1,
                    "tests/test_profile_capture.py": 1,
                    "tests/test_profile_edit.py": 1,
                    "tests/test_profiles.py": 1,
                    "tests/test_provisioning_netsock.py": 1,
                    "tests/test_recipe_boundaries.py": 1,
                    "tests/test_replay_script.py": 1,
                    "tests/test_resample_config_construction.py": 1,
                    "tests/test_restart_canary_script.py": 1,
                    "tests/test_retrain_script.py": 2,
                    "tests/test_role_gate.py": 1,
                    "tests/test_rom_edit_spec.py": 1,
                    "tests/test_rom_editor.py": 2,
                    "tests/test_rom_effects.py": 1,
                    "tests/test_rom_extent.py": 1,
                    "tests/test_rom_levers.py": 1,
                    "tests/test_rom_levers_parity.py": 1,
                    "tests/test_rom_library_api.py": 1,
                    "tests/test_rom_sim_cli.py": 1,
                    "tests/test_rom_targets.py": 1,
                    "tests/test_route_table_snapshot.py": 1,
                    "tests/test_scoring_continuity.py": 1,
                    "tests/test_script_cli_snapshot.py": 1,
                    "tests/test_script_imports.py": 1,
                    "tests/test_scripts_cli.py": 1,
                    "tests/test_sensor_can.py": 1,
                    "tests/test_sequence_autoencoder_training.py": 1,
                    "tests/test_sequence_bakeoff.py": 1,
                    "tests/test_sequence_cadence.py": 1,
                    "tests/test_sequence_calibration.py": 1,
                    "tests/test_sequence_dataset.py": 1,
                    "tests/test_sequence_detector_control.py": 1,
                    "tests/test_sequence_feature_set.py": 1,
                    "tests/test_sequence_registry.py": 1,
                    "tests/test_sequence_scoring.py": 1,
                    "tests/test_session_classifier.py": 1,
                    "tests/test_sim_cells.py": 1,
                    "tests/test_sim_contract.py": 1,
                    "tests/test_sim_design.py": 1,
                    "tests/test_sim_montecarlo.py": 1,
                    "tests/test_sim_report.py": 1,
                    "tests/test_startup_timeline.py": 1,
                    "tests/test_storage_catalog.py": 1,
                    "tests/test_supply_voltage.py": 1,
                    "tests/test_target_phi.py": 1,
                    "tests/test_time_lookup.py": 1,
                    "tests/test_timing_compare.py": 1,
                    "tests/test_torque_plant.py": 1,
                    "tests/test_train_lstm.py": 1,
                    "tests/test_train_sequence_columns.py": 1,
                    "tests/test_train_tcn.py": 1,
                    "tests/test_trainability.py": 1,
                    "tests/test_trip_report_script.py": 1,
                    "tests/test_trip_summary_sidecar.py": 1,
                    "tests/test_twin_command_change.py": 1,
                    "tests/test_twin_profile.py": 1,
                    "tests/test_twin_runner.py": 1,
                    "tests/test_update_canary_script.py": 1,
                    "tests/test_update_runner.py": 1,
                    "tests/test_usb_adc.py": 1,
                    "tests/test_usb_adc_firmware_filter.py": 1,
                    "tests/test_usb_adc_v2.py": 1,
                    "tests/test_vehicle_log_script.py": 1,
                    "tests/test_yaml_load.py": 1,
                },
            ),
        },
        bad=(
            "REPO = Path(__file__)"
            ".resolve().parents[1]\n"
            "ROOT = (\n    Path(__file__)\n    .resolve()\n    .parents[1]\n)\n"
        ),
        good=(
            "from src.paths import REPO_ROOT\n"
            "here = Path(__file__).resolve().parents[0]\nother = Path(x).parents[1]\n"
        ),
    ),
    Rule(
        name="hand-written-refusal-reason",
        why=(
            "an exception that carries a stable `reason` subclasses src.refusal.ReasonedRefusal "
            "(R24.1): the base owns `reason`/`detail` and the pickle rule, and an exception that "
            "wrote its own __init__ without a __reduce__ lost its fields on the trip back from "
            "the workbench's spawn child (E09.2c). Subclass it instead of writing "
            "`self.reason = reason`"
        ),
        roots=("src",),
        suffixes=(".py",),
        find=_hand_written_reason,
        exempt=lambda rel: rel == "src/refusal.py",
        allowed={},
        bad=(
            "class SomeRefusalError(ValueError):\n"
            "    def __init__(self, reason: str, message: str) -> None:\n"
            "        super().__init__(f'{reason}: {message}')\n"
            "        self.reason = reason\n"
        ),
        good=(
            "from src.refusal import ReasonedRefusal\n"
            "class SomeRefusalError(ReasonedRefusal, ValueError):\n"
            "    pass\n"
            "class Tally:\n"
            "    def __init__(self, reason: str) -> None:\n"
            "        self.reason = reason\n"
        ),
    ),
    Rule(
        name="remap-only-http-exception",
        why=(
            "a control refusal is a src.control.status_error.StatusError and create_app registers "
            "one handler that answers it with its own status and body (R29.3): an `except X as "
            "exc: raise HTTPException(exc.status, exc.detail)` block is the hand-written copy of "
            "that handler, and a route that forgot it answered 500. Delete the block; keep one "
            "that changes the status, adds a field or logs"
        ),
        roots=("src",),
        suffixes=(".py",),
        find=_remap_only_http_exception,
        allowed={},
        bad=(
            "try:\n"
            "    run()\n"
            "except Refused as exc:\n"
            "    raise HTTPException(exc.status, exc.detail) from exc\n"
            "try:\n"
            "    run()\n"
            "except Refused as err:\n"
            "    raise HTTPException(status_code=err.status, detail=err.detail) from err\n"
        ),
        good=(
            "try:\n"
            "    run()\n"
            "except Refused as exc:\n"
            "    raise HTTPException(exc.status, {**exc.detail, 'laptop_path': path}) from exc\n"
            "try:\n"
            "    run()\n"
            "except Refused as exc:\n"
            "    log.warning('refused')\n"
            "    raise HTTPException(exc.status, exc.detail) from exc\n"
            "try:\n"
            "    run()\n"
            "except Refused as exc:\n"
            "    raise HTTPException(409, exc.detail) from exc\n"
        ),
    ),
)


# --- machinery ------------------------------------------------------------


def _walk(root: Path, relative: str, suffixes: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    for directory, dirs, files in os.walk(root / relative):
        dirs[:] = sorted(d for d in dirs if d not in SKIPPED_DIRS)
        base = Path(directory).relative_to(root)
        found += [(base / f).as_posix() for f in sorted(files) if f.endswith(suffixes)]
    return found


def scan(rule: Rule, root: Path = REPO) -> dict[str, list[Hit]]:
    """Every hit of ``rule`` under ``root``, by repo-relative path."""
    hits: dict[str, list[Hit]] = {}
    for relative in rule.roots:
        for rel in _walk(root, relative, rule.suffixes):
            if rule.exempt(rel):
                continue
            found = rule.find(rel, (root / rel).read_text(encoding="utf-8", errors="replace"))
            if found:
                hits[rel] = found
    return hits


def verdict(rule: Rule, hits: Mapping[str, list[Hit]]) -> list[str]:
    """The ways ``hits`` break the ratchet, each naming the rule and the place."""
    problems: list[str] = []
    for rel, found in sorted(hits.items()):
        listed = rule.allowed.get(rel)
        where = ", ".join(f"{rel}:{line}" for line, _ in found)
        if listed is None:
            problems.append(f"[{rule.name}] {where}: {found[0][1]} -- {rule.why}")
        elif len(found) > listed[0]:
            problems.append(
                f"[{rule.name}] {rel} has {len(found)} hits, {listed[0]} allowed "
                f"({listed[1]}); new one among {where} -- {rule.why}"
            )
        elif len(found) < listed[0]:
            problems.append(
                f"[{rule.name}] {rel} is down to {len(found)} hits: lower its allowlist "
                f"count from {listed[0]} to {len(found)}"
            )
    for rel in sorted(set(rule.allowed) - set(hits)):
        problems.append(
            f"[{rule.name}] {rel} no longer has the pattern: remove it from the allowlist"
        )
    return problems


@cache
def _scan_repo(name: str) -> dict[str, list[Hit]]:
    return scan(next(rule for rule in RULES if rule.name == name))


def _by_name() -> list[object]:
    return [pytest.param(rule, id=rule.name) for rule in RULES]


@pytest.mark.parametrize("rule", _by_name())
def test_the_allowlist_only_shrinks(rule: Rule) -> None:
    problems = verdict(rule, _scan_repo(rule.name))
    assert not problems, "\n  " + "\n  ".join(problems)


@pytest.mark.parametrize("rule", _by_name())
def test_the_predicate_flags_the_bad_snippet_and_not_the_good_one(rule: Rule) -> None:
    probe = f"{rule.roots[0]}/probe{rule.suffixes[0]}"
    assert rule.find(probe, rule.bad), f"{rule.name}: the predicate misses its own bad example"
    assert not rule.find(probe, rule.good), f"{rule.name}: the predicate flags its good example"


@pytest.mark.parametrize("rule", _by_name())
def test_a_violation_in_a_scanned_file_is_reported(rule: Rule, tmp_path: Path) -> None:
    target = tmp_path / rule.roots[0] / f"brand_new_violation{rule.suffixes[0]}"
    target.parent.mkdir(parents=True)
    target.write_text(rule.bad)
    hits = scan(rule, tmp_path)
    relative = target.relative_to(tmp_path).as_posix()
    assert relative in hits
    problems = verdict(rule, hits)
    assert any(relative in p and rule.name in p and rule.why in p for p in problems)


def test_the_ratchet_reports_growth_shrinkage_and_stale_entries() -> None:
    rule = Rule(
        name="probe",
        why="the incident",
        roots=("x",),
        suffixes=(".py",),
        find=_regex_finder("BAD"),
        allowed={"x/a.py": (1, "old"), "x/b.py": (2, "old"), "x/gone.py": (1, "old")},
    )
    hits = {"x/a.py": [(3, "BAD"), (9, "BAD")], "x/b.py": [(1, "BAD")], "x/new.py": [(2, "BAD")]}
    problems = verdict(rule, hits)
    assert len(problems) == 4
    assert any("x/a.py has 2 hits, 1 allowed" in p and "the incident" in p for p in problems)
    assert any("x/b.py is down to 1" in p and "from 2 to 1" in p for p in problems)
    assert any("x/new.py:2" in p and "the incident" in p for p in problems)
    assert any("x/gone.py no longer has the pattern: remove it" in p for p in problems)
    assert (
        verdict(
            rule,
            {"x/a.py": [(1, "BAD")], "x/b.py": [(1, "BAD"), (2, "BAD")], "x/gone.py": [(1, "BAD")]},
        )
        == []
    )


def test_every_rule_has_a_unique_name_and_a_reason() -> None:
    names = [rule.name for rule in RULES]
    assert len(names) == len(set(names))
    assert all(rule.why and rule.bad and rule.good for rule in RULES)
    assert all(reason for rule in RULES for _, reason in rule.allowed.values())


def test_the_scan_is_by_file_and_skips_vendored_trees(tmp_path: Path) -> None:
    for sub in ("src/a", "src/node_modules/b", "src/dist", "src/.venv/c"):
        (tmp_path / sub).mkdir(parents=True)
        (tmp_path / sub / "m.py").write_text("x = 1\n")
    assert _walk(tmp_path, "src", (".py",)) == ["src/a/m.py"]
