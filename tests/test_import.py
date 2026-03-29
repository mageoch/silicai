"""Tests for import_kicad.py — KiCad schematic → SilicAI YAML converter."""

import pytest
from pathlib import Path

from silicai.import_kicad import (
    NetGraph,
    detect_bus_signal,
    parse_resistance,
    pin_connection_point,
    import_sheet,
    import_project,
    build_kicad_sym_index,
)

FIXTURES = Path(__file__).parent / "fixtures"
COMP_LIB = [FIXTURES / "components"]
KICAD_LIB = Path("/usr/share/kicad/symbols")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _generate_simple_sch(tmp_path: Path) -> Path:
    """Generate a KiCad schematic from the simple fixture circuit."""
    from silicai.generate import resolve
    from silicai.kicad.writer import write_kicad_sch
    resolved = resolve(FIXTURES / "circuits" / "simple.yaml", COMP_LIB)
    out = tmp_path / "simple.kicad_sch"
    write_kicad_sch(resolved, out, KICAD_LIB)
    return out


# ── NetGraph ──────────────────────────────────────────────────────────────────

class TestNetGraph:
    def test_label_registered(self):
        ng = NetGraph()
        ng.add_label(10.0, 20.0, "MY_NET")
        assert ng.net_at(10.0, 20.0) == "MY_NET"

    def test_label_propagates_through_wire(self):
        ng = NetGraph()

        class _P:
            def __init__(self, x, y):
                self.X, self.Y = x, y

        ng.add_wire(_P(0.0, 0.0), _P(10.0, 0.0))
        ng.add_label(10.0, 0.0, "NET_A")
        assert ng.net_at(0.0, 0.0) == "NET_A"

    def test_first_label_wins(self):
        ng = NetGraph()
        ng.add_label(5.0, 5.0, "FIRST")
        ng.add_label(5.0, 5.0, "SECOND")
        assert ng.net_at(5.0, 5.0) == "FIRST"

    def test_missing_point_returns_none(self):
        ng = NetGraph()
        assert ng.net_at(99.0, 99.0) is None

    def test_snap_precision(self):
        ng = NetGraph()
        ng.add_label(10.001, 20.002, "NET")
        # Both round to (10.0, 20.0) at 2 decimal places
        assert ng.net_at(10.001, 20.002) == "NET"
        assert ng.net_at(10.002, 20.003) == "NET"  # rounds to same key


# ── pin_connection_point ──────────────────────────────────────────────────────

class TestPinConnectionPoint:
    def test_zero_angle_no_mirror(self):
        """Angle=0, no mirror: world = (sym + px, sym - py)."""
        wx, wy = pin_connection_point(50.0, 60.0, 0, None, 10.0, 5.0)
        assert abs(wx - 60.0) < 1e-6
        assert abs(wy - 55.0) < 1e-6

    def test_zero_angle_negative_py(self):
        """Y-flip: positive lib_py → negative schematic offset."""
        wx, wy = pin_connection_point(50.0, 60.0, 0, None, 0.0, -10.0)
        assert abs(wx - 50.0) < 1e-6
        assert abs(wy - 70.0) < 1e-6

    def test_90_degree_rotation(self):
        """90° rotation: (px, py) → (py, px) in schematic frame."""
        wx, wy = pin_connection_point(50.0, 50.0, 90, None, 10.0, 0.0)
        assert abs(wx - 50.0) < 1e-3
        assert abs(wy - 40.0) < 1e-3  # 50 - 10 (rot_y flipped)

    def test_mirror_x(self):
        """Mirror x: py negated before rotation."""
        wx, wy = pin_connection_point(0.0, 0.0, 0, "x", 0.0, 5.0)
        assert abs(wy - 5.0) < 1e-6  # py becomes -5, then -(−5)=+5

    def test_mirror_y(self):
        """Mirror y: px negated before rotation."""
        wx, wy = pin_connection_point(0.0, 0.0, 0, "y", 5.0, 0.0)
        assert abs(wx - (-5.0)) < 1e-6


# ── parse_resistance ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("4k7",   {"value": 4.7,   "unit": "kΩ"}),
    ("4.7k",  {"value": 4.7,   "unit": "kΩ"}),
    ("10k",   {"value": 10.0,  "unit": "kΩ"}),
    ("100R",  {"value": 100.0, "unit": "Ω"}),
    ("100",   {"value": 100.0, "unit": "Ω"}),
    ("1M",    {"value": 1.0,   "unit": "MΩ"}),
    ("4.7kΩ", {"value": 4.7,   "unit": "kΩ"}),
])
def test_parse_resistance(value, expected):
    assert parse_resistance(value) == expected


