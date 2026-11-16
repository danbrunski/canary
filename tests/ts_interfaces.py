"""A small reader for the dashboard's TypeScript response types.

The dashboard's ``src/types/*.ts`` restate the shapes the Python daemon serves
(codegen was declined, R08), so nothing but a test stops the two drifting.
``tests/test_dashboard_ts_contracts.py`` builds the real payload in Python and
compares it with what is declared here; this module is the TypeScript half: it
lists an interface's members, the literal values of a union and the strings of
a ``const X = [...]`` array.

It reads what this codebase writes, not TypeScript. Interface bodies are
``name?: type;`` members, with ``/** */`` and ``//`` comments, ``readonly``
and nested ``{ ... }`` types. Anything it does not understand (an index
signature, an unbalanced brace) raises, never skips: a reader that silently
drops a member would make a contract test pass on a type it did not read.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.paths import REPO_ROOT

STATIC = REPO_ROOT / "src" / "dashboard" / "static"
TYPES = STATIC / "src" / "types"
LIB = STATIC / "src" / "lib"

_MEMBER = re.compile(
    r'^(?:readonly\s+)?(?P<name>[A-Za-z_$][\w$]*|"[^"]+")(?P<optional>\?)?\s*:\s*(?P<type>.+)$',
    re.DOTALL,
)
_PRIMITIVES = {"number", "string", "boolean", "null", "undefined", "unknown", "any"}


@dataclass(frozen=True)
class TsField:
    name: str
    optional: bool
    type: str

    def nested(self) -> dict[str, TsField] | None:
        """The members of an inline ``{ ... }`` type (``| null`` ignored), else ``None``."""
        text = _strip_nullish(self.type)
        return object_members(text) if text.startswith("{") else None


def strip_comments(text: str) -> str:
    """``text`` without ``//`` and ``/* */`` comments; string literals are left alone."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in "\"'`":
            end = i + 1
            while end < n and text[end] != ch:
                end += 2 if text[end] == "\\" else 1
            out.append(text[i : end + 1])
            i = end + 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.index("*/", i + 2)
            out.append(" ")
            i = end + 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _matching_brace(text: str, start: int) -> int:
    """Index of the ``}`` closing the ``{`` at ``start`` (``text`` has no comments)."""
    depth = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch in "\"'`":
            end = i + 1
            while end < len(text) and text[end] != ch:
                end += 2 if text[end] == "\\" else 1
            i = end
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError(f"unbalanced brace at offset {start}")


def _split_members(body: str) -> list[str]:
    """Top-level members of an interface body: split on ``;``
    outside braces, brackets, parentheses and strings."""
    members: list[str] = []
    depth = 0
    current: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch in "\"'`":
            end = i + 1
            while end < len(body) and body[end] != ch:
                end += 2 if body[end] == "\\" else 1
            current.append(body[i : end + 1])
            i = end + 1
            continue
        if ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth -= 1
        if ch == ";" and depth == 0:
            members.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    members.append("".join(current))
    return [member.strip() for member in members if member.strip()]


def object_members(body_with_braces: str) -> dict[str, TsField]:
    """The members of ``{ a: T; b?: U }`` (the text starts at the ``{``)."""
    text = strip_comments(body_with_braces).strip()
    if not text.startswith("{"):
        raise ValueError(f"not an object type: {text[:40]!r}")
    end = _matching_brace(text, 0)
    fields: dict[str, TsField] = {}
    for member in _split_members(text[1:end]):
        match = _MEMBER.match(member)
        if match is None:
            raise ValueError(f"cannot read the member {member[:60]!r}")
        name = match["name"].strip('"')
        fields[name] = TsField(name, bool(match["optional"]), " ".join(match["type"].split()))
    return fields


def interface(path: Path, name: str) -> dict[str, TsField]:
    """The members of ``export interface <name> { ... }`` in ``path``."""
    text = strip_comments(path.read_text())
    match = re.search(rf"export interface {re.escape(name)}\s*(?:<[^>{{]*>)?\s*{{", text)
    if match is None:
        raise ValueError(f"no `export interface {name}` in {path}")
    start = match.end() - 1
    return object_members(text[start : _matching_brace(text, start) + 1])


def _strip_nullish(type_text: str) -> str:
    parts = [part.strip() for part in _split_union(type_text)]
    kept = [part for part in parts if part not in {"null", "undefined"}]
    return " | ".join(kept) if kept else type_text


