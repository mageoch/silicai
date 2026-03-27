#!/usr/bin/env python3
"""MCP server exposing SilicAI circuit-design tools to Claude Code."""

import argparse
import json
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator
from mcp.server.fastmcp import FastMCP

from silicai.generate import (
    GenerateError, find_component, load_config, resolve, write_kicad_sch, write_kicad_project,
    _DEFAULT_KICAD_SYM, _BUILTIN_COMPONENTS,
)
from silicai.import_kicad import KiCadImportError, import_project
from silicai.validate import build_registry, resolve_schema


def _load_shared_context(project_dir: Path) -> dict:
    """Load shared buses/power_rails from project.yaml if present, else return empty dict."""
    project_yaml = project_dir / "project.yaml"
    if not project_yaml.exists():
        return {}
    try:
        doc = yaml.safe_load(project_yaml.read_text())
        return doc.get("project", {}).get("shared", {})
    except Exception:
        return {}

# ── Bootstrap ─────────────────────────────────────────────────────────────────
# Resolved once at server startup from --project-dir (default: CWD).

_project_dir: Path = Path.cwd()
_lib_paths: list[Path] = []
_registry = None  # jsonschema Registry, built lazily on first validate call

mcp = FastMCP("silicai")


def _get_registry():
    global _registry
    if _registry is None:
        _registry = build_registry()
    return _registry


def _iter_components():
    """Yield raw component dicts from all configured library paths."""
    for lib in _lib_paths:
        for f in lib.rglob("*.yaml"):
            try:
                doc = yaml.safe_load(f.read_text())
            except Exception:
                continue
            if isinstance(doc, dict) and "component" in doc:
                yield doc["component"]


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_components() -> list[dict]:
    """List all components available in the configured library paths.

    Returns a summary list with mpn, manufacturer, category, and package.
    Use get_component(mpn) to retrieve the full specification.
    """
    return [
        {
            "mpn":          comp.get("mpn", ""),
            "manufacturer": comp.get("manufacturer", ""),
            "category":     comp.get("category", ""),
            "package":      comp.get("package", ""),
        }
        for comp in _iter_components()
    ]


@mcp.tool()
def get_component(mpn: str) -> dict:
    """Return the full component specification for a given MPN.

    Args:
        mpn: Manufacturer Part Number, e.g. "TMP117AIDRVR"

    Returns the complete component dict as defined in the component YAML,
    or a dict with an "error" key if the component is not found.
    """
    try:
        return find_component(mpn, _lib_paths)
    except GenerateError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Unexpected error: {e}"}


@mcp.tool()
def search_components(
    category: str | None = None,
    interface_type: str | None = None,
    keyword: str | None = None,
) -> list[dict]:
    """Search components by category, interface type, and/or keyword.

    All provided filters are ANDed together.

    Args:
        category: Exact category match, e.g. "sensor", "mcu", "power_ic",
            "analog", "logic", "memory", "wireless", "passive", "connector".
        interface_type: Exact interface protocol match, e.g. "I2C", "SPI",
            "UART", "USB", "CAN".
        keyword: Case-insensitive substring matched against mpn and manufacturer.

    Returns a summary list (mpn, manufacturer, category, package).
    """
    kw_lower = keyword.lower() if keyword else None
    results = []

    for comp in _iter_components():
        if category and comp.get("category") != category:
            continue
        if interface_type:
            ifaces = comp.get("interfaces", [])
            if not any(iface.get("type") == interface_type for iface in ifaces):
                continue
        if kw_lower:
            searchable = " ".join(filter(None, [
                comp.get("mpn", ""),
                comp.get("manufacturer", ""),
            ])).lower()
            if kw_lower not in searchable:
                continue
        results.append({
            "mpn":          comp.get("mpn", ""),
            "manufacturer": comp.get("manufacturer", ""),
            "category":     comp.get("category", ""),
            "package":      comp.get("package", ""),
        })

    return results


