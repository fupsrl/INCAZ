"""Generate demo/demo.hex - the calibration dataset for the demo project.

The values match the defaults of the XCP slave simulator, so the HEX can be
used both as an INCAZ dataset (offline calibration) and as a ProF flash image.
"""

import struct
from pathlib import Path

from intelhex import IntelHex

CAL_BASE = 0x00020000


def build() -> IntelHex:
    ih = IntelHex()

    def put(addr: int, data: bytes) -> None:
        ih.puts(addr, data)

    put(CAL_BASE + 0x00, struct.pack("<f", 1.0))          # ScalarGain
    put(CAL_BASE + 0x04, struct.pack("<H", 24000))        # SpeedLimit (6000 rpm)
    put(CAL_BASE + 0x06, bytes([187]))                    # FanOnTemp (~92 degC)

    # TorqueCurve: 8 axis points (rpm, 0.25 rpm/bit) + 8 values (0.1 Nm/bit)
    axis = [(1000 + 1000 * i) * 4 for i in range(8)]
    torque = [80, 150, 210, 250, 245, 220, 180, 120]
    put(CAL_BASE + 0x10, struct.pack("<8H", *axis))
    put(CAL_BASE + 0x20, struct.pack("<8h", *[t * 10 for t in torque]))

    # IgnitionMap: 6 x-axis (rpm), 6 y-axis (load %), 6x6 values (0.5 deg/bit - 10)
    x_axis = [(1000 + 1200 * i) * 4 for i in range(6)]
    y_axis = [i * 20 for i in range(6)]
    put(CAL_BASE + 0x40, struct.pack("<6H", *x_axis))
    put(CAL_BASE + 0x4C, struct.pack("<6H", *y_axis))
    values = bytearray()
    for iy in range(6):
        for ix in range(6):
            deg = 35 - 4 * iy + ix
            values.append(int((deg + 10) * 2))
    put(CAL_BASE + 0x58, bytes(values))
    return ih


if __name__ == "__main__":
    out = Path(__file__).parent / "demo.hex"
    build().write_hex_file(out)
    print(f"written: {out}")
