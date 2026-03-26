"""Tests for silicai-validate (schema validation)."""
import subprocess
import sys
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
BIN = sys.executable.replace("python", "silicai-validate")


def _validate(*paths: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "silicai.validate", *[str(p) for p in paths]],
        capture_output=True, text=True
    )


class TestValidComponents:
    def test_valid_sensor_fixture(self):
        r = _validate(FIXTURES / "components" / "valid_sensor.yaml")
        assert r.returncode == 0
        assert "✓" in r.stdout

    def test_real_tmp117(self):
        """The TMP117 component in silicai-components must stay valid."""
        comp = Path(__file__).parents[2] / "silicai-components/components/sensor/ti/tmp117aidrvr.yaml"
        if not comp.exists():
            import pytest; pytest.skip("silicai-components not found")
        r = _validate(comp)
        assert r.returncode == 0

    def test_real_nrf52840(self):
        comp = Path(__file__).parents[2] / "silicai-components/components/mcu/nordic/nrf52840-qdaa-r.yaml"
        if not comp.exists():
            import pytest; pytest.skip("silicai-components not found")
        r = _validate(comp)
        assert r.returncode == 0

    def test_real_ap2112k(self):
        comp = Path(__file__).parents[2] / "silicai-components/components/power/diodes_inc/ap2112k-3.3.yaml"
        if not comp.exists():
            import pytest; pytest.skip("silicai-components not found")
        r = _validate(comp)
        assert r.returncode == 0


class TestInvalidComponents:
    def test_missing_pins_rejected(self):
        r = _validate(FIXTURES / "components" / "invalid_missing_pins.yaml")
        assert r.returncode != 0
        assert "✗" in r.stdout

    def test_bad_category_rejected(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            "$schema_version: '0.1.0'\n"
            "component:\n"
            "  mpn: X\n  manufacturer: X\n  category: invalid_cat\n"
            "  package: SOT-23\n  pins: []\n"
        )
        r = _validate(bad)
        assert r.returncode != 0

    def test_bad_pin_direction_rejected(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            "$schema_version: '0.1.0'\n"
            "component:\n"
            "  mpn: X\n  manufacturer: X\n  category: sensor\n"
            "  package: SOT-23\n"
            "  pins:\n"
            "    - number: 1\n      name: A\n      direction: wrong_dir\n"
        )
        r = _validate(bad)
        assert r.returncode != 0
