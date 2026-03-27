#!/usr/bin/env python3
"""Import KiCad schematics into SilicAI project YAML."""

import sys
import math
import re
import argparse
from pathlib import Path

import yaml
from kiutils.schematic import Schematic

from silicai.generate import load_config


class KiCadImportError(Exception):
    pass


# ── Union-Find ────────────────────────────────────────────────────────────────

class _UF:
    """Path-compressing union-find keyed on arbitrary hashable values."""

    def __init__(self):
        self._p: dict = {}

    def _make(self, k):
        if k not in self._p:
            self._p[k] = k

    def find(self, k):
        self._make(k)
        if self._p[k] != k:
            self._p[k] = self.find(self._p[k])
        return self._p[k]

    def union(self, a, b):
        self._make(a)
        self._make(b)
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._p[rb] = ra


# ── Net connectivity graph ────────────────────────────────────────────────────

class NetGraph:
    """Wire + label connectivity graph with net-name lookup by coordinate."""

    _SNAP = 2  # decimal places → 0.01 mm precision

    def __init__(self):
        self._uf = _UF()
        self._names: dict[tuple, str] = {}  # root_key → net_name (first registered wins)

    @classmethod
    def _key(cls, x: float, y: float) -> tuple[float, float]:
        return (round(x, cls._SNAP), round(y, cls._SNAP))

    def add_wire(self, p0, p1) -> None:
        self._uf.union(self._key(p0.X, p0.Y), self._key(p1.X, p1.Y))

    def add_label(self, x: float, y: float, name: str) -> None:
        root = self._uf.find(self._key(x, y))
        if root not in self._names:
            self._names[root] = name

    def net_at(self, x: float, y: float) -> str | None:
        root = self._uf.find(self._key(x, y))
        return self._names.get(root)


def build_net_graph(sch: Schematic) -> NetGraph:
    """Build a NetGraph from all wires, power symbols, and labels in a schematic.

    Population order (first registration wins, so higher-priority sources go first):
      1. Wires — union endpoints (no names yet)
      2. Power symbols — register net name at symbol position (single-pin power syms)
      3. GlobalLabels
      4. LocalLabels (lowest priority)
    """
    ng = NetGraph()

    # 1. Wires
    for item in sch.graphicalItems:
        if getattr(item, "type", None) == "wire" and len(item.points) >= 2:
            ng.add_wire(item.points[0], item.points[1])

    # 2. Power symbols (inBom=False, onBoard=False)
    for sym in sch.schematicSymbols:
        if not (sym.inBom is False and sym.onBoard is False):
            continue
        net = next((p.value for p in sym.properties if p.key == "Value"), None)
        if net:
            ng.add_label(sym.position.X, sym.position.Y, net)

    # 3. GlobalLabels
    for lbl in sch.globalLabels:
        ng.add_label(lbl.position.X, lbl.position.Y, lbl.text)

    # 4. LocalLabels
    for lbl in sch.labels:
        ng.add_label(lbl.position.X, lbl.position.Y, lbl.text)

    return ng


# ── Library symbol helpers ────────────────────────────────────────────────────

def get_lib_pins(lib_sym) -> list:
    """Collect all SymbolPin objects from a symbol and its sub-units."""
    pins = list(lib_sym.pins)
    for u in lib_sym.units:
        pins.extend(u.pins)
    return pins


def build_kicad_sym_index(lib_paths: list[Path]) -> dict[str, dict]:
    """Return {kicad_symbol: component_dict} from all library YAML files."""
    index: dict[str, dict] = {}
    for lib in lib_paths:
        for f in lib.rglob("*.yaml"):
            try:
                doc = yaml.safe_load(f.read_text())
            except Exception:
                continue
            if not isinstance(doc, dict):
                continue
            comp = doc.get("component")
            if not comp:
                continue
            ks = comp.get("kicad_symbol")
            if ks and ks not in index:
                index[ks] = comp
    return index


# ── Symbol classification ─────────────────────────────────────────────────────

