"""Self-contained ASAP2 (A2L) parser.

The parser is deliberately tolerant: it tokenizes the file, builds a generic
``/begin`` ... ``/end`` block tree and then extracts the objects INCAZ needs.
Unknown keywords and blocks are silently kept in the generic tree, so files
from any vendor toolchain should load without errors.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .model import (
    A2LDatabase,
    AxisDescr,
    AxisPts,
    Characteristic,
    CompuMethod,
    CompuTab,
    DaqEvent,
    Function,
    Group,
    Measurement,
    MemorySegment,
    RecordLayout,
    RecordLayoutElement,
    XcpTransportInfo,
)

log = logging.getLogger(__name__)

# strings first so comment markers inside strings are preserved
_COMMENT_OR_STRING = re.compile(
    r'("(?:\\.|[^"\\])*")|(/\*.*?\*/)|(//[^\n]*)', re.S
)
_TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|\S+')


def _strip_comments(text: str) -> str:
    return _COMMENT_OR_STRING.sub(lambda m: m.group(1) or " ", text)


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(_strip_comments(text))


@dataclass
class Block:
    """Generic A2L block: keyword, flat parameter tokens, child blocks."""

    kw: str
    params: list[str] = field(default_factory=list)
    children: list["Block"] = field(default_factory=list)

    def child(self, kw: str) -> "Block | None":
        for c in self.children:
            if c.kw == kw:
                return c
        return None

    def all(self, kw: str) -> list["Block"]:
        return [c for c in self.children if c.kw == kw]


def parse_block_tree(tokens: list[str]) -> Block:
    """Parse the token stream into a virtual ROOT block."""
    root = Block("ROOT")
    stack = [root]
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        low = tok.lower()
        if low == "/begin":
            if i + 1 >= n:
                break
            blk = Block(tokens[i + 1].upper())
            stack[-1].children.append(blk)
            stack.append(blk)
            i += 2
        elif low == "/end":
            kw = tokens[i + 1].upper() if i + 1 < n else ""
            # pop to the matching block (tolerate unbalanced files)
            for j in range(len(stack) - 1, 0, -1):
                if stack[j].kw == kw:
                    del stack[j:]
                    break
            else:
                if len(stack) > 1:
                    stack.pop()
            i += 2
        else:
            stack[-1].params.append(tok)
            i += 1
    return root


# ----------------------------------------------------------------- helpers

def _unquote(tok: str) -> str:
    if len(tok) >= 2 and tok[0] == '"' and tok[-1] == '"':
        body = tok[1:-1]
        return body.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
    return tok


def _num(tok: str) -> float:
    tok = tok.rstrip(",")
    try:
        if tok.lower().startswith("0x"):
            return int(tok, 16)
        return float(tok)
    except ValueError:
        return 0.0


def _int(tok: str) -> int:
    return int(_num(tok))


def _kwargs_scan(params: list[str]) -> dict[str, list[str]]:
    """Index optional keyword parameters: KEYWORD -> following tokens."""
    out: dict[str, list[str]] = {}
    for idx, tok in enumerate(params):
        if tok.isupper() and not tok.startswith('"'):
            out.setdefault(tok, []).extend(params[idx + 1: idx + 4])
    return out


def _opt(params: list[str], keyword: str, offset: int = 0) -> str | None:
    """Return the token *offset* positions after *keyword*, if present."""
    try:
        i = params.index(keyword)
        return params[i + 1 + offset]
    except (ValueError, IndexError):
        return None


# ----------------------------------------------------------------- mapping

def _map_measurement(b: Block) -> Measurement:
    p = b.params
    m = Measurement(
        name=p[0],
        long_identifier=_unquote(p[1]) if len(p) > 1 else "",
        datatype=p[2] if len(p) > 2 else "UBYTE",
        conversion=p[3] if len(p) > 3 else "NO_COMPU_METHOD",
        resolution=_int(p[4]) if len(p) > 4 else 0,
        accuracy=_num(p[5]) if len(p) > 5 else 0.0,
        lower_limit=_num(p[6]) if len(p) > 6 else 0.0,
        upper_limit=_num(p[7]) if len(p) > 7 else 0.0,
    )
    if (v := _opt(p, "ECU_ADDRESS")) is not None:
        m.ecu_address = _int(v)
    if (v := _opt(p, "ECU_ADDRESS_EXTENSION")) is not None:
        m.ecu_address_extension = _int(v)
    if (v := _opt(p, "BIT_MASK")) is not None:
        m.bit_mask = _int(v)
    if (v := _opt(p, "ARRAY_SIZE")) is not None:
        m.array_size = _int(v)
    if (v := _opt(p, "FORMAT")) is not None:
        m.format = _unquote(v)
    if (v := _opt(p, "DISPLAY_IDENTIFIER")) is not None:
        m.display_identifier = v
    if "MATRIX_DIM" in p:
        i = p.index("MATRIX_DIM")
        dims = []
        for t in p[i + 1: i + 4]:
            try:
                dims.append(int(_num(t)))
            except Exception:
                break
        m.matrix_dim = tuple(d for d in dims if d > 0)
    return m


def _map_axis_descr(b: Block) -> AxisDescr:
    p = b.params
    a = AxisDescr(
        attribute=p[0] if p else "STD_AXIS",
        input_quantity=p[1] if len(p) > 1 else "NO_INPUT_QUANTITY",
        conversion=p[2] if len(p) > 2 else "NO_COMPU_METHOD",
        max_axis_points=_int(p[3]) if len(p) > 3 else 0,
        lower_limit=_num(p[4]) if len(p) > 4 else 0.0,
        upper_limit=_num(p[5]) if len(p) > 5 else 0.0,
    )
    if (v := _opt(p, "AXIS_PTS_REF")) is not None:
        a.axis_pts_ref = v
    if (v := _opt(p, "FORMAT")) is not None:
        a.format = _unquote(v)
    if "FIX_AXIS_PAR" in p:
        i = p.index("FIX_AXIS_PAR")
        a.fix_axis_par = tuple(_num(t) for t in p[i + 1: i + 4])
    if "FIX_AXIS_PAR_DIST" in p:
        i = p.index("FIX_AXIS_PAR_DIST")
        a.fix_axis_par_dist = tuple(_num(t) for t in p[i + 1: i + 4])
    if (fx := b.child("FIX_AXIS_PAR_LIST")) is not None:
        a.fix_axis_par_list = [_num(t) for t in fx.params]
    return a


def _map_characteristic(b: Block) -> Characteristic:
    p = b.params
    c = Characteristic(
        name=p[0],
        long_identifier=_unquote(p[1]) if len(p) > 1 else "",
        type=p[2].upper() if len(p) > 2 else "VALUE",
        address=_int(p[3]) if len(p) > 3 else 0,
        deposit=p[4] if len(p) > 4 else "",
        max_diff=_num(p[5]) if len(p) > 5 else 0.0,
        conversion=p[6] if len(p) > 6 else "NO_COMPU_METHOD",
        lower_limit=_num(p[7]) if len(p) > 7 else 0.0,
        upper_limit=_num(p[8]) if len(p) > 8 else 0.0,
    )
    c.read_only = "READ_ONLY" in p
    if (v := _opt(p, "NUMBER")) is not None:
        c.number = _int(v)
    if (v := _opt(p, "BIT_MASK")) is not None:
        c.bit_mask = _int(v)
    if (v := _opt(p, "FORMAT")) is not None:
        c.format = _unquote(v)
    if (v := _opt(p, "DISPLAY_IDENTIFIER")) is not None:
        c.display_identifier = v
    if (v := _opt(p, "ECU_ADDRESS_EXTENSION")) is not None:
        c.ecu_address_extension = _int(v)
    if "MATRIX_DIM" in p:
        i = p.index("MATRIX_DIM")
        dims = []
        for t in p[i + 1: i + 4]:
            try:
                dims.append(int(_num(t)))
            except Exception:
                break
        c.matrix_dim = tuple(d for d in dims if d > 0)
    for ax in b.all("AXIS_DESCR"):
        c.axis_descrs.append(_map_axis_descr(ax))
    return c


def _map_axis_pts(b: Block) -> AxisPts:
    p = b.params
    a = AxisPts(
        name=p[0],
        long_identifier=_unquote(p[1]) if len(p) > 1 else "",
        address=_int(p[2]) if len(p) > 2 else 0,
        input_quantity=p[3] if len(p) > 3 else "NO_INPUT_QUANTITY",
        deposit=p[4] if len(p) > 4 else "",
        max_diff=_num(p[5]) if len(p) > 5 else 0.0,
        conversion=p[6] if len(p) > 6 else "NO_COMPU_METHOD",
        max_axis_points=_int(p[7]) if len(p) > 7 else 0,
        lower_limit=_num(p[8]) if len(p) > 8 else 0.0,
        upper_limit=_num(p[9]) if len(p) > 9 else 0.0,
    )
    a.read_only = "READ_ONLY" in p
    if (v := _opt(p, "FORMAT")) is not None:
        a.format = _unquote(v)
    return a


def _map_compu_method(b: Block) -> CompuMethod:
    p = b.params
    cm = CompuMethod(
        name=p[0],
        long_identifier=_unquote(p[1]) if len(p) > 1 else "",
        conversion_type=p[2].upper() if len(p) > 2 else "IDENTICAL",
        format=_unquote(p[3]) if len(p) > 3 else "%6.2f",
        unit=_unquote(p[4]) if len(p) > 4 else "",
    )
    if "COEFFS" in p:
        i = p.index("COEFFS")
        cm.coeffs = tuple(_num(t) for t in p[i + 1: i + 7])
    if "COEFFS_LINEAR" in p:
        i = p.index("COEFFS_LINEAR")
        cm.coeffs_linear = tuple(_num(t) for t in p[i + 1: i + 3])
    if (v := _opt(p, "COMPU_TAB_REF")) is not None:
        cm.compu_tab_ref = v
    if (f := b.child("FORMULA")) is not None:
        if f.params:
            cm.formula = _unquote(f.params[0])
        if (v := _opt(f.params, "FORMULA_INV")) is not None:
            cm.formula_inv = _unquote(v)
    return cm


def _map_compu_tab(b: Block) -> CompuTab:
    p = b.params
    tab = CompuTab(
        name=p[0],
        long_identifier=_unquote(p[1]) if len(p) > 1 else "",
        conversion_type=p[2].upper() if len(p) > 2 else "TAB_VERB",
    )
    rest = p[4:] if len(p) > 4 else []
    if (v := _opt(p, "DEFAULT_VALUE")) is not None:
        tab.default_value = _unquote(v)
        i = rest.index("DEFAULT_VALUE") if "DEFAULT_VALUE" in rest else len(rest)
        rest = rest[:i]
    if b.kw == "COMPU_VTAB_RANGE":
        tab.conversion_type = "TAB_VERB_RANGE"
        rest = p[3:]  # VTAB_RANGE has no conversion-type token
        if "DEFAULT_VALUE" in rest:
            rest = rest[: rest.index("DEFAULT_VALUE")]
        for i in range(0, len(rest) - 2, 3):
            tab.pairs.append((_num(rest[i]), _num(rest[i + 1]), _unquote(rest[i + 2])))
    elif tab.conversion_type == "TAB_VERB":
        for i in range(0, len(rest) - 1, 2):
            tab.pairs.append((_num(rest[i]), _unquote(rest[i + 1])))
    else:
        for i in range(0, len(rest) - 1, 2):
            tab.pairs.append((_num(rest[i]), _num(rest[i + 1])))
    return tab


_RL_VALUE_KINDS = {"FNC_VALUES"}
_RL_AXIS_KINDS = {"AXIS_PTS_X", "AXIS_PTS_Y", "AXIS_PTS_Z", "AXIS_PTS_4", "AXIS_PTS_5"}
_RL_NO_AXIS_KINDS = {"NO_AXIS_PTS_X", "NO_AXIS_PTS_Y", "NO_AXIS_PTS_Z", "NO_AXIS_PTS_4", "NO_AXIS_PTS_5"}
_RL_OTHER_KINDS = {
    "IDENTIFICATION", "AXIS_RESCALE_X", "NO_RESCALE_X", "SRC_ADDR_X", "SRC_ADDR_Y",
    "FIX_NO_AXIS_PTS_X", "FIX_NO_AXIS_PTS_Y", "SHIFT_OP_X", "OFFSET_X", "DIST_OP_X",
    "RESERVED",
}


def _map_record_layout(b: Block) -> RecordLayout:
    p = b.params
    rl = RecordLayout(name=p[0] if p else "?")
    rl.static_record_layout = "STATIC_RECORD_LAYOUT" in p
    i = 1
    while i < len(p):
        kw = p[i]
        if kw in _RL_VALUE_KINDS and i + 2 < len(p):
            rl.elements.append(RecordLayoutElement(
                kind=kw, position=_int(p[i + 1]), datatype=p[i + 2],
                index_mode=p[i + 3] if i + 3 < len(p) else "COLUMN_DIR",
                address_type=p[i + 4] if i + 4 < len(p) else "DIRECT",
            ))
            i += 5
        elif kw in _RL_AXIS_KINDS and i + 2 < len(p):
            rl.elements.append(RecordLayoutElement(
                kind=kw, position=_int(p[i + 1]), datatype=p[i + 2],
                index_order=p[i + 3] if i + 3 < len(p) else "INDEX_INCR",
                address_type=p[i + 4] if i + 4 < len(p) else "DIRECT",
            ))
            i += 5
        elif kw in _RL_NO_AXIS_KINDS and i + 2 < len(p):
            rl.elements.append(RecordLayoutElement(
                kind=kw, position=_int(p[i + 1]), datatype=p[i + 2],
            ))
            i += 3
        elif kw == "FIX_NO_AXIS_PTS_X" and i + 1 < len(p):
            rl.elements.append(RecordLayoutElement(kind=kw, position=0, datatype=p[i + 1]))
            i += 2
        else:
            i += 1
    return rl


def _map_memory_segment(b: Block) -> MemorySegment:
    p = b.params
    return MemorySegment(
        name=p[0] if p else "?",
        long_identifier=_unquote(p[1]) if len(p) > 1 else "",
        prg_type=p[2] if len(p) > 2 else "DATA",
        memory_type=p[3] if len(p) > 3 else "FLASH",
        attribute=p[4] if len(p) > 4 else "INTERN",
        address=_int(p[5]) if len(p) > 5 else 0,
        size=_int(p[6]) if len(p) > 6 else 0,
        offsets=tuple(_int(t) for t in p[7:12]) if len(p) > 7 else (),
    )


def _collect_ref_names(b: Block | None) -> list[str]:
    return list(b.params) if b is not None else []


def _map_group(b: Block) -> Group:
    p = b.params
    g = Group(
        name=p[0] if p else "?",
        long_identifier=_unquote(p[1]) if len(p) > 1 else "",
        is_root="ROOT" in p,
    )
    g.characteristics = _collect_ref_names(b.child("REF_CHARACTERISTIC"))
    g.measurements = _collect_ref_names(b.child("REF_MEASUREMENT"))
    g.sub_groups = _collect_ref_names(b.child("SUB_GROUP"))
    return g


def _map_function(b: Block) -> Function:
    p = b.params
    f = Function(
        name=p[0] if p else "?",
        long_identifier=_unquote(p[1]) if len(p) > 1 else "",
    )
    f.def_characteristics = _collect_ref_names(b.child("DEF_CHARACTERISTIC"))
    f.in_measurements = _collect_ref_names(b.child("IN_MEASUREMENT"))
    f.out_measurements = _collect_ref_names(b.child("OUT_MEASUREMENT"))
    f.loc_measurements = _collect_ref_names(b.child("LOC_MEASUREMENT"))
    return f


def _walk_blocks(b: Block):
    yield b
    for c in b.children:
        yield from _walk_blocks(c)


def _map_xcp_if_data(module: Block) -> XcpTransportInfo:
    """Best-effort scan of IF_DATA XCP for ethernet transport + DAQ events."""
    info = XcpTransportInfo()
    for ifd in module.all("IF_DATA"):
        if not ifd.params or ifd.params[0].upper() not in ("XCP", "XCPPLUS"):
            continue
        for blk in _walk_blocks(ifd):
            kw = blk.kw.upper()
            if kw in ("XCP_ON_UDP_IP", "XCP_ON_TCP_IP"):
                info.protocol = "UDP" if "UDP" in kw else "TCP"
                # params: version port [ADDRESS "ip" | HOST_NAME "name"]
                if len(blk.params) > 1:
                    try:
                        info.port = int(_num(blk.params[1]))
                    except Exception:
                        pass
                if (v := _opt(blk.params, "ADDRESS")) is not None:
                    info.address = _unquote(v)
                elif (v := _opt(blk.params, "HOST_NAME")) is not None:
                    info.address = _unquote(v)
            elif kw == "EVENT":
                p = blk.params
                # EVENT "name" "short" number props... time_cycle time_unit priority
                try:
                    ev = DaqEvent(
                        name=_unquote(p[0]) if p else "?",
                        short_name=_unquote(p[1]) if len(p) > 1 else "",
                        channel=_int(p[2]) if len(p) > 2 else 0,
                        time_cycle=_int(p[5]) if len(p) > 5 else 0,
                        time_unit=_int(p[6]) if len(p) > 6 else 0,
                        priority=_int(p[7]) if len(p) > 7 else 0,
                    )
                    info.events.append(ev)
                except Exception:  # tolerate exotic layouts
                    pass
    return info


def parse_a2l(path_or_text: str | Path) -> A2LDatabase:
    """Parse an A2L file (path or raw text) into an :class:`A2LDatabase`."""
    if isinstance(path_or_text, Path) or (
        len(str(path_or_text)) < 4096 and Path(str(path_or_text)).suffix.lower() in (".a2l", ".a2ml")
    ):
        text = Path(path_or_text).read_text(encoding="latin-1", errors="replace")
    else:
        text = str(path_or_text)

    root = parse_block_tree(tokenize(text))
    db = A2LDatabase()

    project = root.child("PROJECT")
    if project is None:
        raise ValueError("No /begin PROJECT found - not a valid A2L file")
    db.project_name = project.params[0] if project.params else ""
    if (hdr := project.child("HEADER")) is not None and hdr.params:
        db.header_comment = _unquote(hdr.params[0])

    module = project.child("MODULE")
    if module is None:
        raise ValueError("No /begin MODULE found in PROJECT")
    db.module_name = module.params[0] if module.params else ""

    if (mc := module.child("MOD_COMMON")) is not None:
        if (v := _opt(mc.params, "BYTE_ORDER")) is not None:
            db.byte_order = v
    if (mp := module.child("MOD_PAR")) is not None:
        for seg in mp.all("MEMORY_SEGMENT"):
            db.memory_segments.append(_map_memory_segment(seg))

    for b in module.all("MEASUREMENT"):
        try:
            m = _map_measurement(b)
            db.measurements[m.name] = m
        except Exception as exc:
            log.warning("Skipping MEASUREMENT %s: %s", b.params[:1], exc)
    for b in module.all("CHARACTERISTIC"):
        try:
            c = _map_characteristic(b)
            db.characteristics[c.name] = c
        except Exception as exc:
            log.warning("Skipping CHARACTERISTIC %s: %s", b.params[:1], exc)
    for b in module.all("AXIS_PTS"):
        try:
            a = _map_axis_pts(b)
            db.axis_pts[a.name] = a
        except Exception as exc:
            log.warning("Skipping AXIS_PTS %s: %s", b.params[:1], exc)
    for b in module.all("COMPU_METHOD"):
        cm = _map_compu_method(b)
        db.compu_methods[cm.name] = cm
    for kw in ("COMPU_TAB", "COMPU_VTAB", "COMPU_VTAB_RANGE"):
        for b in module.all(kw):
            tab = _map_compu_tab(b)
            db.compu_tabs[tab.name] = tab
    for b in module.all("RECORD_LAYOUT"):
        rl = _map_record_layout(b)
        db.record_layouts[rl.name] = rl
    for b in module.all("GROUP"):
        g = _map_group(b)
        db.groups[g.name] = g
    for b in module.all("FUNCTION"):
        f = _map_function(b)
        db.functions[f.name] = f

    db.xcp_info = _map_xcp_if_data(module)

    log.info(
        "A2L parsed: %d measurements, %d characteristics, %d axis_pts, %d compu methods",
        len(db.measurements), len(db.characteristics), len(db.axis_pts), len(db.compu_methods),
    )
    return db
