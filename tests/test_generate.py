"""Tests for write_kicad_sch() — schematic output correctness."""
import pytest
from pathlib import Path
from silicai.generate import resolve, _net_priority
from silicai.kicad.writer import write_kicad_sch
from silicai.kicad.project import write_kicad_project

FIXTURES = Path(__file__).parent / "fixtures"
COMP_LIB = [FIXTURES / "components"]
KICAD_LIB = Path("/usr/share/kicad/symbols")


def _generate(circuit_file: str, tmp_path: Path) -> str:
    """Resolve and generate; return the schematic file contents."""
    resolved = resolve(FIXTURES / "circuits" / circuit_file, COMP_LIB)
    out = tmp_path / "out.kicad_sch"
    write_kicad_sch(resolved, out, KICAD_LIB)
    return out.read_text()


@pytest.fixture(scope="module")
def simple_sch(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("sch")
    return _generate("simple.yaml", tmp)


class TestPowerSymbols:
    def test_gnd_power_symbol_placed(self, simple_sch):
        assert 'lib_id "power:GND"' in simple_sch

    def test_vcc_power_symbol_placed(self, simple_sch):
        # Decoupling caps connect to the shared rail net via a horizontal bus wire;
        # a VCC power symbol should appear at the left end of that bus.
        assert 'lib_id "power:VCC"' in simple_sch

    def test_no_global_label_for_power_nets(self, simple_sch):
        # Power nets should use symbols, not global labels
        assert 'global_label "GND"' not in simple_sch
        assert 'global_label "VCC"' not in simple_sch

    def test_power_symbols_have_pwr_reference(self, simple_sch):
        assert "#PWR" in simple_sch


class TestPinNumbers:
    def test_passives_hide_pin_numbers(self, simple_sch):
        # Device:C lib symbol entry should have pin_numbers hide
        assert "(pin_numbers hide)" in simple_sch


class TestPassiveOrientation:
    """_net_priority drives passive pin swap for power-on-top convention."""

    def test_power_supply_priority_above_signal(self):
        power_nets = {"+3V3", "GND"}
        assert _net_priority("+3V3", power_nets) > _net_priority("SCL", power_nets)

    def test_signal_priority_above_gnd(self):
        power_nets = {"+3V3", "GND"}
        assert _net_priority("SCL", power_nets) > _net_priority("GND", power_nets)

    def test_gnd_lowest_priority(self):
        power_nets = {"VCC", "GND"}
        assert _net_priority("GND", power_nets) == 0

    def test_power_supply_highest_priority(self):
        power_nets = {"VCC", "GND"}
        assert _net_priority("VCC", power_nets) == 2

    def test_signal_middle_priority(self):
        power_nets = {"VCC", "GND"}
        assert _net_priority("SDA", power_nets) == 1


class TestSchematicOutput:
    def test_output_file_created(self, tmp_path):
        out = tmp_path / "test.kicad_sch"
        resolved = resolve(FIXTURES / "circuits" / "simple.yaml", COMP_LIB)
        write_kicad_sch(resolved, out, KICAD_LIB)
        assert out.exists()
        assert out.stat().st_size > 0

    def test_kicad_sch_header(self, simple_sch):
        assert simple_sch.startswith("(kicad_sch")

    def test_lib_symbols_section_present(self, simple_sch):
        assert "(lib_symbols" in simple_sch


class TestProjectGeneration:
    def test_project_creates_pro_file(self, tmp_path):
        write_kicad_project(FIXTURES / "project.yaml", COMP_LIB, tmp_path, KICAD_LIB)
        pro_files = list(tmp_path.glob("*.kicad_pro"))
        assert len(pro_files) == 1

    def test_project_creates_root_schematic(self, tmp_path):
        write_kicad_project(FIXTURES / "project.yaml", COMP_LIB, tmp_path, KICAD_LIB)
        sch_files = list(tmp_path.glob("*.kicad_sch"))
        # root sch + one sub-sheet
        assert len(sch_files) == 2

    def test_project_root_schematic_has_sheet(self, tmp_path):
        write_kicad_project(FIXTURES / "project.yaml", COMP_LIB, tmp_path, KICAD_LIB)
        root_files = [f for f in tmp_path.glob("*.kicad_sch") if f.stem == "test_project"]
        assert root_files, "Expected a root schematic named after the project"
        assert "(sheet" in root_files[0].read_text()

    def test_project_sub_sheet_is_valid_schematic(self, tmp_path):
        write_kicad_project(FIXTURES / "project.yaml", COMP_LIB, tmp_path, KICAD_LIB)
        sub_files = [f for f in tmp_path.glob("*.kicad_sch") if f.stem != "test_project"]
        assert sub_files
        assert sub_files[0].read_text().startswith("(kicad_sch")
