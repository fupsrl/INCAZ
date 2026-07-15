"""INCAZ main window: menus, docks, MDI experiment area, status bar."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QObject, Signal
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (
    QDockWidget,
    QFileDialog,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
)

from .. import __version__
from ..core.hexfile import MemoryImage
from ..core.recorder import default_recording_name
from .calibration_editor import CalibrationTable, CurveMapEditor
from .database_browser import DatabaseBrowser
from .dialogs import HardwareConfigDialog
from .experiment_area import ExperimentArea
from .flash_dialog import FlashDialog
from .measure_table import MeasureTable
from .oscilloscope import Oscilloscope
from .session import Session
from .variable_browser import VariableBrowser


class _LogBridge(QObject, logging.Handler):
    """Routes python logging records into the GUI console (thread-safe)."""

    record = Signal(str)

    def __init__(self):
        QObject.__init__(self)
        logging.Handler.__init__(self, level=logging.INFO)
        self.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s",
                                            datefmt="%H:%M:%S"))

    def emit(self, rec: logging.LogRecord) -> None:  # logging.Handler API
        try:
            self.record.emit(self.format(rec))
        except Exception:
            pass


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.session = Session()
        self.setWindowTitle(
            f"INCAZ {__version__} - INtegrated Calibration & Acquisition, Zero-cost")
        self.resize(1400, 900)

        self.experiment_area = ExperimentArea()
        self.setCentralWidget(self.experiment_area)

        # ---------------------------------------------------------- docks
        self.db_browser = DatabaseBrowser(self.session)
        dock = QDockWidget("Database", self)
        dock.setWidget(self.db_browser)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)

        self.var_browser = VariableBrowser()
        dock = QDockWidget("Variables", self)
        dock.setWidget(self.var_browser)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        dock = QDockWidget("Log", self)
        dock.setWidget(self.log_view)
        self.addDockWidget(Qt.BottomDockWidgetArea, dock)

        self._log_bridge = _LogBridge()
        self._log_bridge.record.connect(self.log_view.appendPlainText)
        logging.getLogger().addHandler(self._log_bridge)
        self.session.message.connect(
            lambda msg: None)  # session logs also go through logging

        # ---------------------------------------------------------- status bar
        self.status_conn = QLabel("Offline")
        self.status_meas = QLabel("")
        self.status_rec = QLabel("")
        self.statusBar().addPermanentWidget(self.status_rec)
        self.statusBar().addPermanentWidget(self.status_meas)
        self.statusBar().addPermanentWidget(self.status_conn)

        from PySide6.QtCore import QTimer
        self._meas_timer = QTimer(self)
        self._meas_timer.timeout.connect(self._update_meas_status)
        self._meas_last_count = 0

        self._build_menus()
        self._connect_signals()
        self.db_browser.refresh()
        self._update_actions()

    # ================================================================= menus
    def _build_menus(self) -> None:
        bar = self.menuBar()

        m_db = bar.addMenu("&Database")
        m_db.addAction("&New Database ...", self.new_database)
        m_db.addAction("&Open Database ...", self.open_database)
        m_db.addSeparator()
        m_db.addAction("E&xit", self.close)

        m_proj = bar.addMenu("&Project")
        self.act_import_a2l = m_proj.addAction("Import &A2L (new project) ...",
                                               self.import_a2l)
        self.act_add_hex = m_proj.addAction("Add &Dataset (HEX) ...", self.add_dataset)

        m_exp = bar.addMenu("&Experiment")
        self.act_new_mt = m_exp.addAction("New &Measure Table", lambda: self.add_measure_table([]))
        self.act_new_osc = m_exp.addAction("New &Oscilloscope", lambda: self.add_oscilloscope([]))
        self.act_new_cal = m_exp.addAction("New &Calibration Window",
                                           lambda: self.add_calibration_table([]))
        m_exp.addSeparator()
        self.act_new_layer = m_exp.addAction("New &Layer",
                                             lambda: self.experiment_area.add_layer())
        self.act_new_layer.setShortcut("Ctrl+T")
        self.act_rename_layer = m_exp.addAction("&Rename Layer ...",
                                                lambda: self.experiment_area.rename_layer())
        self.act_del_layer = m_exp.addAction("&Delete Layer",
                                             lambda: self.experiment_area.remove_layer())
        m_exp.addSeparator()
        self.act_save_exp = m_exp.addAction("&Save Experiment", self.save_experiment)
        self.act_save_exp.setShortcut("Ctrl+S")

        m_hw = bar.addMenu("&Hardware")
        self.act_hw_cfg = m_hw.addAction("&Configure ...", self.configure_hardware)
        m_hw.addSeparator()
        self.act_connect = m_hw.addAction("Co&nnect", self.connect_hw)
        self.act_connect.setShortcut("F2")
        self.act_disconnect = m_hw.addAction("&Disconnect", self.disconnect_hw)

        m_meas = bar.addMenu("&Measurement")
        self.act_meas_start = m_meas.addAction("&Start Measurement", self.start_measurement)
        self.act_meas_start.setShortcut("F11")
        self.act_meas_stop = m_meas.addAction("S&top Measurement", self.stop_measurement)
        self.act_meas_stop.setShortcut("F9")
        m_meas.addSeparator()
        self.act_rec_start = m_meas.addAction("Start &Recording (MF4)", self.start_recording)
        self.act_rec_start.setShortcut("F12")
        self.act_rec_stop = m_meas.addAction("Stop R&ecording", self.stop_recording)

        m_cal = bar.addMenu("&Calibration")
        self.act_page_wp = m_cal.addAction("Switch to &Working Page (RAM)",
                                           lambda: self.switch_page(0))
        self.act_page_rp = m_cal.addAction("Switch to &Reference Page",
                                           lambda: self.switch_page(1))
        self.act_page_copy = m_cal.addAction("Copy Reference -> Working Page",
                                             self.copy_ref_to_working)
        m_cal.addSeparator()
        self.act_save_ds = m_cal.addAction("&Save Dataset (HEX)", self.save_dataset)

        m_flash = bar.addMenu("&Flash")
        self.act_flash = m_flash.addAction("Flash ECU (&ProF) ...", self.flash_current)

        m_help = bar.addMenu("&Help")
        m_help.addAction("&About INCAZ", self.about)

    def _connect_signals(self) -> None:
        s = self.session
        s.database_changed.connect(self._update_actions)
        s.project_loaded.connect(self._on_project_loaded)
        s.dataset_loaded.connect(lambda _n: self._update_actions())
        s.connection_changed.connect(self._on_connection_changed)
        s.measurement_changed.connect(self._on_measurement_changed)
        s.recording_changed.connect(self._on_recording_changed)

        b = self.db_browser
        b.import_a2l_requested.connect(self.import_a2l)
        b.add_dataset_requested.connect(self.add_dataset_for)
        b.project_open_requested.connect(self.load_project)
        b.dataset_open_requested.connect(self.load_dataset)
        b.experiment_open_requested.connect(self.open_experiment)
        b.experiment_new_requested.connect(self.new_experiment)
        b.flash_requested.connect(self.flash_dataset)

        v = self.var_browser
        v.add_measurements.connect(self.add_to_measure_table)
        v.add_to_oscilloscope.connect(self.add_to_oscilloscope)
        v.open_characteristics.connect(self.add_calibration_table)

    # ================================================================= state
    def _update_actions(self) -> None:
        s = self.session
        has_db = s.db is not None
        has_proj = s.a2l is not None
        connected = s.client.connected
        measuring = s.acq.measuring
        self.act_import_a2l.setEnabled(has_db)
        self.act_add_hex.setEnabled(has_db and s.project is not None)
        for act in (self.act_new_mt, self.act_new_osc, self.act_new_cal,
                    self.act_new_layer, self.act_rename_layer, self.act_del_layer,
                    self.act_save_exp, self.act_hw_cfg):
            act.setEnabled(has_proj)
        self.act_connect.setEnabled(has_proj and not connected)
        self.act_disconnect.setEnabled(connected)
        self.act_meas_start.setEnabled(connected and not measuring or
                                       (has_proj and not measuring))
        self.act_meas_stop.setEnabled(measuring)
        self.act_rec_start.setEnabled(measuring and not s.recorder.recording)
        self.act_rec_stop.setEnabled(s.recorder.recording)
        for act in (self.act_page_wp, self.act_page_rp, self.act_page_copy):
            act.setEnabled(connected)
        self.act_save_ds.setEnabled(s.image is not None and s.image.dirty)
        self.act_flash.setEnabled(has_proj)

    def _on_project_loaded(self, name: str) -> None:
        self.var_browser.set_database(self.session.a2l)
        self.experiment_area.clear_all()
        self.setWindowTitle(f"INCAZ {__version__} - {name}")
        self._update_actions()

    def _on_connection_changed(self, connected: bool, info: str) -> None:
        self.status_conn.setText(f"Online: {info}" if connected else "Offline")
        self.status_conn.setStyleSheet(
            "color: #4c4;" if connected else "color: #c44;")
        self._update_actions()

    def _on_measurement_changed(self, active: bool) -> None:
        if active:
            self._meas_last_count = 0
            self._meas_timer.start(1000)
            self.status_meas.setText(f"MEASURING {self.session.hw_config.daq_mode}")
        else:
            self._meas_timer.stop()
            self.status_meas.setText("")
        self.status_meas.setStyleSheet("color: #4c4; font-weight: bold;" if active else "")
        self._update_actions()

    def _update_meas_status(self) -> None:
        """Live sample counter - makes 'no data' vs 'GUI stuck' obvious."""
        acq = self.session.acq
        rate = acq.sample_count - self._meas_last_count
        self._meas_last_count = acq.sample_count
        text = (f"MEASURING {self.session.hw_config.daq_mode} - "
                f"{acq.sample_count} samples ({rate}/s)")
        if acq.dispatch_errors:
            text += f"  [{acq.dispatch_errors} errors!]"
        if rate == 0 and acq.measuring:
            self.status_meas.setStyleSheet("color: #e90; font-weight: bold;")
        else:
            self.status_meas.setStyleSheet("color: #4c4; font-weight: bold;")
        self.status_meas.setText(text)

    def _on_recording_changed(self, active: bool, path: str) -> None:
        self.status_rec.setText(f"REC {Path(path).name}" if active else "")
        self.status_rec.setStyleSheet("color: #e33; font-weight: bold;" if active else "")
        self._update_actions()

    # ================================================================= database
    def new_database(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "New INCAZ database", "",
                                              "INCAZ database (*.incazdb)")
        if path:
            self.session.open_database(path, create=True)

    def open_database(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Open INCAZ database (folder)")
        if path:
            try:
                self.session.open_database(path)
            except Exception as exc:
                QMessageBox.warning(self, "Database", str(exc))

    def import_a2l(self) -> None:
        if self.session.db is None:
            QMessageBox.information(self, "INCAZ", "Create or open a database first "
                                                   "(Database menu).")
            return
        path, _ = QFileDialog.getOpenFileName(self, "Import A2L", "",
                                              "ASAP2 (*.a2l);;All files (*.*)")
        if not path:
            return
        name, ok = QInputDialog.getText(self, "Project name", "Name for the project:",
                                        text=Path(path).stem)
        if not ok or not name.strip():
            return
        try:
            pid = self.session.import_a2l(name.strip(), path)
            self.load_project(pid)
        except Exception as exc:
            QMessageBox.warning(self, "Import A2L", str(exc))

    def add_dataset(self) -> None:
        if self.session.project is None:
            QMessageBox.information(self, "INCAZ", "Load a project first.")
            return
        self.add_dataset_for(self.session.project.id)

    def add_dataset_for(self, project_id: int) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Add dataset (HEX)", "",
                                              "Intel HEX (*.hex *.ihx *.ihex);;All files (*.*)")
        if not path:
            return
        name, ok = QInputDialog.getText(self, "Dataset name", "Name for the dataset:",
                                        text=Path(path).stem)
        if not ok or not name.strip():
            return
        try:
            self.session.add_dataset(project_id, name.strip(), path)
        except Exception as exc:
            QMessageBox.warning(self, "Add dataset", str(exc))

    def load_project(self, project_id: int) -> None:
        try:
            self.session.load_project(project_id)
        except Exception as exc:
            QMessageBox.warning(self, "Open project", str(exc))

    def load_dataset(self, project_id: int, dataset_id: int, name: str) -> None:
        if self.session.project is None or self.session.project.id != project_id:
            self.load_project(project_id)
        try:
            self.session.load_dataset(dataset_id, name)
        except Exception as exc:
            QMessageBox.warning(self, "Load dataset", str(exc))

    # ================================================================= experiment
    @property
    def mdi(self):
        """The MDI surface of the *active* experiment layer."""
        return self.experiment_area.current_mdi()

    def _all_widgets(self) -> list:
        """Instrument widgets across every layer."""
        return [sw.widget()
                for mdi in self.experiment_area.all_mdis()
                for sw in mdi.subWindowList()]

    def _layer_widgets(self, mdi=None) -> list:
        mdi = mdi or self.experiment_area.current_mdi()
        return [sw.widget() for sw in mdi.subWindowList()]

    def add_measure_table(self, names: list[str], mdi=None) -> MeasureTable:
        mdi = mdi or self.experiment_area.current_mdi()
        w = MeasureTable(self.session)
        mdi.addSubWindow(w)
        w.parent().resize(480, 320)
        w.show()
        if names:
            w.add_variables(names)
        return w

    def add_oscilloscope(self, names: list[str], mdi=None) -> Oscilloscope:
        mdi = mdi or self.experiment_area.current_mdi()
        w = Oscilloscope(self.session)
        mdi.addSubWindow(w)
        w.parent().resize(720, 420)
        w.show()
        if names:
            w.add_variables(names)
        return w

    def add_calibration_table(self, names: list[str], mdi=None) -> CalibrationTable:
        mdi = mdi or self.experiment_area.current_mdi()
        # reuse an existing calibration window of this layer if present
        for w in self._layer_widgets(mdi):
            if isinstance(w, CalibrationTable):
                if names:
                    w.add_variables(names)
                return w
        w = CalibrationTable(self.session)
        w.open_editor.connect(self.open_curve_map)
        mdi.addSubWindow(w)
        w.parent().resize(560, 320)
        w.show()
        if names:
            w.add_variables(names)
        return w

    def add_to_measure_table(self, names: list[str]) -> None:
        mdi = self.experiment_area.current_mdi()
        active = mdi.activeSubWindow()
        if active and isinstance(active.widget(), MeasureTable):
            active.widget().add_variables(names)
            return
        for w in self._layer_widgets(mdi):
            if isinstance(w, MeasureTable):
                w.add_variables(names)
                return
        self.add_measure_table(names, mdi)

    def add_to_oscilloscope(self, names: list[str]) -> None:
        mdi = self.experiment_area.current_mdi()
        active = mdi.activeSubWindow()
        if active and isinstance(active.widget(), Oscilloscope):
            active.widget().add_variables(names)
            return
        for w in self._layer_widgets(mdi):
            if isinstance(w, Oscilloscope):
                w.add_variables(names)
                return
        self.add_oscilloscope(names, mdi)

    def open_curve_map(self, name, mdi=None) -> None:
        mdi = mdi or self.experiment_area.current_mdi()
        names = name if isinstance(name, list) else [name]
        for n in names:
            w = CurveMapEditor(self.session, n)
            mdi.addSubWindow(w)
            w.parent().resize(640, 300)
            w.show()

    def new_experiment(self, project_id: int) -> None:
        if self.session.project is None or self.session.project.id != project_id:
            self.load_project(project_id)
        name, ok = QInputDialog.getText(self, "New experiment", "Experiment name:")
        if not ok or not name.strip():
            return
        self.session.experiment_name = name.strip()
        self.experiment_area.clear_all()
        self.session.db.save_experiment(project_id, self.session.experiment_name,
                                        {"layers": [{"name": "Layer 1", "windows": []}]})
        self.session.database_changed.emit()

    def _restore_window(self, win: dict, mdi) -> None:
        wtype = win.get("type")
        if wtype == "measure_table":
            w = self.add_measure_table(win.get("variables", []), mdi)
        elif wtype == "oscilloscope":
            w = self.add_oscilloscope([], mdi)
            w.from_layout(win)
        elif wtype == "calibration_table":
            w = self.add_calibration_table(win.get("variables", []), mdi)
        elif wtype == "curve_map":
            for n in win.get("variables", []):
                self.open_curve_map(n, mdi)
            return
        else:
            return
        geo = win.get("geometry")
        if geo and w.parent() is not None:
            w.parent().setGeometry(*geo)

    def open_experiment(self, project_id: int, name: str) -> None:
        if self.session.project is None or self.session.project.id != project_id:
            self.load_project(project_id)
        layout = self.session.db.load_experiment(project_id, name)
        self.session.experiment_name = name
        self.session.rasters = dict(layout.get("rasters", {}))
        # pre-layer experiments: a flat "windows" list becomes a single layer
        layers = layout.get("layers")
        if layers is None:
            layers = [{"name": "Layer 1", "windows": layout.get("windows", [])}]
        area = self.experiment_area
        first_mdi = area.clear_all(layers[0].get("name") or "Layer 1")
        for i, ldef in enumerate(layers):
            if i == 0:
                mdi = first_mdi
            else:
                mdi = area.add_layer(ldef.get("name") or f"Layer {i + 1}")
            for win in ldef.get("windows", []):
                self._restore_window(win, mdi)
        area.setCurrentIndex(0)
        self.session.log(f"Experiment '{name}' opened "
                         f"({len(layers)} layer(s))")

    def save_experiment(self) -> None:
        s = self.session
        if s.db is None or s.project is None:
            return
        if not s.experiment_name:
            name, ok = QInputDialog.getText(self, "Save experiment", "Experiment name:")
            if not ok or not name.strip():
                return
            s.experiment_name = name.strip()
        layers = []
        for layer_name, mdi in self.experiment_area.layers():
            windows = []
            for sw in mdi.subWindowList():
                w = sw.widget()
                if hasattr(w, "to_layout"):
                    entry = w.to_layout()
                    g = sw.geometry()
                    entry["geometry"] = [g.x(), g.y(), g.width(), g.height()]
                    windows.append(entry)
            layers.append({"name": layer_name, "windows": windows})
        s.db.save_experiment(s.project.id, s.experiment_name,
                             {"layers": layers, "rasters": s.rasters})
        s.database_changed.emit()
        s.log(f"Experiment '{s.experiment_name}' saved ({len(layers)} layer(s))")

    # ================================================================= hardware
    def configure_hardware(self) -> None:
        dlg = HardwareConfigDialog(self.session.hw_config, self)
        if dlg.exec():
            self.session.hw_config = dlg.result_config()
            self.session.save_hardware()
            self.session.log("Hardware configuration saved")

    def connect_hw(self) -> None:
        try:
            self.session.connect_hw()
        except Exception as exc:
            QMessageBox.warning(self, "Connect", f"Connection failed:\n{exc}")
        self._update_actions()

    def disconnect_hw(self) -> None:
        self.session.disconnect_hw()
        self._update_actions()

    # ================================================================= measurement
    def _experiment_measure_variables(self) -> list[str]:
        """Measurement variables of ALL layers (INCA measures the whole experiment)."""
        names: list[str] = []
        for w in self._all_widgets():
            if isinstance(w, (MeasureTable, Oscilloscope)):
                for n in w.variables():
                    if n not in names:
                        names.append(n)
        return names

    def start_measurement(self) -> None:
        names = self._experiment_measure_variables()
        if not names:
            QMessageBox.information(
                self, "Measurement",
                "No measurement variables in the experiment.\n"
                "Add variables to a Measure Table or Oscilloscope first.")
            return
        try:
            # no pre-connect here: polling connects if needed, and DAQ mode
            # (re)connects with its policy anyway - avoids a double connect
            # that can wedge single-client TCP slaves
            self.session.start_measurement(names)
        except Exception as exc:
            QMessageBox.warning(self, "Measurement", f"Start failed:\n{exc}")
        self._update_actions()

    def stop_measurement(self) -> None:
        self.session.stop_measurement()
        self._update_actions()

    def start_recording(self) -> None:
        default_dir = str(self.session.db.root / "recordings") if self.session.db else ""
        if default_dir:
            Path(default_dir).mkdir(exist_ok=True)
        default = str(Path(default_dir) /
                      default_recording_name(self.session.experiment_name or "measurement"))
        path, _ = QFileDialog.getSaveFileName(self, "Record to MF4", default,
                                              "ASAM MDF4 (*.mf4)")
        if not path:
            return
        try:
            self.session.start_recording(path)
        except Exception as exc:
            QMessageBox.warning(self, "Recording", str(exc))
        self._update_actions()

    def stop_recording(self) -> None:
        self.session.stop_recording()
        self._update_actions()

    # ================================================================= calibration
    def switch_page(self, page: int) -> None:
        try:
            self.session.client.set_cal_page(page)
            which = "working (RAM)" if page == 0 else "reference"
            self.session.log(f"Switched to {which} page")
        except Exception as exc:
            QMessageBox.warning(self, "Calibration page", str(exc))

    def copy_ref_to_working(self) -> None:
        try:
            self.session.client.copy_cal_page(1, 0)
            self.session.log("Reference page copied to working page")
        except Exception as exc:
            QMessageBox.warning(self, "Copy page", str(exc))

    def save_dataset(self) -> None:
        try:
            self.session.save_dataset()
        except Exception as exc:
            QMessageBox.warning(self, "Save dataset", str(exc))
        self._update_actions()

    # ================================================================= flash
    def flash_current(self) -> None:
        s = self.session
        image = s.image
        if image is None:
            path, _ = QFileDialog.getOpenFileName(self, "HEX file to flash", "",
                                                  "Intel HEX (*.hex *.ihx *.ihex)")
            if not path:
                return
            image = MemoryImage(path)
        FlashDialog(s, image, self).exec()

    def flash_dataset(self, project_id: int, dataset_id: int) -> None:
        if self.session.project is None or self.session.project.id != project_id:
            self.load_project(project_id)
        path = self.session.db.dataset_path(dataset_id)
        if path is None:
            return
        FlashDialog(self.session, MemoryImage(path), self).exec()

    # ================================================================= misc
    def about(self) -> None:
        QMessageBox.about(
            self, "About INCAZ",
            f"<h3>INCAZ {__version__}</h3>"
            "<p><b>INtegrated Calibration &amp; Acquisition, Zero-cost</b><br>"
            "<i>measure. calibrate. flash. 100% llama-powered, "
            "0% license server.</i></p>"
            "<p>Open-source measurement &amp; calibration tool for XCP on "
            "Ethernet: A2L projects, HEX datasets, DAQ measurement, MF4 "
            "recording, online/offline calibration and flash programming.</p>"
            "<p>Built on <a href='https://github.com/christoph2/pyxcp'>pyxcp</a>, "
            "asammdf, PySide6 and pyqtgraph.<br>"
            "License: MIT</p>"
            "<p style='color: gray'>INCAZ is an independent open-source "
            "project. It is also a llama.</p>")

    def closeEvent(self, event: QCloseEvent) -> None:
        self.session.shutdown()
        event.accept()