_PASSIVE_LIBIDS = frozenset({
    "Device:R", "Device:R_Small", "Device:R_US",
    "Device:C", "Device:C_Small", "Device:C_Polarized",
    "Device:L", "Device:L_Small",
    "Device:Ferrite_Bead",
})


def is_power_symbol(sym) -> bool:
    """True if this is a KiCad power rail symbol (not a real component)."""
    return sym.inBom is False and sym.onBoard is False


# ── Coordinate transform ──────────────────────────────────────────────────────

def pin_connection_point(
    sym_x: float, sym_y: float,
    sym_angle: float, sym_mirror: str | None,
    pin_x: float, pin_y: float,
) -> tuple[float, float]:
    """Transform a library-frame pin position to schematic world coordinates.

    KiCad library uses Y-up; schematic uses Y-down.

    Steps:
      1. Apply mirror (in lib frame, before rotation)
      2. Rotate by sym_angle
      3. Translate + flip Y (lib Y-up → schematic Y-down)
    """
    if sym_mirror == "x":
        pin_y = -pin_y
    elif sym_mirror == "y":
        pin_x = -pin_x

    angle_rad = math.radians(sym_angle)
    rot_x = pin_x * math.cos(angle_rad) - pin_y * math.sin(angle_rad)
    rot_y = pin_x * math.sin(angle_rad) + pin_y * math.cos(angle_rad)

    return sym_x + rot_x, sym_y - rot_y


def get_pin_nets(sym, lib_sym, net_graph: NetGraph) -> dict[str, str]:
    """Return {pin_name: net_name} for all connected pins of a placed symbol."""
    sym_x = sym.position.X
    sym_y = sym.position.Y
    sym_angle = getattr(sym.position, "angle", 0) or 0
    sym_mirror = getattr(sym, "mirror", None)

    result: dict[str, str] = {}
    for p in get_lib_pins(lib_sym):
        wx, wy = pin_connection_point(
            sym_x, sym_y, sym_angle, sym_mirror,
            p.position.X, p.position.Y,
        )
        net = net_graph.net_at(wx, wy)
        if net is not None:
            result[p.name] = net
    return result


# ── Resistance parsing ────────────────────────────────────────────────────────

def parse_resistance(value_str: str) -> dict | None:
    """Parse a KiCad resistor Value string to {value, unit}.

    Handles: '4k7', '4.7k', '10k', '100R', '4.7kΩ', '1M', '100' (Ω implied).
    Returns None if the string cannot be parsed.
    """
    s = value_str.strip()

    # '4k7' → '4.7k'
    m = re.match(r"^(\d+)([kKmM])(\d+)$", s)
    if m:
        s = f"{m.group(1)}.{m.group(3)}{m.group(2)}"

    m = re.match(r"^(\d+(?:[.,]\d+)?)\s*([kKmMrRΩ]|k[Ω]|K[Ω]|[Oo]hm|[Mm][Oo]hm)?$", s)
    if not m:
        return None

    num = float(m.group(1).replace(",", "."))
    suffix = (m.group(2) or "").upper()

    if suffix.startswith("K"):
        return {"value": num, "unit": "kΩ"}
    if suffix.startswith("M"):
        return {"value": num, "unit": "MΩ"}
    return {"value": num, "unit": "Ω"}


# ── Bus signal detection ──────────────────────────────────────────────────────

# Maps net-name suffix → (bus_type, canonical_role)
_SUFFIX_MAP: dict[str, tuple[str, str]] = {
    "SCL": ("I2C", "scl"),   "SDA": ("I2C", "sda"),
    "MOSI": ("SPI", "mosi"), "MISO": ("SPI", "miso"),
    "SCK": ("SPI", "clk"),   "CLK": ("SPI", "clk"),  "CS": ("SPI", "cs"),
    "TX": ("UART", "tx"),    "RX": ("UART", "rx"),
    "DP": ("USB", "dp"),     "DM": ("USB", "dm"),
    "CANH": ("CAN", "tx"),   "CANL": ("CAN", "rx"),
}


