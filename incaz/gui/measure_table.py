"""Measure table window - INCA's 'Measure Window' (numeric display).

Shows Name | Value | Unit | Min | Max for a set of measurement variables.
Values refresh from the acquisition latest-value store on a Qt timer.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHeaderView,
    QMenu,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .variable_browser import parse_mime


class MeasureTable(QWidget):
    variables_changed = Signal()

    COLUMNS = ["Variable", "Value", "Unit", "Raster", "Min", "Max"]
    COL_VALUE, COL_UNIT, COL_RASTER, COL_MIN, COL_MAX = 1, 2, 3, 4, 5

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        self.setWindowTitle("Measure Table")
        self.setAcceptDrops(True)
        self._rows: list[str] = []
        self._minmax: dict[str, list] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        layout.addWidget(self.table)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(100)

    # ------------------------------------------------------------------ config
    def variables(self) -> list[str]:
        return list(self._rows)

    def add_variables(self, names: list[str]) -> None:
        added = False
        for name in names:
            if name in self._rows:
                continue
            if self.session.a2l and name not in self.session.a2l.measurements:
                continue
            self._rows.append(name)
            self._minmax[name] = [None, None]
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(name))
            for col in range(1, len(self.COLUMNS)):
                item = QTableWidgetItem("-")
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(row, col, item)
            rv = self.session.acq.resolve(name)
            if rv is not None:
                self.table.item(row, self.COL_UNIT).setText(rv.unit)
            self.table.setCellWidget(row, self.COL_RASTER, self._make_raster_combo(name))
            added = True
        if added:
            self.variables_changed.emit()

    def _make_raster_combo(self, name: str) -> QComboBox:
        """DAQ raster selector - INCA's per-variable raster assignment."""
        combo = QComboBox()
        current = self.session.rasters.get(name)
        current_idx = 0
        for i, (label, channel) in enumerate(self.session.raster_choices()):
            combo.addItem(label, channel)
            if channel == current and channel is not None:
                current_idx = i
        combo.setCurrentIndex(current_idx)
        combo.currentIndexChanged.connect(
            lambda idx, c=combo, n=name: self.session.set_raster(n, c.itemData(idx)))
        return combo

    def remove_selected(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for row in rows:
            name = self.table.item(row, 0).text()
            self.table.removeRow(row)
            self._rows.remove(name)
            self._minmax.pop(name, None)
        if rows:
            self.variables_changed.emit()

    def _context_menu(self, pos) -> None:
        menu = QMenu(self)
        menu.addAction("Remove selected", self.remove_selected)
        menu.addAction("Reset min/max", self._reset_minmax)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _reset_minmax(self) -> None:
        for k in self._minmax:
            self._minmax[k] = [None, None]

    # ------------------------------------------------------------------ updates
    def refresh(self) -> None:
        if not self._rows:
            return
        latest = self.session.acq.latest_values()
        for row, name in enumerate(self._rows):
            entry = latest.get(name)
            if entry is None:
                continue
            _t, _raw, phys = entry
            rv = self.session.acq.resolve(name)
            text = rv.converter.format_value(phys) if rv else str(phys)
            self.table.item(row, self.COL_VALUE).setText(text)
            if isinstance(phys, (int, float)):
                mm = self._minmax.setdefault(name, [None, None])
                if mm[0] is None or phys < mm[0]:
                    mm[0] = phys
                if mm[1] is None or phys > mm[1]:
                    mm[1] = phys
                fmt = rv.converter.format_value if rv else str
                self.table.item(row, self.COL_MIN).setText(fmt(mm[0]))
                self.table.item(row, self.COL_MAX).setText(fmt(mm[1]))

    # ------------------------------------------------------------------ dnd
    def dragEnterEvent(self, event):
        payload = parse_mime(event.mimeData())
        if payload and payload.get("kind") == "measurement":
            event.acceptProposedAction()

    def dropEvent(self, event):
        payload = parse_mime(event.mimeData())
        if payload and payload.get("kind") == "measurement":
            self.add_variables(payload.get("names", []))
            event.acceptProposedAction()

    # ------------------------------------------------------------------ persistence
    def to_layout(self) -> dict:
        return {"type": "measure_table", "variables": self.variables()}

    def from_layout(self, layout: dict) -> None:
        self.add_variables(layout.get("variables", []))