@mcp.tool()
def resolve_circuit(circuit_path: str) -> dict:
    """Resolve a circuit YAML file into a netlist and Bill of Materials.

    Args:
        circuit_path: Absolute or project-relative path to the circuit YAML.

    Returns a dict with:
        - name: circuit name
        - bom: list of {ref, mpn, type, value} dicts
        - netlist: dict mapping net name to list of "REF.PIN" strings
        - power_nets: sorted list of net names classified as power rails
        - error: (only present on failure) error message string
    """
    try:
        path = Path(circuit_path)
        if not path.is_absolute():
            path = (_project_dir / path).resolve()

        shared = _load_shared_context(_project_dir)
        resolved = resolve(path, _lib_paths, shared=shared)

        bom = []
        for part in resolved["parts"]:
            if part.get("comp_def") is not None:
                bom.append({"ref": part["ref"], "mpn": part.get("mpn", ""), "type": "ic"})
            else:
                bom.append({"ref": part["ref"], "type": part.get("type", "passive"), "value": part.get("value", "")})

        netlist = {
            net: [f"{ref}.{pin}" for ref, pin in conns]
            for net, conns in resolved["netlist"].items()
        }

        return {
            "name":       resolved["name"],
            "bom":        bom,
            "netlist":    netlist,
            "power_nets": sorted(resolved["power_nets"]),
        }

    except GenerateError as e:
        return {"error": str(e)}
    except FileNotFoundError as e:
        return {"error": f"File not found: {e}"}
    except Exception as e:
        return {"error": f"Unexpected error: {e}"}


@mcp.tool()
def validate_circuit(circuit_path: str) -> dict:
    """Validate a circuit YAML file against the SilicAI circuit schema.

    Args:
        circuit_path: Absolute or project-relative path to the circuit YAML.

    Returns a dict with:
        - valid: bool
        - errors: list of {message, path} dicts (empty when valid)
        - error: (only present on unexpected failure) error message string
    """
    try:
        path = Path(circuit_path)
        if not path.is_absolute():
            path = (_project_dir / path).resolve()

        with open(path) as f:
            doc = yaml.safe_load(f)

        schema_uri = doc.get("$schema", "")
        try:
            schema_ref = resolve_schema(schema_uri)
        except ValueError as e:
            return {"valid": False, "errors": [{"message": str(e), "path": "root"}]}

        schema = json.loads(schema_ref.read_text())
        validator = Draft202012Validator(schema, registry=_get_registry())

        errors = [
            {"message": err.message, "path": ".".join(str(p) for p in err.path) or "root"}
            for err in validator.iter_errors(doc)
        ]
        return {"valid": len(errors) == 0, "errors": errors}

    except FileNotFoundError as e:
        return {"error": f"File not found: {e}", "valid": False, "errors": []}
    except Exception as e:
        return {"error": f"Unexpected error: {e}", "valid": False, "errors": []}


