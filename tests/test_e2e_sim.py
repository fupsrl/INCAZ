"""End-to-end tests: XCP client against the bundled slave simulator.

The full suite runs twice - once over XCP on UDP and once over XCP on TCP
(the simulator serves both). Covers the INCA workflow pieces that need a
live ECU: connect, polling measurement, online calibration write, DAQ
measurement, MF4 recording and flash (ProF).
"""

import threading
import time
from pathlib import Path

import pytest

from incaz.core.a2l import parse_a2l
from incaz.core.acquisition import AcquisitionManager
from incaz.core.calibration import CharacteristicAccessor
from incaz.core.flash import FlashController, ProfConfig
from incaz.core.hardware import HardwareConfig
from incaz.core.hexfile import MemoryImage
from incaz.core.xcp_client import XcpClient
from incaz.sim.slave import XcpSlaveSim

DEMO = Path(__file__).parent.parent / "demo"
PORTS = {"UDP": 55999, "TCP": 55998}


@pytest.fixture(scope="module", params=["UDP", "TCP"])
def env(request):
    """One simulator per transport; every test below runs for both."""
    protocol = request.param
    sim = XcpSlaveSim(port=PORTS[protocol])
    thread = threading.Thread(target=sim.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.4)
    yield {"sim": sim, "protocol": protocol, "port": PORTS[protocol]}
    sim.stop()


@pytest.fixture(scope="module")
def db():
    return parse_a2l(DEMO / "demo.a2l")


@pytest.fixture()
def client(env):
    c = XcpClient()
    yield c
    c.disconnect()
    c.stop()


@pytest.fixture()
def make_config(env):
    def _make(**kw) -> HardwareConfig:
        cfg = HardwareConfig(protocol=env["protocol"], host="127.0.0.1",
                             port=env["port"])
        for k, v in kw.items():
            setattr(cfg, k, v)
        return cfg
    return _make


class XcpBackend:
    def __init__(self, client):
        self.client = client

    def read(self, address, size):
        return self.client.read_memory(address, 0, size)

    def write(self, address, data):
        self.client.write_memory(address, 0, data)


def test_connect(client, make_config):
    props = client.connect(make_config())
    assert client.connected
    assert props["maxCto"] == 64
    assert props["supportsDaq"] and props["supportsPgm"]


def test_polling_measurement(client, db, make_config):
    cfg = make_config(poll_rate_hz=50.0)
    client.connect(cfg)
    acq = AcquisitionManager(client)
    acq.set_database(db)
    samples = []
    acq.subscribe(lambda group, t, values: samples.append((t, values)))
    acq.start(["EngineSpeed", "CoolantTemp", "GearPos", "Counter"], cfg)
    time.sleep(1.0)
    acq.stop()
    assert len(samples) > 20
    _t, values = samples[-1]
    assert 0 < values["EngineSpeed"][1] <= 16000       # rpm
    assert isinstance(values["GearPos"][1], str)       # verbal conversion
    counters = [v["Counter"][0] for _t, v in samples if "Counter" in v]
    assert counters[-1] > counters[0]                  # counting up


def test_online_calibration(client, db, make_config):
    client.connect(make_config())
    be = XcpBackend(client)
    acc = CharacteristicAccessor(db, db.characteristics["ScalarGain"])
    original = acc.read(be).phys
    acc.write_scalar(be, 2.0)
    assert abs(acc.read(be).phys - 2.0) < 1e-6
    acc.write_scalar(be, original)

    curve = CharacteristicAccessor(db, db.characteristics["TorqueCurve"])
    val = curve.read(be)
    assert val.kind == "curve" and val.phys[3] == 250.0
    curve.write_fnc_element(be, 3, None, 260.0)
    assert curve.read(be).phys[3] == 260.0
    curve.write_fnc_element(be, 3, None, 250.0)


def test_cal_pages(client, make_config):
    client.connect(make_config())
    client.set_cal_page(1)
    page = client.get_cal_page()
    page = getattr(page, "physical", page)
    assert int(page) == 1
    client.set_cal_page(0)
    page = client.get_cal_page()
    assert int(getattr(page, "physical", page)) == 0


def test_auto_event_selection(db, make_config):
    """Unassigned variables go to the cyclic raster nearest 10 ms, never
    blindly to event 0 (which can be a microsecond task on real ECUs)."""
    acq = AcquisitionManager(XcpClient())
    acq.set_database(db)
    cfg = make_config(daq_mode="DAQ", default_event=42)
    # demo A2L: ev0 = 10 ms, ev1 = 100 ms -> auto picks ev0
    assert acq.auto_event(cfg) == 0
    # without A2L event info the configured fallback is used
    db_no_events = parse_a2l(DEMO / "demo.a2l")
    db_no_events.xcp_info.events = []
    acq.set_database(db_no_events)
    assert acq.auto_event(cfg) == 42


def test_daq_measurement(client, db, make_config):
    cfg = make_config(daq_mode="DAQ")
    acq = AcquisitionManager(client)
    acq.set_database(db)
    samples = []
    acq.subscribe(lambda group, t, values: samples.append((group, t, values)))
    acq.start(["EngineSpeed", "ThrottlePos"], cfg,
              events={"EngineSpeed": 0, "ThrottlePos": 1})
    time.sleep(1.5)
    acq.stop()
    groups = {g for g, _t, _v in samples}
    assert "daq_ev0" in groups and "daq_ev1" in groups
    ev0 = [s for s in samples if s[0] == "daq_ev0"]
    assert len(ev0) > 50  # ~10 ms raster
    rpm = [v["EngineSpeed"][1] for _g, _t, v in ev0 if "EngineSpeed" in v]
    assert all(0 <= r <= 16000 for r in rpm)
    # timestamps sit on the nominal raster grid (10 ms), NOT on the bursty
    # arrival times - this is what makes waveforms smooth
    ts = [t for _g, t, _v in ev0]
    dts = {round(b - a, 6) for a, b in zip(ts, ts[1:])}
    assert dts == {0.01}, f"expected equidistant 10 ms grid, got dts={sorted(dts)[:5]}"


def test_recorder_mf4(client, db, make_config, tmp_path):
    from incaz.core.recorder import Mf4Recorder

    cfg = make_config(poll_rate_hz=100.0)
    client.connect(cfg)
    acq = AcquisitionManager(client)
    acq.set_database(db)
    rec = Mf4Recorder()
    acq.subscribe(rec.on_samples)
    acq.start(["EngineSpeed", "BattVoltage"], cfg)
    rec.start(tmp_path / "test.mf4", {"EngineSpeed": {"unit": "rpm"}})
    time.sleep(1.0)
    path = rec.stop()
    acq.stop()
    assert path is not None and path.exists()

    from asammdf import MDF
    with MDF(path) as mdf:
        names = {ch.name for group in mdf.groups for ch in group.channels}
        assert "EngineSpeed" in names and "BattVoltage" in names
        sig = mdf.get("EngineSpeed")
        # Windows sleep granularity caps effective polling around 50-60 Hz
        assert len(sig.samples) > 30
        assert sig.samples.max() <= 16000


def test_flash_prof(client, make_config):
    client.connect(make_config())
    image = MemoryImage(DEMO / "demo.hex")
    logs = []
    prof = ProfConfig(name="test", verify_checksum=True, reset_after=False)
    fc = FlashController(client, image, prof, logger=logs.append)
    fc.run()
    assert any("Flash finished" in line for line in logs)
    # simulator wrote the image into its memory: check one known value
    raw = client.read_memory(0x00020004, 0, 2)
    assert int.from_bytes(raw, "little") == 24000  # SpeedLimit default
