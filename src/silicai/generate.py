#!/usr/bin/env python3
"""Generate KiCad schematics from SilicAI circuit definitions."""

import sys
import copy
import uuid
import tomllib
import argparse
from pathlib import Path

import yaml
from kiutils.schematic import Schematic
from kiutils.symbol import SymbolLib
from kiutils.items.schitems import SchematicSymbol, GlobalLabel
from kiutils.items.common import Position, Effects, Font, Justify

# ── Defaults ──────────────────────────────────────────────────────────────────
_DEFAULT_KICAD_SYM = Path("/usr/share/kicad/symbols")

_PASSIVE_SYM = {
    "resistor": "Device:R",
    "capacitor": "Device:C",
    "inductor": "Device:L",
}

# KiCad GlobalLabel shape semantics (from the label's perspective):
#   "input"  shape: arrow at the FAR end, pointing away from connection
#   "output" shape: arrow at the CONNECTION end, pointing toward the circuit
# For a pin on the LEFT side of an IC receiving a signal, we want the arrow
# at the IC boundary pointing RIGHT (into IC) → use "output" shape.
# For a pin on the RIGHT side emitting a signal → use "input" shape.
# Hence we swap input↔output to align arrows with signal flow direction.
_DIR_TO_SHAPE = {
    "input":         "output",    # arrow at IC boundary points into IC
    "output":        "input",     # arrow at IC boundary points out of IC
    "bidirectional": "bidirectional",
    "power_in":      "passive",
    "power_out":     "passive",
    "open_drain":    "input",
}


class GenerateError(Exception):
    pass


# ── Config & component library ────────────────────────────────────────────────

def load_config(project_dir: Path) -> dict:
    with open(project_dir / "pyproject.toml", "rb") as f:
        return tomllib.load(f).get("tool", {}).get("silicai", {})


def find_component(mpn: str, lib_paths: list[Path]) -> dict:
    for lib in lib_paths:
        for f in lib.rglob("*.yaml"):
            try:
                doc = yaml.safe_load(f.read_text())
            except Exception:
                continue
            if isinstance(doc, dict) and doc.get("component", {}).get("mpn") == mpn:
                return doc["component"]
    raise GenerateError(f"Component {mpn!r} not found in libraries")


# ── Circuit resolution ────────────────────────────────────────────────────────

