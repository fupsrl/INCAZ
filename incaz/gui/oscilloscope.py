"""Oscilloscope window - INCA-style scrolling signal plot (pyqtgraph).

Performance notes: buffers are plain lists (cheap appends from the
acquisition thread); the redraw finds the visible window with bisect
(O(log n)) and hands numpy arrays to pyqtgraph with peak-downsampling,
so kHz DAQ rasters stay fluid.
"""

from __future__ import annotations

import threading
from bisect import bisect_left

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .variable_browser import parse_mime

_COLORS = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
           "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"]

_MAX_POINTS = 100_000


class Oscilloscope(QWidget):
    variables_changed = Signal()

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        self.setWindowTitle("Oscilloscope")
        self.setAcceptDrops(True)

        self._lock = threading.Lock()
        self._buffers: dict[str, tuple[list, list]] = {}     # name -> (t, y)
        self._curves: dict[str, pg.PlotDataItem] = {}
        self._paused = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("Time window [s]:"))
        self.window_spin = QDoubleSpinBox()
        self.window_spin.setRange(1.0, 3600.0)
        self.window_spin.setValue(30.0)
        toolbar.addWidget(self.window_spin)
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setCheckable(True)
        self.pause_btn.toggled.connect(self._on_pause)
        toolbar.addWidget(self.pause_btn)
        self.autoscale = QCheckBox("Auto scale Y")
        self.autoscale.setChecked(True)
        toolbar.addWidget(self.autoscale)
        toolbar.addStretch()
        layout.addLayout(toolbar)

        self.plot = pg.PlotWidget(background="k")
        self.plot.showGrid(x=True, y=True, alpha=0.3)
        self.plot.addLegend(offset=(10, 10))
        self.plot.setLabel("bottom", "time", units="s")
        self.plot.setClipToView(True)
        self.plot.setDownsampling(auto=True, mode="peak")
        self.plot.setContextMenuPolicy(Qt.CustomContextMenu)
        self.plot.customContextMenuRequested.connect(self._context_menu)
        layout.addWidget(self.plot)

        self.session.acq.subscribe(self._on_samples)
        self.destroyed.connect(lambda: self.session.acq.unsubscribe(self._on_samples))

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._redraw)
        self._timer.start(33)  # ~30 fps

    # ------------------------------------------------------------------ config
    def variables(self) -> list[str]:
        return list(self._buffers.keys())

    def add_variables(self, names: list[str]) -> None:
        added = False
        with self._lock:
            for name in names:
                if name in self._buffers:
                    continue
                if self.session.a2l and name not in self.session.a2l.measurements:
                    continue
                self._buffers[name] = ([], [])
                color = _COLORS[len(self._curves) % len(_COLORS)]
                self._curves[name] = self.plot.plot([], [], pen=pg.mkPen(color, width=1.5),
                                                    name=name)
                added = True
        if added:
            self.variables_changed.emit()

    def remove_variable(self, name: str) -> None:
        with self._lock:
            self._buffers.pop(name, None)
            curve = self._curves.pop(name, None)
        if curve is not None:
            self.plot.removeItem(curve)
            self.plot.getPlotItem().legend.removeItem(curve)
            self.variables_changed.emit()

    def _context_menu(self, pos) -> None:
        menu = QMenu(self)
        clear = menu.addAction("Clear buffers")
        clear.triggered.connect(self._clear)
        if self._buffers:
            raster_menu = menu.addMenu("Raster")
            for name in list(self._buffers):
                sub = raster_menu.addMenu(name)
                current = self.session.rasters.get(name)
                for label, channel in self.session.raster_choices():
                    act = sub.addAction(label)
                    act.setCheckable(True)
                    act.setChecked(channel == current or
                                   (channel is None and current is None))
                    act.triggered.connect(
                        lambda _c=False, n=name, ch=channel:
                        self.session.set_raster(n, ch))
            rm = menu.addMenu("Remove signal")
            for name in list(self._buffers):
                rm.addAction(name, lambda n=name: self.remove_variable(n))
        menu.exec(self.plot.mapToGlobal(pos))

    def _clear(self) -> None:
        with self._lock:
            for t, y in self._buffers.values():
                t.clear()
                y.clear()

    def _on_pause(self, checked: bool) -> None:
        self._paused = checked
        self.pause_btn.setText("Resume" if checked else "Pause")

    # ------------------------------------------------------------------ data
    def _on_samples(self, _group: str, t: float, values: dict) -> None:
        if self._paused:
            return
        with self._lock:
            for name, (_raw, phys) in values.items():
                buf = self._buffers.get(name)
                if buf is None or not isinstance(phys, (int, float)):
                    continue
                buf[0].append(t)
                buf[1].append(float(phys))
                # bounded memory: drop the oldest half once in a while
                if len(buf[0]) > _MAX_POINTS:
                    del buf[0][:_MAX_POINTS // 2]
                    del buf[1][:_MAX_POINTS // 2]

    def _redraw(self) -> None:
        if self._paused or not self._buffers:
            return
        window = self.window_spin.value()
        t_max = None
        with self._lock:
            # slice only the visible window (bisect: buffers are time-ordered)
            snapshots = {}
            for name, (ts, ys) in self._buffers.items():
                if not ts:
                    continue
                start = bisect_left(ts, ts[-1] - window)
                snapshots[name] = (np.asarray(ts[start:]), np.asarray(ys[start:]))
        for name, (ts, ys) in snapshots.items():
            if t_max is None or ts[-1] > t_max:
                t_max = float(ts[-1])
            curve = self._curves.get(name)
            if curve is not None:
                curve.setData(ts, ys)
        if t_max is not None:
            self.plot.setXRange(max(0.0, t_max - window), t_max, padding=0)
            if self.autoscale.isChecked():
                self.plot.enableAutoRange(axis="y")

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
        return {"type": "oscilloscope", "variables": self.variables(),
                "window_s": self.window_spin.value()}

    def from_layout(self, layout: dict) -> None:
        self.window_spin.setValue(layout.get("window_s", 30.0))
        self.add_variables(layout.get("variables", []))
