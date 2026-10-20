# ruff: noqa: E501
"""Write BOM-offboard.csv: what the bank needs besides the parts on its board (the lead and the fixings).

The lead's parts come from hardware/tools/capbank_lead.py, which CANary-PWR's J4 shares.
Usage: offboard.py <BOM-offboard.csv>
"""

import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import capbank_lead  # noqa: E402

FIXINGS = (
    (
        "4",
        "",
        "",
        "Cable tie, miniature, <= 2.5 mm wide, >= 140 mm long (e.g. Panduit PLT1.5M); one round each column of two cells at each tie slot pair",
    ),
    (
        "1",
        "",
        "",
        "Silicone RTV, neutral cure (alkoxy or oxime, not acetoxy: its acetic acid corrodes the leads); a bead under each cell body",
    ),
    ("4", "", "", "M3 screw and standoff, length to suit the enclosure; board holes are 3.2 mm"),
)


def main(out):
    with open(out, "w", newline="") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_ALL)
        w.writerow(("Item", "Qty", "Manufacturer", "MPN", "Description"))
        for item, rows in (("lead", capbank_lead.LEAD), ("fixing", FIXINGS)):
            for qty, mfr, mpn, desc in rows:
                w.writerow((item, qty, mfr, mpn, desc))
    print(f"{os.path.basename(out)} written")


if __name__ == "__main__":
    main(sys.argv[1])
