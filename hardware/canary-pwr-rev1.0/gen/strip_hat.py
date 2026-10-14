# ruff: noqa: E501
"""Strip the HAT board to a template: stackup, layers, setup, title block, Dwgs.User drawings. Usage: strip_hat.py in out"""

import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sexp import dumps, find, parse

tree = parse(open(sys.argv[1], encoding="utf-8").read())[0]
print("top-level:", Counter(x[0] for x in tree if isinstance(x, list)))
keep = {
    "version",
    "generator",
    "generator_version",
    "general",
    "paper",
    "title_block",
    "layers",
    "setup",
    "embedded_fonts",
}
out = [tree[0]]
for x in tree[1:]:
    if not isinstance(x, list):
        out.append(x)
        continue
    if x[0] in keep:
        out.append(x)
    elif x[0].startswith("gr_") and (find(x, "layer") or [None, None])[1] == "Dwgs.User":
        out.append(x)
open(sys.argv[2], "w", encoding="utf-8").write(dumps(out) + "\n")
print("kept:", Counter(x[0] for x in out if isinstance(x, list)))
