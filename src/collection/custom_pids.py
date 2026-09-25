"""User-defined custom PIDs with a sandboxed formula evaluator (Task 6.8).

Torque Pro's headline extensibility feature is custom PIDs: the operator
points the tool at an arbitrary Mode 01 / 09 / 22 request and writes a
formula over the response bytes. This module is the data-query,
formula-sandboxed equivalent:

* :class:`CustomPid` — a strict, typed definition (name, request, formula,
  unit, bounds, category, dashboard hint, poll tier). This feature accepts
  only the data-query services 0x01 / 0x09 / 0x22 because its schema describes
  a polled value, not an action. General diagnostic/control requests use the
  separate authenticated `obd_request` path.
* :func:`evaluate_formula` — an AST-walking evaluator with a node and call
  allow-list. No attribute access, indexing, comprehensions, lambdas,
  imports, or arbitrary calls: the only callables are a fixed safe set
  (abs/min/max/round/int/float/pow/sqrt and a two's-complement ``signed``
  helper). The formula sees the response bytes as ``A``..``H`` (Torque's
  convention) plus ``bytes`` for length-aware formulas.
* Torque custom-PID CSV import/export, and versioned YAML persistence
  mirroring ``data/dids.yaml``.

Daemon polling integration — scheduling these alongside the J1979 tiers via
``obd_scheduler.build_poll_requests`` (``kind="custom"`` requests) and
decoding responses into ``custom:<name>`` telemetry channels — is wired in
by Task 2.6 (``src.collection.collectors.default_collectors`` loads
``data/custom_pids.yaml`` once at startup via ``load_custom_pids``, or
leaves it empty if absent).
"""

from __future__ import annotations

import ast
import csv
import io
import math
import operator
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.atomic_write import atomic_write_text
from src.coerce import finite_float

__all__ = [
    "DEFAULT_CUSTOM_PIDS_PATH",
    "CustomPid",
    "CustomPidSet",
    "FormulaError",
    "custom_pids_to_torque_csv",
    "evaluate_custom_pid",
    "evaluate_formula",
    "load_custom_pids",
    "save_custom_pids",
    "torque_csv_to_custom_pids",
]

DEFAULT_CUSTOM_PIDS_PATH = Path("data/custom_pids.yaml")

#: Services that can define a custom *polled value*. The legacy public name is
#: retained for compatibility; 0x04 (clear DTC), 0x2E (write data), and 0x2F
#: (IO control) are actions rather than PIDs and use the general request path.
READ_ONLY_MODES: frozenset[int] = frozenset({0x01, 0x09, 0x22})


class FormulaError(ValueError):
    """The formula is syntactically or semantically unsafe / invalid."""


# ---------------------------------------------------------------------------
# Sandboxed formula evaluator
# ---------------------------------------------------------------------------

_ALLOWED_BINOPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.BitAnd: operator.and_,
    ast.BitOr: operator.or_,
    ast.BitXor: operator.xor,
    ast.LShift: operator.lshift,
    ast.RShift: operator.rshift,
}
_ALLOWED_UNARYOPS: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
    ast.Invert: operator.invert,
    ast.Not: operator.not_,
}
_ALLOWED_COMPARE: dict[type[ast.cmpop], Callable[[Any, Any], Any]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}


def _signed(value: int, bits: int = 8) -> int:
    """Two's-complement reinterpretation of an unsigned integer."""
    sign_bit = 1 << (bits - 1)
    return (int(value) & (sign_bit - 1)) - (int(value) & sign_bit)


_SAFE_FUNCS: dict[str, Callable[..., Any]] = {
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
    "int": int,
    "float": float,
    "pow": pow,
    "sqrt": math.sqrt,
    "signed": _signed,
}

# Everything else is rejected. Notably absent: Attribute, Subscript, Call to
# anything but _SAFE_FUNCS names, Lambda, comprehensions, Starred, walrus.
_ALLOWED_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.Compare,
    ast.IfExp,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.Call,
)


def _validate_ast(node: ast.AST) -> None:
    for child in ast.walk(node):
        if isinstance(child, ast.BinOp) and type(child.op) not in _ALLOWED_BINOPS:
            raise FormulaError(f"operator {type(child.op).__name__} not allowed")
        if isinstance(child, ast.UnaryOp) and type(child.op) not in _ALLOWED_UNARYOPS:
            raise FormulaError(f"unary {type(child.op).__name__} not allowed")
        if isinstance(child, ast.Call):
            if not isinstance(child.func, ast.Name) or child.func.id not in _SAFE_FUNCS:
                raise FormulaError("only the safe math functions may be called")
            if child.keywords:
                raise FormulaError("keyword arguments are not allowed in formulas")
        if not isinstance(
            child, (*_ALLOWED_NODES, ast.operator, ast.unaryop, ast.cmpop, ast.boolop)
        ):
            raise FormulaError(f"syntax element {type(child).__name__} is not allowed")


