"""U5 (BMP384, rev 2.0 only) register-level diagnostic: is it the command sequence, or the part?

Runs each channel on its own (temp-only, press-only), both together at two
oversampling settings, then normal mode at a valid OSR/ODR pair, printing the
raw words, STATUS and ERR_REG each time. Then compensates the pressure word
with the chip's own NVM trims at the LSM6DSOX (U6) die temperature, because on
the first rev 2.0 board the temperature word is always 0 (CANary-HAT-Rev2.1.md
§13.5.6). Writes only U5's volatile config and U6's CTRL1_XL; run on the Pi:

    python3 baro_diag.py
"""

import struct
import sys
import time

from hat_rev import detect
from smbus2 import SMBus

_rev, _ = detect()
if not _rev["barometer"]:
    sys.exit(f"CANary-HAT rev {_rev['name']} has no barometer: U5 was removed in rev 2.1")

BARO, IMU = 0x77, 0x6B
bus = SMBus(1)


def r1(reg):
    return bus.read_byte_data(BARO, reg)


def wr(reg, val):
    bus.write_byte_data(BARO, reg, val)


def softreset():
    wr(0x7E, 0xB6)
    time.sleep(0.01)


def regs(tag):
    print(
        f"[{tag}] CHIP={r1(0x00):#04x} ERR={r1(0x02):#04x} STATUS={r1(0x03):#04x} "
        f"EVENT={r1(0x10):#04x} PWR={r1(0x1B):#04x} OSR={r1(0x1C):#04x} ODR={r1(0x1D):#04x}"
    )


def data():
    d = bus.read_i2c_block_data(BARO, 0x04, 6)
    return d[0] | d[1] << 8 | d[2] << 16, d[3] | d[4] << 8 | d[5] << 16


def forced(pwr, osr, n, label):
    """One forced conversion per iteration; waits on the drdy bits, not a sleep."""
    wr(0x1C, osr)
    out = []
    for _ in range(n):
        wr(0x1B, pwr)
        t0 = time.monotonic()
        while not r1(0x03) & 0x60 and time.monotonic() - t0 < 0.5:
            time.sleep(0.002)
        status, err = r1(0x03), r1(0x02)  # before DATA: reading DATA clears drdy
        out.append((*data(), status, err))
        time.sleep(0.02)
    ps, ts = [o[0] for o in out], [o[1] for o in out]
    print(
        f"  {label}: STATUS={out[-1][2]:#04x} ERR={out[-1][3]:#04x} "
        f"P={[hex(p) for p in ps[:5]]} T={[hex(t) for t in ts[:5]]}"
        + (f"  spread P={max(ps) - min(ps)} T={max(ts) - min(ts)}" if n > 1 else "")
    )
    return ps


def trims():
    """NVM_PAR_* scaled per BMP384 datasheet §8.4 (floating point)."""
    raw = bytes(bus.read_i2c_block_data(BARO, 0x31, 21))
    t1, t2, t3, *p = struct.unpack("<HHbhhbbHHbbhbb", raw)
    print(f"NVM {raw.hex()}  T1={t1} T2={t2} T3={t3}")
    scale = (2**20, 2**29, 2**32, 2**37, 2**-3, 2**6, 2**8, 2**15, 2**48, 2**48, 2**65)
    pp = [v / s for v, s in zip(p, scale, strict=True)]
    pp[0] = (p[0] - 2**14) / 2**20
    pp[1] = (p[1] - 2**14) / 2**29
    return (t1 * 2**8, t2 / 2**30, t3 / 2**48), pp


def pressure(raw, t, pp):
    p1, p2, p3, p4, p5, p6, p7, p8, p9, p10, p11 = pp
    o1 = p5 + p6 * t + p7 * t * t + p8 * t**3
    o2 = raw * (p1 + p2 * t + p3 * t * t + p4 * t**3)
    return o1 + o2 + raw * raw * (p9 + p10 * t) + raw**3 * p11


def imu_temp():
    bus.write_byte_data(IMU, 0x10, 0x40)  # CTRL1_XL 104 Hz: the temp sensor updates with it
    time.sleep(0.2)
    v = int.from_bytes(bytes(bus.read_i2c_block_data(IMU, 0x20, 2)), "little", signed=True)
    return 25 + v / 256


regs("as found")
_, pp = trims()
softreset()
regs("after softreset")
p0, t0 = data()
print(f"  DATA after reset P={p0:#08x} T={t0:#08x} (datasheet reset value 0x800000 both)")
forced(0x12, 0x00, 1, "forced temp-only ")
softreset()
forced(0x11, 0x00, 1, "forced press-only")
softreset()
forced(0x13, 0x00, 10, "forced both OSR x1  ")
ps = forced(0x13, 0x1D, 5, "forced both OSR p32/t8")
softreset()
wr(0x1C, 0x00)
wr(0x1D, 0x04)  # 12.5 Hz: valid for OSR x1 (conf_err otherwise)
wr(0x1B, 0x33)
time.sleep(0.5)
regs("normal 12.5 Hz")
print("  normal P/T=", [tuple(hex(v) for v in data()) for _ in range(3)])
wr(0x1B, 0x00)

praw = sorted(ps)[len(ps) // 2]
t_imu = imu_temp()
print(f"U6 die temperature {t_imu:.2f} C")
for t in (t_imu - 5, t_imu, t_imu + 5):
    print(f"  P({praw:#08x} @ {t:.1f} C) = {pressure(praw, t, pp) / 1000:.2f} kPa")
