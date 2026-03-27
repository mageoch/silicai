#!/usr/bin/env python3
"""Generate KiCad schematics from SilicAI circuit definitions."""

import sys
import copy
import math
import uuid
import tomllib
import argparse
from pathlib import Path

import yaml
from importlib.resources import files
from kiutils.schematic import Schematic
from kiutils.symbol import SymbolLib
from kiutils.items.schitems import (
    SchematicSymbol, GlobalLabel, LocalLabel, Connection,
    HierarchicalSheet, HierarchicalSheetInstance,
    HierarchicalSheetProjectInstance, HierarchicalSheetProjectPath,
)
from kiutils.items.common import Position, Effects, Font, Justify, Property, Stroke, ColorRGBA

# ── Defaults ──────────────────────────────────────────────────────────────────
_DEFAULT_KICAD_SYM = Path("/usr/share/kicad/symbols")

_PASSIVE_SYM = {
    "resistor": "Device:R",
    "capacitor": "Device:C",
    "inductor": "Device:L",
}

# KiCad GlobalLabel shapes match the pin direction on the IC.
_DIR_TO_SHAPE = {
    "input":         "input",
    "output":        "output",
    "bidirectional": "bidirectional",
    "power_in":      "passive",
    "power_out":     "passive",
    "open_drain":    "output",
}


class GenerateError(Exception):
    pass


# Bus types whose open-drain signals are managed by a circuit-level pull_ups definition.
# Component-level scope:bus pull-ups are skipped for these to avoid double-placement.
_BUS_LEVEL_PULL_UP_TYPES: frozenset[str] = frozenset({"I2C", "SMBus"})


# ── Config & component library ────────────────────────────────────────────────

_BUILTIN_COMPONENTS = Path(str(files("silicai").joinpath("components")))


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

