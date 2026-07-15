"""MF4 (ASAM MDF 4) measurement recording via asammdf.

The recorder subscribes to the AcquisitionManager; samples are buffered per
acquisition group (one channel group per DAQ list / polling raster) and
written to the .mf4 file when recording stops.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)


class Mf4Recorder:
    def __init__(self):
        self._lock = threading.Lock()
        self._groups: dict[str, dict] = {}
        self._meta: dict[str, dict] = {}
        self.path: Optional[Path] = None
        self.recording = False
        self.start_wallclock: Optional[datetime] = None
        self._t_first: Optional[float] = None

    # ------------------------------------------------------------------ control
    def start(self, path: str | Path, channel_meta: dict[str, dict] | None = None) -> None:
        """channel_meta: name -> {"unit": str, "comment": str}"""
        with self._lock:
            self._groups = {}
            self._meta = channel_meta or {}
            self.path = Path(path)
            self.start_wallclock = datetime.now()
            self._t_first = None
            self.recording = True
        log.info("Recording to %s", self.path)

    def on_samples(self, group: str, t: float, values: dict) -> None:
        """AcquisitionManager subscriber."""
        if not self.recording:
            return
        with self._lock:
            if self._t_first is None:
                self._t_first = t
            g = self._groups.get(group)
            if g is None:
                g = {"t": [], "channels": {}}
                self._groups[group] = g
            g["t"].append(t - self._t_first)
            n = len(g["t"])
            for name, (raw, phys) in values.items():
                ch = g["channels"].get(name)
                if ch is None:
                    ch = []
                    g["channels"][name] = ch
                # pad channels that appeared late
                while len(ch) < n - 1:
                    ch.append(np.nan)
                value = phys if isinstance(phys, (int, float)) else raw
                try:
                    ch.append(float(value))
                except (TypeError, ValueError):
                    ch.append(np.nan)
            # keep all channels rectangular
            for ch in g["channels"].values():
                while len(ch) < n:
                    ch.append(np.nan)

    def stop(self) -> Optional[Path]:
        with self._lock:
            if not self.recording:
                return None
            self.recording = False
            groups = self._groups
            self._groups = {}
        if not groups or all(len(g["t"]) == 0 for g in groups.values()):
            log.warning("Recording stopped: no samples captured")
            return None
        path = self._write(groups)
        log.info("Recording saved: %s", path)
        return path

    # ------------------------------------------------------------------ write
    def _write(self, groups: dict[str, dict]) -> Path:
        from asammdf import MDF, Signal

        mdf = MDF(version="4.10")
        if self.start_wallclock is not None:
            mdf.header.start_time = self.start_wallclock
        for group_name, g in groups.items():
            timestamps = np.asarray(g["t"], dtype=np.float64)
            signals = []
            for name, samples in g["channels"].items():
                meta = self._meta.get(name, {})
                signals.append(Signal(
                    samples=np.asarray(samples, dtype=np.float64),
                    timestamps=timestamps,
                    name=name,
                    unit=meta.get("unit", ""),
                    comment=meta.get("comment", ""),
                ))
            if signals:
                mdf.append(signals, acq_name=group_name,
                           comment=f"INCAZ acquisition group {group_name}")
        path = self.path
        if path.suffix.lower() != ".mf4":
            path = path.with_suffix(".mf4")
        # unique filename like INCA does
        if path.exists():
            stem, i = path.stem, 1
            while path.exists():
                path = path.with_name(f"{stem}_{i:03d}.mf4")
                i += 1
        mdf.save(path, overwrite=False, compression=2)
        mdf.close()
        return path


def default_recording_name(experiment: str = "measurement") -> str:
    return f"{experiment}_{time.strftime('%Y%m%d_%H%M%S')}.mf4"