def resolve(circuit_path: Path, lib_paths: list[Path]) -> dict:
    """
    Expand circuit YAML + component defs into flat parts list + netlist.
    Returns {"name", "parts": [...], "netlist": {net: [(ref, pin)]}}
    """
    doc = yaml.safe_load(circuit_path.read_text())
    circuit = doc["circuit"]
    buses = {b["id"]: b for b in circuit.get("buses", [])}

    parts: list[dict] = []
    netlist: dict[str, list] = {}
    ref_counters: dict[str, int] = {}
    bus_ext_placed: dict[str | None, set] = {}

    def alloc(prefix: str) -> str:
        n = ref_counters.get(prefix, 1)
        ref_counters[prefix] = n + 1
        return f"{prefix}{n}"

    def connect(net: str, ref: str, pin: str) -> None:
        netlist.setdefault(net, []).append((ref, pin))

    def add_passive(ptype: str, value: str, net_1: str, net_2: str) -> None:
        ref = alloc("R" if ptype == "resistor" else "C")
        # Use KiCad pin numbers "1"/"2" as keys so they match the standard symbol
        parts.append({"ref": ref, "type": ptype, "value": value,
                      "comp_def": None, "pin_nets": {"1": net_1, "2": net_2}})
        connect(net_1, ref, "1")
        connect(net_2, ref, "2")

    for inst in circuit["instances"]:
        ref = inst["ref"]
        mpn = inst["mpn"]
        comp = find_component(mpn, lib_paths)

        # Build rail net name map: instance can override component's default rail.net
        # via `rails: {rail_id: net_name}` in the circuit YAML.
        inst_rail_overrides = inst.get("rails", {})
        rail_net_map = {r["id"]: inst_rail_overrides.get(r["id"], r["net"])
                        for r in comp.get("rails", [])}
        # Reverse map to remap required_external.to references that use the
        # component's default rail net name.
        default_to_actual = {r["net"]: rail_net_map[r["id"]]
                             for r in comp.get("rails", [])}

        # pin_config: explicit net overrides per pin (highest priority).
        # Values go through the same rail-rename map for consistency.
        pin_config = {
            pname: default_to_actual.get(net, net)
            for pname, net in inst.get("pin_config", {}).items()
        }

        bus_id = inst.get("bus")
        bus_type = buses[bus_id]["type"] if bus_id and bus_id in buses else None

        # Map interface pin names → bus net names
        iface_nets: dict[str, str] = {}
        if bus_type:
            for iface in comp.get("interfaces", []):
                if iface["type"] == bus_type:
                    for role, pname in iface.get("pins", {}).items():
                        iface_nets[pname] = f"{bus_id}_{role.upper()}"

        # Resolve each pin to a net
        pin_nets: dict[str, str] = {}
        for p in comp["pins"]:
            pname = p["name"]
            if pname in pin_config:
                net = pin_config[pname]
            elif "net" in p:
                net = p["net"]
            elif "rail" in p:
                net = rail_net_map[p["rail"]]
            elif pname in iface_nets:
                net = iface_nets[pname]
            else:
                net = f"{ref}_{pname}"
            pin_nets[pname] = net
            connect(net, ref, pname)

        parts.append({"ref": ref, "mpn": mpn, "comp_def": comp, "pin_nets": pin_nets})

        # Required externals on pins
        placed = bus_ext_placed.setdefault(bus_id, set())
        for p in comp["pins"]:
            pin_net = pin_nets[p["name"]]
            for ext in p.get("required_external", []):
                scope = ext.get("scope", "component")
                ext_to = default_to_actual.get(ext["to"], ext["to"])
                # Skip self-loops: pin directly tied to the target by pin_config
                if pin_net == ext_to:
                    continue
                key = (pin_net, ext_to)
                if scope == "bus":
                    if key in placed:
                        continue
                    placed.add(key)
                if ext["type"] == "resistor":
                    add_passive("resistor", _fmt_r(ext["resistance"]), pin_net, ext_to)
                elif ext["type"] == "capacitor":
                    add_passive("capacitor", _fmt_c(ext["capacitance"]), pin_net, ext_to)

        # Per-rail decoupling caps
        for rail in comp.get("rails", []):
            rail_net = rail_net_map[rail["id"]]
            for decoup in rail.get("per_pin_decoupling", []):
                add_passive("capacitor", _fmt_c(decoup["capacitance"]), rail_net, "GND")

    # Collect power rail nets: from circuit's power_rails declaration + component rails
    power_nets: set[str] = {"GND"}
    power_nets.update(pr["net"] for pr in circuit.get("power_rails", []))
    for part in parts:
        comp_def = part.get("comp_def")
        if comp_def:
            for rail in comp_def.get("rails", []):
                power_nets.add(rail["net"])

    return {"name": circuit["name"], "parts": parts, "netlist": netlist,
            "power_nets": power_nets}


_R_UNITS = {"kΩ": "k", "kOhm": "k", "Ω": "R", "Ohm": "R", "MΩ": "M"}

def _fmt_r(r: dict) -> str:
    return f"{r['value']}{_R_UNITS.get(r['unit'], r['unit'])}"

def _fmt_c(c: dict) -> str:
    return f"{c['value']}{c['unit']}"


# ── KiCad symbol loading ──────────────────────────────────────────────────────

_sym_lib_cache: dict[str, SymbolLib] = {}


def _load_kicad_sym(kicad_sym: str, kicad_lib_path: Path):
    """Return (deepcopy of Symbol, all pins list) from a KiCad standard library."""
    lib_name, sym_name = kicad_sym.split(":", 1)
    if lib_name not in _sym_lib_cache:
        lib_file = kicad_lib_path / f"{lib_name}.kicad_sym"
        if not lib_file.exists():
            raise GenerateError(f"KiCad library not found: {lib_file}")
        _sym_lib_cache[lib_name] = SymbolLib.from_file(str(lib_file))
    lib = _sym_lib_cache[lib_name]
    sym = next((s for s in lib.symbols if s.entryName == sym_name), None)
    if sym is None:
        raise GenerateError(f"Symbol {sym_name!r} not found in {lib_name}.kicad_sym")
    sym = copy.deepcopy(sym)
    sym.libraryNickname = lib_name
    return sym


def _all_pins(sym) -> list:
    """Collect all SymbolPin objects from a symbol and its sub-units."""
    pins = list(sym.pins)
    for u in sym.units:
        pins.extend(u.pins)
    return pins


# ── Placement ─────────────────────────────────────────────────────────────────

