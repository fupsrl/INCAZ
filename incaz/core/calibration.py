"""Characteristic (calibration parameter) access.

Decodes RECORD_LAYOUTs and reads/writes scalar values, VAL_BLKs, curves and
maps from any *memory backend*:

* :class:`HexBackend`  - offline dataset (Intel HEX image)
* online XCP backend   - the connected ECU working page
  (any object with ``read(addr, size)`` / ``write(addr, data)``)

Supported axis types: STD_AXIS (deposited with the characteristic),
COM_AXIS (separate AXIS_PTS object) and FIX_AXIS (computed, read-only).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional, Protocol

from .a2l.model import (
    A2LDatabase,
    AxisDescr,
    Characteristic,
    RecordLayout,
    datatype_size,
    decode_value,
    encode_value,
)
from .conversions import Converter, make_converter

log = logging.getLogger(__name__)


class MemoryBackend(Protocol):
    def read(self, address: int, size: int) -> bytes: ...
    def write(self, address: int, data: bytes) -> None: ...


class HexBackend:
    """Offline dataset backend over a :class:`~incaz.core.hexfile.MemoryImage`."""

    def __init__(self, image):
        self.image = image

    def read(self, address: int, size: int) -> bytes:
        return self.image.read_padded(address, size)

    def write(self, address: int, data: bytes) -> None:
        self.image.write(address, data)


# --------------------------------------------------------------------------- model

@dataclass
class AxisInfo:
    """Resolved axis of a curve/map."""

    kind: str                     # STD_AXIS | COM_AXIS | FIX_AXIS
    count: int
    raw: list = field(default_factory=list)
    phys: list = field(default_factory=list)
    converter: Optional[Converter] = None
    address: Optional[int] = None    # base address of axis points (None: not writable)
    datatype: Optional[str] = None
    index_order: str = "INDEX_INCR"
    editable: bool = False
    unit: str = ""


@dataclass
class CalValue:
    """Decoded characteristic value (physical representation)."""

    kind: str                     # value | array | curve | map | ascii
    phys: object = None           # scalar / str / list / 2D list [iy][ix]
    raw: object = None
    axes: list[AxisInfo] = field(default_factory=list)
    unit: str = ""
    error: Optional[str] = None


class CharacteristicAccessor:
    """Read / write one CHARACTERISTIC through a memory backend."""

    def __init__(self, db: A2LDatabase, char: Characteristic):
        self.db = db
        self.char = char
        self.layout: Optional[RecordLayout] = db.record_layouts.get(char.deposit)
        self.converter = make_converter(db, char.conversion)
        self.bo = db.struct_byte_order

    # ------------------------------------------------------------------ axes
    def _resolve_axis(self, backend: MemoryBackend, axis: AxisDescr,
                      inline_address: Optional[int], inline_datatype: Optional[str],
                      inline_order: str) -> AxisInfo:
        conv = make_converter(self.db, axis.conversion)
        if axis.attribute == "FIX_AXIS":
            raws: list[float] = []
            if axis.fix_axis_par_list:
                raws = list(axis.fix_axis_par_list)
            elif axis.fix_axis_par:
                off, shift, n = axis.fix_axis_par
                raws = [off + i * (2 ** shift) for i in range(int(n))]
            elif axis.fix_axis_par_dist:
                off, dist, n = axis.fix_axis_par_dist
                raws = [off + i * dist for i in range(int(n))]
            info = AxisInfo(kind="FIX_AXIS", count=len(raws), raw=raws,
                            converter=conv, editable=False, unit=conv.unit)
            info.phys = [conv.raw_to_phys(r) for r in raws]
            return info

        if axis.attribute == "COM_AXIS" and axis.axis_pts_ref:
            ap = self.db.axis_pts.get(axis.axis_pts_ref)
            if ap is None:
                return AxisInfo(kind="COM_AXIS", count=axis.max_axis_points or 0)
            rl = self.db.record_layouts.get(ap.deposit)
            base, count, datatype, order = self._axis_pts_layout(backend, ap.address, rl,
                                                                 ap.max_axis_points)
            conv_ap = make_converter(self.db, ap.conversion)
            info = AxisInfo(kind="COM_AXIS", count=count, converter=conv_ap,
                            address=base, datatype=datatype, index_order=order,
                            editable=not ap.read_only, unit=conv_ap.unit)
            self._read_axis_values(backend, info)
            return info

        # STD_AXIS - points deposited inside the characteristic record
        count = axis.max_axis_points or 0
        info = AxisInfo(kind="STD_AXIS", count=count, converter=conv,
                        address=inline_address, datatype=inline_datatype,
                        index_order=inline_order, editable=inline_address is not None,
                        unit=conv.unit)
        if inline_address is not None and inline_datatype is not None:
            self._read_axis_values(backend, info)
        return info

    def _axis_pts_layout(self, backend: MemoryBackend, address: int,
                         rl: Optional[RecordLayout], max_points: int):
        """Resolve (base_address, count, datatype, index_order) of an AXIS_PTS object."""
        offset = address
        count = max_points
        datatype = "UBYTE"
        order = "INDEX_INCR"
        if rl is None:
            return offset, count, datatype, order
        for el in rl.sorted_elements():
            if el.kind == "NO_AXIS_PTS_X":
                stored = decode_value(backend.read(offset, datatype_size(el.datatype)),
                                      el.datatype, self.bo)
                count = min(int(stored), max_points) if stored else max_points
                offset += datatype_size(el.datatype)
            elif el.kind == "AXIS_PTS_X":
                datatype = el.datatype
                order = el.index_order
                return offset, count, datatype, order
            elif el.kind in ("IDENTIFICATION", "RESERVED"):
                offset += datatype_size(el.datatype)
        return offset, count, datatype, order

    def _read_axis_values(self, backend: MemoryBackend, info: AxisInfo) -> None:
        if info.address is None or info.datatype is None or info.count <= 0:
            return
        esize = datatype_size(info.datatype)
        data = backend.read(info.address, esize * info.count)
        raws = [decode_value(data[i * esize:(i + 1) * esize], info.datatype, self.bo)
                for i in range(info.count)]
        if info.index_order == "INDEX_DECR":
            raws.reverse()
        info.raw = raws
        conv = info.converter or Converter(None)
        info.phys = [conv.raw_to_phys(r) for r in raws]

    # ------------------------------------------------------------------ layout walk
    def _walk_layout(self, backend: MemoryBackend):
        """Walk the record layout; returns (axes_by_dim, fnc_address, fnc_element, counts).

        axes_by_dim: {'X': AxisInfo, 'Y': AxisInfo}
        """
        char = self.char
        if self.layout is None:
            raise ValueError(f"Unknown record layout '{char.deposit}' for {char.name}")

        axes: dict[str, AxisInfo] = {}
        counts: dict[str, int] = {}
        # counts default from AXIS_DESCR
        dims = "XYZ"
        for i, ax in enumerate(char.axis_descrs):
            counts[dims[i]] = ax.max_axis_points or 0

        offset = char.address
        fnc_address = None
        fnc_el = None
        for el in self.layout.sorted_elements():
            kind = el.kind
            if kind.startswith("NO_AXIS_PTS_"):
                dim = kind[-1]
                stored = decode_value(backend.read(offset, datatype_size(el.datatype)),
                                      el.datatype, self.bo)
                limit = counts.get(dim, 0)
                counts[dim] = min(int(stored), limit) if limit else int(stored)
                offset += datatype_size(el.datatype)
            elif kind.startswith("AXIS_PTS_") and len(kind) == len("AXIS_PTS_X"):
                dim = kind[-1]
                idx = dims.index(dim)
                axis_descr = char.axis_descrs[idx] if idx < len(char.axis_descrs) else None
                n = counts.get(dim, axis_descr.max_axis_points if axis_descr else 0)
                if axis_descr is not None:
                    info = self._resolve_axis(backend, axis_descr, offset, el.datatype, el.index_order)
                    info.count = min(info.count or n, n) or n
                    axes[dim] = info
                offset += datatype_size(el.datatype) * n
            elif kind in ("IDENTIFICATION", "RESERVED"):
                offset += datatype_size(el.datatype)
            elif kind == "FNC_VALUES":
                fnc_address = offset
                fnc_el = el
                # FNC_VALUES is usually last; if not, we cannot know its size
                # until counts are complete, so keep walking only fixed parts.
                break

        # axes not deposited inline (COM_AXIS / FIX_AXIS)
        for i, ax in enumerate(char.axis_descrs):
            dim = dims[i]
            if dim not in axes:
                info = self._resolve_axis(backend, ax, None, None, "INDEX_INCR")
                if info.count:
                    counts[dim] = min(counts.get(dim, info.count) or info.count, info.count)
                axes[dim] = info

        return axes, fnc_address, fnc_el, counts

    # ------------------------------------------------------------------ read
    def read(self, backend: MemoryBackend) -> CalValue:
        char = self.char
        try:
            if char.type == "VALUE":
                return self._read_value(backend)
            if char.type in ("VAL_BLK", "ASCII"):
                return self._read_array(backend)
            if char.type == "CURVE":
                return self._read_curve(backend)
            if char.type == "MAP":
                return self._read_map(backend)
            return CalValue(kind="value", error=f"Unsupported characteristic type {char.type}")
        except Exception as exc:
            log.exception("Read of %s failed", char.name)
            return CalValue(kind="value", error=str(exc))

    def _fnc_datatype(self) -> str:
        el = self.layout.element("FNC_VALUES") if self.layout else None
        return el.datatype if el else "UBYTE"

    def _read_value(self, backend: MemoryBackend) -> CalValue:
        dt = self._fnc_datatype()
        raw = decode_value(backend.read(self.char.address, datatype_size(dt)), dt, self.bo)
        if self.char.bit_mask:
            raw = raw & self.char.bit_mask
        phys = self.converter.raw_to_phys(raw)
        return CalValue(kind="value", phys=phys, raw=raw, unit=self.converter.unit)

    def _read_array(self, backend: MemoryBackend) -> CalValue:
        dt = self._fnc_datatype()
        esize = datatype_size(dt)
        n = self.char.number or (self.char.matrix_dim[0] if self.char.matrix_dim else 1)
        data = backend.read(self.char.address, esize * n)
        raws = [decode_value(data[i * esize:(i + 1) * esize], dt, self.bo) for i in range(n)]
        if self.char.type == "ASCII":
            text = bytes(int(r) & 0xFF for r in raws).split(b"\0")[0].decode("latin-1", "replace")
            return CalValue(kind="ascii", phys=text, raw=raws)
        phys = [self.converter.raw_to_phys(r) for r in raws]
        return CalValue(kind="array", phys=phys, raw=raws, unit=self.converter.unit)

    def _read_curve(self, backend: MemoryBackend) -> CalValue:
        axes, fnc_addr, fnc_el, counts = self._walk_layout(backend)
        x = axes.get("X") or AxisInfo(kind="STD_AXIS", count=counts.get("X", 0))
        n = x.count or counts.get("X", 0)
        if fnc_addr is None or fnc_el is None:
            raise ValueError("Record layout has no FNC_VALUES")
        esize = datatype_size(fnc_el.datatype)
        data = backend.read(fnc_addr, esize * n)
        raws = [decode_value(data[i * esize:(i + 1) * esize], fnc_el.datatype, self.bo)
                for i in range(n)]
        phys = [self.converter.raw_to_phys(r) for r in raws]
        return CalValue(kind="curve", phys=phys, raw=raws, axes=[x], unit=self.converter.unit)

    def _read_map(self, backend: MemoryBackend) -> CalValue:
        axes, fnc_addr, fnc_el, counts = self._walk_layout(backend)
        x = axes.get("X") or AxisInfo(kind="STD_AXIS", count=counts.get("X", 0))
        y = axes.get("Y") or AxisInfo(kind="STD_AXIS", count=counts.get("Y", 0))
        nx, ny = x.count, y.count
        if fnc_addr is None or fnc_el is None:
            raise ValueError("Record layout has no FNC_VALUES")
        esize = datatype_size(fnc_el.datatype)
        data = backend.read(fnc_addr, esize * nx * ny)
        raws_flat = [decode_value(data[i * esize:(i + 1) * esize], fnc_el.datatype, self.bo)
                     for i in range(nx * ny)]
        rows: list[list] = []
        for iy in range(ny):
            row = []
            for ix in range(nx):
                idx = iy * nx + ix if fnc_el.index_mode != "COLUMN_DIR" else ix * ny + iy
                row.append(raws_flat[idx])
            rows.append(row)
        phys = [[self.converter.raw_to_phys(r) for r in row] for row in rows]
        return CalValue(kind="map", phys=phys, raw=rows, axes=[x, y], unit=self.converter.unit)

    # ------------------------------------------------------------------ write
    def write_scalar(self, backend: MemoryBackend, phys) -> None:
        dt = self._fnc_datatype()
        raw = self.converter.phys_to_raw(phys)
        backend.write(self.char.address, encode_value(raw, dt, self.bo))

    def write_ascii(self, backend: MemoryBackend, text: str) -> None:
        n = self.char.number or len(text)
        data = text.encode("latin-1", "replace")[:n].ljust(n, b"\0")
        backend.write(self.char.address, data)

    def write_array_element(self, backend: MemoryBackend, index: int, phys) -> None:
        dt = self._fnc_datatype()
        esize = datatype_size(dt)
        raw = self.converter.phys_to_raw(phys)
        backend.write(self.char.address + index * esize, encode_value(raw, dt, self.bo))

    def write_fnc_element(self, backend: MemoryBackend, ix: int, iy: int | None, phys) -> None:
        """Write one curve/map function value cell."""
        axes, fnc_addr, fnc_el, counts = self._walk_layout(backend)
        if fnc_addr is None or fnc_el is None:
            raise ValueError("Record layout has no FNC_VALUES")
        esize = datatype_size(fnc_el.datatype)
        if iy is None:
            offset = ix
        else:
            nx = axes["X"].count
            ny = axes["Y"].count
            offset = iy * nx + ix if fnc_el.index_mode != "COLUMN_DIR" else ix * ny + iy
        raw = self.converter.phys_to_raw(phys)
        backend.write(fnc_addr + offset * esize, encode_value(raw, fnc_el.datatype, self.bo))

    def write_axis_element(self, backend: MemoryBackend, dim: str, index: int, phys) -> None:
        """Write one axis point (STD_AXIS inline or COM_AXIS AXIS_PTS)."""
        axes, _fnc_addr, _fnc_el, _counts = self._walk_layout(backend)
        info = axes.get(dim)
        if info is None or not info.editable or info.address is None:
            raise ValueError(f"Axis {dim} of {self.char.name} is not writable")
        conv = info.converter or Converter(None)
        raw = conv.phys_to_raw(phys)
        esize = datatype_size(info.datatype)
        idx = index if info.index_order != "INDEX_DECR" else (info.count - 1 - index)
        backend.write(info.address + idx * esize, encode_value(raw, info.datatype, self.bo))
