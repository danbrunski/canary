# ruff: noqa: E501
"""Write the 3D models that KiCad's library lacks, for CANary-PWR and the cap bank.

KiCad's stock Micro-Fit 3.0 footprints reference header models that its 3D library does not contain.
They are missing from 10.0 and, as of 2026-10-20, from upstream kicad-packages3D too. The cap bank's
cell footprint is its own (canary-capbank-rev1.0/gen/footprints.py). These models fill both gaps, for
the 3D viewer and the boards' STEP export. The other parts use KiCad's stock models.

The models take their plan geometry from the footprints: pin positions, pegs, the body outline on F.Fab,
and the cell's lead pitch and end-face distance. The Micro-Fit's heights are estimates. Molex's drawing
could not be fetched, so the body is drawn 8.4 mm tall with the latch ramp to 9.4 mm. Before using that
height for an enclosure clearance, check it against Molex's own STEP model for 43045-1002.

Model axes are KiCad's: x and y as on the footprint but with y pointing up the page, and z up from
the board's top face. Needs cadquery, which the project venv does not carry:

    python3 -m venv /tmp/cq && /tmp/cq/bin/pip install cadquery
    /tmp/cq/bin/python hardware/tools/gen_3dmodels.py
"""

import importlib.util
import math
import os

import cadquery as cq

HERE = os.path.dirname(os.path.abspath(__file__))
HW = os.path.dirname(HERE)
PWR_MODELS = os.path.join(HW, "canary-pwr-rev1.0", "lib", "3dmodels")
BANK_MODELS = os.path.join(HW, "canary-capbank-rev1.0", "lib", "3dmodels")

BLACK = cq.Color(0.08, 0.08, 0.08)
GOLD = cq.Color(0.86, 0.72, 0.30)
TIN = cq.Color(0.80, 0.80, 0.82)
SLEEVE = cq.Color(0.14, 0.20, 0.36)
STRIPE = cq.Color(0.82, 0.84, 0.86)
ALUMINIUM = cq.Color(0.70, 0.71, 0.73)

BOARD_T = 1.6

# Micro-Fit 3.0 right-angle dual-row header (43045-xx00, gold -xx02 on the same footprint). Plan figures
# are from KiCad's footprint; heights are estimated (see the module docstring).
MF_PITCH = 3.0
MF_SIDE = 3.575  # body edge beyond the outer pins (F.Fab)
MF_FRONT = 8.92  # mating face, ahead of the front row (F.Fab, footprint y -8.92)
MF_BACK = 0.99  # body's back face, behind the front row
MF_H = 8.4  # body height (estimate)
MF_RAMP = 1.0  # the latch ramp above the body (estimate)
MF_ROW1_Z = 2.6  # front-row contact height above the board (estimate)
MF_CAVITY = 6.0  # depth of the receptacle cavity from the mating face (estimate)
MF_PIN = 0.64  # square pin
MF_TAIL = 3.18  # tail below the housing
MF_PEG_D = 2.9  # split peg in a 3.0 mm NPTH
MF_PEG_L = 2.4
MF_PEGS = {3: (3.0,), 5: (2.16, 9.86)}  # peg x positions, by pins per row; footprint y -4.32


def box(x0, y0, z0, x1, y1, z1):
    return cq.Workplane("XY").box(x1 - x0, y1 - y0, z1 - z0, centered=False).translate((x0, y0, z0))


def microfit(per_row):
    """A 43045 right-angle header with per_row pins in each row. Footprint row 1 (pins 1..n) is the
    front row at y 0, under the body; row 2 is 3 mm behind it, outside the body."""
    span = (per_row - 1) * MF_PITCH
    x0, x1 = -MF_SIDE, span + MF_SIDE
    body = box(x0, -MF_BACK, 0, x1, MF_FRONT, MF_H)
    # The receptacle's cavity, open at the mating face.
    z_lo, z_hi = MF_ROW1_Z - 1.6, MF_ROW1_Z + MF_PITCH + 1.6
    body = body.cut(box(-2.0, MF_FRONT - MF_CAVITY, z_lo, span + 2.0, MF_FRONT + 1, z_hi))
    # The latch ramp the receptacle's latch catches, on top at the mating face.
    mid = span / 2
    body = body.union(box(mid - 1.7, MF_FRONT - 3.0, MF_H, mid + 1.7, MF_FRONT, MF_H + MF_RAMP))
    for px in MF_PEGS[per_row]:
        peg = (
            cq.Workplane("XY")
            .circle(MF_PEG_D / 2)
            .extrude(MF_PEG_L)
            .translate((px, 4.32, -MF_PEG_L))
        )
        body = body.union(peg)

    pins = None
    h = MF_PIN / 2
    for y_tail, z in ((0.0, MF_ROW1_Z), (-MF_PITCH, MF_ROW1_Z + MF_PITCH)):
        for i in range(per_row):
            x = i * MF_PITCH
            pin = box(x - h, y_tail - h, z - h, x + h, MF_FRONT - 1.0, z + h)  # along, to the face
            pin = pin.union(box(x - h, y_tail - h, -MF_TAIL, x + h, y_tail + h, z + h))  # the tail
            pins = pin if pins is None else pins.union(pin)
    name = f"Molex_Micro-Fit_3.0_43045-{2 * per_row:02d}00_2x{per_row:02d}_P3.00mm_Horizontal"
    assy = cq.Assembly(name=name)
    assy.add(body, name="housing", color=BLACK)
    assy.add(pins, name="pins", color=GOLD)
    return name, assy