def _place(parts: list[dict]) -> list[tuple[dict, float, float]]:
    result = []
    ic_x, ic_y   = 50.0, 60.0
    pass_x, pass_y = 120.0, 30.0
    for part in parts:
        if part.get("comp_def") is not None:
            result.append((part, ic_x, ic_y))
            ic_y += 70.0
        else:
            result.append((part, pass_x, pass_y))
            pass_y += 30.0
    return result


# ── Passive orientation ───────────────────────────────────────────────────────

def _net_priority(net: str, power_nets: set[str]) -> int:
    """Return ordering priority for passive pin assignment.
    Power supply (2) goes to the top pin, GND (0) to the bottom, signals in between."""
    if net in power_nets and net != "GND":
        return 2
    if net == "GND":
        return 0
    return 1


# ── Power symbol placement ────────────────────────────────────────────────────

def _place_power_symbol(
    sch,
    net: str,
    px: float,
    py: float,
    kicad_lib_path: Path,
    added_syms: set[str],
    pwr_counter: list[int],
) -> bool:
    """
    Place a KiCad power symbol (power:{net}) at (px, py).
    Angle=0: GND body extends downward, VCC-like body extends upward.
    Returns True if placed, False if the symbol wasn't found in the library.
    """
    kicad_sym = f"power:{net}"
    try:
        lib_sym = _load_kicad_sym(kicad_sym, kicad_lib_path)
    except GenerateError:
        return False

    if kicad_sym not in added_syms:
        lib_sym_copy = copy.deepcopy(lib_sym)
        lib_sym_copy.hidePinNumbers = True
        sch.libSymbols.append(lib_sym_copy)
        added_syms.add(kicad_sym)

    pwr_counter[0] += 1

    inst = SchematicSymbol()
    inst.libId = kicad_sym
    inst.position = Position(X=px, Y=py, angle=0)
    inst.unit = 1
    inst.inBom = False
    inst.onBoard = False
    inst.uuid = str(uuid.uuid4())

    inst.properties = copy.deepcopy(lib_sym.properties)
    for prop in inst.properties:
        prop.position.X += px
        prop.position.Y = py - prop.position.Y
        if prop.key == "Reference":
            prop.value = f"#PWR{pwr_counter[0]:02d}"
            if prop.effects is None:
                prop.effects = Effects(font=Font(height=1.27, width=1.27))
            prop.effects.hide = True
        elif prop.key == "Value":
            prop.value = net
        else:
            if prop.effects is None:
                prop.effects = Effects(font=Font(height=1.27, width=1.27))
            prop.effects.hide = True

    all_lib_pins = _all_pins(lib_sym)
    for p in all_lib_pins:
        inst.pins[p.number] = str(uuid.uuid4())
    if not all_lib_pins:
        inst.pins["1"] = str(uuid.uuid4())

    sch.schematicSymbols.append(inst)
    return True


# ── KiCad schematic writer ────────────────────────────────────────────────────

