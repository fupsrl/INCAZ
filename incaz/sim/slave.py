"""Demo XCP-on-Ethernet slave (ECU simulator) - serves UDP *and* TCP.

Implements enough of the XCP protocol for INCAZ end-to-end demos:
CONNECT / memory upload & download / calibration pages / dynamic DAQ /
flash programming (PGM). Signal values match ``demo/demo.a2l``.

Both transports listen on the same port number; the XCP framing
(WORD length + WORD counter header) is identical, but TCP is a byte
stream, so frames are reassembled from the stream before dispatch.

Run:  python -m incaz.sim [--port 5555] [--hex demo/demo.hex]
"""

from __future__ import annotations

import argparse
import logging
import math
import socket
import struct
import threading
import time

log = logging.getLogger("incaz.sim")

# ---------------------------------------------------------------- memory map
MEAS_BASE = 0x00010000       # measurement RAM
CAL_BASE = 0x00020000        # calibration segment (working / reference pages)
CAL_SIZE = 0x1000

# command codes
CONNECT, DISCONNECT, GET_STATUS, SYNCH = 0xFF, 0xFE, 0xFD, 0xFC
GET_COMM_MODE_INFO, GET_ID = 0xFB, 0xFA
BUILD_CHECKSUM = 0xF3
SET_MTA, UPLOAD, SHORT_UPLOAD = 0xF6, 0xF5, 0xF4
DOWNLOAD, SHORT_DOWNLOAD = 0xF0, 0xED
SET_CAL_PAGE, GET_CAL_PAGE, COPY_CAL_PAGE = 0xEB, 0xEA, 0xE4
SET_DAQ_PTR, WRITE_DAQ, SET_DAQ_LIST_MODE = 0xE2, 0xE1, 0xE0
START_STOP_DAQ_LIST, START_STOP_SYNCH, GET_DAQ_CLOCK = 0xDE, 0xDD, 0xDC
GET_DAQ_PROCESSOR_INFO, GET_DAQ_RESOLUTION_INFO = 0xDA, 0xD9
FREE_DAQ, ALLOC_DAQ, ALLOC_ODT, ALLOC_ODT_ENTRY = 0xD6, 0xD5, 0xD4, 0xD3
PROGRAM_START, PROGRAM_CLEAR, PROGRAM, PROGRAM_RESET = 0xD2, 0xD1, 0xD0, 0xCF
PROGRAM_NEXT = 0xCA

ERR_CMD_UNKNOWN = 0x20


class Memory:
    """Sparse byte-addressable memory."""

    def __init__(self):
        self._data: dict[int, int] = {}

    def read(self, addr: int, size: int) -> bytes:
        return bytes(self._data.get(addr + i, 0) for i in range(size))

    def write(self, addr: int, data: bytes) -> None:
        for i, b in enumerate(data):
            self._data[addr + i] = b

    def u16(self, addr: int, value: int) -> None:
        self.write(addr, struct.pack("<H", int(value) & 0xFFFF))

    def u32(self, addr: int, value: int) -> None:
        self.write(addr, struct.pack("<I", int(value) & 0xFFFFFFFF))

    def u8(self, addr: int, value: int) -> None:
        self.write(addr, bytes([int(value) & 0xFF]))

    def f32(self, addr: int, value: float) -> None:
        self.write(addr, struct.pack("<f", value))

    def get_f32(self, addr: int) -> float:
        return struct.unpack("<f", self.read(addr, 4))[0]

    def get_u16(self, addr: int) -> int:
        return struct.unpack("<H", self.read(addr, 2))[0]

    def get_u8(self, addr: int) -> int:
        return self.read(addr, 1)[0]


class OdtEntry:
    __slots__ = ("address", "ext", "size")

    def __init__(self):
        self.address = 0
        self.ext = 0
        self.size = 0


class DaqListState:
    def __init__(self):
        self.odts: list[list[OdtEntry]] = []
        self.event = 0
        self.selected = False
        self.running = False


