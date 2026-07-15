"""XCP master wrapper around pyxcp.

All XCP traffic is serialized through a single worker thread that owns the
``pyxcp`` :class:`Master` instance (pyxcp masters are not thread-safe).
Public methods return results synchronously (they block the calling thread,
not the GUI event loop if called from helper threads) or schedule work with
callbacks.

A periodic *poll task* can be installed; it runs in the gaps between queued
commands, which is how POLLING-mode measurement works while calibration
read/writes stay possible.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Callable, Optional

from .hardware import HardwareConfig

log = logging.getLogger(__name__)


def ensure_pyxcp_application(config: HardwareConfig):
    """Create + register the global pyxcp application.

    pyxcp components (DAQ policies, loggers) fall back to
    ``get_application()``, which would parse *our* command line (and crash
    on foreign args like pytest's ``-v``) unless an application is set.
    """
    from pyxcp.config import create_application_from_config, set_application

    app = create_application_from_config(config.pyxcp_config(),
                                         log_level=logging.WARNING)
    set_application(app)
    return app


@dataclass
class PollVariable:
    name: str
    address: int
    ext: int
    size: int


class PollTask:
    """Cyclic SHORT_UPLOAD sweep over a set of variables."""

    def __init__(self, variables: list[PollVariable], rate_hz: float,
                 callback: Callable[[float, dict[str, bytes]], None]):
        self.variables = variables
        self.period = 1.0 / max(rate_hz, 0.1)
        self.callback = callback
        self.next_due = 0.0
        self.t0 = time.perf_counter()

    def sweep(self, master) -> None:
        max_read = max(8, master.slaveProperties.maxCto - 1)
        values: dict[str, bytes] = {}
        for var in self.variables:
            if var.size <= max_read:
                data = bytes(master.shortUpload(var.size, var.address, var.ext))[: var.size]
            else:
                master.setMta(var.address, var.ext)
                chunks = b""
                remaining = var.size
                while remaining > 0:
                    n = min(remaining, max_read)
                    chunks += bytes(master.upload(n))[:n]
                    remaining -= n
                data = chunks
            values[var.name] = data
        self.callback(time.perf_counter() - self.t0, values)


class XcpClient:
    """Threaded XCP-on-Ethernet client with an INCA-like surface."""

    def __init__(self):
        self.config = HardwareConfig()
        self._queue: "queue.Queue[tuple[Callable, Future]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._master = None
        self._app = None
        self._policy = None
        self.connected = False
        self.poll_task: Optional[PollTask] = None
        self.on_state_change: Optional[Callable[[bool, str], None]] = None
        self.on_error: Optional[Callable[[str], None]] = None

    # ------------------------------------------------------------- thread core
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="xcp-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    def _loop(self) -> None:
        while self._running:
            timeout = 0.05
            if self.poll_task is not None and self.connected:
                timeout = max(0.0, self.poll_task.next_due - time.perf_counter())
                timeout = min(timeout, 0.05)
            try:
                fn, fut = self._queue.get(timeout=timeout)
            except queue.Empty:
                fn = fut = None
            if fn is not None:
                try:
                    fut.set_result(fn())
                except Exception as exc:  # propagate to caller
                    log.debug("XCP command failed: %s", exc, exc_info=True)
                    fut.set_exception(exc)
            task = self.poll_task
            if task is not None and self.connected and self._master is not None:
                now = time.perf_counter()
                if now >= task.next_due:
                    task.next_due = now + task.period
                    try:
                        task.sweep(self._master)
                    except Exception as exc:
                        log.error("Poll sweep failed: %s", exc)
                        self.poll_task = None
                        if self.on_error:
                            self.on_error(f"Measurement stopped: {exc}")

    def call(self, fn: Callable) -> Future:
        """Schedule *fn* on the XCP worker thread."""
        fut: Future = Future()
        self._queue.put((fn, fut))
        return fut

    def call_sync(self, fn: Callable, timeout: float = 30.0):
        return self.call(fn).result(timeout=timeout)

    # ------------------------------------------------------------- connection
    def connect(self, config: HardwareConfig, policy=None) -> dict:
        """Create the pyxcp master and connect. Returns slave properties."""
        self.start()
        self.config = config

        def _do():
            from pyxcp.master import Master

            had_master = self._master is not None
            self._teardown_master()
            if had_master:
                # single-client TCP slaves (e.g. XCPlite) need a moment to
                # free their connection slot before accepting the next one
                time.sleep(0.3)
            self._app = ensure_pyxcp_application(config)
            self._policy = policy
            self._master = Master("eth", config=self._app, policy=policy)
            self._master.transport.connect()  # open socket
            resp = self._master.connect()
            self.connected = True
            props = dict(self._master.slaveProperties)
            log.info("Connected to XCP slave: maxCto=%s maxDto=%s resources: "
                     "CAL/PAG=%s DAQ=%s PGM=%s",
                     resp.maxCto, resp.maxDto,
                     resp.resource.calpag, resp.resource.daq, resp.resource.pgm)
            if self.on_state_change:
                self.on_state_change(True, f"{config.host}:{config.port} ({config.protocol})")
            return props

        return self.call_sync(_do)

    def disconnect(self) -> None:
        def _do():
            self.poll_task = None
            self._teardown_master()
            if self.on_state_change:
                self.on_state_change(False, "")

        if self._thread is not None and self._thread.is_alive():
            try:
                self.call_sync(_do, timeout=10.0)
            except Exception as exc:
                log.warning("Disconnect: %s", exc)

    def _teardown_master(self) -> None:
        if self._master is not None:
            try:
                if self.connected:
                    self._master.disconnect()
            except Exception:
                pass
            try:
                self._master.close()
            except Exception:
                pass
            self._master = None
        self.connected = False

    def unlock(self) -> None:
        """Seed & key unlock of all protected resources (needs DLL in config)."""
        def _do():
            protection = self._master.getCurrentProtectionStatus()
            if any(protection.values()) if isinstance(protection, dict) else protection:
                self._master.cond_unlock()
        self.call_sync(_do)

    @property
    def slave_properties(self):
        return dict(self._master.slaveProperties) if self._master is not None else {}

    # ------------------------------------------------------------- memory access
    def read_memory(self, address: int, ext: int, size: int) -> bytes:
        def _do():
            master = self._master
            max_read = max(8, master.slaveProperties.maxCto - 1)
            if size <= max_read:
                return bytes(master.shortUpload(size, address, ext))[:size]
            data = b""
            master.setMta(address, ext)
            remaining = size
            while remaining > 0:
                n = min(remaining, max_read)
                data += bytes(master.upload(n))[:n]
                remaining -= n
            return data
        return self.call_sync(_do)

    def write_memory(self, address: int, ext: int, data: bytes) -> None:
        def _do():
            master = self._master
            max_write = max(1, master.slaveProperties.maxCto - 8)
            offset = 0
            while offset < len(data):
                chunk = data[offset:offset + max_write]
                master.shortDownload(address + offset, ext, chunk)
                offset += len(chunk)
        self.call_sync(_do)

    # ------------------------------------------------------------- cal pages
    def get_cal_page(self, segment: int = 0, mode: int = 0x01) -> int:
        return self.call_sync(lambda: self._master.getCalPage(mode, segment))

    def set_cal_page(self, page: int, segment: int = 0, mode: int = 0x83) -> None:
        # mode 0x83: ECU access + XCP access + ALL segments
        self.call_sync(lambda: self._master.setCalPage(mode, segment, page))

    def copy_cal_page(self, src_page: int, dst_page: int, segment: int = 0) -> None:
        self.call_sync(lambda: self._master.copyCalPage(segment, src_page, segment, dst_page))

    # ------------------------------------------------------------- polling
    def start_polling(self, variables: list[PollVariable], rate_hz: float,
                      callback: Callable[[float, dict[str, bytes]], None]) -> None:
        task = PollTask(variables, rate_hz, callback)
        task.next_due = time.perf_counter()
        self.poll_task = task

    def stop_polling(self) -> None:
        self.poll_task = None

    # ------------------------------------------------------------- DAQ (via policy)
    def daq_setup_and_start(self) -> None:
        """Set up + start DAQ lists of the policy passed to connect()."""
        def _do():
            self._policy.setup(write_multiple=False)
            self._policy.start()
        self.call_sync(_do)

    def daq_stop(self) -> None:
        def _do():
            try:
                self._policy.stop()
            finally:
                pass
        if self._policy is not None:
            self.call_sync(_do)

    # ------------------------------------------------------------- checksum
    def build_checksum(self, address: int, size: int):
        def _do():
            self._master.setMta(address, 0)
            return self._master.buildChecksum(size)
        return self.call_sync(_do)