def detect_bus_signal(net_name: str) -> tuple[str, str, str] | None:
    """Detect bus type/role from net name suffix (e.g. 'i2c_main_SCL').

    Returns (bus_id, bus_type, role) or None.
    """
    upper = net_name.upper()
    for suffix, (bus_type, role) in _SUFFIX_MAP.items():
        if upper.endswith("_" + suffix):
            bus_id = net_name[:-(len(suffix) + 1)].rstrip("_")
            return (bus_id or bus_type.lower()), bus_type, role
    return None


# ── Rail / address inference ──────────────────────────────────────────────────

def _rail_net_map(comp: dict, pin_nets: dict[str, str]) -> dict[str, str]:
    """Build {rail_id: actual_net} for a component instance."""
    result: dict[str, str] = {}
    for rail in comp.get("rails", []):
        rid = rail["id"]
        for pin in comp.get("pins", []):
            if pin.get("rail") == rid:
                actual = pin_nets.get(pin["name"])
                if actual:
                    result[rid] = actual
                    break
        if rid not in result:
            result[rid] = rail.get("net", "")
    return result


def _resolve_connect_to(
    connect_to: str,
    comp: dict,
    pin_nets: dict[str, str],
    rnm: dict[str, str],
) -> str | None:
    """Resolve an address_select option's connect_to field to an actual net name."""
    if connect_to in ("GND", "0V"):
        return "GND"
    if connect_to in rnm:
        return rnm[connect_to]
    if connect_to in ("V+", "VCC", "VDD", "VDDIO"):
        # Return the first non-GND rail net
        for rail in comp.get("rails", []):
            if rail.get("net", "").upper() not in ("GND", "0V"):
                return rnm.get(rail["id"])
    return pin_nets.get(connect_to)


def infer_i2c_address(
    comp: dict, pin_nets: dict[str, str], rnm: dict[str, str]
) -> str | None:
    """Infer I2C address from address-select pin connectivity."""
    for pin in comp.get("pins", []):
        fn = pin.get("primary_function", {})
        if fn.get("type") != "address_select":
            continue
        actual = pin_nets.get(pin["name"])
        if actual is None:
            continue
        for option in fn.get("options", []):
            resolved = _resolve_connect_to(
                option.get("connect_to", ""), comp, pin_nets, rnm
            )
            if resolved == actual:
                return option.get("i2c_address")
    return None


# ── Pull-up detection ─────────────────────────────────────────────────────────

def infer_pull_ups(
    sch: Schematic,
    net_graph: NetGraph,
    power_nets: set[str],
    lib_sym_by_id: dict,
) -> dict:
    """Scan Device:R symbols to find bus pull-ups.

    Returns {bus_id: {"type": str, "pull_ups": {role: {resistance, net}}}}.
    """
    result: dict = {}

    for sym in sch.schematicSymbols:
        if is_power_symbol(sym) or not sym.libId.startswith("Device:R"):
            continue

        value_str = next((p.value for p in sym.properties if p.key == "Value"), "")
        resistance = parse_resistance(value_str)
        if resistance is None:
            continue

        lib_sym = lib_sym_by_id.get(sym.libId)
        if lib_sym is None:
            continue

        pin_nets = get_pin_nets(sym, lib_sym, net_graph)
        nets = list(pin_nets.values())
        if len(nets) < 2:
            continue

        power_side = next((n for n in nets if n in power_nets), None)
        signal_side = next((n for n in nets if n and n not in power_nets), None)
        if power_side is None or signal_side is None:
            continue

        detected = detect_bus_signal(signal_side)
        if detected is None:
            continue
        bus_id, bus_type, role = detected

        entry = result.setdefault(bus_id, {"type": bus_type, "pull_ups": {}})
        entry["pull_ups"][role] = {"resistance": resistance, "net": power_side}

    return result


# ── Instance builder ──────────────────────────────────────────────────────────