def _eval(node: ast.AST, names: dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval(node.body, names)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise FormulaError("only numeric literals are allowed")
    if isinstance(node, ast.Name):
        if node.id in names:
            return names[node.id]
        if node.id in _SAFE_FUNCS:
            return _SAFE_FUNCS[node.id]
        raise FormulaError(f"unknown variable {node.id!r}")
    if isinstance(node, ast.BinOp):
        return _ALLOWED_BINOPS[type(node.op)](_eval(node.left, names), _eval(node.right, names))
    if isinstance(node, ast.UnaryOp):
        return _ALLOWED_UNARYOPS[type(node.op)](_eval(node.operand, names))
    if isinstance(node, ast.BoolOp):
        values = [_eval(v, names) for v in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)
    if isinstance(node, ast.Compare):
        left = _eval(node.left, names)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = _eval(comparator, names)
            if not _ALLOWED_COMPARE[type(op)](left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.IfExp):
        return _eval(node.body, names) if _eval(node.test, names) else _eval(node.orelse, names)
    if isinstance(node, ast.Call):
        assert isinstance(node.func, ast.Name)  # _validate_ast guaranteed this
        func = _SAFE_FUNCS[node.func.id]
        return func(*[_eval(a, names) for a in node.args])
    raise FormulaError(f"cannot evaluate {type(node).__name__}")


_BYTE_NAMES = "ABCDEFGH"


def evaluate_formula(formula: str, payload: bytes) -> float | None:
    """Evaluate ``formula`` over ``payload``.

    ``A``..``H`` bind to ``payload[0..7]`` (Torque convention); ``bytes``
    binds to the payload length so a formula can branch on size. A formula
    that references a byte the payload doesn't contain returns ``None``
    (short response — not an error). ``FormulaError`` is raised only for an
    *unsafe or malformed* formula, which is a definition bug, not runtime
    data.
    """
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"invalid formula syntax: {exc}") from exc
    _validate_ast(tree)
    names: dict[str, Any] = {"bytes": len(payload)}
    for i, byte in enumerate(payload[: len(_BYTE_NAMES)]):
        names[_BYTE_NAMES[i]] = byte
    try:
        result = _eval(tree, names)
    except FormulaError as exc:
        # A reference to an absent byte (short response) is a None, not a
        # crash; a genuinely unsafe formula already raised in _validate_ast.
        if "unknown variable" in str(exc):
            return None
        raise
    except (ZeroDivisionError, ValueError, OverflowError):
        return None
    if isinstance(result, bool) or not isinstance(result, (int, float)):
        return None
    return float(result)


# ---------------------------------------------------------------------------
# Custom PID model + persistence
# ---------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CustomPid(_Strict):
    """One user-defined data request plus decode formula."""

    name: str  # canonical channel name (telemetry key)
    display_name: str
    mode: int = Field(ge=0, le=0xFF)
    pid: int = Field(ge=0, le=0xFFFF)
    formula: str
    unit: str = ""
    category: str = "custom"
    minimum: float | None = None
    maximum: float | None = None
    poll_tier: Literal["fast", "medium", "slow", "on_demand"] = "medium"
    dashboard_hint: Literal["gauge", "graph", "text", "indicator", "list"] = "graph"
    #: Optional explicit request CAN ID (e.g. ``0x7E0`` for an engine-ECU
    #: Mode 22). ``None`` ⇒ use the collector's default request ID.
    header: int | None = Field(default=None, ge=0, le=0x1FFFFFFF)

    @field_validator("mode")
    @classmethod
    def _read_only_mode(cls, mode: int) -> int:
        if mode not in READ_ONLY_MODES:
            raise ValueError(
                f"mode 0x{mode:02X} is not a read-only service; custom PIDs may "
                f"only use {sorted(hex(m) for m in READ_ONLY_MODES)}"
            )
        return mode

    @field_validator("formula")
    @classmethod
    def _formula_is_safe(cls, formula: str) -> str:
        # Validate at definition time so a bad formula fails on load, not
        # mid-drive. Sample payload is irrelevant to the safety check.
        try:
            tree = ast.parse(formula, mode="eval")
        except SyntaxError as exc:
            raise ValueError(f"invalid formula syntax: {exc}") from exc
        _validate_ast(tree)
        return formula

    def evaluate(self, payload: bytes) -> float | None:
        """Decode a response payload to ``(value)`` via the formula.

        ``payload`` is the service data after the ``41 PID`` / ``62 DID``
        echo — exactly what ``CanObdClient.query_*().payload`` returns.
        """
        return evaluate_formula(self.formula, payload)


class CustomPidSet(_Strict):
    """Top-level versioned ``custom_pids.yaml`` document."""

    schema_version: int = 1
    custom_pids: list[CustomPid] = Field(default_factory=list)

    @field_validator("custom_pids")
    @classmethod
    def _unique_names(cls, pids: list[CustomPid]) -> list[CustomPid]:
        seen: set[str] = set()
        for pid in pids:
            if pid.name in seen:
                raise ValueError(f"duplicate custom PID name {pid.name!r}")
            seen.add(pid.name)
        return pids


