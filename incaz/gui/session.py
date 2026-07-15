"""Application session: the glue between database, project, hardware and GUI.

Owns the INCA-style state machine:

    database -> project (A2L) -> dataset (HEX) -> hardware -> experiment
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Signal

from ..core.a2l import A2LDatabase, parse_a2l
from ..core.acquisition import AcquisitionManager
from ..core.calibration import CharacteristicAccessor, HexBackend
from ..core.database import CalDatabase, ProjectInfo
from ..core.hardware import HardwareConfig
from ..core.hexfile import MemoryImage
from ..core.recorder import Mf4Recorder
from ..core.xcp_client import XcpClient

log = logging.getLogger(__name__)


class XcpMemoryBackend:
    """Calibration memory backend that talks to the connected ECU."""

    def __init__(self, client: XcpClient, ext: int = 0):
        self.client = client
        self.ext = ext

    def read(self, address: int, size: int) -> bytes:
        return self.client.read_memory(address, self.ext, size)

    def write(self, address: int, data: bytes) -> None:
        self.client.write_memory(address, self.ext, data)


class Session(QObject):
    database_changed = Signal()
    project_loaded = Signal(str)          # project name
    dataset_loaded = Signal(str)          # dataset name
    connection_changed = Signal(bool, str)
    measurement_changed = Signal(bool)
    recording_changed = Signal(bool, str)
    message = Signal(str)                 # log line for the console

    def __init__(self):
        super().__init__()
        self.db: Optional[CalDatabase] = None
        self.project: Optional[ProjectInfo] = None
        self.a2l: Optional[A2LDatabase] = None
        self.image: Optional[MemoryImage] = None
        self.dataset_id: Optional[int] = None
        self.dataset_name: str = ""
        self.hw_config = HardwareConfig()
        self.client = XcpClient()
        self.acq = AcquisitionManager(self.client)
        self.recorder = Mf4Recorder()
        self.experiment_name: str = ""
        #: variable name -> DAQ event channel; the experiment's raster
        #: assignment (INCA: one raster per variable). None/absent = auto.
        self.rasters: dict[str, int] = {}

        self.acq.subscribe(self.recorder.on_samples)
        self.client.on_state_change = self._on_conn_state
        self.client.on_error = lambda msg: self.message.emit(f"[XCP] {msg}")

    # ---------------------------------------------------------------- helpers
    def _on_conn_state(self, connected: bool, info: str) -> None:
        # called from XCP worker thread - Signal emission is thread-safe
        self.connection_changed.emit(connected, info)

    def log(self, msg: str) -> None:
        log.info(msg)
        self.message.emit(msg)

    # ---------------------------------------------------------------- database
    def open_database(self, path: str | Path, create: bool = False) -> None:
        if self.db is not None:
            self.db.close()
        self.db = CalDatabase(path)
        if create:
            self.db.create()
            self.log(f"Database created: {self.db.root}")
        else:
            self.db.open()
            self.log(f"Database opened: {self.db.root}")
        self.database_changed.emit()

    def import_a2l(self, name: str, a2l_path: str | Path, comment: str = "") -> int:
        if self.db is None:
            raise RuntimeError("Open or create a database first")
        pid = self.db.add_project(name, a2l_path, comment)
        self.log(f"Project '{name}' imported from {a2l_path}")
        self.database_changed.emit()
        return pid

    def add_dataset(self, project_id: int, name: str, hex_path: str | Path) -> int:
        did = self.db.add_dataset(project_id, name, hex_path)
        self.log(f"Dataset '{name}' imported from {hex_path}")
        self.database_changed.emit()
        return did

    # ---------------------------------------------------------------- project
    def load_project(self, project_id: int) -> None:
        info = self.db.project(project_id)
        if info is None:
            raise RuntimeError(f"No project with id {project_id}")
        self.a2l = parse_a2l(info.a2l_file)
        self.project = info
        self.acq.set_database(self.a2l)
        self.hw_config = HardwareConfig.from_dict(self.db.load_hardware(project_id))
        # sensible defaults from IF_DATA XCP
        xi = self.a2l.xcp_info
        if not self.db.load_hardware(project_id):
            if xi.protocol:
                self.hw_config.protocol = xi.protocol
            if xi.address:
                self.hw_config.host = xi.address
            if xi.port:
                self.hw_config.port = xi.port
        self.image = None
        self.dataset_id = None
        self.dataset_name = ""
        self.rasters = {}
        self.log(f"Project '{info.name}' loaded: "
                 f"{len(self.a2l.measurements)} measurements, "
                 f"{len(self.a2l.characteristics)} characteristics")
        self.project_loaded.emit(info.name)

    def load_dataset(self, dataset_id: int, name: str = "") -> None:
        path = self.db.dataset_path(dataset_id)
        if path is None:
            raise RuntimeError(f"No dataset with id {dataset_id}")
        self.image = MemoryImage(path)
        self.dataset_id = dataset_id
        self.dataset_name = name or path.stem
        self.log(f"Dataset '{self.dataset_name}' loaded ({len(self.image)} bytes)")
        self.dataset_loaded.emit(self.dataset_name)

    def save_dataset(self) -> None:
        if self.image is not None and self.image.dirty:
            self.image.save()
            self.log(f"Dataset saved: {self.image.path}")

    def save_hardware(self) -> None:
        if self.db is not None and self.project is not None:
            self.db.save_hardware(self.project.id, self.hw_config.to_dict())

    # ---------------------------------------------------------------- hardware
    def connect_hw(self) -> None:
        if self.a2l is None:
            raise RuntimeError("Load a project first")
        self.client.connect(self.hw_config)
        self.log(f"Connected to {self.hw_config.host}:{self.hw_config.port} "
                 f"({self.hw_config.protocol})")

    def disconnect_hw(self) -> None:
        if self.acq.measuring:
            self.stop_measurement()
        self.client.disconnect()
        self.log("Disconnected")

    # ---------------------------------------------------------------- measurement
    def start_measurement(self, names: list[str],
                          events: Optional[dict[str, int]] = None) -> None:
        if not names:
            raise RuntimeError("No variables in the experiment")
        if events is None:
            events = {k: v for k, v in self.rasters.items() if v is not None}
        self.acq.start(names, self.hw_config, events)
        mode = self.hw_config.daq_mode
        self.log(f"Measurement started ({mode}, {len(names)} variables)")
        self.measurement_changed.emit(True)

    # ---------------------------------------------------------------- rasters
    def raster_choices(self) -> list[tuple[str, Optional[int]]]:
        """(label, event_channel) pairs for raster combo boxes."""
        choices: list[tuple[str, Optional[int]]] = [("auto", None)]
        if self.a2l is not None:
            for e in self.a2l.xcp_info.events:
                cycle = f"{e.cycle_time_ms:g} ms" if e.cycle_time_ms else "non-cyclic"
                choices.append((f"{e.name} ({cycle})", e.channel))
        return choices

    def set_raster(self, name: str, event: Optional[int]) -> None:
        if event is None:
            self.rasters.pop(name, None)
        else:
            self.rasters[name] = event
        if self.acq.measuring:
            self.log(f"Raster of {name} changed - restart the measurement to apply")

    def stop_measurement(self) -> None:
        if self.recorder.recording:
            self.stop_recording()
        self.acq.stop()
        self.log("Measurement stopped")
        self.measurement_changed.emit(False)

    # ---------------------------------------------------------------- recording
    def start_recording(self, path: str | Path) -> None:
        if not self.acq.measuring:
            raise RuntimeError("Start the measurement first")
        meta = {}
        for name, rv in self.acq._resolved.items():
            meta[name] = {"unit": rv.unit,
                          "comment": rv.measurement.long_identifier}
        self.recorder.start(path, meta)
        self.recording_changed.emit(True, str(path))
        self.log(f"Recording started: {path}")

    def stop_recording(self) -> None:
        path = self.recorder.stop()
        self.recording_changed.emit(False, str(path) if path else "")
        if path:
            self.log(f"Recording saved: {path}")

    # ---------------------------------------------------------------- calibration
    def cal_backend(self):
        """Online backend when connected, else the offline dataset."""
        if self.client.connected:
            return XcpMemoryBackend(self.client)
        if self.image is not None:
            return HexBackend(self.image)
        return None

    def cal_backend_kind(self) -> str:
        if self.client.connected:
            return "online (ECU working page)"
        if self.image is not None:
            return f"offline (dataset '{self.dataset_name}')"
        return "none"

    def accessor(self, characteristic_name: str) -> Optional[CharacteristicAccessor]:
        if self.a2l is None:
            return None
        char = self.a2l.characteristics.get(characteristic_name)
        if char is None:
            return None
        return CharacteristicAccessor(self.a2l, char)

    def shutdown(self) -> None:
        try:
            if self.acq.measuring:
                self.stop_measurement()
            self.client.disconnect()
            self.client.stop()
        except Exception:
            pass
        if self.db is not None:
            self.db.close()
