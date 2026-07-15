"""Dialogs: hardware configuration (XCP on Ethernet), etc."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from ..core.hardware import HardwareConfig


class HardwareConfigDialog(QDialog):
    """XCP on Ethernet parameters - INCA's hardware configuration."""

    def __init__(self, config: HardwareConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Hardware Configuration - XCP on Ethernet")
        self.setMinimumWidth(420)
        self._config = config

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.protocol = QComboBox()
        self.protocol.addItems(["UDP", "TCP"])
        self.protocol.setCurrentText(config.protocol)
        form.addRow("Protocol:", self.protocol)

        self.host = QLineEdit(config.host)
        form.addRow("Host / IP:", self.host)

        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(int(config.port))
        form.addRow("Port:", self.port)

        self.daq_mode = QComboBox()
        self.daq_mode.addItems(["POLLING", "DAQ"])
        self.daq_mode.setCurrentText(config.daq_mode)
        self.daq_mode.setToolTip(
            "POLLING: master reads variables cyclically (works everywhere).\n"
            "DAQ: slave sends data on its own rasters (needs DAQ support).")
        form.addRow("Measurement mode:", self.daq_mode)

        self.poll_rate = QDoubleSpinBox()
        self.poll_rate.setRange(0.5, 1000.0)
        self.poll_rate.setValue(config.poll_rate_hz)
        self.poll_rate.setSuffix(" Hz")
        form.addRow("Polling rate:", self.poll_rate)

        self.default_event = QSpinBox()
        self.default_event.setRange(0, 65534)
        self.default_event.setValue(config.default_event)
        self.default_event.setToolTip(
            "Fallback event channel, used only if the A2L declares no DAQ "
            "events.\nOtherwise unassigned variables go to the cyclic event "
            "closest to 10 ms;\nassign rasters per variable in the measure "
            "table ('Raster' column).")
        form.addRow("Fallback DAQ event:", self.default_event)

        self.timestamps = QCheckBox("Use slave DAQ timestamps")
        self.timestamps.setChecked(config.enable_timestamps)
        form.addRow("", self.timestamps)

        dll_row = QHBoxLayout()
        self.seed_key = QLineEdit(config.seed_n_key_dll)
        self.seed_key.setPlaceholderText("Optional seed & key DLL ...")
        browse = QPushButton("...")
        browse.setFixedWidth(30)
        browse.clicked.connect(self._browse_dll)
        dll_row.addWidget(self.seed_key)
        dll_row.addWidget(browse)
        form.addRow("Seed && Key DLL:", dll_row)

        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse_dll(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Seed & Key DLL", "",
                                              "DLL (*.dll);;All files (*.*)")
        if path:
            self.seed_key.setText(path)

    def result_config(self) -> HardwareConfig:
        cfg = self._config
        cfg.protocol = self.protocol.currentText()
        cfg.host = self.host.text().strip()
        cfg.port = self.port.value()
        cfg.daq_mode = self.daq_mode.currentText()
        cfg.poll_rate_hz = self.poll_rate.value()
        cfg.default_event = self.default_event.value()
        cfg.enable_timestamps = self.timestamps.isChecked()
        cfg.seed_n_key_dll = self.seed_key.text().strip()
        return cfg