def load_custom_pids(path: Path = DEFAULT_CUSTOM_PIDS_PATH) -> CustomPidSet:
    """Load + validate the custom PID file. Empty set if the file is absent."""
    if not Path(path).exists():
        return CustomPidSet()
    raw = yaml.safe_load(Path(path).read_text()) or {}
    return CustomPidSet.model_validate(raw)


def save_custom_pids(pid_set: CustomPidSet, path: Path = DEFAULT_CUSTOM_PIDS_PATH) -> None:
    """Atomically write the custom PID file (temp + rename)."""
    path = Path(path)
    atomic_write_text(
        path, yaml.safe_dump(pid_set.model_dump(mode="json"), sort_keys=False), mkdir=True
    )


# ---------------------------------------------------------------------------
# Torque Pro CSV interop
# ---------------------------------------------------------------------------

#: Torque exports custom PIDs as CSV with this header (order varies a little
#: in the wild; we map by lower-cased name).
_TORQUE_COLUMNS = [
    "Name",
    "ShortName",
    "ModeAndPID",
    "Equation",
    "Min Value",
    "Max Value",
    "Units",
    "Header",
]


def _parse_mode_and_pid(token: str) -> tuple[int, int]:
    """Parse Torque's ``ModeAndPID`` (e.g. ``220F40``, ``01 0C``, ``2105``).

    First byte = service mode; the remainder = the PID/DID. Whitespace and an
    optional ``0x`` are tolerated.
    """
    cleaned = token.strip().replace(" ", "").lower().removeprefix("0x")
    if len(cleaned) < 4 or len(cleaned) % 2 != 0:
        raise FormulaError(f"unparseable ModeAndPID {token!r}")
    try:
        mode = int(cleaned[:2], 16)
        pid = int(cleaned[2:], 16)
    except ValueError as exc:
        raise FormulaError(f"unparseable ModeAndPID {token!r}") from exc
    return mode, pid


def torque_csv_to_custom_pids(text: str) -> tuple[list[CustomPid], list[str]]:
    """Parse a Torque custom-PID CSV into :class:`CustomPid` records.

    Returns ``(accepted, warnings)``. A row that names a write mode, has a
    bad formula, or an unparseable ModeAndPID is *skipped with a warning* —
    one bad row in an import never drops the rest, and a write-mode row can
    never slip through.
    """
    accepted: list[CustomPid] = []
    warnings: list[str] = []
    reader = csv.DictReader(io.StringIO(text))
    for i, raw_row in enumerate(reader, start=2):  # row 1 is the header
        row = {(k or "").strip().lower(): (v or "") for k, v in raw_row.items()}
        name = row.get("name", "").strip()
        if not name:
            warnings.append(f"row {i}: missing Name; skipped")
            continue
        try:
            mode, pid = _parse_mode_and_pid(row.get("modeandpid", ""))
            custom = CustomPid(
                name=row.get("shortname", "").strip() or name.replace(" ", "_").upper(),
                display_name=name,
                mode=mode,
                pid=pid,
                formula=row.get("equation", "").strip(),
                unit=row.get("units", "").strip(),
                minimum=finite_float(row.get("min value", "")),
                maximum=finite_float(row.get("max value", "")),
                header=(int(row["header"].strip(), 16) if row.get("header", "").strip() else None),
            )
        except (FormulaError, ValueError) as exc:
            warnings.append(f"row {i} ({name!r}): skipped — {exc}")
            continue
        accepted.append(custom)
    return accepted, warnings


def custom_pids_to_torque_csv(pids: list[CustomPid]) -> str:
    """Serialize custom PIDs back to Torque's CSV shape (round-trips)."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_TORQUE_COLUMNS)
    for p in pids:
        writer.writerow(
            [
                p.display_name,
                p.name,
                f"{p.mode:02X}{p.pid:02X}" if p.pid <= 0xFF else f"{p.mode:02X}{p.pid:04X}",
                p.formula,
                "" if p.minimum is None else p.minimum,
                "" if p.maximum is None else p.maximum,
                p.unit,
                "" if p.header is None else f"{p.header:X}",
            ]
        )
    return buf.getvalue()


def evaluate_custom_pid(pid: CustomPid, payload: bytes) -> tuple[float, str] | None:
    """Decode + clamp-check a custom PID. Returns ``(value, unit)`` or ``None``.

    A value outside the declared ``[minimum, maximum]`` band is *still
    returned* (Torque shows it; the operator wants to see the out-of-range
    reading) — bounds are advisory metadata, not a filter.
    """
    value = pid.evaluate(payload)
    if value is None:
        return None
    return (value, pid.unit)
