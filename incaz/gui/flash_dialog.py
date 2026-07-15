"""ProF flash dialog: program a HEX dataset into the ECU via XCP PGM."""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from ..core.flash import FlashAbort, FlashController, ProfConfig
from ..core.hexfile import MemoryImage


class _Bridge(QObject):
    progress = Signal(int, str)
    log = Signal(str)
    finished = Signal(bool, str)


class FlashDialog(QDialog):
    def __init__(self, session, image: MemoryImage, parent=None):
        super().__init__(parent)
        self.session = session
        self.image = image
        self.controller: FlashController | None = None
        self._thread: threading.Thread | None = None

        self.setWindowTitle("Flash ECU (ProF)")
        self.setMinimumSize(560, 460)
        layout = QVBoxLayout(self)

        segments = image.segments()
        total = sum(s for _, s in segments)
        seg_text = "\n".join(f"  0x{a:08X}  {s:>8} bytes" for a, s in segments[:12])
        if len(segments) > 12:
            seg_text += f"\n  ... {len(segments) - 12} more"
        layout.addWidget(QLabel(
            f"Image: {image.path.name if image.path else '<memory>'}\n"
            f"{len(segments)} segment(s), {total} bytes total\n{seg_text}"))

        form = QFormLayout()
        self.clear_cb = QCheckBox("PROGRAM_CLEAR before programming")
        self.clear_cb.setChecked(True)
        form.addRow(self.clear_cb)
        self.verify_cb = QCheckBox("Verify with BUILD_CHECKSUM")
        self.verify_cb.setChecked(True)
        form.addRow(self.verify_cb)
        self.reset_cb = QCheckBox("PROGRAM_RESET after flashing (restart ECU)")
        self.reset_cb.setChecked(True)
        form.addRow(self.reset_cb)
        self.unlock_cb = QCheckBox("Seed && key unlock PGM resource first")
        form.addRow(self.unlock_cb)
        layout.addLayout(form)

        self.progress = QProgressBar()
        layout.addWidget(self.progress)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        layout.addWidget(self.log_view)

        buttons = QHBoxLayout()
        self.start_btn = QPushButton("Start flashing")
        self.start_btn.clicked.connect(self._start)
        self.abort_btn = QPushButton("Abort")
        self.abort_btn.setEnabled(False)
        self.abort_btn.clicked.connect(self._abort)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        buttons.addWidget(self.start_btn)
        buttons.addWidget(self.abort_btn)
        buttons.addStretch()
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

        self.bridge = _Bridge()
        self.bridge.progress.connect(self._on_progress)
        self.bridge.log.connect(self.log_view.appendPlainText)
        self.bridge.finished.connect(self._on_finished)

    # ------------------------------------------------------------------ run
    def _start(self) -> None:
        if not self.session.client.connected:
            try:
                self.session.connect_hw()
            except Exception as exc:
                self.log_view.appendPlainText(f"Connect failed: {exc}")
                return
        prof = ProfConfig(
            name="gui",
            clear_before_program=self.clear_cb.isChecked(),
            verify_checksum=self.verify_cb.isChecked(),
            reset_after=self.reset_cb.isChecked(),
            unlock_pgm=self.unlock_cb.isChecked(),
        )
        self.controller = FlashController(
            self.session.client, self.image, prof,
            progress=lambda pct, msg: self.bridge.progress.emit(pct, msg),
            logger=lambda msg: self.bridge.log.emit(msg),
        )
        self.start_btn.setEnabled(False)
        self.abort_btn.setEnabled(True)

        def worker():
            try:
                self.controller.run()
                self.bridge.finished.emit(True, "")
            except FlashAbort:
                self.bridge.finished.emit(False, "Aborted")
            except Exception as exc:
                self.bridge.finished.emit(False, str(exc))

        self._thread = threading.Thread(target=worker, name="flash", daemon=True)
        self._thread.start()

    def _abort(self) -> None:
        if self.controller is not None:
            self.controller.abort()

    def _on_progress(self, pct: int, msg: str) -> None:
        self.progress.setValue(pct)
        if msg:
            self.progress.setFormat(f"{msg}  %p%")

    def _on_finished(self, ok: bool, error: str) -> None:
        self.start_btn.setEnabled(True)
        self.abort_btn.setEnabled(False)
        if ok:
            self.log_view.appendPlainText("=== FLASH SUCCESSFUL ===")
        else:
            self.log_view.appendPlainText(f"=== FLASH FAILED: {error} ===")