def write_kicad_sch(resolved: dict, output: Path, kicad_lib_path: Path) -> None:
    sch = Schematic.create_new()
    added_syms: set[str] = set()
    label_effects = Effects(font=Font(height=1.27, width=1.27))
    power_nets = resolved.get("power_nets", set())
    pwr_counter = [0]

    for part, cx, cy in _place(resolved["parts"]):
        # Resolve KiCad symbol name
        if part.get("comp_def"):
            kicad_sym = part["comp_def"].get("kicad_symbol")
            if not kicad_sym:
                print(f"warning: {part['ref']} ({part['mpn']}) has no kicad_symbol — skipped",
                      file=sys.stderr)
                continue
        else:
            kicad_sym = _PASSIVE_SYM.get(part["type"])
            if not kicad_sym:
                continue

        # Load from KiCad library
        try:
            lib_sym = _load_kicad_sym(kicad_sym, kicad_lib_path)
        except GenerateError as e:
            print(f"warning: {e}", file=sys.stderr)
            continue

        # Add to lib_symbols once per unique symbol
        if kicad_sym not in added_syms:
            lib_sym_copy = copy.deepcopy(lib_sym)
            if part.get("comp_def") is None:  # passive: hide pin numbers
                lib_sym_copy.hidePinNumbers = True
            sch.libSymbols.append(lib_sym_copy)
            added_syms.add(kicad_sym)

        # Build placed instance
        inst = SchematicSymbol()
        inst.libId = kicad_sym
        inst.position = Position(X=cx, Y=cy, angle=0)
        inst.unit = 1
        inst.inBom = True
        inst.onBoard = True
        inst.uuid = str(uuid.uuid4())

        ref_val = part["ref"]
        value   = part.get("mpn") or part.get("value", "?")

        # Copy properties from lib symbol and update Reference/Value
        # KiCad lib symbols use Y-up; schematics use Y-down.
        # Absolute schematic pos = (cx + lib_x, cy - lib_y).
        inst.properties = copy.deepcopy(lib_sym.properties)
        for prop in inst.properties:
            prop.position.X += cx
            prop.position.Y = cy - prop.position.Y
            if prop.key == "Reference":
                prop.value = ref_val
            elif prop.key == "Value":
                prop.value = value
            else:
                if prop.effects is None:
                    prop.effects = Effects(font=Font(height=1.27, width=1.27))
                prop.effects.hide = True

        # Register pin UUIDs (required by KiCad)
        for p in _all_pins(lib_sym):
            inst.pins[p.number] = str(uuid.uuid4())

        sch.schematicSymbols.append(inst)

        # Build pin-name → label shape from component definition directions
        pin_shapes: dict[str, str] = {}
        if part.get("comp_def"):
            for p_def in part["comp_def"]["pins"]:
                direction = p_def.get("direction", "bidirectional")
                pin_shapes[p_def["name"]] = _DIR_TO_SHAPE.get(direction, "bidirectional")

        # For passives: swap pin_nets if pin 2 has higher priority than pin 1
        # so that power supplies end up at the top (pin 1) and GND at the bottom (pin 2).
        pin_nets = part["pin_nets"]
        if part.get("comp_def") is None:
            n1, n2 = pin_nets.get("1"), pin_nets.get("2")
            if n1 and n2 and _net_priority(n2, power_nets) > _net_priority(n1, power_nets):
                pin_nets = {"1": n2, "2": n1}
        for p in _all_pins(lib_sym):
            # Match by pin name (ICs) or pin number (passives, where name is '~')
            net = pin_nets.get(p.name) or pin_nets.get(p.number)
            if net is None:
                continue

            pin_x = cx + p.position.X
            pin_y = cy - p.position.Y

            # Power rail nets use a power symbol (body up for supply, down for GND)
            if net in power_nets:
                if _place_power_symbol(sch, net, pin_x, pin_y,
                                       kicad_lib_path, added_syms, pwr_counter):
                    continue
                # Symbol not found in library — fall through to GlobalLabel

            lib_angle = p.position.angle or 0
            # Label extends opposite to pin stub direction; Y-axis is flipped
            # between lib (Y-up) and schematic (Y-down), so use (lib_angle+180)%360.
            lbl_angle = int((lib_angle + 180) % 360)
            lbl = GlobalLabel()
            lbl.text = net
            lbl.shape = pin_shapes.get(p.name, pin_shapes.get(p.number, "passive"))
            lbl.position = Position(X=pin_x, Y=pin_y, angle=lbl_angle)
            lbl.fieldsAutoplaced = True
            # Text justify follows label orientation (matches KiCad convention)
            justify = "right" if lbl_angle in (180, 270) else "left"
            lbl.effects = copy.deepcopy(label_effects)
            lbl.effects.justify = Justify(horizontally=justify)
            lbl.uuid = str(uuid.uuid4())
            sch.globalLabels.append(lbl)

    sch.to_file(str(output))
    print(f"✓ {output}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate KiCad schematic from SilicAI circuit definition"
    )
    parser.add_argument("circuit", type=Path)
    parser.add_argument("-o", "--output", type=Path, default=None)
    parser.add_argument("--project-dir", type=Path, default=None)
    args = parser.parse_args()

    project_dir = args.project_dir or Path.cwd()
    output = args.output or args.circuit.with_suffix(".kicad_sch")

    config = load_config(project_dir)
    lib_paths = [
        (project_dir / entry["path"]).resolve()
        for entry in config.get("component_libraries", [])
    ]
    kicad_lib_path = Path(config.get("kicad_library_path", str(_DEFAULT_KICAD_SYM)))

    try:
        resolved = resolve(args.circuit, lib_paths)
    except GenerateError as e:
        print(f"✗ {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Circuit: {resolved['name']}")
    print(f"Parts ({len(resolved['parts'])}):")
    for part in resolved["parts"]:
        label = part.get("mpn") or f"{part['type']} {part['value']}"
        print(f"  {part['ref']:5s}  {label}")
    print(f"Nets ({len(resolved['netlist'])}):")
    for net, conns in sorted(resolved["netlist"].items()):
        pins = ", ".join(f"{r}.{p}" for r, p in conns)
        print(f"  {net}: {pins}")

    write_kicad_sch(resolved, output, kicad_lib_path)


if __name__ == "__main__":
    main()
