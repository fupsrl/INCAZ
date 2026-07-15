"""Variable browser dock: measurements & characteristics of the active project.

Supports search, drag & drop into experiment windows (mime type
``application/x-incaz-variables``) and double-click / context-menu adding,
mirroring INCA's variable selection dialog.
"""

from __future__ import annotations

import json

from PySide6.QtCore import QMimeData, Qt, Signal
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import (
    QLineEdit,
    QMenu,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.a2l import A2LDatabase

MIME_VARIABLES = "application/x-incaz-variables"


def make_mime(kind: str, names: list[str]) -> QMimeData:
    mime = QMimeData()
    mime.setData(MIME_VARIABLES, json.dumps({"kind": kind, "names": names}).encode())
    mime.setText(", ".join(names))
    return mime


def parse_mime(mime: QMimeData):
    if not mime.hasFormat(MIME_VARIABLES):
        return None
    try:
        return json.loads(bytes(mime.data(MIME_VARIABLES)).decode())
    except Exception:
        return None


class _VarTree(QTreeWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderLabels(["Name", "Type", "Unit", "Description"])
        self.setColumnWidth(0, 220)
        self.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.setDragEnabled(True)
        self.setRootIsDecorated(True)
        self.setAlternatingRowColors(True)

    def startDrag(self, actions):
        items = [i for i in self.selectedItems() if i.parent() is not None]
        if not items:
            return
        kinds = {i.data(0, Qt.UserRole) for i in items}
        # a drag carries either measurements or characteristics, not both
        kind = "measurement" if "measurement" in kinds else "characteristic"
        names = [i.text(0) for i in items if i.data(0, Qt.UserRole) == kind]
        drag = QDrag(self)
        drag.setMimeData(make_mime(kind, names))
        drag.exec(Qt.CopyAction)


class VariableBrowser(QWidget):
    add_measurements = Signal(list)        # [names] -> to active/new measure window
    add_to_oscilloscope = Signal(list)
    open_characteristics = Signal(list)    # [names] -> calibration window

    def __init__(self, parent=None):
        super().__init__(parent)
        self.a2l: A2LDatabase | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter variables ...")
        self.search.textChanged.connect(self._apply_filter)
        layout.addWidget(self.search)
        self.tree = _VarTree()
        layout.addWidget(self.tree)
        self.tree.itemDoubleClicked.connect(self._on_double_click)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)

    # ---------------------------------------------------------------- content
    def set_database(self, a2l: A2LDatabase | None) -> None:
        self.a2l = a2l
        self.tree.clear()
        if a2l is None:
            return
        meas_root = QTreeWidgetItem(self.tree, [f"Measurements ({len(a2l.measurements)})"])
        cal_root = QTreeWidgetItem(self.tree, [f"Characteristics ({len(a2l.characteristics)})"])
        for m in sorted(a2l.measurements.values(), key=lambda x: x.name.lower()):
            conv = a2l.compu_method(m.conversion)
            unit = conv.unit if conv else ""
            item = QTreeWidgetItem(meas_root, [m.name, m.datatype, unit, m.long_identifier])
            item.setData(0, Qt.UserRole, "measurement")
            if m.ecu_address is None:
                item.setDisabled(True)
                item.setToolTip(0, "No ECU_ADDRESS - not measurable via XCP")
        for c in sorted(a2l.characteristics.values(), key=lambda x: x.name.lower()):
            conv = a2l.compu_method(c.conversion)
            unit = conv.unit if conv else ""
            item = QTreeWidgetItem(cal_root, [c.name, c.type, unit, c.long_identifier])
            item.setData(0, Qt.UserRole, "characteristic")
        meas_root.setExpanded(True)
        cal_root.setExpanded(True)

    def _apply_filter(self, text: str) -> None:
        text = text.lower()
        for t in range(self.tree.topLevelItemCount()):
            root = self.tree.topLevelItem(t)
            visible_children = 0
            for i in range(root.childCount()):
                child = root.child(i)
                match = (not text or text in child.text(0).lower()
                         or text in child.text(3).lower())
                child.setHidden(not match)
                visible_children += 0 if child.isHidden() else 1
            root.setHidden(visible_children == 0)

    # ---------------------------------------------------------------- actions
    def _selected(self, kind: str) -> list[str]:
        return [i.text(0) for i in self.tree.selectedItems()
                if i.parent() is not None and i.data(0, Qt.UserRole) == kind
                and not i.isDisabled()]

    def _on_double_click(self, item: QTreeWidgetItem, _col: int) -> None:
        kind = item.data(0, Qt.UserRole)
        if kind == "measurement" and not item.isDisabled():
            self.add_measurements.emit([item.text(0)])
        elif kind == "characteristic":
            self.open_characteristics.emit([item.text(0)])

    def _context_menu(self, pos) -> None:
        menu = QMenu(self)
        meas = self._selected("measurement")
        chars = self._selected("characteristic")
        if meas:
            menu.addAction(f"Add to Measure Window ({len(meas)})",
                           lambda: self.add_measurements.emit(meas))
            menu.addAction(f"Add to Oscilloscope ({len(meas)})",
                           lambda: self.add_to_oscilloscope.emit(meas))
        if chars:
            menu.addAction(f"Open in Calibration Editor ({len(chars)})",
                           lambda: self.open_characteristics.emit(chars))
        if menu.actions():
            menu.exec(self.tree.viewport().mapToGlobal(pos))