@mcp.tool()
def get_component_template() -> str:
    """Return an annotated YAML template for creating a new SilicAI component.

    Use this as the target format when extracting component data from a datasheet PDF.
    Fields marked REQUIRED must be present. All others are optional but recommended
    when the information is available in the datasheet.

    Returns the template as a YAML string with inline comments.
    """
    return """\
$schema: "https://github.com/mageoch/silicai/schema/component.schema.json"
$schema_version: "0.1.0"

component:
  # ── Identity (REQUIRED) ────────────────────────────────────────────────────
  mpn: "PARTNUMBER"              # REQUIRED — Manufacturer Part Number exactly as on the datasheet
  manufacturer: "Manufacturer"   # REQUIRED — Full manufacturer name
  category: sensor               # REQUIRED — one of: mcu mpu power_ic power_discrete analog
                                 #   logic memory wireless sensor passive connector discrete
  package: "SOIC-8"              # REQUIRED — package code as printed on the datasheet
  datasheet: "https://..."       # URL to the official datasheet PDF
  kicad_symbol: "Library:Symbol" # KiCad symbol reference (Library:SymbolName)
  description: >                 # One-sentence description of purpose
    Short description here.

  # ── Absolute Maximum Ratings ───────────────────────────────────────────────
  absolute_maximum_ratings:
    - parameter: supply_voltage
      pins: ["VCC"]
      unit: V
      max: 6.0
    - parameter: io_voltage
      pins: [SDA, SCL]
      unit: V
      max: 5.5

  # ── Recommended Operating Conditions ──────────────────────────────────────
  recommended_operating_conditions:
    - parameter: supply_voltage
      pins: ["VCC"]
      unit: V
      min: 1.7
      nom: 3.3
      max: 5.5

  # ── Temperature Range ──────────────────────────────────────────────────────
  temperature_range:
    operating:
      min: { value: -40, unit: "°C" }
      max: { value: 85,  unit: "°C" }
    storage:
      min: { value: -55, unit: "°C" }
      max: { value: 150, unit: "°C" }
    junction_max:          { value: 125, unit: "°C" }
    thermal_resistance_ja: { value: 200, unit: "°C/W" }

  # ── Power Consumption ──────────────────────────────────────────────────────
  power_consumption:
    - mode: active              # short identifier, no spaces
      description: "Normal operation"
      rail: vcc                 # references a rail id defined below
      current: { value: 1.5, unit: mA }
      conditions: "VCC=3.3V, 25°C"
    - mode: shutdown
      description: "Shutdown mode"
      rail: vcc
      current: { value: 1, unit: µA }

  # ── Power Rails ────────────────────────────────────────────────────────────
  rails:
    - id: vcc                   # id used by pin.rail and power_consumption.rail
      net: VCC                  # schematic net name (overridable per-instance in circuit YAML)
      per_pin_decoupling:
        - type: capacitor
          capacitance: { value: 100, unit: nF }
          voltage_rating: { value: 10, unit: V }
          dielectric: [X5R, X7R]
          placement: close

  # ── Pins (REQUIRED — list every pin) ──────────────────────────────────────
  pins:
    - number: 1                 # pin number (integer or string for e.g. "A1" BGA)
      name: VCC                 # pin name from datasheet
      direction: power_in       # input output bidirectional tri_state passive
                                # open_collector open_emitter power_in power_out no_connect
      rail: vcc                 # references rail id above (for power pins)

    - number: 2
      name: GND
      direction: power_in
      net: GND                  # explicit net name (use for GND instead of a rail)

    - number: 3
      name: SCL
      direction: input
      open_drain: true
      primary_function:
        type: i2c_scl           # gpio reset boot_select clock_in clock_out power
                                # analog_in analog_out i2c_scl i2c_sda spi_sck spi_mosi
                                # spi_miso spi_cs uart_tx uart_rx usb_dp usb_dm
                                # swd_io swd_clk adc alert address_select enable interrupt pwm
      required_external:
        - type: resistor
          resistance: { value: 4.7, unit: kΩ }
          to: VCC               # net name the other end connects to
          scope: bus            # bus: one shared pull-up per bus; component: one per IC

    - number: 4
      name: SDA
      direction: bidirectional
      open_drain: true
      primary_function:
        type: i2c_sda
      required_external:
        - type: resistor
          resistance: { value: 4.7, unit: kΩ }
          to: VCC
          scope: bus

    - number: 5
      name: ADDR
      direction: input
      must_connect: true        # pin must not be left floating
      primary_function:
        type: address_select
        options:
          - connect_to: GND
            i2c_address: "0x48"
          - connect_to: VCC
            i2c_address: "0x49"

    - number: 6
      name: ALERT
      direction: output
      open_drain: true
      primary_function:
        type: alert
        polarity: active_low
      required_external:
        - type: resistor
          resistance: { value: 10, unit: kΩ }
          to: VCC
          scope: component

  # ── Interfaces ─────────────────────────────────────────────────────────────
  interfaces:
    - type: I2C                 # I2C SPI UART USB CAN Ethernet SDIO I2S SAI
                                # ADC DAC JTAG SWD SMBus 1-Wire
      instance: 1
      speed_max: { value: 400, unit: kHz }
      pins:
        scl: SCL                # role: pin_name  (roles depend on protocol)
        sda: SDA                # for SPI: clk mosi miso cs
                                # for UART: tx rx
                                # for USB: dp dm
"""