def resolve(
    circuit_path: Path,
    lib_paths: list[Path],
    shared: dict | None = None,
    placed_bus_pullups: set[str] | None = None,
) -> dict:
    """
    Expand circuit YAML + component defs into flat parts list + netlist.

    shared: optional dict with keys "buses" and "power_rails" from project-level
        shared resources. Circuit-local definitions take precedence on ID/net conflicts.
    placed_bus_pullups: optional mutable set of bus IDs whose pull-ups have already
        been placed by a previous circuit in the same project. Pull-ups for those
        bus IDs are skipped; IDs of newly placed pull-ups are added to the set.

    Returns {"name", "parts": [...], "netlist": {net: [(ref, pin)]}}
    """
    doc = yaml.safe_load(circuit_path.read_text())
    circuit = doc["circuit"]

    # Merge shared buses (lower priority) with circuit-local buses (higher priority).
    shared_buses = {b["id"]: b for b in (shared or {}).get("buses", [])}
    local_buses  = {b["id"]: b for b in circuit.get("buses", [])}
    buses = {**shared_buses, **local_buses}

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

        # address: derive address-select pin connection from component's
        # address_select options table. Lower priority than explicit pin_config.
        for bus_conn in inst.get("buses", []):
            address = bus_conn.get("address")
            if not address:
                continue
            bid = bus_conn["id"]
            for p in comp["pins"]:
                if p.get("primary_function", {}).get("type") != "address_select":
                    continue
                for opt in p["primary_function"].get("options", []):
                    if opt.get("i2c_address") != address:
                        continue
                    connect_to = opt["connect_to"]
                    # Resolve connect_to: look up the named pin's net, or
                    # treat as a bus signal name if no pin with that name exists.
                    ref_pin = next((pp for pp in comp["pins"] if pp["name"] == connect_to), None)
                    if ref_pin is not None:
                        if "rail" in ref_pin:
                            resolved = rail_net_map[ref_pin["rail"]]
                        elif "net" in ref_pin:
                            resolved = default_to_actual.get(ref_pin["net"], ref_pin["net"])
                        else:
                            resolved = f"{bid}_{connect_to.upper()}"
                    else:
                        resolved = connect_to  # literal net (e.g. GND)
                    if p["name"] not in pin_config:
                        pin_config[p["name"]] = resolved
                    break

        # Map component pin names → bus net names, and pin name → bus_id for
        # deduplication of bus-scoped required_external passives.
        iface_nets: dict[str, str] = {}
        pin_to_bus: dict[str, str] = {}
        for bus_conn in inst.get("buses", []):
            bid = bus_conn["id"]
            bus_type = buses[bid]["type"] if bid in buses else None
            if not bus_type:
                continue
            explicit_pins = bus_conn.get("pins", {})  # role → component pin name
            if explicit_pins:
                # Flexible-pin component (e.g. MCU): caller declares which GPIO
                # serves which role; invert to pin_name → net_name.
                for role, pname in explicit_pins.items():
                    iface_nets[pname] = f"{bid}_{role.upper()}"
                    pin_to_bus[pname] = bid
            else:
                # Fixed-pin component (e.g. TMP117): auto-map from the first
                # matching interface definition in the component YAML.
                for iface in comp.get("interfaces", []):
                    if iface["type"] == bus_type:
                        for role, pname in iface.get("pins", {}).items():
                            iface_nets[pname] = f"{bid}_{role.upper()}"
                            pin_to_bus[pname] = bid
                        break

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

        # Compute local nets for rails with per-pin decoupling.
        # e.g. RP2350A rail "iovdd" on ref "U2" → local net "U2_IOVDD".
        # These local nets are placed alongside the power symbol at each rail pin
        # so that the decoupling cap is visually anchored to its IC pin.
        rail_local_nets: dict[str, str] = {
            rail["id"]: f"{ref}_{rail['id'].upper()}"
            for rail in comp.get("rails", [])
            if rail.get("per_pin_decoupling")
        }

        parts.append({"ref": ref, "mpn": mpn, "comp_def": comp, "pin_nets": pin_nets,
                      "rail_local_nets": rail_local_nets})

        # Required externals on pins
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
                    bid = pin_to_bus.get(p["name"])
                    # Skip if this bus type manages pull-ups at the circuit level
                    # (circuit bus.pull_ups), to avoid double-placing them.
                    bus_type = buses.get(bid, {}).get("type") if bid else None
                    if bus_type in _BUS_LEVEL_PULL_UP_TYPES and buses.get(bid, {}).get("pull_ups"):
                        continue
                    placed = bus_ext_placed.setdefault(bid, set())
                    if key in placed:
                        continue
                    placed.add(key)
                if ext["type"] == "resistor":
                    add_passive("resistor", _fmt_r(ext["resistance"]), pin_net, ext_to)
                elif ext["type"] == "capacitor":
                    add_passive("capacitor", _fmt_c(ext["capacitance"]), pin_net, ext_to)

        # Per-rail decoupling caps — use local net so the cap is anchored to its IC pin.
        for rail in comp.get("rails", []):
            rail_net = rail_net_map[rail["id"]]
            local_net = rail_local_nets.get(rail["id"])
            cap_net = local_net if local_net else rail_net
            for decoup in rail.get("per_pin_decoupling", []):
                add_passive("capacitor", _fmt_c(decoup["capacitance"]), cap_net, "GND")

    # Bus-level pull-up resistors — one per declared signal in pull_ups.
    # For shared buses, skip if pull-ups were already placed by a previous circuit.
    if placed_bus_pullups is None:
        placed_bus_pullups = set()
    for bus in buses.values():
        pull_ups = bus.get("pull_ups", {})
        bid = bus["id"]
        if not pull_ups or bid in placed_bus_pullups:
            continue
        # Only place pull-ups if this bus is actually referenced by an instance in this circuit.
        if not any(
            any(bc.get("id") == bid for bc in inst.get("buses", []))
            for inst in circuit["instances"]
        ):
            continue
        for signal, cfg in pull_ups.items():
            net = f"{bid}_{signal.upper()}"
            add_passive("resistor", _fmt_r(cfg["resistance"]), net, cfg["net"])
        placed_bus_pullups.add(bid)

    # Collect power rail nets: from shared + circuit's power_rails + component rails
    power_nets: set[str] = {"GND"}
    power_nets.update(pr["net"] for pr in (shared or {}).get("power_rails", []))
    power_nets.update(pr["net"] for pr in circuit.get("power_rails", []))
    for part in parts:
        comp_def = part.get("comp_def")
        if comp_def:
            for rail in comp_def.get("rails", []):
                power_nets.add(rail["net"])

    # Collect all local decoupling net names (used to render them as LocalLabel)
    local_nets: set[str] = {
        local
        for part in parts
        for local in part.get("rail_local_nets", {}).values()
    }

    return {"name": circuit["name"], "parts": parts, "netlist": netlist,
            "power_nets": power_nets, "local_nets": local_nets}


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
            ic_y += 200.0
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

_WIRE_STUB_LEN = 15.24  # mm (12 × 1.27 mm grid units) — space between IC pin and power symbol


def _make_local_label(text: str, x: float, y: float, lbl_angle: int,
                      label_effects: Effects) -> LocalLabel:
    """Create a LocalLabel (sheet-scoped net label) at the given position."""
    justify = "right" if lbl_angle in (180, 270) else "left"
    lbl = LocalLabel()
    lbl.text = text
    lbl.position = Position(X=x, Y=y, angle=lbl_angle)
    lbl.fieldsAutoplaced = True
    lbl.effects = copy.deepcopy(label_effects)
    lbl.effects.justify = Justify(horizontally=justify)
    lbl.uuid = str(uuid.uuid4())
    return lbl


