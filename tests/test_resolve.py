"""Tests for resolve() — the circuit→netlist expansion step."""
import pytest
from pathlib import Path
from silicai.generate import resolve, GenerateError

FIXTURES = Path(__file__).parent / "fixtures"
COMP_LIB = [FIXTURES / "components"]


def _resolve(circuit_file: str) -> dict:
    return resolve(FIXTURES / "circuits" / circuit_file, COMP_LIB)


class TestBasicResolution:
    def test_parts_list(self):
        r = _resolve("simple.yaml")
        ics = [p for p in r["parts"] if p.get("comp_def")]
        assert len(ics) == 1
        assert ics[0]["ref"] == "U1"

    def test_netlist_has_power_nets(self):
        r = _resolve("simple.yaml")
        assert "VCC" in r["netlist"]
        assert "GND" in r["netlist"]

    def test_power_nets_collected(self):
        r = _resolve("simple.yaml")
        assert "VCC" in r["power_nets"]
        assert "GND" in r["power_nets"]

    def test_unknown_mpn_raises(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            "circuit:\n  name: X\n  instances:\n"
            "    - ref: U1\n      mpn: DOES_NOT_EXIST\n"
        )
        with pytest.raises(GenerateError, match="not found"):
            resolve(bad, COMP_LIB)


class TestPinConfig:
    def test_pin_config_overrides_net(self):
        """pin_config: ALERT: GND should tie ALERT directly to GND."""
        r = _resolve("simple.yaml")
        u1_nets = r["parts"][0]["pin_nets"]
        assert u1_nets["ALERT"] == "GND"

    def test_pin_config_degenerate_external_skipped(self):
        """When a pin is tied directly to its externals target, no passive is generated."""
        r = _resolve("simple.yaml")
        # ALERT has no externals in the fixture, but if it did,
        # a resistor from ALERT to GND would be skipped when ALERT IS GND.
        # Here we just verify no extra passives appear beyond what's expected.
        part_refs = [p["ref"] for p in r["parts"]]
        assert "U1" in part_refs
        # Only the decoupling cap from the rail should be present
        passives = [p for p in r["parts"] if p.get("comp_def") is None]
        assert len(passives) == 1  # one decoupling cap on VCC rail


class TestRailOverrides:
    def test_rail_net_renamed(self):
        """rails: {vcc: VCC} should rename component's internal rail net."""
        r = _resolve("simple.yaml")
        u1_nets = r["parts"][0]["pin_nets"]
        assert u1_nets["VCC"] == "VCC"   # renamed from component default

    def test_power_nets_uses_circuit_power_rails(self):
        r = _resolve("simple.yaml")
        # Circuit declares power_rails: [{net: VCC}, {net: GND}]
        assert "VCC" in r["power_nets"]


class TestDecoupling:
    def test_decoupling_cap_generated(self):
        r = _resolve("simple.yaml")
        passives = [p for p in r["parts"] if p.get("comp_def") is None]
        cap = next((p for p in passives if p["type"] == "capacitor"), None)
        assert cap is not None, "Expected a decoupling capacitor"
        # Per-pin decoupling caps connect to the shared rail net; rail_group
        # is set so the schematic writer arranges them in a horizontal bus.
        assert cap["pin_nets"]["1"] == "VCC"
        assert cap["pin_nets"]["2"] == "GND"


class TestInductorAndCrystal:
    def test_inductor_in_parts(self):
        r = _resolve("mcu_passives.yaml")
        inductors = [p for p in r["parts"] if p.get("type") == "inductor"]
        assert len(inductors) == 1

    def test_inductor_value(self):
        r = _resolve("mcu_passives.yaml")
        ind = next(p for p in r["parts"] if p.get("type") == "inductor")
        assert ind["value"] == "3.3u"

    def test_inductor_ref_prefix(self):
        r = _resolve("mcu_passives.yaml")
        ind = next(p for p in r["parts"] if p.get("type") == "inductor")
        assert ind["ref"].startswith("L")

    def test_inductor_nets(self):
        r = _resolve("mcu_passives.yaml")
        ind = next(p for p in r["parts"] if p.get("type") == "inductor")
        nets = set(ind["pin_nets"].values())
        assert "U1_VREG_LX" in nets
        assert "DVDD" in nets

    def test_inductor_in_netlist(self):
        r = _resolve("mcu_passives.yaml")
        ind = next(p for p in r["parts"] if p.get("type") == "inductor")
        ref = ind["ref"]
        all_refs = {r for conns in r["netlist"].values() for r, _ in conns}
        assert ref in all_refs

    def test_crystal_in_parts(self):
        r = _resolve("mcu_passives.yaml")
        xtals = [p for p in r["parts"] if p.get("type") == "crystal"]
        assert len(xtals) == 1

    def test_crystal_value(self):
        r = _resolve("mcu_passives.yaml")
        xtal = next(p for p in r["parts"] if p.get("type") == "crystal")
        assert xtal["value"] == "12MHz"

    def test_crystal_ref_prefix(self):
        r = _resolve("mcu_passives.yaml")
        xtal = next(p for p in r["parts"] if p.get("type") == "crystal")
        assert xtal["ref"].startswith("X")

    def test_crystal_nets(self):
        r = _resolve("mcu_passives.yaml")
        xtal = next(p for p in r["parts"] if p.get("type") == "crystal")
        nets = set(xtal["pin_nets"].values())
        assert "U1_XIN" in nets
        assert "U1_XOUT" in nets

    def test_crystal_in_netlist(self):
        r = _resolve("mcu_passives.yaml")
        xtal = next(p for p in r["parts"] if p.get("type") == "crystal")
        ref = xtal["ref"]
        all_refs = {r for conns in r["netlist"].values() for r, _ in conns}
        assert ref in all_refs

    def test_unknown_type_warns(self, tmp_path, capsys):
        import yaml as _yaml
        from silicai.generate import resolve as _resolve_fn
        comp = tmp_path / "comp.yaml"
        comp.write_text(_yaml.dump({
            "component": {
                "mpn": "TEST_UNKNOWN_EXT",
                "manufacturer": "X", "category": "other", "package": "SOT-23",
                "kicad_symbol": "Device:R",
                "pins": [
                    {"number": 1, "name": "A", "direction": "passive",
                     "externals": [{"type": "foobar", "required": True, "to": "GND"}]},
                    {"number": 2, "name": "B", "direction": "passive", "net": "GND"},
                ],
            }
        }))
        circuit = tmp_path / "c.yaml"
        circuit.write_text(_yaml.dump({
            "circuit": {
                "name": "unknown ext test",
                "instances": [{"ref": "U1", "mpn": "TEST_UNKNOWN_EXT"}],
            }
        }))
        _resolve_fn(circuit, [tmp_path])
        captured = capsys.readouterr()
        assert "warning" in captured.err
        assert "foobar" in captured.err