@mcp.tool()
def validate_component(component_path: str) -> dict:
    """Validate a component YAML file against the SilicAI component schema.

    Args:
        component_path: Absolute or project-relative path to the component YAML.

    Returns a dict with:
        - valid: bool
        - errors: list of {message, path} dicts (empty when valid)
        - error: (only present on unexpected failure) error message string
    """
    try:
        path = Path(component_path)
        if not path.is_absolute():
            path = (_project_dir / path).resolve()

        with open(path) as f:
            doc = yaml.safe_load(f)

        schema_uri = doc.get("$schema", "")
        try:
            schema_ref = resolve_schema(schema_uri)
        except ValueError as e:
            return {"valid": False, "errors": [{"message": str(e), "path": "root"}]}

        schema = json.loads(schema_ref.read_text())
        validator = Draft202012Validator(schema, registry=_get_registry())

        errors = [
            {"message": err.message, "path": ".".join(str(p) for p in err.path) or "root"}
            for err in validator.iter_errors(doc)
        ]
        return {"valid": len(errors) == 0, "errors": errors}

    except FileNotFoundError as e:
        return {"error": f"File not found: {e}", "valid": False, "errors": []}
    except Exception as e:
        return {"error": f"Unexpected error: {e}", "valid": False, "errors": []}


@mcp.tool()
def save_component(content: str, output_path: str | None = None) -> dict:
    """Save a component YAML to the component library.

    The component is validated before saving. The file is written to the first
    configured component library path under {category}/{manufacturer}/{mpn}.yaml,
    unless output_path is provided.

    Args:
        content: Full YAML content of the component file (including $schema header).
        output_path: Absolute or project-relative path for the output file.
            If omitted, derived automatically from mpn, category, and manufacturer.

    Returns a dict with:
        - saved: path where the file was written
        - valid: bool — whether the component passed schema validation
        - errors: list of {message, path} validation error dicts
        - error: (only present on unexpected failure) error message string
    """
    try:
        doc = yaml.safe_load(content)
        comp = doc.get("component", {})
        mpn = comp.get("mpn", "unknown")
        category = comp.get("category", "misc")
        manufacturer = comp.get("manufacturer", "unknown")

        if output_path:
            dest = Path(output_path)
            if not dest.is_absolute():
                dest = (_project_dir / dest).resolve()
        else:
            if not _lib_paths:
                return {"error": "No component library paths configured in pyproject.toml"}
            mfr_slug = manufacturer.lower().replace(" ", "_").replace(".", "").replace(",", "")
            dest = _lib_paths[0] / category / mfr_slug / f"{mpn}.yaml"

        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)

        # Validate after saving
        schema_uri = doc.get("$schema", "")
        try:
            schema_ref = resolve_schema(schema_uri)
            schema = json.loads(schema_ref.read_text())
            errors = [
                {"message": err.message, "path": ".".join(str(p) for p in err.path) or "root"}
                for err in Draft202012Validator(schema, registry=_get_registry()).iter_errors(doc)
            ]
        except ValueError as e:
            errors = [{"message": str(e), "path": "root"}]

        return {"saved": str(dest), "valid": len(errors) == 0, "errors": errors}

    except Exception as e:
        return {"error": f"Unexpected error: {e}"}


