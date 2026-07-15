"""Multi-layer experiment area - INCA's experiment 'layers'.

The experiment is a tab widget (tabs at the bottom, like INCA): each tab is
a *layer* holding its own MDI surface with instruments (measure tables,
oscilloscopes, calibration windows). Users add, rename, reorder and delete
layers; measurement always covers the variables of *all* layers.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QInputDialog,
    QMdiArea,
    QMenu,
    QMessageBox,
    QTabWidget,
    QToolButton,
)


def _make_mdi() -> QMdiArea:
    mdi = QMdiArea()
    mdi.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    mdi.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    return mdi


class ExperimentArea(QTabWidget):
    """Tabbed experiment layers; every tab is a QMdiArea."""

    layers_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTabPosition(QTabWidget.South)
        self.setMovable(True)
        self.setTabsClosable(True)
        self.setDocumentMode(True)
        self.tabCloseRequested.connect(self._on_close_requested)
        self.tabBarDoubleClicked.connect(self.rename_layer)

        add_btn = QToolButton(self)
        add_btn.setText("+")
        add_btn.setToolTip("Add layer")
        add_btn.setAutoRaise(True)
        add_btn.clicked.connect(lambda: self.add_layer())
        self.setCornerWidget(add_btn, Qt.TopRightCorner)

        self.tabBar().setContextMenuPolicy(Qt.CustomContextMenu)
        self.tabBar().customContextMenuRequested.connect(self._tab_context_menu)

        self.add_layer("Layer 1")

    # ---------------------------------------------------------------- layers
    def add_layer(self, name: str | None = None) -> QMdiArea:
        if not name:
            existing = {self.tabText(i) for i in range(self.count())}
            n = self.count() + 1
            while f"Layer {n}" in existing:
                n += 1
            name = f"Layer {n}"
        mdi = _make_mdi()
        self.addTab(mdi, name)
        self.setCurrentWidget(mdi)
        self.layers_changed.emit()
        return mdi

    def current_mdi(self) -> QMdiArea:
        if self.count() == 0:
            return self.add_layer("Layer 1")
        return self.currentWidget()

    def layers(self) -> list[tuple[str, QMdiArea]]:
        return [(self.tabText(i), self.widget(i)) for i in range(self.count())]

    def all_mdis(self) -> list[QMdiArea]:
        return [self.widget(i) for i in range(self.count())]

    def rename_layer(self, index: int | None = None) -> None:
        if index is None or index < 0:
            index = self.currentIndex()
        if index < 0:
            return
        name, ok = QInputDialog.getText(self, "Rename layer", "Layer name:",
                                        text=self.tabText(index))
        if ok and name.strip():
            self.setTabText(index, name.strip())
            self.layers_changed.emit()

    def remove_layer(self, index: int | None = None) -> None:
        if index is None or index < 0:
            index = self.currentIndex()
        if index < 0:
            return
        self._on_close_requested(index)

    def clear_all(self, first_layer_name: str = "Layer 1") -> QMdiArea:
        """Remove every layer and start over with one empty layer."""
        while self.count():
            mdi: QMdiArea = self.widget(0)
            mdi.closeAllSubWindows()
            self.removeTab(0)
            mdi.deleteLater()
        return self.add_layer(first_layer_name)

    # ---------------------------------------------------------------- internals
    def _on_close_requested(self, index: int) -> None:
        mdi: QMdiArea = self.widget(index)
        if mdi is None:
            return
        if mdi.subWindowList():
            answer = QMessageBox.question(
                self, "Delete layer",
                f"Layer '{self.tabText(index)}' contains "
                f"{len(mdi.subWindowList())} window(s).\nDelete it anyway?")
            if answer != QMessageBox.Yes:
                return
        mdi.closeAllSubWindows()
        self.removeTab(index)
        mdi.deleteLater()
        if self.count() == 0:
            self.add_layer("Layer 1")
        self.layers_changed.emit()

    def _tab_context_menu(self, pos) -> None:
        index = self.tabBar().tabAt(pos)
        menu = QMenu(self)
        menu.addAction("Add layer", lambda: self.add_layer())
        if index >= 0:
            menu.addAction("Rename layer ...", lambda: self.rename_layer(index))
            menu.addSeparator()
            menu.addAction("Delete layer", lambda: self._on_close_requested(index))
        menu.exec(self.tabBar().mapToGlobal(pos))
