"""Minimal S-expression reader/writer for KiCad files."""

import re


class Sym(str):
    """An unquoted atom."""

    pass


_tok = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))', re.S)


def parse(text):
    pos = 0
    stack = [[]]
    n = len(text)
    while True:
        m = _tok.match(text, pos)
        if not m:
            if text[pos:].strip():
                raise ValueError(f"parse error at {pos}: {text[pos : pos + 40]!r}")
            break
        pos = m.end()
        if m.group(1):
            stack.append([])
        elif m.group(2):
            lst = stack.pop()
            stack[-1].append(lst)
        elif m.group(3) is not None:
            stack[-1].append(m.group(3).replace('\\"', '"').replace("\\\\", "\\"))
        else:
            stack[-1].append(Sym(m.group(4)))
        if pos >= n:
            break
    assert len(stack) == 1, "unbalanced"
    return stack[0]


def q(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def dumps(e, indent=0):
    t = "\t"
    if isinstance(e, list):
        if not e:
            return "()"
        simple = all(not isinstance(x, list) for x in e)
        head = fmt_atom(e[0])
        if simple:
            return "(" + " ".join(fmt_atom(x) for x in e) + ")"
        out = "(" + head
        for x in e[1:]:
            if isinstance(x, list):
                out += "\n" + t * (indent + 1) + dumps(x, indent + 1)
            else:
                out += " " + fmt_atom(x)
        out += "\n" + t * indent + ")"
        return out
    return fmt_atom(e)


def fmt_atom(x):
    if isinstance(x, Sym):
        return str(x)
    if isinstance(x, bool):
        return "yes" if x else "no"
    if isinstance(x, (int,)):
        return str(x)
    if isinstance(x, float):
        s = f"{x:.4f}".rstrip("0").rstrip(".")
        return s if s not in ("-0", "") else "0"
    return q(x)


def find(e, key):
    for x in e:
        if isinstance(x, list) and x and x[0] == key:
            return x
    return None


def findall(e, key):
    return [x for x in e if isinstance(x, list) and x and x[0] == key]