@mcp.tool()
def generate_kicad(path: str, output_path: str | None = None) -> dict:
    """Generate KiCad output from a project YAML or a single circuit YAML.

    When given a project YAML, generates a full KiCad project:
      - {slug}.kicad_pro
      - {slug}.kicad_sch  (root sheet with hierarchical sub-sheet symbols)
      - {stem}.kicad_sch  (one sub-sheet per circuit)

    When given a circuit YAML, generates a single .kicad_sch file.

    Args:
        path: Absolute or project-relative path to a project YAML or circuit YAML.
        output_path: Output file path (circuit mode) or output directory (project mode).
            Defaults to same directory as the input file.

    Returns a dict with:
        - outputs: list of generated file paths
        - error: (only present on failure) error message string
    """
    try:
        src = Path(path)
        if not src.is_absolute():
            src = (_project_dir / src).resolve()

        config = load_config(_project_dir)
        kicad_lib_path = Path(config.get("kicad_library_path", str(_DEFAULT_KICAD_SYM)))

        doc = __import__("yaml").safe_load(src.read_text())

        if "project" in doc:
            # Project mode: generate full KiCad project
            out_dir = Path(output_path) if output_path else src.parent
            if not out_dir.is_absolute():
                out_dir = (_project_dir / out_dir).resolve()
            outputs = write_kicad_project(src, _lib_paths, out_dir, kicad_lib_path)
            return {"outputs": [str(p) for p in outputs]}

        else:
            # Circuit mode: generate single .kicad_sch
            if output_path:
                out = Path(output_path)
                if not out.is_absolute():
                    out = (_project_dir / out).resolve()
            else:
                out = src.with_suffix(".kicad_sch")
            resolved = resolve(src, _lib_paths)
            write_kicad_sch(resolved, out, kicad_lib_path)
            return {"outputs": [str(out)]}

    except GenerateError as e:
        return {"error": str(e)}
    except FileNotFoundError as e:
        return {"error": f"File not found: {e}"}
    except Exception as e:
        return {"error": f"Unexpected error: {e}"}


@mcp.tool()
def import_kicad(path: str, output_path: str | None = None) -> dict:
    """Import a KiCad schematic or project into SilicAI YAML.

    Converts a .kicad_pro or .kicad_sch file into a SilicAI project.yaml and
    circuits/*.yaml files. Components are matched against the configured library
    by their kicad_symbol field; unrecognised symbols use the schematic Value
    property as the MPN with a warning.

    Args:
        path: Absolute or project-relative path to a .kicad_pro or .kicad_sch file.
        output_path: Output directory. Defaults to the same directory as the input.

    Returns a dict with:
        - outputs: list of written file paths
        - warnings: list of warning strings (unrecognised symbols, missing sub-sheets)
        - error: (only present on failure) error message string
    """
    try:
        src = Path(path)
        if not src.is_absolute():
            src = (_project_dir / src).resolve()
        out_dir = Path(output_path) if output_path else src.parent
        if not out_dir.is_absolute():
            out_dir = (_project_dir / out_dir).resolve()

        written, warnings = import_project(src, _lib_paths, out_dir)
        return {"outputs": [str(p) for p in written], "warnings": warnings}

    except KiCadImportError as e:
        return {"error": str(e)}
    except FileNotFoundError as e:
        return {"error": f"File not found: {e}"}
    except Exception as e:
        return {"error": f"Unexpected error: {e}"}


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="SilicAI MCP server — exposes circuit design tools to Claude Code"
    )
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=None,
        help="Path to the project containing pyproject.toml (default: CWD)",
    )
    args = parser.parse_args()

    global _project_dir, _lib_paths
    _project_dir = (args.project_dir or Path.cwd()).resolve()

    config = load_config(_project_dir)
    _lib_paths = [_BUILTIN_COMPONENTS] + [
        (_project_dir / entry["path"]).resolve()
        for entry in config.get("component_libraries", [])
    ]

    mcp.run()


if __name__ == "__main__":
    main()
