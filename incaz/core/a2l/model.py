"""Data model for the subset of ASAP2 (A2L) used by INCAZ.

Only the objects needed for measurement & calibration are modelled:
MEASUREMENT, CHARACTERISTIC, AXIS_PTS, COMPU_METHOD, COMPU_(V)TAB,
RECORD_LAYOUT, MOD_COMMON/MOD_PAR, GROUP/FUNCTION and the XCP IF_DATA.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Optional

#: ASAP2 datatype -> (struct format char, size in bytes)
DATATYPES: dict[str, tuple[str, int]] = {
    "UBYTE": ("B", 1),
    "SBYTE": ("b", 1),
    "UWORD": ("H", 2),
    "SWORD": ("h", 2),
    "ULONG": ("I", 4),
    "SLONG": ("i", 4),
    "A_UINT64": ("Q", 8),
    "A_INT64": ("q", 8),
    "FLOAT16_IEEE": ("e", 2),
    "FLOAT32_IEEE": ("f", 4),
    "FLOAT64_IEEE": ("d", 8),
}


def datatype_size(datatype: str) -> int:
    return DATATYPES[datatype][1]


def decode_value(data: bytes, datatype: str, byte_order: str = "<"):
    fmt, size = DATATYPES[datatype]
    return struct.unpack(byte_order + fmt, bytes(data[:size]))[0]


def encode_value(value, datatype: str, byte_order: str = "<") -> bytes:
    fmt, _ = DATATYPES[datatype]
    if fmt in "bBhHiIqQ":
        value = int(round(float(value)))
        # clamp to datatype range
        bits = {"b": 8, "B": 8, "h": 16, "H": 16, "i": 32, "I": 32, "q": 64, "Q": 64}[fmt]
        if fmt.isupper():
            value = max(0, min(value, (1 << bits) - 1))
        else:
            value = max(-(1 << (bits - 1)), min(value, (1 << (bits - 1)) - 1))
    return struct.pack(byte_order + fmt, value)


@dataclass
class CompuTab:
    """COMPU_TAB / COMPU_VTAB / COMPU_VTAB_RANGE."""

    name: str
    long_identifier: str = ""
    conversion_type: str = "TAB_VERB"
    #: numeric table: list of (in, out); verbal: list of (in, "text");
    #: verbal range: list of (lower, upper, "text")
    pairs: list = field(default_factory=list)
    default_value: Optional[str] = None


@dataclass
class CompuMethod:
    name: str
    long_identifier: str = ""
    conversion_type: str = "IDENTICAL"  # IDENTICAL | LINEAR | RAT_FUNC | TAB_INTP | TAB_NOINTP | TAB_VERB | FORM
    format: str = "%6.2f"
    unit: str = ""
    coeffs: Optional[tuple] = None          # RAT_FUNC: (a, b, c, d, e, f)
    coeffs_linear: Optional[tuple] = None   # LINEAR: (a, b)
    compu_tab_ref: Optional[str] = None
    formula: Optional[str] = None
    formula_inv: Optional[str] = None


@dataclass
class Measurement:
    name: str
    long_identifier: str = ""
    datatype: str = "UBYTE"
    conversion: str = "NO_COMPU_METHOD"
    resolution: int = 0
    accuracy: float = 0.0
    lower_limit: float = 0.0
    upper_limit: float = 0.0
    ecu_address: Optional[int] = None
    ecu_address_extension: int = 0
    bit_mask: Optional[int] = None
    array_size: Optional[int] = None
    matrix_dim: Optional[tuple] = None
    format: Optional[str] = None
    display_identifier: Optional[str] = None

    @property
    def size(self) -> int:
        return datatype_size(self.datatype)


@dataclass
class AxisDescr:
    attribute: str = "STD_AXIS"  # STD_AXIS | COM_AXIS | FIX_AXIS | RES_AXIS | CURVE_AXIS
    input_quantity: str = "NO_INPUT_QUANTITY"
    conversion: str = "NO_COMPU_METHOD"
    max_axis_points: int = 0
    lower_limit: float = 0.0
    upper_limit: float = 0.0
    axis_pts_ref: Optional[str] = None
    # FIX_AXIS_PAR: offset, shift, number  -> x_i = offset + i * 2^shift
    fix_axis_par: Optional[tuple] = None
    # FIX_AXIS_PAR_DIST: offset, distance, number -> x_i = offset + i * distance
    fix_axis_par_dist: Optional[tuple] = None
    fix_axis_par_list: Optional[list] = None
    format: Optional[str] = None


@dataclass
class Characteristic:
    name: str
    long_identifier: str = ""
    type: str = "VALUE"  # VALUE | CURVE | MAP | VAL_BLK | ASCII | CUBOID
    address: int = 0
    deposit: str = ""    # record layout name
    max_diff: float = 0.0
    conversion: str = "NO_COMPU_METHOD"
    lower_limit: float = 0.0
    upper_limit: float = 0.0
    axis_descrs: list[AxisDescr] = field(default_factory=list)
    number: Optional[int] = None       # VAL_BLK / ASCII element count
    matrix_dim: Optional[tuple] = None
    bit_mask: Optional[int] = None
    read_only: bool = False
    format: Optional[str] = None
    display_identifier: Optional[str] = None
    ecu_address_extension: int = 0


@dataclass
class AxisPts:
    name: str
    long_identifier: str = ""
    address: int = 0
    input_quantity: str = "NO_INPUT_QUANTITY"
    deposit: str = ""
    max_diff: float = 0.0
    conversion: str = "NO_COMPU_METHOD"
    max_axis_points: int = 0
    lower_limit: float = 0.0
    upper_limit: float = 0.0
    read_only: bool = False
    format: Optional[str] = None


@dataclass
class RecordLayoutElement:
    kind: str            # FNC_VALUES | AXIS_PTS_X/Y/Z | NO_AXIS_PTS_X/Y/Z | ...
    position: int
    datatype: str = "UBYTE"
    index_mode: str = "COLUMN_DIR"   # FNC_VALUES: COLUMN_DIR | ROW_DIR | ALTERNATE_*
    index_order: str = "INDEX_INCR"  # AXIS_PTS_*: INDEX_INCR | INDEX_DECR
    address_type: str = "DIRECT"


@dataclass
class RecordLayout:
    name: str
    elements: list[RecordLayoutElement] = field(default_factory=list)
    static_record_layout: bool = False

    def element(self, kind: str) -> Optional[RecordLayoutElement]:
        for e in self.elements:
            if e.kind == kind:
                return e
        return None

    def sorted_elements(self) -> list[RecordLayoutElement]:
        return sorted(self.elements, key=lambda e: e.position)


@dataclass
class MemorySegment:
    name: str
    long_identifier: str = ""
    prg_type: str = "DATA"       # CODE | DATA | RESERVED | ...
    memory_type: str = "FLASH"   # RAM | FLASH | EEPROM | ...
    attribute: str = "INTERN"
    address: int = 0
    size: int = 0
    offsets: tuple = ()


@dataclass
class Group:
    name: str
    long_identifier: str = ""
    is_root: bool = False
    characteristics: list[str] = field(default_factory=list)
    measurements: list[str] = field(default_factory=list)
    sub_groups: list[str] = field(default_factory=list)


@dataclass
class Function:
    name: str
    long_identifier: str = ""
    def_characteristics: list[str] = field(default_factory=list)
    in_measurements: list[str] = field(default_factory=list)
    out_measurements: list[str] = field(default_factory=list)
    loc_measurements: list[str] = field(default_factory=list)


@dataclass
class DaqEvent:
    """Event channel from IF_DATA XCP / DAQ."""

    name: str
    short_name: str = ""
    channel: int = 0
    time_cycle: int = 0
    time_unit: int = 0
    priority: int = 0

    @property
    def cycle_time_ms(self) -> Optional[float]:
        """Cycle time in milliseconds, None for non-cyclic events."""
        if self.time_cycle == 0:
            return None
        # time_unit: 10^(unit-9) seconds per DAQ timestamp unit convention
        factors_ns = {0: 1, 1: 10, 2: 100, 3: 1_000, 4: 10_000, 5: 100_000,
                      6: 1_000_000, 7: 10_000_000, 8: 100_000_000, 9: 1_000_000_000}
        ns = factors_ns.get(self.time_unit, 1_000_000) * self.time_cycle
        return ns / 1_000_000


@dataclass
class XcpTransportInfo:
    """Best-effort extraction from IF_DATA XCP (ethernet transport + events)."""

    protocol: Optional[str] = None   # "UDP" | "TCP"
    address: Optional[str] = None
    port: Optional[int] = None
    events: list[DaqEvent] = field(default_factory=list)


@dataclass
class A2LDatabase:
    project_name: str = ""
    module_name: str = ""
    header_comment: str = ""
    byte_order: str = "MSB_LAST"  # MSB_LAST = little endian (Intel)
    measurements: dict[str, Measurement] = field(default_factory=dict)
    characteristics: dict[str, Characteristic] = field(default_factory=dict)
    axis_pts: dict[str, AxisPts] = field(default_factory=dict)
    compu_methods: dict[str, CompuMethod] = field(default_factory=dict)
    compu_tabs: dict[str, CompuTab] = field(default_factory=dict)
    record_layouts: dict[str, RecordLayout] = field(default_factory=dict)
    memory_segments: list[MemorySegment] = field(default_factory=list)
    groups: dict[str, Group] = field(default_factory=dict)
    functions: dict[str, Function] = field(default_factory=dict)
    xcp_info: XcpTransportInfo = field(default_factory=XcpTransportInfo)

    @property
    def struct_byte_order(self) -> str:
        """struct module prefix for the module byte order."""
        return ">" if self.byte_order == "MSB_FIRST" else "<"

    def compu_method(self, name: str) -> Optional[CompuMethod]:
        if name in ("NO_COMPU_METHOD", ""):
            return None
        return self.compu_methods.get(name)
