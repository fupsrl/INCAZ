"""COMPU_METHOD evaluation: raw (ECU internal) <-> physical values.

Supports IDENTICAL, LINEAR, RAT_FUNC, TAB_INTP/TAB_NOINTP, TAB_VERB(_RANGE)
and best-effort FORM (restricted eval).
"""

from __future__ import annotations

import logging
import math
from typing import Optional, Union

from .a2l.model import A2LDatabase, CompuMethod, CompuTab

log = logging.getLogger(__name__)

_FORMULA_NS = {name: getattr(math, name) for name in
               ("sin", "cos", "tan", "asin", "acos", "atan", "sqrt", "exp", "log", "log10", "pow", "fabs", "floor", "ceil")}
_FORMULA_NS["abs"] = abs


class Converter:
    """Bidirectional raw <-> physical converter for one COMPU_METHOD."""

    def __init__(self, method: Optional[CompuMethod], tab: Optional[CompuTab] = None):
        self.method = method
        self.tab = tab
        self.unit = method.unit if method else ""
        self.format = method.format if method else "%6.2f"

    # -------------------------------------------------------------- raw -> phys
    def raw_to_phys(self, raw: float) -> Union[float, str]:
        m = self.method
        if m is None:
            return raw
        ct = m.conversion_type
        try:
            if ct == "IDENTICAL":
                return raw
            if ct == "LINEAR" and m.coeffs_linear:
                a, b = m.coeffs_linear
                return a * raw + b
            if ct == "RAT_FUNC" and m.coeffs:
                return self._rat_raw_to_phys(raw)
            if ct in ("TAB_INTP", "TAB_NOINTP") and self.tab:
                return self._tab_lookup(raw, interpolate=(ct == "TAB_INTP"))
            if ct in ("TAB_VERB", "TAB_VERB_RANGE") and self.tab:
                return self._verb_lookup(raw)
            if ct == "FORM" and m.formula:
                expr = m.formula.replace("X1", "X").replace("x1", "X")
                return eval(expr, {"__builtins__": {}}, {**_FORMULA_NS, "X": raw})  # noqa: S307
        except Exception as exc:
            log.debug("Conversion %s failed for %r: %s", m.name, raw, exc)
        return raw

    # -------------------------------------------------------------- phys -> raw
    def phys_to_raw(self, phys: Union[float, str]) -> float:
        m = self.method
        if m is None:
            return float(phys)
        ct = m.conversion_type
        try:
            if ct == "IDENTICAL":
                return float(phys)
            if ct == "LINEAR" and m.coeffs_linear:
                a, b = m.coeffs_linear
                if a == 0:
                    raise ZeroDivisionError("LINEAR coefficient a == 0")
                return (float(phys) - b) / a
            if ct == "RAT_FUNC" and m.coeffs:
                return self._rat_phys_to_raw(float(phys))
            if ct in ("TAB_INTP", "TAB_NOINTP") and self.tab:
                return self._tab_lookup_inv(float(phys), interpolate=(ct == "TAB_INTP"))
            if ct in ("TAB_VERB", "TAB_VERB_RANGE") and self.tab:
                return self._verb_lookup_inv(str(phys))
            if ct == "FORM" and m.formula_inv:
                expr = m.formula_inv.replace("X1", "X").replace("x1", "X")
                return float(eval(expr, {"__builtins__": {}}, {**_FORMULA_NS, "X": float(phys)}))  # noqa: S307
        except Exception as exc:
            log.debug("Inverse conversion %s failed for %r: %s", m.name, phys, exc)
        return float(phys)

    # -------------------------------------------------------------- RAT_FUNC
    # ASAP2: raw = (a*phys^2 + b*phys + c) / (d*phys^2 + e*phys + f)
    def _rat_raw_to_phys(self, raw: float) -> float:
        a, b, c, d, e, f = self.method.coeffs
        # common affine case
        if a == 0 and d == 0 and e == 0:
            if b == 0:
                return raw
            return (f * raw - c) / b
        # general: solve (a - raw*d)*p^2 + (b - raw*e)*p + (c - raw*f) = 0
        A = a - raw * d
        B = b - raw * e
        C = c - raw * f
        if abs(A) < 1e-12:
            return -C / B if B else raw
        disc = B * B - 4 * A * C
        if disc < 0:
            return raw
        r1 = (-B + math.sqrt(disc)) / (2 * A)
        r2 = (-B - math.sqrt(disc)) / (2 * A)
        # heuristics: pick the root inside the (unknown) plausible range: prefer r1
        return r1 if abs(r1) <= abs(r2) else r2

    def _rat_phys_to_raw(self, phys: float) -> float:
        a, b, c, d, e, f = self.method.coeffs
        den = d * phys * phys + e * phys + f
        if den == 0:
            raise ZeroDivisionError("RAT_FUNC denominator is zero")
        return (a * phys * phys + b * phys + c) / den

    # -------------------------------------------------------------- tables
    def _tab_lookup(self, raw: float, interpolate: bool) -> float:
        pairs = sorted(self.tab.pairs, key=lambda p: p[0])
        if not pairs:
            return raw
        if raw <= pairs[0][0]:
            return pairs[0][1]
        if raw >= pairs[-1][0]:
            return pairs[-1][1]
        for (x0, y0), (x1, y1) in zip(pairs, pairs[1:]):
            if x0 <= raw <= x1:
                if not interpolate or x1 == x0:
                    return y0
                t = (raw - x0) / (x1 - x0)
                return y0 + t * (y1 - y0)
        return raw

    def _tab_lookup_inv(self, phys: float, interpolate: bool) -> float:
        pairs = sorted(self.tab.pairs, key=lambda p: p[1])
        if not pairs:
            return phys
        if phys <= pairs[0][1]:
            return pairs[0][0]
        if phys >= pairs[-1][1]:
            return pairs[-1][0]
        for (x0, y0), (x1, y1) in zip(pairs, pairs[1:]):
            if y0 <= phys <= y1:
                if not interpolate or y1 == y0:
                    return x0
                t = (phys - y0) / (y1 - y0)
                return x0 + t * (x1 - x0)
        return phys

    def _verb_lookup(self, raw: float) -> str:
        if self.tab.conversion_type == "TAB_VERB_RANGE":
            for lo, hi, text in self.tab.pairs:
                if lo <= raw <= hi:
                    return text
        else:
            for val, text in self.tab.pairs:
                if val == raw:
                    return text
        return self.tab.default_value if self.tab.default_value is not None else str(raw)

    def _verb_lookup_inv(self, text: str) -> float:
        if self.tab.conversion_type == "TAB_VERB_RANGE":
            for lo, _hi, t in self.tab.pairs:
                if t == text:
                    return lo
        else:
            for val, t in self.tab.pairs:
                if t == text:
                    return val
        # maybe the user typed a number
        return float(text)

    # -------------------------------------------------------------- misc
    @property
    def is_verbal(self) -> bool:
        return bool(self.method and self.method.conversion_type in ("TAB_VERB", "TAB_VERB_RANGE"))

    def verbal_choices(self) -> list[str]:
        if not (self.is_verbal and self.tab):
            return []
        if self.tab.conversion_type == "TAB_VERB_RANGE":
            return [p[2] for p in self.tab.pairs]
        return [p[1] for p in self.tab.pairs]

    def format_value(self, phys) -> str:
        if isinstance(phys, str):
            return phys
        fmt = self.format or "%6.2f"
        try:
            return (fmt % phys).strip()
        except (TypeError, ValueError):
            return str(phys)


def make_converter(db: A2LDatabase, conversion_name: str) -> Converter:
    method = db.compu_method(conversion_name)
    tab = None
    if method is not None and method.compu_tab_ref:
        tab = db.compu_tabs.get(method.compu_tab_ref)
    return Converter(method, tab)
