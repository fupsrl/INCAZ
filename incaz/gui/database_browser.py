"""Database browser dock - INCA's database navigator.

Tree: Database -> Projects -> (A2L, Datasets, Experiments).
Context menus drive the INCA workflow: import A2L, add HEX dataset,
create/open experiments, flash a dataset.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QInputDialog,
    QMenu,
    QMessageBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)


class DatabaseBrowser(QWidget):
    import_a2l_requested = Signal()
    add_dataset_requested = Signal(int)          # project_id
    project_open_requested = Signal(int)         # project_id
    dataset_open_requested = Signal(int, int, str)  # project_id, dataset_id, name
    experiment_open_requested = Signal(int, str)    # project_id, name
    experiment_new_requested = Signal(int)          # project_id
    flash_requested = Signal(int, int)              # project_id, dataset_id

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Database"])
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.itemDoubleClicked.connect(self._double_click)
        layout.addWidget(self.tree)
        session.database_changed.connect(self.refresh)

    def refresh(self) -> None:
        self.tree.clear()
        db = self.session.db
        if db is None:
            root = QTreeWidgetItem(self.tree, ["<no database open>"])
            root.setDisabled(True)
            return
        root = QTreeWidgetItem(self.tree, [db.name])
        root.setData(0, Qt.UserRole, ("db", None))
        for proj in db.projects():
            p_item = QTreeWidgetItem(root, [proj.name])
            p_item.setData(0, Qt.UserRole, ("project", proj.id))
            a2l_item = QTreeWidgetItem(p_item, [f"A2L: {proj.a2l_file.name}"])
            a2l_item.setData(0, Qt.UserRole, ("a2l", proj.id))
            ds_root = QTreeWidgetItem(p_item, [f"Datasets ({len(proj.datasets)})"])
            ds_root.setData(0, Qt.UserRole, ("datasets", proj.id))
            for did, dname, _path in proj.datasets:
                d_item = QTreeWidgetItem(ds_root, [dname])
                d_item.setData(0, Qt.UserRole, ("dataset", (proj.id, did, dname)))
            ex_root = QTreeWidgetItem(p_item, [f"Experiments ({len(proj.experiments)})"])
            ex_root.setData(0, Qt.UserRole, ("experiments", proj.id))
            for _eid, ename in proj.experiments:
                e_item = QTreeWidgetItem(ex_root, [ename])
                e_item.setData(0, Qt.UserRole, ("experiment", (proj.id, ename)))
            p_item.setExpanded(True)
            ds_root.setExpanded(True)
            ex_root.setExpanded(True)
        root.setExpanded(True)

    # ------------------------------------------------------------------ events
    def _double_click(self, item: QTreeWidgetItem, _col: int) -> None:
        kind, data = item.data(0, Qt.UserRole) or (None, None)
        if kind == "project":
            self.project_open_requested.emit(data)
        elif kind == "dataset":
            pid, did, name = data
            self.dataset_open_requested.emit(pid, did, name)
        elif kind == "experiment":
            pid, name = data
            self.experiment_open_requested.emit(pid, name)

    def _context_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        menu = QMenu(self)
        if item is None or self.session.db is None:
            if self.session.db is not None:
                menu.addAction("Import A2L (new project) ...",
                               self.import_a2l_requested.emit)
        else:
            kind, data = item.data(0, Qt.UserRole) or (None, None)
            if kind == "db":
                menu.addAction("Import A2L (new project) ...",
                               self.import_a2l_requested.emit)
            elif kind == "project":
                menu.addAction("Open project", lambda: self.project_open_requested.emit(data))
                menu.addAction("Add dataset (HEX) ...",
                               lambda: self.add_dataset_requested.emit(data))
                menu.addAction("New experiment",
                               lambda: self.experiment_new_requested.emit(data))
                menu.addSeparator()
                menu.addAction("Delete project", lambda: self._delete_project(data))
            elif kind == "datasets":
                menu.addAction("Add dataset (HEX) ...",
                               lambda: self.add_dataset_requested.emit(data))
            elif kind == "dataset":
                pid, did, name = data
                menu.addAction("Load dataset (offline calibration)",
                               lambda: self.dataset_open_requested.emit(pid, did, name))
                menu.addAction("Flash to ECU (ProF) ...",
                               lambda: self.flash_requested.emit(pid, did))
                menu.addSeparator()
                menu.addAction("Delete dataset", lambda: self._delete_dataset(did))
            elif kind == "experiments":
                menu.addAction("New experiment",
                               lambda: self.experiment_new_requested.emit(data))
            elif kind == "experiment":
                pid, name = data
                menu.addAction("Open experiment",
                               lambda: self.experiment_open_requested.emit(pid, name))
                menu.addSeparator()
                menu.addAction("Delete experiment", lambda: self._delete_experiment(pid, name))
        if menu.actions():
            menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _delete_project(self, project_id: int) -> None:
        if QMessageBox.question(self, "Delete project",
                                "Delete this project including datasets "
                                "and experiments?") == QMessageBox.Yes:
            self.session.db.remove_project(project_id)
            self.session.database_changed.emit()

    def _delete_dataset(self, dataset_id: int) -> None:
        if QMessageBox.question(self, "Delete dataset",
                                "Delete this dataset?") == QMessageBox.Yes:
            self.session.db.remove_dataset(dataset_id)
            self.session.database_changed.emit()

    def _delete_experiment(self, project_id: int, name: str) -> None:
        if QMessageBox.question(self, "Delete experiment",
                                f"Delete experiment '{name}'?") == QMessageBox.Yes:
            self.session.db.remove_experiment(project_id, name)
            self.session.database_changed.emit()
