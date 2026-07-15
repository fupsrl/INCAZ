"""GUI smoke test (offscreen): main window builds, windows open, layout saves."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def test_main_window(app, tmp_path_factory):
    from incaz.gui.main_window import MainWindow

    tmp = tmp_path_factory.mktemp("db")
    win = MainWindow()
    try:
        # database -> project -> windows, all headless
        win.session.open_database(tmp / "smoke", create=True)
        from pathlib import Path
        demo = Path(__file__).parent.parent / "demo"
        pid = win.session.import_a2l("demo", demo / "demo.a2l")
        win.session.load_project(pid)
        assert win.session.a2l is not None

        mt = win.add_measure_table(["EngineSpeed", "CoolantTemp"])
        assert mt.variables() == ["EngineSpeed", "CoolantTemp"]
        osc = win.add_oscilloscope(["EngineSpeed"])
        assert osc.variables() == ["EngineSpeed"]
        cal = win.add_calibration_table(["ScalarGain", "SpeedLimit"])
        assert set(cal.variables()) == {"ScalarGain", "SpeedLimit"}

        # curve editor reads from... nothing (no backend) - must not crash
        win.open_curve_map("TorqueCurve")

        win.session.experiment_name = "smoke"
        win.save_experiment()
        layout = win.session.db.load_experiment(pid, "smoke")
        types = sorted(w["type"] for w in layout["layers"][0]["windows"])
        assert "measure_table" in types and "oscilloscope" in types

        # reopen experiment
        win.experiment_area.clear_all()
        win.open_experiment(pid, "smoke")
        assert len(win.mdi.subWindowList()) >= 3
    finally:
        win.session.shutdown()
        win.close()


def test_multi_layer_experiment(app, tmp_path_factory):
    """INCA-style layers: windows live on tabs; save/reload keeps them apart."""
    from pathlib import Path

    from incaz.gui.main_window import MainWindow
    from incaz.gui.measure_table import MeasureTable
    from incaz.gui.oscilloscope import Oscilloscope

    tmp = tmp_path_factory.mktemp("db3")
    win = MainWindow()
    try:
        win.session.open_database(tmp / "layers", create=True)
        demo = Path(__file__).parent.parent / "demo"
        pid = win.session.import_a2l("demo", demo / "demo.a2l")
        win.session.load_project(pid)
        area = win.experiment_area

        # layer 1: a measure table; layer 2: an oscilloscope + calibration
        assert area.count() == 1
        win.add_measure_table(["EngineSpeed"])
        mdi2 = area.add_layer("Scope layer")
        assert area.count() == 2 and area.current_mdi() is mdi2
        win.add_oscilloscope(["ThrottlePos", "BattVoltage"])
        win.add_calibration_table(["ScalarGain"])
        assert len(mdi2.subWindowList()) == 2

        # measurement covers variables of ALL layers
        assert set(win._experiment_measure_variables()) == {
            "EngineSpeed", "ThrottlePos", "BattVoltage"}

        # persistence round trip
        win.session.experiment_name = "layered"
        win.save_experiment()
        layout = win.session.db.load_experiment(pid, "layered")
        assert [ldef["name"] for ldef in layout["layers"]] == ["Layer 1", "Scope layer"]

        win.open_experiment(pid, "layered")
        area = win.experiment_area
        assert area.count() == 2
        assert area.tabText(1) == "Scope layer"
        layer1 = [sw.widget() for sw in area.widget(0).subWindowList()]
        layer2 = [sw.widget() for sw in area.widget(1).subWindowList()]
        assert any(isinstance(w, MeasureTable) for w in layer1)
        assert any(isinstance(w, Oscilloscope) for w in layer2)

        # raster assignment persists with the experiment
        win.session.set_raster("EngineSpeed", 1)
        win.save_experiment()
        win.session.rasters = {}
        win.open_experiment(pid, "layered")
        assert win.session.rasters == {"EngineSpeed": 1}

        # pre-layer format still opens (backward compatibility)
        win.session.db.save_experiment(pid, "flat", {"windows": [
            {"type": "measure_table", "variables": ["EngineSpeed"]}]})
        win.open_experiment(pid, "flat")
        assert win.experiment_area.count() == 1
        assert len(win.mdi.subWindowList()) == 1
    finally:
        win.session.shutdown()
        win.close()


def test_offline_calibration_via_gui_backend(app, tmp_path_factory):
    from pathlib import Path

    from incaz.gui.session import Session

    demo = Path(__file__).parent.parent / "demo"
    tmp = tmp_path_factory.mktemp("db2")
    s = Session()
    try:
        s.open_database(tmp / "smoke2", create=True)
        pid = s.import_a2l("demo", demo / "demo.a2l")
        s.load_project(pid)
        did = s.add_dataset(pid, "ds1", demo / "demo.hex")
        s.load_dataset(did, "ds1")
        assert s.cal_backend() is not None
        acc = s.accessor("SpeedLimit")
        backend = s.cal_backend()
        assert acc.read(backend).phys == 6000.0
        acc.write_scalar(backend, 5000.0)
        assert acc.read(backend).phys == 5000.0
        assert s.image.dirty
    finally:
        s.shutdown()
