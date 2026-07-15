"""Measurement acquisition: POLLING and DAQ modes.

Samples flow:  XCP worker / pyxcp transport thread
                 -> AcquisitionManager.dispatch()
                 -> subscribers (thread-safe): latest-value store,
                    oscilloscope buffers, MF4 recorder.

GUI widgets never receive callbacks directly; they read the latest-value
store / their own buffers on a Qt timer.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .a2l.model import A2LDatabase, Measurement, datatype_size, decode_value
from .conversions import Converter, make_converter
from .hardware import HardwareConfig
from .xcp_client import PollVariable, XcpClient

log = logging.getLogger(__name__)

#: A2L datatype -> pyxcp DaqList type string
_DAQ_TYPES = {
    "UBYTE": "U8", "SBYTE": "I8",
    "UWORD": "U16", "SWORD": "I16",
    "ULONG": "U32", "SLONG": "I32",
    "A_UINT64": "U64", "A_INT64": "I64",
    "FLOAT32_IEEE": "F32", "FLOAT64_IEEE": "F64",
}

# subscriber signature: (group_name, timestamp_s, {name: (raw, phys)})
Subscriber = Callable[[str, float, dict], None]


@dataclass
class ResolvedVariable:
    name: str
    measurement: Measurement
    converter: Converter
    event: Optional[int] = None   # DAQ event channel assignment

    @property
    def unit(self) -> str:
        return self.converter.unit


class AcquisitionManager:
    def __init__(self, client: XcpClient):
        self.client = client
        self.db: Optional[A2LDatabase] = None
        self.subscribers: list[Subscriber] = []
        self.latest: dict[str, tuple[float, object, object]] = {}
        self._lock = threading.Lock()
        self.measuring = False
        self.mode = "POLLING"
        self._daq_policy = None
        self._resolved: dict[str, ResolvedVariable] = {}
        self.on_error: Optional[Callable[[str], None]] = None
        #: diagnostics for the GUI status bar
        self.sample_count = 0
        self.dispatch_errors = 0

    def set_database(self, db: A2LDatabase) -> None:
        self.db = db

    # -------------------------------------------------------------- subscribe
    def subscribe(self, cb: Subscriber) -> None:
        if cb not in self.subscribers:
            self.subscribers.append(cb)

    def unsubscribe(self, cb: Subscriber) -> None:
        if cb in self.subscribers:
            self.subscribers.remove(cb)

    def dispatch(self, group: str, t: float, values: dict) -> None:
        self.sample_count += len(values)
        with self._lock:
            for name, pair in values.items():
                self.latest[name] = (t, pair[0], pair[1])
        for cb in list(self.subscribers):
            try:
                cb(group, t, values)
            except Exception:
                log.exception("Subscriber failed")

    def latest_values(self) -> dict[str, tuple[float, object, object]]:
        with self._lock:
            return dict(self.latest)

    # -------------------------------------------------------------- resolve
    def resolve(self, name: str) -> Optional[ResolvedVariable]:
        if self.db is None:
            return None
        meas = self.db.measurements.get(name)
        if meas is None or meas.ecu_address is None:
            return None
        return ResolvedVariable(name=name, measurement=meas,
                                converter=make_converter(self.db, meas.conversion))

    # -------------------------------------------------------------- start/stop
    def start(self, names: list[str], config: HardwareConfig,
              events: Optional[dict[str, int]] = None) -> None:
        """Start measurement of *names*. ``events`` maps name -> DAQ event."""
        if self.measuring:
            raise RuntimeError("Measurement already running")
        if self.db is None:
            raise RuntimeError("No project (A2L) loaded")
        resolved: dict[str, ResolvedVariable] = {}
        skipped: list[str] = []
        for name in names:
            rv = self.resolve(name)
            if rv is None:
                skipped.append(name)
                continue
            if events and name in events:
                rv.event = events[name]
            resolved[name] = rv
        if skipped:
            log.warning("No ECU address, skipped: %s", ", ".join(skipped))
        if not resolved:
            raise RuntimeError("No measurable variables selected")
        self._resolved = resolved
        self.sample_count = 0
        self.dispatch_errors = 0
        self.mode = config.daq_mode.upper()
        if self.mode == "DAQ":
            self._start_daq(config)
        else:
            self._start_polling(config)
        self.measuring = True

    def stop(self) -> None:
        if not self.measuring:
            return
        try:
            if self.mode == "DAQ":
                try:
                    self.client.daq_stop()
                except Exception as exc:
                    log.warning("DAQ stop: %s", exc)
            else:
                self.client.stop_polling()
        finally:
            self.measuring = False

    # -------------------------------------------------------------- polling
    def _start_polling(self, config: HardwareConfig) -> None:
        if not self.client.connected:
            self.client.connect(config)
        variables = []
        for rv in self._resolved.values():
            m = rv.measurement
            variables.append(PollVariable(rv.name, m.ecu_address,
                                          m.ecu_address_extension, m.size))
        bo = self.db.struct_byte_order

        def on_sweep(t: float, raw_bytes: dict[str, bytes]) -> None:
            out = {}
            for name, data in raw_bytes.items():
                rv = self._resolved.get(name)
                if rv is None:
                    continue
                m = rv.measurement
                raw = decode_value(data, m.datatype, bo)
                if m.bit_mask is not None:
                    raw &= m.bit_mask
                out[name] = (raw, rv.converter.raw_to_phys(raw))
            self.dispatch("polling", t, out)

        self.client.start_polling(variables, config.poll_rate_hz, on_sweep)

    def auto_event(self, config: HardwareConfig) -> int:
        """Raster for variables without an explicit assignment: the cyclic
        A2L event closest to 10 ms - fast enough to look live, slow enough
        not to flood the master (a blind default of event 0 can mean a
        100 us task on real ECUs!)."""
        events = [e for e in (self.db.xcp_info.events if self.db else [])
                  if e.cycle_time_ms]
        if not events:
            return config.default_event
        return min(events, key=lambda e: abs(e.cycle_time_ms - 10.0)).channel

    # -------------------------------------------------------------- DAQ
    def _start_daq(self, config: HardwareConfig) -> None:
        from .xcp_client import ensure_pyxcp_application

        # must exist before any pyxcp DAQ object is created, otherwise pyxcp
        # tries to parse the host application's command line
        ensure_pyxcp_application(config)
        from pyxcp.daq_stim import DaqList, DaqOnlinePolicy

        default_ev = self.auto_event(config)
        # group variables by event channel
        groups: dict[int, list[ResolvedVariable]] = {}
        for rv in self._resolved.values():
            m = rv.measurement
            if m.datatype not in _DAQ_TYPES:
                raise RuntimeError(
                    f"{rv.name}: datatype {m.datatype} not DAQ-capable, use POLLING mode")
            ev = rv.event if rv.event is not None else default_ev
            groups.setdefault(ev, []).append(rv)
        log.info("DAQ lists: %s", ", ".join(
            f"event {ev}: {len(rvs)} var(s)" for ev, rvs in sorted(groups.items())))

        ev_cycle_s = {e.channel: (e.cycle_time_ms or 0) / 1000.0
                      for e in self.db.xcp_info.events}
        daq_lists = []
        list_channels: list[list[str]] = []
        list_labels: list[str] = []
        list_cycles: list[float] = []
        for ev, rvs in sorted(groups.items()):
            meas = [(rv.name, rv.measurement.ecu_address,
                     rv.measurement.ecu_address_extension,
                     _DAQ_TYPES[rv.measurement.datatype]) for rv in rvs]
            daq_lists.append(DaqList(
                name=f"daq_ev{ev}",
                event_num=ev,
                stim=False,
                enable_timestamps=bool(config.enable_timestamps),
                measurements=meas,
                priority=0,
                prescaler=1,
            ))
            list_channels.append([rv.name for rv in rvs])
            list_labels.append(f"daq_ev{ev}")
            list_cycles.append(ev_cycle_s.get(ev) or 0.0)

        manager = self
        t_base: list = []
        # Slaves flush DTOs in bursts (transmit queues, Nagle), so arrival
        # time is a terrible sample time base. For cyclic rasters we place
        # samples on the nominal raster grid (count * cycle), anchored at
        # first arrival and re-anchored if they drift apart (drops, pauses).
        time_state = [{"count": 0, "anchor": 0.0} for _ in daq_lists]

        class GuiDaqPolicy(DaqOnlinePolicy):
            def on_daq_list(self, daq_list: int, timestamp0: int, timestamp1: int,
                            payload: list) -> None:
                try:
                    names = list_channels[daq_list]
                    if not t_base:
                        t_base.append(timestamp0)
                    arrival = (timestamp0 - t_base[0]) / 1e9
                    cycle = list_cycles[daq_list]
                    st = time_state[daq_list]
                    if cycle > 0.0:
                        if st["count"] == 0:
                            st["anchor"] = arrival
                        t = st["anchor"] + st["count"] * cycle
                        if abs(arrival - t) > max(0.25, 3.0 * cycle):
                            st["anchor"] = arrival
                            st["count"] = 0
                            t = arrival
                        st["count"] += 1
                    else:  # non-cyclic event: arrival time is all we have
                        t = arrival
                    out = {}
                    for name, raw in zip(names, payload):
                        rv = manager._resolved.get(name)
                        if rv is None:
                            continue
                        m = rv.measurement
                        if m.bit_mask is not None and isinstance(raw, int):
                            raw &= m.bit_mask
                        out[name] = (raw, rv.converter.raw_to_phys(raw))
                    manager.dispatch(list_labels[daq_list], t, out)
                except Exception:
                    manager.dispatch_errors += 1
                    log.exception("DAQ dispatch failed")

            def initialize(self):
                pass

            def finalize(self):
                pass

        policy = GuiDaqPolicy(daq_lists)
        # DAQ policies are wired at Master construction => reconnect
        self.client.connect(config, policy=policy)
        self.client.daq_setup_and_start()
