"""Calibration windows: scalar table and curve/map editor (INCA-style).

Edits are written through the active calibration backend: the ECU working
page when online, otherwise the loaded HEX dataset (offline calibration).
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .variable_browser import parse_mime

log = logging.getLogger(__name__)

_AXIS_BG = QColor(60, 60, 90)
_RO_FG = QColor(150, 150, 150)


def _fmt(accessor, value) -> str:
    if isinstance(value, str):
        return value
    return accessor.converter.format_value(value)


class CalibrationTable(QWidget):
    """Scalar calibration window (VALUE / ASCII / VAL_BLK as text)."""

    variables_changed = Signal()
    open_editor = Signal(str)     # curve/map name -> main window opens editor

    COLUMNS = ["Parameter", "Value", "Unit", "Min", "Max", "Type"]

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        self.setWindowTitle("Calibration Window")
        self.setAcceptDrops(True)
        self._rows: list[str] = []
        self._updating = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        bar = QHBoxLayout()
        self.info = QLabel("")
        bar.addWidget(self.info)
        bar.addStretch()
        read_btn = QPushButton("Read all")
        read_btn.clicked.connect(self.read_all)
        bar.addWidget(read_btn)
        layout.addLayout(bar)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.cellChanged.connect(self._on_cell_changed)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        layout.addWidget(self.table)

    # ------------------------------------------------------------------ config
    def variables(self) -> list[str]:
        return list(self._rows)

    def add_variables(self, names: list[str]) -> None:
        added = False
        for name in names:
            if name in self._rows or self.session.a2l is None:
                continue
            char = self.session.a2l.characteristics.get(name)
            if char is None:
                continue
            if char.type in ("CURVE", "MAP"):
                self.open_editor.emit(name)
                continue
            self._rows.append(name)
            self._insert_row(name, char)
            added = True
        if added:
            self.read_all()
            self.variables_changed.emit()

    def _insert_row(self, name: str, char) -> None:
        self._updating = True
        try:
            row = self.table.rowCount()
            self.table.insertRow(row)
            item = QTableWidgetItem(name)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            if char.read_only:
                item.setForeground(_RO_FG)
            self.table.setItem(row, 0, item)
            value_item = QTableWidgetItem("-")
            value_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            if char.read_only:
                value_item.setFlags(value_item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row, 1, value_item)
            acc = self.session.accessor(name)
            unit = acc.converter.unit if acc else ""
            for col, text in ((2, unit),
                              (3, str(char.lower_limit)),
                              (4, str(char.upper_limit)),
                              (5, char.type)):
                it = QTableWidgetItem(text)
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                self.table.setItem(row, col, it)
        finally:
            self._updating = False

    def remove_selected(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for row in rows:
            self._rows.remove(self.table.item(row, 0).text())
            self.table.removeRow(row)
        if rows:
            self.variables_changed.emit()

    def _context_menu(self, pos) -> None:
        menu = QMenu(self)
        menu.addAction("Read all", self.read_all)
        menu.addAction("Remove selected", self.remove_selected)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------------ IO
    def read_all(self) -> None:
        backend = self.session.cal_backend()
        self.info.setText(f"Source: {self.session.cal_backend_kind()}")
        if backend is None:
            return
        self._updating = True
        try:
            for row, name in enumerate(self._rows):
                acc = self.session.accessor(name)
                if acc is None:
                    continue
                val = acc.read(backend)
                item = self.table.item(row, 1)
                if val.error:
                    item.setText(f"<{val.error}>")
                elif val.kind == "array":
                    item.setText(", ".join(_fmt(acc, v) for v in val.phys))
                else:
                    item.setText(_fmt(acc, val.phys))
        finally:
            self._updating = False

    def _on_cell_changed(self, row: int, col: int) -> None:
        if self._updating or col != 1:
            return
        name = self.table.item(row, 0).text()
        text = self.table.item(row, 1).text().strip()
        backend = self.session.cal_backend()
        acc = self.session.accessor(name)
        if backend is None or acc is None:
            QMessageBox.warning(self, "Calibration",
                                "No calibration backend: connect to the ECU "
                                "or load a dataset (HEX).")
            self.read_all()
            return
        try:
            char = acc.char
            if char.type == "ASCII":
                acc.write_ascii(backend, text)
            elif char.type == "VAL_BLK":
                values = [v.strip() for v in text.split(",")]
                for i, v in enumerate(values):
                    acc.write_array_element(backend, i, float(v))
            else:
                try:
                    value = float(text)
                except ValueError:
                    value = text  # verbal conversion
                acc.write_scalar(backend, value)
            self.session.log(f"Calibration write: {name} = {text} "
                             f"[{self.session.cal_backend_kind()}]")
        except Exception as exc:
            log.exception("Write failed")
            QMessageBox.warning(self, "Calibration", f"Write of {name} failed:\n{exc}")
        self.read_all()

    # ------------------------------------------------------------------ dnd
    def dragEnterEvent(self, event):
        payload = parse_mime(event.mimeData())
        if payload and payload.get("kind") == "characteristic":
            event.acceptProposedAction()

    def dropEvent(self, event):
        payload = parse_mime(event.mimeData())
        if payload and payload.get("kind") == "characteristic":
            self.add_variables(payload.get("names", []))
            event.acceptProposedAction()

    # ------------------------------------------------------------------ persistence
    def to_layout(self) -> dict:
        return {"type": "calibration_table", "variables": self.variables()}

    def from_layout(self, layout: dict) -> None:
        self.add_variables(layout.get("variables", []))


class CurveMapEditor(QWidget):
    """Editor for one CURVE or MAP characteristic."""

    def __init__(self, session, name: str, parent=None):
        super().__init__(parent)
        self.session = session
        self.name = name
        self.setWindowTitle(f"{name}")
        self._updating = False
        self._value = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        bar = QHBoxLayout()
        self.info = QLabel(name)
        bar.addWidget(self.info)
        bar.addStretch()
        read_btn = QPushButton("Read")
        read_btn.clicked.connect(self.read)
        bar.addWidget(read_btn)
        layout.addLayout(bar)

        self.table = QTableWidget(0, 0)
        self.table.cellChanged.connect(self._on_cell_changed)
        layout.addWidget(self.table)
        self.read()

    # ------------------------------------------------------------------ IO
    def read(self) -> None:
        backend = self.session.cal_backend()
        acc = self.session.accessor(self.name)
        if backend is None or acc is None:
            self.info.setText(f"{self.name} - no backend (connect or load dataset)")
            return
        val = acc.read(backend)
        self._value = val
        if val.error:
            self.info.setText(f"{self.name} - error: {val.error}")
            return
        unit = f" [{val.unit}]" if val.unit else ""
        self.info.setText(f"{self.name} ({val.kind}){unit} - "
                          f"{self.session.cal_backend_kind()}")
        self._updating = True
        try:
            if val.kind == "curve":
                self._show_curve(acc, val)
            elif val.kind == "map":
                self._show_map(acc, val)
        finally:
            self._updating = False

    def _axis_item(self, axis, index: int) -> QTableWidgetItem:
        text = "-"
        if axis and index < len(axis.phys):
            conv = axis.converter
            v = axis.phys[index]
            text = conv.format_value(v) if conv else str(v)
        item = QTableWidgetItem(text)
        item.setBackground(_AXIS_BG)
        item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        if not (axis and axis.editable):
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        return item

    def _show_curve(self, acc, val) -> None:
        x = val.axes[0] if val.axes else None
        n = len(val.phys)
        self.table.clear()
        self.table.setRowCount(2)
        self.table.setColumnCount(n)
        self.table.setVerticalHeaderLabels(
            [f"X ({x.unit})" if x and x.unit else "X", self.name])
        self.table.setHorizontalHeaderLabels([str(i) for i in range(n)])
        for i in range(n):
            self.table.setItem(0, i, self._axis_item(x, i))
            item = QTableWidgetItem(_fmt(acc, val.phys[i]))
            item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(1, i, item)

    def _show_map(self, acc, val) -> None:
        x = val.axes[0] if len(val.axes) > 0 else None
        y = val.axes[1] if len(val.axes) > 1 else None
        ny = len(val.phys)
        nx = len(val.phys[0]) if ny else 0
        self.table.clear()
        self.table.setRowCount(ny + 1)
        self.table.setColumnCount(nx + 1)
        corner = QTableWidgetItem("Y \\ X")
        corner.setFlags(corner.flags() & ~Qt.ItemIsEditable)
        corner.setBackground(_AXIS_BG)
        self.table.setItem(0, 0, corner)
        for ix in range(nx):
            self.table.setItem(0, ix + 1, self._axis_item(x, ix))
        for iy in range(ny):
            self.table.setItem(iy + 1, 0, self._axis_item(y, iy))
            for ix in range(nx):
                item = QTableWidgetItem(_fmt(acc, val.phys[iy][ix]))
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(iy + 1, ix + 1, item)

    # ------------------------------------------------------------------ edits
    def _on_cell_changed(self, row: int, col: int) -> None:
        if self._updating or self._value is None:
            return
        backend = self.session.cal_backend()
        acc = self.session.accessor(self.name)
        if backend is None or acc is None:
            return
        text = self.table.item(row, col).text().strip()
        try:
            value = float(text)
        except ValueError:
            QMessageBox.warning(self, self.name, f"Not a number: {text}")
            self.read()
            return
        try:
            kind = self._value.kind
            if kind == "curve":
                if row == 0:
                    acc.write_axis_element(backend, "X", col, value)
                else:
                    acc.write_fnc_element(backend, col, None, value)
            elif kind == "map":
                if row == 0 and col >= 1:
                    acc.write_axis_element(backend, "X", col - 1, value)
                elif col == 0 and row >= 1:
                    acc.write_axis_element(backend, "Y", row - 1, value)
                elif row >= 1 and col >= 1:
                    acc.write_fnc_element(backend, col - 1, row - 1, value)
                else:
                    return
            self.session.log(f"Calibration write: {self.name}[{row},{col}] = {value} "
                             f"[{self.session.cal_backend_kind()}]")
        except Exception as exc:
            log.exception("Curve/map write failed")
            QMessageBox.warning(self, self.name, f"Write failed:\n{exc}")
        self.read()

    # ------------------------------------------------------------------ persistence
    def to_layout(self) -> dict:
        return {"type": "curve_map", "variables": [self.name]}