class XcpSlaveSim:
    def __init__(self, port: int = 5555, hex_file: str | None = None):
        self.port = port
        self.mem = Memory()
        self.ref_page = Memory()      # calibration reference page
        self.active_page = 0          # 0 = working (RAM), 1 = reference
        self.mta = 0
        self.mta_ext = 0
        self.connected = False
        self.daq_lists: list[DaqListState] = []
        self.daq_ptr = (0, 0, 0)
        self.daq_running = False
        self.pgm_active = False
        #: sends a complete raw frame to the current master (UDP or TCP)
        self._sender = None
        self.ctr_out = 0
        self._lock = threading.Lock()        # memory / ECU state
        self._send_lock = threading.Lock()   # counter + wire (TCP frames must not interleave)
        self._cmd_lock = threading.Lock()    # serialize commands from both listeners
        self._stop = threading.Event()
        self._init_cal(hex_file)

    # ---------------------------------------------------------------- setup
    def _init_cal(self, hex_file: str | None) -> None:
        if hex_file:
            from intelhex import IntelHex
            ih = IntelHex(hex_file)
            for start, stop in ih.segments():
                self.mem.write(start, ih.tobinstr(start=start, end=stop - 1))
            log.info("Calibration initialised from %s", hex_file)
        else:
            m = self.mem
            m.f32(CAL_BASE + 0x00, 1.0)            # ScalarGain
            m.u16(CAL_BASE + 0x04, 24000)          # SpeedLimit  (6000 rpm / 0.25)
            m.u8(CAL_BASE + 0x06, 187)             # FanOnTemp   (~92 degC)
            for i in range(8):                     # TorqueCurve axis: 1000..8000 rpm
                m.u16(CAL_BASE + 0x10 + 2 * i, (1000 + 1000 * i) * 4)
            torque = [80, 150, 210, 250, 245, 220, 180, 120]
            for i, tq in enumerate(torque):        # values, 0.1 Nm/bit
                m.write(CAL_BASE + 0x20 + 2 * i, struct.pack("<h", tq * 10))
            for i in range(6):                     # IgnitionMap X axis (rpm)
                m.u16(CAL_BASE + 0x40 + 2 * i, (1000 + 1200 * i) * 4)
            for i in range(6):                     # Y axis (load %)
                m.u16(CAL_BASE + 0x4C + 2 * i, i * 20)
            for iy in range(6):                    # values 0.5 deg/bit - 10
                for ix in range(6):
                    deg = 35 - 4 * iy + ix
                    self.mem.u8(CAL_BASE + 0x58 + iy * 6 + ix, int((deg + 10) * 2))
        # snapshot as reference page
        self.ref_page.write(CAL_BASE, self.mem.read(CAL_BASE, CAL_SIZE))

    # ---------------------------------------------------------------- signals
    def _signal_thread(self) -> None:
        t0 = time.perf_counter()
        counter = 0
        while not self._stop.is_set():
            t = time.perf_counter() - t0
            m = self.mem
            with self._lock:
                gain = max(0.0, min(self.mem.get_f32(CAL_BASE + 0x00), 4.0))
                limit_raw = self.mem.get_u16(CAL_BASE + 0x04)
                counter = (counter + 1) & 0xFFFFFFFF
                m.u32(MEAS_BASE + 0x00, counter)
                rpm = 900 + (2500 + 2000 * math.sin(2 * math.pi * 0.1 * t)) * gain
                rpm = min(rpm, limit_raw * 0.25)
                m.u16(MEAS_BASE + 0x04, int(rpm / 0.25))
                temp = 70 + 20 * (1 - math.exp(-t / 60)) + 2 * math.sin(0.5 * t)
                m.u8(MEAS_BASE + 0x06, int((temp + 48) / 0.75))
                throttle = abs((t * 20) % 200 - 100)
                m.u8(MEAS_BASE + 0x07, int(throttle))
                m.u8(MEAS_BASE + 0x08, int(t / 3) % 6)
                m.u16(MEAS_BASE + 0x0A, int(13800 + 400 * math.sin(3 * t)))
                m.f32(MEAS_BASE + 0x0C, rpm)
            time.sleep(0.005)

    # ---------------------------------------------------------------- cal pages
    def _cal_read(self, addr: int, size: int) -> bytes:
        if self.active_page == 1 and CAL_BASE <= addr < CAL_BASE + CAL_SIZE:
            return self.ref_page.read(addr, size)
        return self.mem.read(addr, size)

    # ---------------------------------------------------------------- framing
    def _send(self, payload: bytes) -> None:
        with self._send_lock:
            self.ctr_out = (self.ctr_out + 1) & 0xFFFF
            frame = struct.pack("<HH", len(payload), self.ctr_out) + payload
            sender = self._sender
            if sender is not None:
                try:
                    sender(frame)
                except OSError as exc:
                    log.debug("send failed (master gone?): %s", exc)

    def _pos(self, *payload: int, raw: bytes = b"") -> None:
        self._send(bytes([0xFF, *payload]) + raw)

    def _err(self, code: int) -> None:
        self._send(bytes([0xFE, code]))

    # ---------------------------------------------------------------- DAQ tx
    def _daq_thread(self) -> None:
        tick = 0
        while not self._stop.is_set():
            time.sleep(0.010)
            tick += 1
            if not self.daq_running:
                continue
            for event in (0,) if tick % 10 else (0, 1):
                self._send_daq_event(event)

    def _send_daq_event(self, event: int) -> None:
        abs_odt = 0
        for lst in self.daq_lists:
            if not lst.running or lst.event != event:
                abs_odt += len(lst.odts)
                continue
            for odt in lst.odts:
                data = b"".join(self._cal_read(e.address, e.size) for e in odt)
                if data:
                    self._send(bytes([abs_odt & 0xFF]) + data)
                abs_odt += 1

    # ---------------------------------------------------------------- commands
    def handle(self, p: bytes) -> None:
        cmd = p[0]
        if cmd == CONNECT:
            self.connected = True
            # resources: CAL/PAG | DAQ | PGM ; comm mode: little endian, byte
            # granularity, optional comm mode info available
            self._pos(0x15, 0x80, 64, *struct.pack("<H", 512), 0x01, 0x01)
        elif cmd == DISCONNECT:
            self.connected = False
            self.daq_running = False
            self._pos()
        elif cmd == GET_STATUS:
            self._pos(0x00, 0x00, 0x00, *struct.pack("<H", 0))
        elif cmd == SYNCH:
            self._err(0x00)
        elif cmd == GET_COMM_MODE_INFO:
            self._pos(0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x10)
        elif cmd == GET_ID:
            ident = b"INCAZ demo ECU simulator"
            self._pos(0x01, 0x00, 0x00, *struct.pack("<I", len(ident)), raw=ident)
        elif cmd == BUILD_CHECKSUM:
            size = struct.unpack_from("<I", p, 4)[0]
            data = self._cal_read(self.mta, size)
            self._pos(0x01, 0x00, 0x00, *struct.pack("<I", sum(data) & 0xFFFFFFFF))
            self.mta += size
        elif cmd == SET_MTA:
            self.mta_ext = p[3]
            self.mta = struct.unpack_from("<I", p, 4)[0]
            self._pos()
        elif cmd == UPLOAD:
            n = p[1]
            self._pos(raw=self._cal_read(self.mta, n))
            self.mta += n
        elif cmd == SHORT_UPLOAD:
            n, ext = p[1], p[3]
            addr = struct.unpack_from("<I", p, 4)[0]
            self._pos(raw=self._cal_read(addr, n))
        elif cmd == DOWNLOAD:
            n = p[1]
            with self._lock:
                self.mem.write(self.mta, p[2:2 + n])
            self.mta += n
            self._pos()
        elif cmd == SHORT_DOWNLOAD:
            n, ext = p[1], p[3]
            addr = struct.unpack_from("<I", p, 4)[0]
            with self._lock:
                self.mem.write(addr, p[8:8 + n])
            self._pos()
        elif cmd == SET_CAL_PAGE:
            self.active_page = p[3]
            self._pos()
        elif cmd == GET_CAL_PAGE:
            self._pos(0x00, 0x00, self.active_page)
        elif cmd == COPY_CAL_PAGE:
            # only ref(1) -> work(0) supported
            with self._lock:
                self.mem.write(CAL_BASE, self.ref_page.read(CAL_BASE, CAL_SIZE))
            self._pos()
        # -------------------------------------------------------------- DAQ
        elif cmd == FREE_DAQ:
            self.daq_lists = []
            self.daq_running = False
            self._pos()
        elif cmd == ALLOC_DAQ:
            count = struct.unpack_from("<H", p, 2)[0]
            self.daq_lists = [DaqListState() for _ in range(count)]
            self._pos()
        elif cmd == ALLOC_ODT:
            daq = struct.unpack_from("<H", p, 2)[0]
            self.daq_lists[daq].odts = [[] for _ in range(p[4])]
            self._pos()
        elif cmd == ALLOC_ODT_ENTRY:
            daq = struct.unpack_from("<H", p, 2)[0]
            odt, count = p[4], p[5]
            self.daq_lists[daq].odts[odt] = [OdtEntry() for _ in range(count)]
            self._pos()
        elif cmd == SET_DAQ_PTR:
            daq = struct.unpack_from("<H", p, 2)[0]
            self.daq_ptr = (daq, p[4], p[5])
            self._pos()
        elif cmd == WRITE_DAQ:
            daq, odt, entry = self.daq_ptr
            e = self.daq_lists[daq].odts[odt][entry]
            e.size = p[2]
            e.ext = p[3]
            e.address = struct.unpack_from("<I", p, 4)[0]
            self.daq_ptr = (daq, odt, entry + 1)
            self._pos()
        elif cmd == SET_DAQ_LIST_MODE:
            daq = struct.unpack_from("<H", p, 2)[0]
            self.daq_lists[daq].event = struct.unpack_from("<H", p, 4)[0]
            self._pos()
        elif cmd == START_STOP_DAQ_LIST:
            mode = p[1]
            daq = struct.unpack_from("<H", p, 2)[0]
            lst = self.daq_lists[daq]
            if mode == 0:
                lst.running = lst.selected = False
            elif mode == 1:
                lst.running = True
            elif mode == 2:
                lst.selected = True
            first_pid = sum(len(li.odts) for li in self.daq_lists[:daq])
            self._pos(first_pid & 0xFF)
        elif cmd == START_STOP_SYNCH:
            mode = p[1]
            if mode == 0:
                for lst in self.daq_lists:
                    lst.running = lst.selected = False
                self.daq_running = False
            elif mode == 1:
                for lst in self.daq_lists:
                    if lst.selected:
                        lst.running = True
                self.daq_running = True
            elif mode == 2:
                for lst in self.daq_lists:
                    if lst.selected:
                        lst.running = False
                self.daq_running = any(li.running for li in self.daq_lists)
            self._pos()
        elif cmd == GET_DAQ_CLOCK:
            ticks = int(time.perf_counter() * 1e3) & 0xFFFFFFFF
            self._pos(0x00, 0x00, 0x00, *struct.pack("<I", ticks))
        elif cmd == GET_DAQ_PROCESSOR_INFO:
            # dynamic DAQ, no timestamps, absolute ODT identification
            self._pos(0x01, *struct.pack("<H", 8), *struct.pack("<H", 2), 0x00, 0x00)
        elif cmd == GET_DAQ_RESOLUTION_INFO:
            self._pos(0x01, 0xC8, 0x01, 0xC8, 0x00, *struct.pack("<H", 0))
        # -------------------------------------------------------------- PGM
        elif cmd == PROGRAM_START:
            self.pgm_active = True
            # comm mode pgm: master block mode; maxCtoPgm=64, maxBsPgm=32
            self._pos(0x00, 0x01, 64, 32, 0x00, 0x00)
        elif cmd == PROGRAM_CLEAR:
            size = struct.unpack_from("<I", p, 4)[0]
            with self._lock:
                self.mem.write(self.mta, b"\xFF" * size)
            self._pos()
        elif cmd in (PROGRAM, PROGRAM_NEXT):
            # p[1] = remaining elements in the block (master block mode!):
            # the master expects a response only after the LAST frame of a
            # block - answering every frame would desync request/response.
            block_remaining = p[1]
            data = p[2:]
            with self._lock:
                self.mem.write(self.mta, data)
                if CAL_BASE <= self.mta < CAL_BASE + CAL_SIZE:
                    self.ref_page.write(self.mta, data)
            self.mta += len(data)
            if len(data) >= block_remaining:
                self._pos()
        elif cmd == PROGRAM_RESET:
            self.pgm_active = False
            self.connected = False
            log.info("PROGRAM_RESET received - 'ECU' restarts")
            self._pos()
        else:
            log.debug("Unknown command 0x%02X", cmd)
            self._err(ERR_CMD_UNKNOWN)

    # ---------------------------------------------------------------- run
    def _dispatch(self, payload: bytes, sender) -> None:
        if not payload:
            return
        with self._cmd_lock:
            self._sender = sender
            try:
                self.handle(payload)
            except Exception:
                log.exception("Command 0x%02X failed", payload[0])
                self._err(0x22)  # ERR_OUT_OF_RANGE

    def _udp_loop(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", self.port))
        sock.settimeout(0.5)
        log.info("XCP slave simulator listening on UDP port %d", self.port)
        try:
            while not self._stop.is_set():
                try:
                    frame, peer = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if len(frame) < 5:
                    continue
                length, _ctr = struct.unpack_from("<HH", frame, 0)
                self._dispatch(frame[4:4 + length],
                               lambda data, s=sock, p=peer: s.sendto(data, p))
        finally:
            sock.close()

    def _tcp_loop(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.port))
        srv.listen(1)
        srv.settimeout(0.5)
        log.info("XCP slave simulator listening on TCP port %d", self.port)
        try:
            while not self._stop.is_set():
                try:
                    conn, peer = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                log.info("TCP master connected: %s:%d", *peer)
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self._tcp_client(conn)
        finally:
            srv.close()

    def _tcp_client(self, conn: socket.socket) -> None:
        """Serve one TCP master: reassemble XCP frames from the byte stream."""
        sender = conn.sendall
        conn.settimeout(0.5)
        buf = b""
        try:
            while not self._stop.is_set():
                try:
                    data = conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not data:      # master closed the connection
                    break
                buf += data
                while len(buf) >= 4:
                    length, _ctr = struct.unpack_from("<HH", buf, 0)
                    if len(buf) < 4 + length:
                        break     # frame incomplete - wait for more bytes
                    self._dispatch(buf[4:4 + length], sender)
                    buf = buf[4 + length:]
        finally:
            with self._send_lock:
                if self._sender == sender:
                    self._sender = None
            self.daq_running = False
            self.connected = False
            conn.close()
            log.info("TCP master disconnected")

    def serve_forever(self) -> None:
        threading.Thread(target=self._signal_thread, daemon=True).start()
        threading.Thread(target=self._daq_thread, daemon=True).start()
        udp = threading.Thread(target=self._udp_loop, daemon=True)
        tcp = threading.Thread(target=self._tcp_loop, daemon=True)
        udp.start()
        tcp.start()
        while not self._stop.is_set():
            time.sleep(0.2)
        udp.join(timeout=2.0)
        tcp.join(timeout=2.0)

    def stop(self) -> None:
        self._stop.set()


def main() -> None:
    parser = argparse.ArgumentParser(description="INCAZ demo XCP slave (UDP)")
    parser.add_argument("--port", type=int, default=5555)
    parser.add_argument("--hex", help="HEX file to initialise the calibration segment")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    sim = XcpSlaveSim(port=args.port, hex_file=args.hex)
    try:
        sim.serve_forever()
    except KeyboardInterrupt:
        sim.stop()


if __name__ == "__main__":
    main()