def _make_wire(x0: float, y0: float, x1: float, y1: float) -> Connection:
    """Create a schematic wire segment between two points."""
    wire = Connection(type="wire")
    wire.points = [Position(X=x0, Y=y0), Position(X=x1, Y=y1)]
    wire.uuid = str(uuid.uuid4())
    return wire


def write_kicad_sch(resolved: dict, output: Path, kicad_lib_path: Path) -> None:
    sch = Schematic.create_new()
    added_syms: set[str] = set()
    label_effects = Effects(font=Font(height=1.27, width=1.27))
    power_nets = resolved.get("power_nets", set())
    local_nets = resolved.get("local_nets", set())
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

        # Build pin-name → local net for IC rail pins that have per-pin decoupling.
        # The local net label is placed alongside the power symbol at the pin so that
        # the decoupling cap (which uses the local net) is visually anchored here.
        pin_to_local_net: dict[str, str] = {}
        if part.get("comp_def"):
            for p_def in part["comp_def"]["pins"]:
                rail_id = p_def.get("rail")
                if rail_id:
                    local = part.get("rail_local_nets", {}).get(rail_id)
                    if local:
                        pin_to_local_net[p_def["name"]] = local

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

            lib_angle = p.position.angle or 0
            # Label extends opposite to pin stub direction; Y-axis is flipped
            # between lib (Y-up) and schematic (Y-down), so use (lib_angle+180)%360.
            lbl_angle = int((lib_angle + 180) % 360)

            # Direction vector pointing away from IC (in schematic / screen coords).
            rad = math.radians(lbl_angle)
            dx = math.cos(rad)
            dy = -math.sin(rad)

            # Power rail nets use a power symbol (body up for supply, down for GND)
            if net in power_nets:
                local_net = pin_to_local_net.get(p.name)
                if local_net:
                    # Wire stub: IC pin ──[LocalLabel]── wire ──[power symbol]
                    # LocalLabel at the IC pin end labels the wire; power symbol at far end.
                    end_x = pin_x + dx * _WIRE_STUB_LEN
                    end_y = pin_y + dy * _WIRE_STUB_LEN
                    sch.graphicalItems.append(_make_wire(pin_x, pin_y, end_x, end_y))
                    sch.labels.append(
                        _make_local_label(local_net, pin_x, pin_y, lbl_angle, label_effects)
                    )
                    _place_power_symbol(sch, net, end_x, end_y,
                                        kicad_lib_path, added_syms, pwr_counter)
                    continue  # handled — skip GlobalLabel fallback regardless
                else:
                    if _place_power_symbol(sch, net, pin_x, pin_y,
                                           kicad_lib_path, added_syms, pwr_counter):
                        continue
                    # Symbol not found in library — fall through to GlobalLabel

            # Non-power nets: use LocalLabel for local decoupling nets, GlobalLabel otherwise.
            justify = "right" if lbl_angle in (180, 270) else "left"
            if net in local_nets:
                sch.labels.append(
                    _make_local_label(net, pin_x, pin_y, lbl_angle, label_effects)
                )
            else:
                lbl = GlobalLabel()
                lbl.text = net
                lbl.shape = pin_shapes.get(p.name, pin_shapes.get(p.number, "passive"))
                lbl.position = Position(X=pin_x, Y=pin_y, angle=lbl_angle)
                lbl.fieldsAutoplaced = True
                lbl.effects = copy.deepcopy(label_effects)
                lbl.effects.justify = Justify(horizontally=justify)
                lbl.uuid = str(uuid.uuid4())
                sch.globalLabels.append(lbl)

    sch.to_file(str(output))
    print(f"✓ {output}")


# ── Project generation ────────────────────────────────────────────────────────

def _slug(name: str) -> str:
    """Convert a project name to a safe filename stem."""
    return name.lower().replace(" ", "_").replace("/", "_")


def _write_kicad_pro(proj: dict, output: Path) -> None:
    """Emit a minimal .kicad_pro file."""
    import json
    name = proj["name"]
    data = {
        "meta": {"filename": output.name, "version": 1},
        "schematic": {
            "annotate_start_num": 0,
            "drawing": {
                "default_bus_thickness": 12.0,
                "default_junction_size": 40.0,
                "default_line_thickness": 6.0,
                "default_text_size": 50.0,
                "default_wire_thickness": 6.0,
                "field_names": [],
                "junction_size_choice": 3,
                "label_size_ratio": 0.375,
                "pin_symbol_size": 25.0,
                "text_offset_ratio": 0.15,
            },
        },
        "text_variables": {},
    }
    if proj.get("revision"):
        data["text_variables"]["REVISION"] = proj["revision"]
    if proj.get("company"):
        data["text_variables"]["COMPANY"] = proj["company"]
    if proj.get("description"):
        data["text_variables"]["TITLE"] = name
    output.write_text(json.dumps(data, indent=2))


