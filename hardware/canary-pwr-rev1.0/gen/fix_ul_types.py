# ruff: noqa: E501
"""Correct electrical pin types in the Ultra Librarian symbols (project copies) from the datasheet pin tables."""

import re
import sys

TYPES = {
    "LTC3350EUHF-PBF": {
        "1": "input",
        "2": "bidirectional",
        "3": "open_collector",
        "4": "open_collector",
        "5": "passive",
        "6": "input",
        "7": "input",
        "8": "power_in",
        "9": "passive",
        "10": "input",
        "11": "passive",
        "12": "passive",
        "13": "passive",
        "14": "passive",
        "15": "passive",
        "16": "passive",
        "17": "passive",
        "18": "passive",
        "19": "passive",
        "20": "output",
        "21": "passive",
        "22": "passive",
        "23": "power_out",
        "24": "passive",
        "25": "output",
        "26": "passive",
        "27": "output",
        "28": "power_in",
        "29": "power_out",
        "30": "passive",
        "31": "power_in",
        "32": "passive",
        "33": "output",
        "34": "power_in",
        "35": "input",
        "36": "input",
        "37": "input",
        "38": "open_collector",
        "39": "power_in",
    },
    "LM61460AFSQRJRRQ1": {
        "1": "power_in",
        "2": "power_out",
        "3": "power_in",
        "4": "input",
        "5": "open_collector",
        "6": "passive",
        "7": "input",
        "8": "power_in",
        "9": "power_in",
        "10": "output",
        "11": "power_in",
        "12": "power_in",
        "13": "passive",
        "14": "passive",
    },
}
for path in sys.argv[1:]:
    name = path.rsplit("/", 1)[1][:-10]
    s = open(path, encoding="utf-8").read()

    def sub(m, name=name):
        num = re.search(r'\(number "([^"]+)"', m.group(0)).group(1)
        return m.group(0).replace(m.group(1), TYPES[name][num], 1)

    s2, n = re.subn(r'\(pin (\w+) \w+\n(?:(?!\(pin ).)*?\(number "[^"]+"', sub, s, flags=re.S)
    open(path, "w", encoding="utf-8").write(s2)
    print(name, "pins retyped:", n)