def build_instance(
    sym,
    comp: dict | None,
    pin_nets: dict[str, str],
    power_nets: set[str],
    pull_ups_by_bus: dict,
) -> dict:
    """Build a SilicAI circuit instance dict from a placed schematic symbol."""
    ref = next((p.value for p in sym.properties if p.key == "Reference"), "?")
    value = next((p.value for p in sym.properties if p.key == "Value"), "?")
    mpn = comp["mpn"] if comp else value

    instance: dict = {"ref": ref, "mpn": mpn}
    if comp is None:
        return instance

    rnm = _rail_net_map(comp, pin_nets)

    # ── Rail overrides ─────────────────────────────────────────────────────
    rails_out: dict[str, str] = {}
    for rail in comp.get("rails", []):
        rid = rail["id"]
        actual = rnm.get(rid, "")
        if actual and actual != rail.get("net", ""):
            rails_out[rid] = actual
    if rails_out:
        instance["rails"] = rails_out

    # ── Bus connections ────────────────────────────────────────────────────
    cat = comp.get("category", "")
    is_master = cat in ("mcu", "mpu")
    buses_out: list[dict] = []

    # Strategy A: use declared interfaces (fixed-pin components like TMP117)
    for iface in comp.get("interfaces", []):
        iface_type = iface["type"]
        iface_pins: dict[str, str] = iface.get("pins", {})  # role → pin_name

        # Gather nets for each interface role via the pin mapping
        role_nets: dict[str, str] = {}
        for role_key, pin_name in iface_pins.items():
            net = pin_nets.get(pin_name)
            if net and net not in power_nets:
                role_nets[role_key] = net

        if not role_nets:
            continue

        # Derive bus_id from net-name pattern
        bus_ids: list[str] = []
        for net in role_nets.values():
            d = detect_bus_signal(net)
            if d:
                bus_ids.append(d[0])
        if not bus_ids:
            continue
        bus_id = sorted(set(bus_ids))[0]

        role = "master" if is_master else "slave"
        bus_conn: dict = {"id": bus_id, "interface": iface_type, "role": role}

        # For MCUs/MPUs, include explicit pin mapping (GPIO pins can vary)
        if is_master:
            bus_conn["pins"] = {
                rk: iface_pins[rk] for rk in iface_pins if rk in role_nets
            }

        # I2C slave address
        if iface_type in ("I2C", "SMBus") and not is_master:
            addr = infer_i2c_address(comp, pin_nets, rnm)
            if addr:
                bus_conn["address"] = addr

        buses_out.append(bus_conn)

    # Strategy B: fallback — detect bus from net-name patterns
    if not buses_out:
        bus_groups: dict[str, dict] = {}
        for pin_name, net in pin_nets.items():
            if net in power_nets:
                continue
            detected = detect_bus_signal(net)
            if not detected:
                continue
            bus_id, bus_type, role_key = detected
            g = bus_groups.setdefault(bus_id, {"type": bus_type, "pins": {}})
            g["pins"][role_key] = pin_name

        for bus_id, g in bus_groups.items():
            role = "master" if is_master else "slave"
            bus_conn = {"id": bus_id, "interface": g["type"], "role": role}
            if g["pins"]:
                bus_conn["pins"] = g["pins"]
            buses_out.append(bus_conn)

    if buses_out:
        instance["buses"] = buses_out

    return instance


# ── Sheet importer ────────────────────────────────────────────────────────────