def write_kicad_project(
    project_path: Path,
    lib_paths: list[Path],
    output_dir: Path,
    kicad_lib_path: Path = _DEFAULT_KICAD_SYM,
) -> list[Path]:
    """Generate a full KiCad project from a project YAML.

    Produces:
      {output_dir}/{slug}.kicad_pro   — project file
      {output_dir}/{slug}.kicad_sch   — root schematic with sheet symbols
      {output_dir}/{stem}.kicad_sch   — one sub-sheet per circuit
    """
    doc = yaml.safe_load(project_path.read_text())
    proj = doc["project"]
    slug = _slug(proj["name"])
    output_dir.mkdir(parents=True, exist_ok=True)

    shared = proj.get("shared", {})
    placed_bus_pullups: set[str] = set()

    # ── Generate each circuit sub-sheet ───────────────────────────────────────
    sub_sheets: list[dict] = []
    for circuit_rel in proj["circuits"]:
        circuit_path = (project_path.parent / circuit_rel).resolve()
        circuit_doc = yaml.safe_load(circuit_path.read_text())
        circuit_name = circuit_doc["circuit"]["name"]
        out_name = circuit_path.stem + ".kicad_sch"
        out_path = output_dir / out_name
        resolved = resolve(circuit_path, lib_paths,
                           shared=shared, placed_bus_pullups=placed_bus_pullups)
        write_kicad_sch(resolved, out_path, kicad_lib_path)
        sub_sheets.append({"name": circuit_name, "file": out_name, "uuid": str(uuid.uuid4())})

    # ── Generate root schematic ────────────────────────────────────────────────
    root_path = output_dir / f"{slug}.kicad_sch"
    from kiutils.items.common import PageSettings
    sch = Schematic.create_new()
    sch.paper = PageSettings(paperSize="A3")

    _BOX_W, _BOX_H, _BOX_GAP = 120.0, 40.0, 20.0
    _ORIGIN_X, _ORIGIN_Y = 30.0, 30.0
    _NAME_OFFSET, _FILE_OFFSET = 2.5, 2.5
    _FONT = Font(height=1.27, width=1.27)

    for i, cs in enumerate(sub_sheets):
        bx = _ORIGIN_X
        by = _ORIGIN_Y + i * (_BOX_H + _BOX_GAP)

        sheet = HierarchicalSheet()
        sheet.position = Position(X=bx, Y=by)
        sheet.width = _BOX_W
        sheet.height = _BOX_H
        sheet.uuid = cs["uuid"]

        sheet.sheetName = Property(
            key="Sheet name", value=cs["name"],
            position=Position(X=bx, Y=by - _NAME_OFFSET, angle=0),
            effects=Effects(font=_FONT, justify=Justify(horizontally="left", vertically="bottom")),
        )
        sheet.fileName = Property(
            key="Sheet file", value=cs["file"],
            position=Position(X=bx, Y=by + _BOX_H + _FILE_OFFSET, angle=0),
            effects=Effects(font=_FONT, justify=Justify(horizontally="left", vertically="top"), hide=True),
        )

        proj_path = HierarchicalSheetProjectPath()
        proj_path.sheetInstancePath = f"/{cs['uuid']}"
        proj_path.page = str(i + 2)
        proj_inst = HierarchicalSheetProjectInstance()
        proj_inst.name = proj["name"]
        proj_inst.paths = [proj_path]
        sheet.instances = [proj_inst]

        sch.sheets.append(sheet)

    root_inst = HierarchicalSheetInstance()
    root_inst.instancePath = "/"
    root_inst.page = "1"
    sch.sheetInstances = [root_inst]
    for cs in sub_sheets:
        inst = HierarchicalSheetInstance()
        inst.instancePath = f"/{cs['uuid']}"
        inst.page = str(sub_sheets.index(cs) + 2)
        sch.sheetInstances.append(inst)

    sch.to_file(str(root_path))

    # ── Generate .kicad_pro ────────────────────────────────────────────────────
    pro_path = output_dir / f"{slug}.kicad_pro"
    _write_kicad_pro(proj, pro_path)

    return [pro_path, root_path] + [output_dir / cs["file"] for cs in sub_sheets]


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
    lib_paths = [_BUILTIN_COMPONENTS] + [
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
