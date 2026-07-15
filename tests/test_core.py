"""Core unit tests: A2L parsing, conversions, offline calibration access."""

import math
from pathlib import Path

import pytest

from incaz.core.a2l import parse_a2l
from incaz.core.calibration import CharacteristicAccessor, HexBackend
from incaz.core.conversions import make_converter
from incaz.core.hexfile import MemoryImage

DEMO = Path(__file__).parent.parent / "demo"


@pytest.fixture(scope="module")
def db():
    return parse_a2l(DEMO / "demo.a2l")


@pytest.fixture(scope="module")
def image():
    hex_path = DEMO / "demo.hex"
    if not hex_path.exists():
        import subprocess
        import sys
        subprocess.run([sys.executable, str(DEMO / "make_demo_hex.py")], check=True)
    return MemoryImage(hex_path)


def test_a2l_basics(db):
    assert db.project_name == "INCAZ_Demo"
    assert db.module_name == "DemoECU"
    assert db.byte_order == "MSB_LAST"
    assert len(db.measurements) == 7
    assert len(db.characteristics) == 5
    assert db.measurements["EngineSpeed"].ecu_address == 0x00010004
    assert db.measurements["EngineSpeed"].datatype == "UWORD"


def test_a2l_xcp_if_data(db):
    assert db.xcp_info.protocol == "UDP"
    assert db.xcp_info.address == "127.0.0.1"
    assert db.xcp_info.port == 5555
    assert [e.channel for e in db.xcp_info.events] == [0, 1]
    assert db.xcp_info.events[0].cycle_time_ms == 10.0


def test_conversions_linear(db):
    conv = make_converter(db, "CM_RPM")
    assert conv.raw_to_phys(4000) == 1000.0
    assert conv.phys_to_raw(1000.0) == 4000
    assert conv.unit == "rpm"


def test_conversions_rat_func(db):
    conv = make_converter(db, "CM_TEMP")
    # phys = 0.75*raw - 48
    assert math.isclose(conv.raw_to_phys(187), 0.75 * 187 - 48)
    assert math.isclose(conv.phys_to_raw(92.25), 187)


def test_conversions_verbal(db):
    conv = make_converter(db, "CM_GEAR")
    assert conv.raw_to_phys(3) == "D"
    assert conv.phys_to_raw("D") == 3
    assert conv.is_verbal


def test_scalar_read(db, image):
    be = HexBackend(image)
    acc = CharacteristicAccessor(db, db.characteristics["ScalarGain"])
    assert math.isclose(acc.read(be).phys, 1.0)
    acc = CharacteristicAccessor(db, db.characteristics["SpeedLimit"])
    assert acc.read(be).phys == 6000.0


def test_scalar_write(db, image):
    be = HexBackend(image)
    acc = CharacteristicAccessor(db, db.characteristics["SpeedLimit"])
    acc.write_scalar(be, 5500.0)
    assert acc.read(be).phys == 5500.0
    acc.write_scalar(be, 6000.0)
    assert acc.read(be).phys == 6000.0


def test_curve_read(db, image):
    be = HexBackend(image)
    acc = CharacteristicAccessor(db, db.characteristics["TorqueCurve"])
    val = acc.read(be)
    assert val.kind == "curve"
    assert val.axes[0].phys == [1000.0 * (i + 1) for i in range(8)]
    assert val.phys[0] == 80.0 and val.phys[3] == 250.0


def test_curve_write(db, image):
    be = HexBackend(image)
    acc = CharacteristicAccessor(db, db.characteristics["TorqueCurve"])
    acc.write_fnc_element(be, 2, None, 999.9)
    assert math.isclose(acc.read(be).phys[2], 999.9, abs_tol=0.11)
    acc.write_fnc_element(be, 2, None, 210.0)
    acc.write_axis_element(be, "X", 0, 1100.0)
    assert acc.read(be).axes[0].phys[0] == 1100.0
    acc.write_axis_element(be, "X", 0, 1000.0)


def test_map_read_write(db, image):
    be = HexBackend(image)
    acc = CharacteristicAccessor(db, db.characteristics["IgnitionMap"])
    val = acc.read(be)
    assert val.kind == "map"
    assert len(val.phys) == 6 and len(val.phys[0]) == 6
    # demo formula: deg = 35 - 4*iy + ix
    assert val.phys[0][0] == 35.0
    assert val.phys[2][3] == 35 - 8 + 3
    acc.write_fnc_element(be, 1, 1, 12.5)
    assert acc.read(be).phys[1][1] == 12.5
    acc.write_fnc_element(be, 1, 1, 32.0)


def test_measure_decode(db):
    from incaz.core.a2l.model import decode_value, encode_value
    assert decode_value(b"\x10\x27", "UWORD", "<") == 10000
    assert encode_value(10000, "UWORD", "<") == b"\x10\x27"
    # clamping
    assert encode_value(300, "UBYTE", "<") == b"\xff"
    assert encode_value(-5, "UBYTE", "<") == b"\x00"