def import_sheet(
    sch_path: Path,
    lib_paths: list[Path],
    circuit_name: str | None = None,
) -> dict:
    """Import a single .kicad_sch file into a circuit dict.

    Returns {'name', 'instances', 'pull_ups_by_bus', 'power_nets', 'warnings'}.
    """
    sch = Schematic.from_file(str(sch_path))

    lib_sym_by_id: dict = {ls.libId: ls for ls in sch.libSymbols}
    kicad_idx = build_kicad_sym_index(lib_paths)
    net_graph = build_net_graph(sch)

    # Collect power net names from power symbols
    power_nets: set[str] = set()
    for sym in sch.schematicSymbols:
        if is_power_symbol(sym):
            net = next((p.value for p in sym.properties if p.key == "Value"), None)
            if net:
                power_nets.add(net)

    pull_ups_by_bus = infer_pull_ups(sch, net_graph, power_nets, lib_sym_by_id)

    instances: list[dict] = []
    warnings: list[str] = []

    for sym in sch.schematicSymbols:
        if is_power_symbol(sym):
            continue
        if sym.libId in _PASSIVE_LIBIDS:
            continue  # skipped — SilicAI regenerates passives from component defs

        lib_sym = lib_sym_by_id.get(sym.libId)
        if lib_sym is None:
            ref = next((p.value for p in sym.properties if p.key == "Reference"), "?")
            val = next((p.value for p in sym.properties if p.key == "Value"), "?")
            warnings.append(
                f"Unknown symbol lib_id={sym.libId!r} for {ref} — using Value {val!r} as MPN"
            )
            instances.append({"ref": ref, "mpn": val})
            continue

        comp = kicad_idx.get(sym.libId)
        pin_nets = get_pin_nets(sym, lib_sym, net_graph)

        if comp is None:
            ref = next((p.value for p in sym.properties if p.key == "Reference"), "?")
            val = next((p.value for p in sym.properties if p.key == "Value"), "?")
            warnings.append(
                f"Component {sym.libId!r} not found in library for {ref} — using Value {val!r} as MPN"
            )
            instances.append({"ref": ref, "mpn": val})
            continue

        instances.append(
            build_instance(sym, comp, pin_nets, power_nets, pull_ups_by_bus)
        )

    return {
        "name": circuit_name or sch_path.stem,
        "instances": instances,
        "pull_ups_by_bus": pull_ups_by_bus,
        "power_nets": power_nets,
        "warnings": warnings,
    }


# ── YAML serialisation ────────────────────────────────────────────────────────

_CIRCUIT_SCHEMA = "https://mageoch.github.io/silicai/schema/circuit.schema.json"
_PROJECT_SCHEMA = "https://mageoch.github.io/silicai/schema/project.schema.json"
_SCHEMA_VERSION = "0.1.0"