def _bank_footprint_constants():
    path = os.path.join(HW, "canary-capbank-rev1.0", "gen", "footprints.py")
    spec = importlib.util.spec_from_file_location("bank_footprints", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cell():
    """The cap bank's cell lying flat: Eaton TVA1625-3R0256-R at its maximum size, 16.5 x 28.9 mm.
    Pad 1 (+) is the origin and pad 2 (-) 7.5 mm down the page; the body runs along -x from an end face
    HOLE_TO_BODY from the holes, and the leads leave it at the axis height and bend down into the holes."""
    fp = _bank_footprint_constants()
    dia, length, lead_d, bend = 16.5, 28.9, 0.8, 2.0
    r = dia / 2
    y_axis, z_axis = -fp.PITCH / 2, r
    end = -fp.HOLE_TO_BODY
    far = end - length

    def along_x(x0, x1, radius):
        plane = cq.Plane(origin=(x0, y_axis, z_axis), xDir=(0, 1, 0), normal=(1, 0, 0))
        return cq.Workplane(plane).circle(radius).extrude(x1 - x0)

    sleeve = along_x(far, end, r)
    # The polarity stripe runs along the - side, like the bar on the silk.
    stripe = along_x(far + 0.6, end - 0.6, r + 0.03).intersect(
        box(far, y_axis - r - 1, z_axis - 2.6, end, y_axis - r + 1.4, z_axis + 2.6)
    )
    seal = along_x(end - 0.2, end + 0.05, r - 0.9)  # the rubber bung at the lead end
    can = along_x(far - 0.05, far + 0.2, r - 0.9)  # the can's bottom, with the vent, at the far end

    leads = None
    for y in (0.0, -fp.PITCH):
        z_turn = z_axis - bend
        c = math.sqrt(0.5) * bend
        path = cq.Wire.assembleEdges(
            [
                cq.Edge.makeLine(cq.Vector(end, y, z_axis), cq.Vector(-bend, y, z_axis)),
                cq.Edge.makeThreePointArc(
                    cq.Vector(-bend, y, z_axis),
                    cq.Vector(-bend + c, y, z_turn + c),
                    cq.Vector(0, y, z_turn),
                ),
                cq.Edge.makeLine(cq.Vector(0, y, z_turn), cq.Vector(0, y, -BOARD_T - 1.0)),
            ]
        )
        plane = cq.Plane(origin=(end, y, z_axis), xDir=(0, 1, 0), normal=(1, 0, 0))
        lead = cq.Workplane(plane).circle(lead_d / 2).sweep(cq.Workplane().add(path))
        leads = lead if leads is None else leads.union(lead)

    name = fp.NAME
    assy = cq.Assembly(name=name)
    assy.add(sleeve, name="sleeve", color=SLEEVE)
    assy.add(stripe, name="stripe", color=STRIPE)
    assy.add(seal, name="seal", color=BLACK)
    assy.add(can, name="can", color=ALUMINIUM)
    assy.add(leads, name="leads", color=TIN)
    return name, assy


def save(assy, name, *dirs):
    for d in dirs:
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name + ".step")
        assy.export(path) if hasattr(assy, "export") else assy.save(path)
        print("wrote", os.path.relpath(path, HW))


def main():
    name, assy = microfit(5)
    save(assy, name, PWR_MODELS, BANK_MODELS)  # PWR J4 and the bank's J1
    name, assy = microfit(3)
    save(assy, name, PWR_MODELS)  # PWR J3, HAT out
    name, assy = cell()
    save(assy, name, BANK_MODELS)


if __name__ == "__main__":
    main()
