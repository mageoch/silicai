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