def _write_circuit_yaml(name: str, instances: list[dict], out_path: Path) -> None:
    doc = {
        "$schema": _CIRCUIT_SCHEMA,
        "$schema_version": _SCHEMA_VERSION,
        "circuit": {
            "name": name,
            "instances": instances,
        },
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        yaml.dump(doc, f, allow_unicode=True, sort_keys=False, default_flow_style=False)


def _write_project_yaml(
    name: str,
    circuit_paths: list[str],
    shared_buses: list[dict],
    shared_power_rails: list[dict],
    out_path: Path,
) -> None:
    shared: dict = {}
    if shared_power_rails:
        shared["power_rails"] = shared_power_rails
    if shared_buses:
        shared["buses"] = shared_buses

    proj: dict = {"name": name, "circuits": circuit_paths}
    if shared:
        proj["shared"] = shared

    doc = {
        "$schema": _PROJECT_SCHEMA,
        "$schema_version": _SCHEMA_VERSION,
        "project": proj,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        yaml.dump(doc, f, allow_unicode=True, sort_keys=False, default_flow_style=False)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


# ── Project importer ──────────────────────────────────────────────────────────

def import_project(
    input_path: Path,
    lib_paths: list[Path],
    output_dir: Path,
) -> tuple[list[Path], list[str]]:
    """Import a KiCad .kicad_pro or .kicad_sch into SilicAI YAML.

    Returns (list_of_written_paths, list_of_warning_strings).
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Resolve root schematic
    if input_path.suffix == ".kicad_pro":
        root_sch = input_path.with_suffix(".kicad_sch")
        if not root_sch.exists():
            raise KiCadImportError(f"No matching .kicad_sch found next to {input_path}")
        project_name = input_path.stem
    else:
        root_sch = input_path
        project_name = input_path.stem

    root = Schematic.from_file(str(root_sch))
    sub_sheets = root.sheets  # HierarchicalSheet objects

    circuits_dir = output_dir / "circuits"
    circuit_rel_paths: list[str] = []
    all_warnings: list[str] = []

    # Aggregated across all circuits
    all_power_nets: set[str] = set()
    all_pull_ups: dict[str, dict] = {}  # bus_id → {type, pull_ups}

    circuits_data: list[dict] = []

    if sub_sheets:
        for sheet in sub_sheets:
            sheet_file = getattr(sheet.fileName, "value", str(sheet.fileName))
            sheet_name = getattr(sheet.sheetName, "value", str(sheet.sheetName))
            sch_path = (root_sch.parent / sheet_file).resolve()

            if not sch_path.exists():
                all_warnings.append(f"Sub-sheet not found: {sch_path}")
                continue

            data = import_sheet(sch_path, lib_paths, circuit_name=sheet_name)
            all_warnings.extend(data["warnings"])
            all_power_nets.update(data["power_nets"])
            for bid, bdata in data["pull_ups_by_bus"].items():
                all_pull_ups.setdefault(bid, bdata)

            out_name = _slug(sheet_name) + ".yaml"
            out_path = circuits_dir / out_name
            _write_circuit_yaml(data["name"], data["instances"], out_path)
            circuit_rel_paths.append(f"circuits/{out_name}")
            circuits_data.append(data)
    else:
        # Single-sheet: import root schematic
        data = import_sheet(root_sch, lib_paths)
        all_warnings.extend(data["warnings"])
        all_power_nets.update(data["power_nets"])
        all_pull_ups.update(data["pull_ups_by_bus"])

        out_name = _slug(project_name) + ".yaml"
        out_path = circuits_dir / out_name
        _write_circuit_yaml(data["name"], data["instances"], out_path)
        circuit_rel_paths.append(f"circuits/{out_name}")
        circuits_data.append(data)

    written: list[Path] = list(circuits_dir.glob("*.yaml"))

    # Shared power rails (GND first, then alphabetical)
    power_rails: list[dict] = []
    if "GND" in all_power_nets:
        power_rails.append({"net": "GND"})
    power_rails += [{"net": n} for n in sorted(all_power_nets) if n != "GND"]

    # Shared buses (those with pull-ups take priority)
    shared_buses: list[dict] = []
    for bid, bdata in all_pull_ups.items():
        entry: dict = {"id": bid, "type": bdata["type"]}
        if bdata.get("pull_ups"):
            entry["pull_ups"] = bdata["pull_ups"]
        shared_buses.append(entry)

    proj_path = output_dir / "project.yaml"
    _write_project_yaml(
        name=project_name.replace("_", " ").title(),
        circuit_paths=circuit_rel_paths,
        shared_buses=shared_buses,
        shared_power_rails=power_rails,
        out_path=proj_path,
    )
    written.insert(0, proj_path)

    return written, all_warnings


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import KiCad schematics into SilicAI project YAML"
    )
    parser.add_argument("input", type=Path, help=".kicad_pro or .kicad_sch file")
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Output directory (default: same directory as input)",
    )
    parser.add_argument(
        "--project-dir", type=Path, default=None,
        help="Project directory containing pyproject.toml",
    )
    args = parser.parse_args()

    project_dir = (args.project_dir or Path.cwd()).resolve()
    output_dir = (args.output or args.input.parent).resolve()

    try:
        config = load_config(project_dir)
    except FileNotFoundError:
        config = {}

    lib_paths = [
        (project_dir / entry["path"]).resolve()
        for entry in config.get("component_libraries", [])
    ]

    try:
        written, warnings = import_project(args.input.resolve(), lib_paths, output_dir)
    except KiCadImportError as e:
        print(f"✗ {e}", file=sys.stderr)
        sys.exit(1)

    for w in warnings:
        print(f"⚠ {w}", file=sys.stderr)
    for p in written:
        print(f"✓ {p}")


if __name__ == "__main__":
    main()