@pytest.mark.parametrize("bad", ["abc", "", "10uF", "nF"])
def test_parse_resistance_invalid(bad):
    assert parse_resistance(bad) is None


# ── detect_bus_signal ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("net_name,expected", [
    ("i2c_main_SCL",  ("i2c_main", "I2C",  "scl")),
    ("i2c_main_SDA",  ("i2c_main", "I2C",  "sda")),
    ("spi1_MOSI",     ("spi1",     "SPI",  "mosi")),
    ("spi1_MISO",     ("spi1",     "SPI",  "miso")),
    ("uart0_TX",      ("uart0",    "UART", "tx")),
    ("SCL",           None),   # no prefix separator → no match
    ("RANDOM_NET",    None),
    ("VCC",           None),
])
def test_detect_bus_signal(net_name, expected):
    assert detect_bus_signal(net_name) == expected


def test_detect_bus_signal_empty_prefix():
    """Net name '_SCL' produces bus_id 'i2c' (type fallback)."""
    result = detect_bus_signal("_SCL")
    assert result is not None
    assert result[1] == "I2C"
    assert result[2] == "scl"


# ── build_kicad_sym_index ─────────────────────────────────────────────────────

def test_build_kicad_sym_index():
    idx = build_kicad_sym_index(COMP_LIB)
    # valid_sensor.yaml has kicad_symbol "Sensor_Temperature:TMP117xxYBG"
    assert "Sensor_Temperature:TMP117xxYBG" in idx
    comp = idx["Sensor_Temperature:TMP117xxYBG"]
    assert comp["mpn"] == "TEST_SENSOR_001"


# ── Round-trip: generate → import ────────────────────────────────────────────

@pytest.fixture(scope="module")
def simple_sch(tmp_path_factory):
    """Generate the simple fixture schematic once per test session."""
    return _generate_simple_sch(tmp_path_factory.mktemp("kicad"))


class TestImportSheet:
    def test_u1_instance_present(self, simple_sch):
        result = import_sheet(simple_sch, COMP_LIB)
        refs = [i["ref"] for i in result["instances"]]
        assert "U1" in refs

    def test_u1_mpn(self, simple_sch):
        result = import_sheet(simple_sch, COMP_LIB)
        u1 = next(i for i in result["instances"] if i["ref"] == "U1")
        assert u1["mpn"] == "TEST_SENSOR_001"

    def test_power_nets_detected(self, simple_sch):
        result = import_sheet(simple_sch, COMP_LIB)
        # GND is always placed as a power symbol. VCC may fall back to a GlobalLabel
        # if the KiCad symbol's pin name ('V+') differs from the component YAML ('VCC').
        assert "GND" in result["power_nets"]

    def test_no_power_symbols_as_instances(self, simple_sch):
        result = import_sheet(simple_sch, COMP_LIB)
        for inst in result["instances"]:
            assert not inst["ref"].startswith("#PWR")

    def test_no_warnings_for_known_component(self, simple_sch):
        result = import_sheet(simple_sch, COMP_LIB)
        # U1 should be recognised; decoupling caps are passives (skipped silently)
        ic_warnings = [w for w in result["warnings"] if "U1" in w]
        assert ic_warnings == []


class TestImportProject:
    def test_writes_project_yaml(self, simple_sch, tmp_path):
        written, _ = import_project(simple_sch, COMP_LIB, tmp_path)
        paths = [p.name for p in written]
        assert "project.yaml" in paths

    def test_writes_circuit_yaml(self, simple_sch, tmp_path):
        written, _ = import_project(simple_sch, COMP_LIB, tmp_path)
        circuit_files = [p for p in written if p.parent.name == "circuits"]
        assert len(circuit_files) >= 1

    def test_project_yaml_is_valid(self, simple_sch, tmp_path):
        """Imported project.yaml must pass silicai-validate."""
        import subprocess
        import sys
        written, _ = import_project(simple_sch, COMP_LIB, tmp_path)
        proj = next(p for p in written if p.name == "project.yaml")
        result = subprocess.run(
            [sys.executable, "-m", "silicai.validate", str(proj)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_circuit_yaml_is_valid(self, simple_sch, tmp_path):
        """Imported circuit YAML must pass silicai-validate."""
        import subprocess
        import sys
        written, _ = import_project(simple_sch, COMP_LIB, tmp_path)
        circuits = [p for p in written if p.parent.name == "circuits"]
        for circuit_path in circuits:
            result = subprocess.run(
                [sys.executable, "-m", "silicai.validate", str(circuit_path)],
                capture_output=True, text=True,
            )
            assert result.returncode == 0, f"{circuit_path}: " + result.stdout + result.stderr