def _split_union(type_text: str) -> list[str]:
    """The ``|`` alternatives of a type, outside braces, brackets, parentheses, angles."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    i = 0
    while i < len(type_text):
        ch = type_text[i]
        if ch == '"':
            end = type_text.index('"', i + 1)
            current.append(type_text[i : end + 1])
            i = end + 1
            continue
        if ch in "{[(<":
            depth += 1
        elif ch in "}])>" and not (ch == ">" and type_text[i - 1] == "="):
            depth -= 1
        if ch == "|" and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def union_literals(path: Path, type_name: str) -> list[str]:
    """The string literals of ``export type <type_name> = "a" | "b";``, in order."""
    text = strip_comments(path.read_text())
    match = re.search(rf"export type {re.escape(type_name)}\s*=\s*([^;]*);", text)
    if match is None:
        raise ValueError(f"no `export type {type_name}` in {path}")
    return re.findall(r'"([^"]*)"', match.group(1))


def const_strings(path: Path, const_name: str) -> list[str]:
    """The strings of ``export const <const_name> = [ ... ]`` (``as const`` or not), in order."""
    text = strip_comments(path.read_text())
    match = re.search(rf"export const {re.escape(const_name)}\b[^=]*=\s*\[(.*?)\]", text, re.DOTALL)
    if match is None:
        raise ValueError(f"no `export const {const_name}` array in {path}")
    return re.findall(r'"([^"]*)"', match.group(1))


def const_number(path: Path, const_name: str) -> int | float:
    """The value of ``export const <const_name> = 1024;``."""
    text = strip_comments(path.read_text())
    match = re.search(rf"export const {re.escape(const_name)}\b[^=]*=\s*([0-9_.]+)\s*;", text)
    if match is None:
        raise ValueError(f"no numeric `export const {const_name}` in {path}")
    raw = match.group(1).replace("_", "")
    return float(raw) if "." in raw else int(raw)


# ---------------------------------------------------------------------------
# Comparing a real payload with a declared interface
# ---------------------------------------------------------------------------


def _kinds(type_text: str) -> set[str] | None:
    """The JSON kinds a type of primitives and literals allows, or ``None`` when it
    names anything else (an alias, an object, an array) and cannot be judged here."""
    kinds: set[str] = set()
    for part in _split_union(type_text):
        if part in {"unknown", "any"}:
            return None
        if part in _PRIMITIVES:
            kinds.add(part)
        elif part.startswith('"'):
            kinds.add("string")
        elif part in {"true", "false"}:
            kinds.add("boolean")
        elif re.fullmatch(r"-?[0-9.]+", part):
            kinds.add("number")
        else:
            return None
    return kinds


def _json_kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return "other"


def compare(
    label: str,
    payloads: Iterable[Mapping[str, Any]],
    declared: Mapping[str, TsField],
    *,
    payload_only_ok: Iterable[str] = (),
) -> list[str]:
    """Every way the declared type and the real payload(s) disagree, as sentences.

    ``payloads`` are alternatives (a block that appears only sometimes, a field
    present only on one branch): their keys are unioned. A payload key the type
    lacks, a *required* member no payload carries, and a value whose JSON kind
    the type's primitives rule out (a ``bool`` under ``Record<string, number |
    null>``; a ``None`` under ``number``) are each reported. An optional (``?``)
    member may be absent. Inline object members are compared the same way.
    """
    payload_list = list(payloads)
    seen: dict[str, list[Any]] = {}
    for payload in payload_list:
        for key, value in payload.items():
            seen.setdefault(key, []).append(value)
    problems: list[str] = []
    ignored = set(payload_only_ok)
    missing_in_ts = sorted(set(seen) - set(declared) - ignored)
    if missing_in_ts:
        problems.append(f"{label}: the payload carries {missing_in_ts}, the TS type does not")
    absent = sorted(
        name for name, field in declared.items() if not field.optional and name not in seen
    )
    if absent:
        problems.append(
            f"{label}: the TS type requires {absent}, the payload never carries them "
            "(mark them `?` or remove them)"
        )
    for name, values in seen.items():
        field = declared.get(name)
        if field is None:
            continue
        allowed = _kinds(field.type)
        if allowed is not None:
            for value in values:
                kind = _json_kind(value)
                if kind not in allowed:
                    problems.append(
                        f"{label}.{name}: the payload carries {kind} ({value!r}); "
                        f"the TS type is `{field.type}`"
                    )
                    break
        inner = field.nested()
        if inner is not None:
            dicts = [value for value in values if isinstance(value, Mapping)]
            if dicts:
                problems.extend(compare(f"{label}.{name}", dicts, inner))
    return problems
