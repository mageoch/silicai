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
from kiutils.schematic import Schematic, Junction
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
    "crystal":  "Device:Crystal_GND24",
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
    resolved_power_nets: set[str] = set()  # actual net names after circuit-level rail overrides
    _filt_gid = 0  # counter for filter group IDs

    def alloc(prefix: str) -> str:
        n = ref_counters.get(prefix, 1)
        ref_counters[prefix] = n + 1
        return f"{prefix}{n}"

    def connect(net: str, ref: str, pin: str) -> None:
        netlist.setdefault(net, []).append((ref, pin))

    def add_passive(ptype: str, value: str, net_1: str, net_2: str,
                    pin_annotation: str | None = None,
                    rail_group: str | None = None,
                    pin_sort: int = 0,
                    extra_pins: dict[str, str] | None = None,
                    filter_group: str | None = None,
                    filter_role: str | None = None,
                    filter_internal_pins: set[str] | None = None) -> None:
        _PREFIX = {"resistor": "R", "inductor": "L", "crystal": "X"}
        ref = alloc(_PREFIX.get(ptype, "C"))
        pin_nets: dict[str, str] = {"1": net_1, "2": net_2}
        if extra_pins:
            pin_nets.update(extra_pins)
        part: dict = {"ref": ref, "type": ptype, "value": value,
                      "comp_def": None, "pin_nets": pin_nets}
        if pin_annotation:
            part["pin_annotation"] = pin_annotation
        if rail_group is not None:
            part["rail_group"] = rail_group
            part["pin_sort"]   = pin_sort
        if filter_group is not None:
            part["filter_group"] = filter_group
            part["filter_role"]  = filter_role
        if filter_internal_pins:
            part["filter_internal_pins"] = filter_internal_pins
        parts.append(part)
        for pin, net in pin_nets.items():
            connect(net, ref, pin)

    for inst in circuit["instances"]:
        ref = inst["ref"]
        mpn = inst["mpn"]
        comp = find_component(mpn, lib_paths)

        # Build rail net name map: instance can override component's default rail.net
        # via `rails: {rail_id: net_name}` in the circuit YAML.
        inst_rail_overrides = inst.get("rails", {})
        rail_net_map = {r["id"]: inst_rail_overrides.get(r["id"], r["net"])
                        for r in comp.get("rails", [])}
        resolved_power_nets.update(rail_net_map.values())
        # Reverse map to remap externals.to references that use the
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
        # deduplication of bus-scoped externals passives.
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

        parts.append({"ref": ref, "mpn": mpn, "comp_def": comp, "pin_nets": pin_nets})

        # Externals on pins (required: true items only)
        for p in comp["pins"]:
            pin_net = pin_nets[p["name"]]
            pin_label = f"{p['name']}[{p['number']}]"
            for ext in p.get("externals", []):
                if not ext.get("required", False):
                    continue
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
                    add_passive("resistor", _fmt_r(ext["resistance"]), pin_net, ext_to,
                                pin_annotation=pin_label)
                elif ext["type"] == "capacitor":
                    add_passive("capacitor", _fmt_c(ext["capacitance"]), pin_net, ext_to,
                                pin_annotation=pin_label)
                elif ext["type"] == "crystal":
                    # Crystal_GND24: pin 1 = XIN-side, pin 3 = XOUT-side, pins 2 & 4 = case/GND
                    series_r = ext.get("series_r")
                    xout_cap = ext.get("xout_cap")
                    xin_cap  = ext.get("xin_cap")
                    if series_r:
                        # Rs is in series between MCU XOUT and the crystal.
                        # Introduce an intermediate net for the crystal's XOUT-side terminal
                        # so that Rs, the crystal, and C_xout are correctly connected.
                        xtal_node = f"__{ref}_XTAL_XOUT"
                        add_passive("crystal", _fmt_f(ext["frequency"]), pin_net, "GND",
                                    pin_annotation=pin_label,
                                    extra_pins={"3": xtal_node, "4": "GND"})
                        add_passive("resistor", _fmt_r(series_r), ext_to, xtal_node,
                                    pin_annotation=pin_label)
                        if xin_cap:
                            add_passive("capacitor", _fmt_c(xin_cap), pin_net, "GND",
                                        pin_annotation=pin_label)
                        if xout_cap:
                            add_passive("capacitor", _fmt_c(xout_cap), xtal_node, "GND",
                                        pin_annotation=pin_label)
                    else:
                        add_passive("crystal", _fmt_f(ext["frequency"]), pin_net, "GND",
                                    pin_annotation=pin_label,
                                    extra_pins={"3": ext_to, "4": "GND"})
                        if xin_cap:
                            add_passive("capacitor", _fmt_c(xin_cap), pin_net, "GND",
                                        pin_annotation=pin_label)
                elif ext["type"] == "inductor":
                    add_passive("inductor", _fmt_l(ext["inductance"]), pin_net, ext_to,
                                pin_annotation=pin_label)
                else:
                    print(f"warning: unknown required_external type {ext['type']!r} on pin "
                          f"{p['name']!r} — skipped", file=sys.stderr)

        # Per-pin decoupling — defined directly on each pin.
        # Caps on a rail are grouped together for the horizontal bus layout.
        for p in comp["pins"]:
            for decoup in p.get("decoupling", []):
                pin_net = pin_nets.get(p["name"])
                to_net  = decoup.get("to", "GND")
                pin_num = int(p["number"]) if str(p["number"]).isdigit() else 0
                rail_id = p.get("rail")
                group   = rail_net_map.get(rail_id) if rail_id else None
                ptype   = decoup["type"]
                if ptype == "capacitor":
                    add_passive("capacitor", _fmt_c(decoup["capacitance"]),
                                pin_net, to_net,
                                pin_annotation=str(p["number"]),
                                rail_group=group, pin_sort=pin_num)
                elif ptype == "resistor":
                    add_passive("resistor", _fmt_r(decoup["resistance"]),
                                pin_net, to_net,
                                pin_annotation=str(p["number"]))

        # Rail input filters — e.g. RC filter between a supply and a sensitive rail.
        # 'from'/'to' net names are resolved through the rail net map so that circuit-level
        # rail overrides (e.g. vreg_vin → +3V3) are respected.
        rail_default_to_actual: dict[str, str] = {
            r["net"]: rail_net_map.get(r["id"], r["net"])
            for r in comp.get("rails", [])
        }
        for rail in comp.get("rails", []):
            rnet    = rail_net_map.get(rail["id"], rail["net"])
            filters = rail.get("input_filter", [])
            # RC filters (series R + shunt C) are grouped so the L-filter topology is
            # rendered visually.  Pure shunt caps (no series R) are promoted to the rail
            # bus row.  Other topologies (LC, ferrite, etc.) fall through as regular passives.
            has_series_r = any(f["type"] == "resistor" for f in filters)
            has_shunt_c  = any(
                f["type"] == "capacitor"
                and (rail_default_to_actual.get(f.get("to", rnet), f.get("to", rnet)) or rnet) == "GND"
                for f in filters
            )
            use_filter_group = has_series_r and has_shunt_c
            fgid: str | None = None
            if use_filter_group:
                _filt_gid += 1
                fgid = f"filt_{_filt_gid}"
            for filt in filters:
                ptype    = filt["type"]
                from_raw = filt.get("from", rnet)
                to_raw   = filt.get("to", rnet)
                from_net: str = rail_default_to_actual.get(from_raw, from_raw) or from_raw
                to_net:   str = rail_default_to_actual.get(to_raw,   to_raw)   or to_raw
                if ptype == "resistor":
                    add_passive("resistor", _fmt_r(filt["resistance"]), from_net, to_net,
                                filter_group=fgid if use_filter_group else None,
                                filter_role="series_r" if use_filter_group else None,
                                filter_internal_pins={"1", "2"} if use_filter_group else None)
                elif ptype == "capacitor":
                    is_shunt = (to_net == "GND")
                    if is_shunt and use_filter_group:
                        # Shunt cap is the L-filter's vertical leg.  All pin wiring
                        # (stubs, labels, power symbols) is drawn in the filter section.
                        add_passive("capacitor", _fmt_c(filt["capacitance"]), from_net, to_net,
                                    filter_group=fgid, filter_role="shunt_c",
                                    filter_internal_pins={"1", "2"})
                    else:
                        promote = is_shunt and not has_series_r
                        add_passive("capacitor", _fmt_c(filt["capacitance"]), from_net, to_net,
                                    rail_group=from_net if promote else None)
                elif ptype in ("ferrite_bead", "inductor"):
                    l_val = filt.get("impedance_at_100mhz") or filt.get("inductance", {})
                    add_passive("inductor", _fmt_l(l_val), from_net, to_net)

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
    # resolved_power_nets already contains the actual (post-override) net names.
    power_nets: set[str] = {"GND"}
    power_nets.update(pr["net"] for pr in (shared or {}).get("power_rails", []))
    power_nets.update(pr["net"] for pr in circuit.get("power_rails", []))
    power_nets.update(resolved_power_nets)

    local_nets: set[str] = set()  # reserved for future local-net use

    # ── Crystal circuit group detection ───────────────────────────────────────
    # Tag crystal + associated passives so the schematic layout groups them.
    _xtal_gid = 0
    for part in list(parts):
        if part.get("type") != "crystal" or "crystal_group" in part:
            continue
        _xtal_gid += 1
        gid      = f"xtal_{_xtal_gid}"
        xin_net  = part["pin_nets"]["1"]
        xout_net = part["pin_nets"]["3"]
        part["crystal_group"]      = gid
        part["xtal_internal_pins"] = {"1", "2", "3", "4"}  # all pins handled in group layout
        for other in parts:
            if other is part or "crystal_group" in other or other.get("comp_def") is not None:
                continue
            n1, n2 = other["pin_nets"].get("1"), other["pin_nets"].get("2")
            nets = frozenset({n1, n2})
            if nets == frozenset({xin_net, "GND"}):
                other["crystal_group"]      = gid
                other["crystal_role"]       = "cap_xin"
                other["xtal_internal_pins"] = {"1", "2"}
            elif nets == frozenset({xout_net, "GND"}):
                # Cap on the crystal XOUT-side node (intermediate net when series_r present,
                # or MCU XOUT net when no series_r).
                other["crystal_group"]      = gid
                other["crystal_role"]       = "cap_xout"
                other["xtal_internal_pins"] = {"1", "2"}
            elif nets == frozenset({xin_net, xout_net}):
                # Feedback R across both oscillator nodes (no intermediate net).
                other["crystal_group"]      = gid
                other["crystal_role"]       = "resistor"
                other["xtal_internal_pins"] = {"1", "2"}
            elif xout_net in nets and "GND" not in nets and xin_net not in nets:
                # Series R: one pin on the crystal's XOUT-side node (xout_net = intermediate),
                # other pin on the MCU XOUT net.  xout_net is the intermediate net here.
                mcu_xout = next(iter(nets - {xout_net}))
                other["crystal_group"]      = gid
                other["crystal_role"]       = "resistor"
                other["xtal_internal_pins"] = {"1", "2"}
                other["crystal_mcu_xout"]   = mcu_xout  # MCU XOUT net for the GlobalLabel

    return {"name": circuit["name"], "parts": parts, "netlist": netlist,
            "power_nets": power_nets, "local_nets": local_nets}


_UNIT_NORM = {"uF": "µF", "uH": "µH", "Ohm": "Ω", "kOhm": "kΩ"}
_C_LADDER  = [("pF", "p"), ("nF", "n"), ("µF", "u"), ("mF", "m")]
_R_LADDER  = [("Ω", ""),   ("kΩ", "k"), ("MΩ", "M")]
_L_LADDER  = [("nH", "n"), ("µH", "u"), ("mH", "m"), ("H", "H")]
_F_LADDER  = [("Hz", "Hz"), ("kHz", "kHz"), ("MHz", "MHz"), ("GHz", "GHz")]

def _fmt_eng(value: float, unit: str, ladder: list[tuple[str, str]]) -> str:
    """Engineering notation: scale up when ≥ 1000, drop base unit, keep prefix."""
    unit = _UNIT_NORM.get(unit, unit)
    v    = float(value)
    idx  = next((i for i, (u, _) in enumerate(ladder) if u == unit), 0)
    while v >= 1000 and idx < len(ladder) - 1:
        v /= 1000
        idx += 1
    return f"{v:g}{ladder[idx][1]}"

def _fmt_r(r: dict) -> str: return _fmt_eng(r["value"], r["unit"], _R_LADDER)
def _fmt_c(c: dict) -> str: return _fmt_eng(c["value"], c["unit"], _C_LADDER)
def _fmt_l(l: dict) -> str: return _fmt_eng(l["value"], l["unit"], _L_LADDER)
def _fmt_f(f: dict) -> str: return _fmt_eng(f["value"], f["unit"], _F_LADDER)


# ── KiCad symbol loading ──────────────────────────────────────────────────────

_sym_lib_cache: dict[str, SymbolLib] = {}


def _load_kicad_sym(kicad_sym: str, kicad_lib_path: Path):
    """Return a deepcopy of a Symbol from a KiCad standard library.

    If the symbol uses `extends`, the inheritance is flattened in-place:
    the parent's properties are merged with the child's overrides so the
    returned symbol has no `extends` and can be embedded in a schematic
    lib_symbols section without requiring a parent entry.
    """
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

    # Flatten inheritance: merge parent geometry/pins into child overrides
    if sym.extends:
        parent_name = sym.extends
        parent = next((s for s in lib.symbols if s.entryName == parent_name), None)
        if parent:
            parent = copy.deepcopy(parent)
            # Child properties override parent; keep all parent props not in child
            child_prop_keys = {p.key for p in sym.properties}
            for prop in parent.properties:
                if prop.key not in child_prop_keys:
                    sym.properties.append(prop)
            # Inherit geometry (units/pins) from parent, renaming unit prefixes
            # from the parent name to the child name so KiCad accepts them.
            if not sym.units:
                for unit in parent.units:
                    unit.entryName = unit.entryName.replace(parent_name, sym_name, 1)
                sym.units = parent.units
            if not sym.pins:
                sym.pins = parent.pins
        sym.extends = None

    sym.libraryNickname = lib_name
    return sym


def _all_pins(sym) -> list:
    """Collect all SymbolPin objects from a symbol and its sub-units."""
    pins = list(sym.pins)
    for u in sym.units:
        pins.extend(u.pins)
    return pins


def _apply_rotation(px: float, py: float, angle: int) -> tuple[float, float]:
    """Rotate a lib-space offset (px, py) by angle degrees CCW."""
    if angle == 90:   return -py,  px
    if angle == 180:  return -px, -py
    if angle == 270:  return  py, -px
    return px, py  # 0


# ── Placement ─────────────────────────────────────────────────────────────────

# Rail bus layout constants (all in mm, on 1.27 mm KiCad grid)
_BUS_PWR_X     = 25.4   # X of power symbol (fixed left anchor) (20 × 1.27)
_BUS_CAP_X     = 38.1   # X of first decoupling cap             (30 × 1.27)
_BUS_TOP_Y     = 25.4   # Y of top (power) bus wire, row 0      (20 × 1.27)
_BUS_BOT_Y     = 38.1   # Y of bottom (GND) bus wire, row 0    (30 × 1.27)
_BUS_ROW_H     = 25.4   # row pitch between bus rows (20 × 1.27); row 1 top = 25.4+25.4=50.8
_CAP_STEP      = 12.7   # horizontal pitch between caps         (10 × 1.27)
_BUS_TRAIL     = 12.7   # X space from last cap to GND sym      (10 × 1.27)
_CAP_PIN_OFF   = 3.81   # Device:C pin tip offset from centre   (verified from kicad_sym)
_BUS_TO_IC_GAP = 50.8   # horizontal gap: bus right edge → IC   (40 × 1.27)
_BUS_HALF_H    = (_BUS_BOT_Y - _BUS_TOP_Y) / 2  # 6.35 — cap centre to either wire
_XTAL_GND_X  = _BUS_PWR_X   # 25.4 — GND bus X for crystal H-layout (fixed)


def _place(
    parts: list[dict],
) -> tuple[list[tuple[dict, float, float, int]], list[dict], list[dict], list[dict]]:
    """
    Assign schematic positions to all parts.

    Returns:
        placed       – list of (part, x, y, angle)
        bus_specs    – one entry per rail group
        xtal_specs   – one entry per crystal group (for wire drawing)
        filter_specs – one entry per filter group (for horizontal junction wire)
    """
    ics            = [p for p in parts if p.get("comp_def") is not None]
    rail_caps      = [p for p in parts if p.get("comp_def") is None and "rail_group" in p]
    other_passives = [p for p in parts if p.get("comp_def") is None and "rail_group" not in p]

    placed: list[tuple[dict, float, float, int]] = []
    bus_specs: list[dict] = []

    # ── Horizontal bus rows (one row per rail group) ───────────────────────────
    groups: dict[str, list[dict]] = {}
    for cap in rail_caps:
        groups.setdefault(cap["rail_group"], []).append(cap)

    rightmost_x = _BUS_CAP_X
    for row_idx, (net, caps) in enumerate(groups.items()):
        y_top = _BUS_TOP_Y + row_idx * _BUS_ROW_H
        y_bot = _BUS_BOT_Y + row_idx * _BUS_ROW_H
        y_cap = y_top + _BUS_HALF_H          # cap centre, equidistant from both wires

        # Sort caps by ascending pin number for consistent left-to-right ordering
        caps.sort(key=lambda p: p.get("pin_sort", 0))

        first_x = _BUS_CAP_X
        for i, cap in enumerate(caps):
            placed.append((cap, first_x + i * _CAP_STEP, y_cap, 0))
        last_x = first_x + (len(caps) - 1) * _CAP_STEP
        x_pwr  = _BUS_PWR_X                 # fixed power symbol X (far left)
        x_gnd  = last_x  + _BUS_TRAIL       # GND symbol right of last cap
        bus_specs.append({
            "net":     net,
            "gnd":     caps[0]["pin_nets"]["2"],
            "x_pwr":   x_pwr,
            "x_top_r": last_x,
            "x_bot_l": first_x,
            "x_gnd":   x_gnd,
            "y_top":   y_top,
            "y_bot":   y_bot,
            "y_cap":   y_cap,
        })
        rightmost_x = max(rightmost_x, x_gnd)

    n_rows = len(groups)
    last_bot_y = _BUS_BOT_Y + max(0, n_rows - 1) * _BUS_ROW_H

    # ── ICs (right of bus section) ────────────────────────────────────────────
    ic_x = rightmost_x + _BUS_TO_IC_GAP
    ic_y = last_bot_y + 30.48
    for ic in ics:
        placed.append((ic, ic_x, ic_y, 0))
        ic_y += 200.0

    # ── Crystal and filter group separation ──────────────────────────────────
    xtal_group_map: dict[str, dict] = {}
    filter_group_map: dict[str, dict] = {}
    for part in other_passives:
        if gid := part.get("crystal_group"):
            xtal_group_map.setdefault(gid, {})[part.get("crystal_role", "crystal")] = part
        elif fgid := part.get("filter_group"):
            filter_group_map.setdefault(fgid, {})[part.get("filter_role", "unknown")] = part

    regular_passives = [p for p in other_passives
                        if "crystal_group" not in p and "filter_group" not in p]
    xtal_specs: list[dict] = []
    filter_specs: list[dict] = []

    # ── Filter groups (L-filter inline with last bus row) ────────────────────
    # The R sits on the last bus row's top wire; the C sits at y_cap (same as
    # bus decoupling caps), with GND at y_bot.  This keeps all filter elements
    # within the bus section's vertical band, avoiding a separate layout section.
    #
    #  from_net@pwr_sym_x ──wire── [R] ──wire──┬── to_net label (at junction_x+_CAP_STEP)
    #                                           │ (junction at junction_x)
    #                                          [C] (centre at y_cap)
    #                                           │
    #                                          GND@gnd_y
    #
    # All R and C pin wiring is handled in write_kicad_sch() filter section
    # (filter_internal_pins suppresses the standard pin loop).
    last_bus  = bus_specs[-1] if bus_specs else None
    filter_y  = last_bus["y_top"] if last_bus else _BUS_TOP_Y
    filter_cy = last_bus["y_cap"] if last_bus else (_BUS_TOP_Y + _BUS_HALF_H)
    filter_gnd_y = last_bus["y_bot"] if last_bus else _BUS_BOT_Y
    # Start filter after last bus's GND symbol, with one _CAP_STEP gap
    filter_cur_x = (last_bus["x_gnd"] + _CAP_STEP) if last_bus else _BUS_PWR_X

    for group in filter_group_map.values():
        series_r = group.get("series_r")
        shunt_c  = group.get("shunt_c")
        if not series_r or not shunt_c:
            filter_cur_x += _CAP_STEP
            continue
        pwr_sym_x  = filter_cur_x
        r_cx       = pwr_sym_x + _CAP_STEP
        junction_x = r_cx + _CAP_STEP
        label_x    = junction_x + _CAP_STEP          # label connection point (off-right)
        placed.append((series_r, r_cx,       filter_y,  90))
        placed.append((shunt_c,  junction_x, filter_cy,  0))
        filter_specs.append({
            "from_net":   series_r["pin_nets"]["1"],
            "to_net":     series_r["pin_nets"]["2"],
            "gnd_net":    shunt_c["pin_nets"]["2"],
            "pwr_sym_x":  pwr_sym_x,
            "r_pin1_x":   r_cx - _CAP_PIN_OFF,
            "r_pin2_x":   r_cx + _CAP_PIN_OFF,
            "filter_y":   filter_y,
            "junction_x": junction_x,
            "label_x":    label_x,
            "c_pin1_y":   filter_cy - _CAP_PIN_OFF,
            "c_pin2_y":   filter_cy + _CAP_PIN_OFF,
            "gnd_y":      filter_gnd_y,
        })
        filter_cur_x = label_x + _CAP_STEP           # 4 columns per filter group

    # ── Regular passives (tighter 12.7 mm band below bus section) ────────────
    # Each passive occupies one column.  The body is centred at the band midpoint;
    # wire stubs extend ±_CAP_PIN_OFF to the pin tips, then up/down to the
    # on-grid label/power-symbol endpoints (pass_top_y and pass_bot_y).
    pass_start_y = last_bot_y + _BUS_ROW_H
    for i, p in enumerate(regular_passives):
        cx = _BUS_PWR_X + i * _CAP_STEP
        cy = pass_start_y + _CAP_STEP / 2           # centre at mid-band
        p["pass_top_y"] = pass_start_y              # top label/power-sym Y
        p["pass_bot_y"] = pass_start_y + _CAP_STEP  # bottom label/power-sym Y
        placed.append((p, cx, cy, 0))
    last_pass_bot_y = pass_start_y + (_CAP_STEP if regular_passives else 0)

    # ── Crystal H-layout (below regular passives) ─────────────────────────────
    # X positions are on the 12.7 mm grid; Y positions start after the regular
    # passives band so there is no overlap with bus rows or passives.
    xtal_cur_x    = _XTAL_GND_X + _CAP_STEP
    xtal_xin_y    = last_pass_bot_y + _BUS_ROW_H
    xtal_xout_y   = xtal_xin_y + _CAP_STEP
    xtal_center_y = (xtal_xin_y + xtal_xout_y) / 2
    for group in xtal_group_map.values():
        xtal     = group["crystal"]
        cap_xin  = group.get("cap_xin")
        cap_xout = group.get("cap_xout")
        resistor = group.get("resistor")

        # H-layout: Y positions derived from last_bot_y (dynamic); X positions on grid.
        # The crystal's own pins sit at center ± _CAP_PIN_OFF, which differs from
        # the wire Y values, so short vertical stubs connect crystal pins to wires.
        #
        # Crystal at 270°: pin1→(xtal_x, xtal_pin1_y), pin3→(xtal_x, xtal_pin3_y)
        #                  case GND pins → (xtal_x − 5.08, center_y)  [GND sym placed there]
        # Cap at 270°:     pin1→right(cap_x+PIN_OFF, wire_y), pin2→left(cap_x−PIN_OFF, wire_y)
        #                  GND stub bridges from gnd_bus_x to cap pin2
        # R at 90°:        pin1→left(r_x−PIN_OFF, xout_y), pin2→right(r_x+PIN_OFF, xout_y)
        xin_y      = xtal_xin_y
        xout_y     = xtal_xout_y
        cap_x      = xtal_cur_x                        # cap centre X (on 12.7 mm grid)
        gnd_bus_x  = _XTAL_GND_X                       # GND bus always at fixed left anchor
        xtal_x     = cap_x + _CAP_STEP                 # crystal centre X
        xtal_pin1_y = xtal_center_y - _CAP_PIN_OFF     # crystal pin1 Y (above center)
        xtal_pin3_y = xtal_center_y + _CAP_PIN_OFF     # crystal pin3 Y (below center)
        case_gnd_x  = xtal_x - 5.08                    # crystal case GND pin X (at 270°)
        r_x        = (xtal_x + _CAP_STEP) if resistor else None
        lbl_x      = (r_x + _CAP_STEP) if r_x else (xtal_x + _CAP_STEP)

        placed.append((xtal, xtal_x, xtal_center_y, 270))
        if cap_xin:
            placed.append((cap_xin,  cap_x, xin_y,  270))
        if cap_xout:
            placed.append((cap_xout, cap_x, xout_y, 270))
        if resistor and r_x:
            placed.append((resistor, r_x, xout_y, 90))

        # Junctions only at T-intersections (where crystal stub meets a wire that
        # continues on both sides; not needed when the stub end IS the wire endpoint).
        xin_jcts:  list[tuple[float, float]] = [(xtal_x, xin_y)]  if cap_xin  else []
        xout_jcts: list[tuple[float, float]] = [(xtal_x, xout_y)] if cap_xout else []

        # For series-R topology the XOUT label uses the MCU XOUT net name.
        xout_lbl_net = (
            resistor.get("crystal_mcu_xout") if resistor else None
        ) or xtal["pin_nets"]["3"]

        xtal_specs.append({
            "xin_net":        xtal["pin_nets"]["1"],
            "xout_net":       xout_lbl_net,
            "xin_y":          xin_y,
            "xout_y":         xout_y,
            "center_y":       xtal_center_y,
            # XIN wire: continuous from cap pin1 (or xtal_x) to label
            "xin_wire_x0":    (cap_x + _CAP_PIN_OFF) if cap_xin  else xtal_x,
            "xin_wire_x1":    lbl_x,
            # XOUT wire: split by R
            "xout_wire_x0":   (cap_x + _CAP_PIN_OFF) if cap_xout else xtal_x,
            "xout_wire_x1":   (r_x - _CAP_PIN_OFF)   if r_x      else lbl_x,
            "xout_wire2_x0":  (r_x + _CAP_PIN_OFF)   if r_x      else None,
            "xout_wire2_x1":  lbl_x                   if r_x      else None,
            "xin_jcts":       xin_jcts,
            "xout_jcts":      xout_jcts,
            # Vertical stubs: connect crystal pins (at ±_CAP_PIN_OFF from center)
            # down/up to the horizontal wires (which are at _XTAL_XIN_Y / _XTAL_XOUT_Y)
            "xtal_stub_x":    xtal_x,
            "xtal_pin1_y":    xtal_pin1_y,  # stub top end (crystal pin1 Y)
            "xtal_pin3_y":    xtal_pin3_y,  # stub bottom end (crystal pin3 Y)
            # GND bus (vertical wire) and horizontal stubs to cap GND pins
            "gnd_bus_x":      gnd_bus_x,
            "gnd_bus_y0":     xin_y,
            "gnd_bus_y1":     xout_y,
            # Stub from gnd_bus_x to cap's GND pin (cap_x − PIN_OFF); None if no cap
            "gnd_stub_xin":   (cap_x - _CAP_PIN_OFF) if cap_xin  else None,
            "gnd_stub_xout":  (cap_x - _CAP_PIN_OFF) if cap_xout else None,
            # Crystal case GND: GND symbol placed directly at pin (no stub to bus)
            "case_gnd_x":     case_gnd_x,
            "case_gnd_y":     xtal_center_y,
            "xin_lbl_x":      lbl_x,
            "xout_lbl_x":     lbl_x,
        })

        n_cols = 2 + (1 if resistor else 0)
        xtal_cur_x += (n_cols + 1) * _CAP_STEP
        rightmost_x = max(rightmost_x, xtal_cur_x)


    return placed, bus_specs, xtal_specs, filter_specs


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
    from kiutils.items.common import PageSettings
    sch = Schematic.create_new()
    sch.paper = PageSettings(paperSize="A3")
    added_syms: set[str] = set()
    label_effects = Effects(font=Font(height=1.27, width=1.27))
    power_nets = resolved.get("power_nets", set())
    local_nets = resolved.get("local_nets", set())
    pwr_counter = [0]

    placed, bus_specs, xtal_specs, filter_specs = _place(resolved["parts"])
    for part, cx, cy, sym_angle in placed:
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
            if part.get("comp_def") is None:  # passive: hide pin numbers and names
                lib_sym_copy.hidePinNumbers = True
                lib_sym_copy.pinNamesHide   = True
            sch.libSymbols.append(lib_sym_copy)
            added_syms.add(kicad_sym)

        # Build placed instance
        inst = SchematicSymbol()
        inst.libId = kicad_sym
        inst.position = Position(X=cx, Y=cy, angle=sym_angle)
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
            rpx, rpy = _apply_rotation(prop.position.X, prop.position.Y, sym_angle)
            prop.position.X = cx + rpx
            prop.position.Y = cy - rpy
            if prop.key == "Reference":
                prop.value = ref_val
            elif prop.key == "Value":
                prop.value = value
            else:
                if prop.effects is None:
                    prop.effects = Effects(font=Font(height=1.27, width=1.27))
                prop.effects.hide = True

        # Crystal: Reference/Value to the right of crystal body, angle=90.
        # Positions verified against manually placed reference schematic.
        if part.get("type") == "crystal":
            for prop in inst.properties:
                if prop.key == "Reference":
                    if prop.effects is None:
                        prop.effects = Effects(font=Font(height=1.27, width=1.27))
                    prop.position = Position(X=cx + 4.318, Y=cy - 1.016, angle=90)
                    prop.effects.justify = Justify(horizontally="left")
                elif prop.key == "Value":
                    if prop.effects is None:
                        prop.effects = Effects(font=Font(height=1.27, width=1.27))
                    prop.position = Position(X=cx + 4.318, Y=cy + 1.016, angle=90)
                    prop.effects.justify = Justify(horizontally="left")

        # Crystal-group caps at 270°: Reference top-left, Value bottom-right, angle=90.
        # Positions verified against manually placed reference schematic.
        elif part.get("crystal_group") and part.get("type") == "capacitor":
            for prop in inst.properties:
                if prop.key == "Reference":
                    if prop.effects is None:
                        prop.effects = Effects(font=Font(height=1.27, width=1.27))
                    prop.position = Position(X=cx - 1.27, Y=cy - 0.508, angle=90)
                    prop.effects.justify = Justify(horizontally="right", vertically="bottom")
                elif prop.key == "Value":
                    if prop.effects is None:
                        prop.effects = Effects(font=Font(height=1.27, width=1.27))
                    prop.position = Position(X=cx + 1.27, Y=cy + 0.508, angle=90)
                    prop.effects.justify = Justify(horizontally="left", vertically="top")

        # Pin annotation for rail-bus caps: rotated text below the bottom bus wire.
        # Skipped for other passives — their GlobalLabels already identify the net.
        if "pin_annotation" in part and "rail_group" in part:
            ann = Property(key="Pin", value=part["pin_annotation"])
            ann.id = len(inst.properties)
            ann.position = Position(X=cx, Y=cy + _BUS_HALF_H + 1.27, angle=270)
            ann.effects = Effects(font=Font(height=1.0, width=1.0))
            ann.effects.justify = Justify(horizontally="left")
            inst.properties.append(ann)

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

        pin_to_local_net: dict[str, str] = {}  # reserved for future local-net routing

        # For passives: swap pin_nets if pin 2 has higher priority than pin 1
        # so that power supplies end up at the top (pin 1) and GND at the bottom (pin 2).
        pin_nets = part["pin_nets"]
        if part.get("comp_def") is None and len(pin_nets) == 2:
            n1, n2 = pin_nets.get("1"), pin_nets.get("2")
            if n1 and n2 and _net_priority(n2, power_nets) > _net_priority(n1, power_nets):
                pin_nets = {"1": n2, "2": n1}
        for p in _all_pins(lib_sym):
            # Match by pin name (ICs) or pin number (passives, where name is '~')
            net = pin_nets.get(p.name) or pin_nets.get(p.number)
            if net is None:
                continue

            # Rail bus caps connect via the horizontal bus wire — skip individual labels.
            if "rail_group" in part:
                continue

            # Crystal group internal pins are wired within the section — skip labels.
            if p.number in part.get("xtal_internal_pins", set()):
                continue

            # Filter group: all wiring and labels handled in the filter section below.
            if p.number in part.get("filter_internal_pins", set()):
                continue

            rpx, rpy = _apply_rotation(p.position.X, p.position.Y, sym_angle)
            pin_x = cx + rpx
            pin_y = cy - rpy

            # Regular passives: draw a vertical stub from the pin tip to the
            # grid-aligned label/power-symbol endpoint (pass_top_y or pass_bot_y).
            pass_top_y: float | None = part.get("pass_top_y")
            pass_bot_y: float | None = part.get("pass_bot_y")
            if pass_top_y is not None and pass_bot_y is not None:
                lbl_y: float = pass_top_y if pin_y <= cy else pass_bot_y
                if abs(lbl_y - pin_y) > 0.001:
                    sch.graphicalItems.append(_make_wire(pin_x, pin_y, pin_x, lbl_y))
            else:
                lbl_y = pin_y

            lib_angle = p.position.angle or 0
            # Label extends opposite to pin stub direction; Y-axis is flipped
            # between lib (Y-up) and schematic (Y-down), so add sym_angle and 180.
            lbl_angle = int((lib_angle + sym_angle + 180) % 360)

            # Direction vector pointing away from IC (in schematic / screen coords).
            rad = math.radians(lbl_angle)
            dx = math.cos(rad)
            dy = -math.sin(rad)

            # Power rail nets use a power symbol (body up for supply, down for GND)
            if net in power_nets:
                local_net = pin_to_local_net.get(str(p.number))
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
                    if _place_power_symbol(sch, net, pin_x, lbl_y,
                                           kicad_lib_path, added_syms, pwr_counter):
                        continue
                    # Symbol not found in library — fall through to GlobalLabel

            # Non-power nets: use LocalLabel for local decoupling nets, GlobalLabel otherwise.
            justify = "right" if lbl_angle in (180, 270) else "left"
            if net in local_nets:
                sch.labels.append(
                    _make_local_label(net, pin_x, lbl_y, lbl_angle, label_effects)
                )
            else:
                lbl = GlobalLabel()
                lbl.text = net
                lbl.shape = pin_shapes.get(p.name, pin_shapes.get(p.number, "passive"))
                lbl.position = Position(X=pin_x, Y=lbl_y, angle=lbl_angle)
                lbl.fieldsAutoplaced = True
                lbl.effects = copy.deepcopy(label_effects)
                lbl.effects.justify = Justify(horizontally=justify)
                lbl.uuid = str(uuid.uuid4())
                sch.globalLabels.append(lbl)

    # ── Horizontal rail bus wires + vertical stubs + junction markers ─────────
    for bus in bus_specs:
        top_y   = bus["y_top"]
        bot_y   = bus["y_bot"]
        cap_y   = bus["y_cap"]
        first_x = bus["x_bot_l"]
        last_x  = bus["x_top_r"]

        # Horizontal bus wires
        sch.graphicalItems.append(_make_wire(bus["x_pwr"], top_y, last_x,        top_y))
        sch.graphicalItems.append(_make_wire(first_x,      bot_y, bus["x_gnd"],  bot_y))

        n_caps = int(round((last_x - first_x) / _CAP_STEP)) + 1
        for i in range(n_caps):
            cap_x = first_x + i * _CAP_STEP
            # Vertical stubs connecting cap pins to bus wires
            sch.graphicalItems.append(_make_wire(cap_x, top_y, cap_x, cap_y - _CAP_PIN_OFF))
            sch.graphicalItems.append(_make_wire(cap_x, cap_y + _CAP_PIN_OFF, cap_x, bot_y))
            # Junctions at T-intersections on top wire (not at right endpoint)
            if i < n_caps - 1:
                j = Junction()
                j.position = Position(X=cap_x, Y=top_y)
                j.uuid = str(uuid.uuid4())
                sch.junctions.append(j)
            # Junctions at T-intersections on bottom wire (not at left endpoint)
            if i > 0:
                j = Junction()
                j.position = Position(X=cap_x, Y=bot_y)
                j.uuid = str(uuid.uuid4())
                sch.junctions.append(j)
        # Supply symbol (or GlobalLabel fallback) at left end of top wire
        if not _place_power_symbol(sch, bus["net"], bus["x_pwr"], top_y,
                                   kicad_lib_path, added_syms, pwr_counter):
            lbl = GlobalLabel()
            lbl.text = bus["net"]
            lbl.shape = "passive"
            lbl.position = Position(X=bus["x_pwr"], Y=top_y, angle=180)
            lbl.fieldsAutoplaced = True
            lbl.effects = copy.deepcopy(label_effects)
            lbl.effects.justify = Justify(horizontally="right")
            lbl.uuid = str(uuid.uuid4())
            sch.globalLabels.append(lbl)
        # GND symbol (or GlobalLabel fallback) at right end of bottom wire
        if not _place_power_symbol(sch, bus["gnd"], bus["x_gnd"], bot_y,
                                   kicad_lib_path, added_syms, pwr_counter):
            lbl = GlobalLabel()
            lbl.text = bus["gnd"]
            lbl.shape = "passive"
            lbl.position = Position(X=bus["x_gnd"], Y=bot_y, angle=0)
            lbl.fieldsAutoplaced = True
            lbl.effects = copy.deepcopy(label_effects)
            lbl.effects.justify = Justify(horizontally="left")
            lbl.uuid = str(uuid.uuid4())
            sch.globalLabels.append(lbl)

    # ── Crystal section wires + boundary GlobalLabels ─────────────────────────
    for xtal in xtal_specs:
        xin_y    = xtal["xin_y"]
        xout_y   = xtal["xout_y"]
        gnd_x    = xtal["gnd_bus_x"]
        stub_x   = xtal["xtal_stub_x"]

        # XIN wire (top, continuous)
        sch.graphicalItems.append(_make_wire(
            xtal["xin_wire_x0"], xin_y, xtal["xin_wire_x1"], xin_y))
        # XOUT wire seg1
        sch.graphicalItems.append(_make_wire(
            xtal["xout_wire_x0"], xout_y, xtal["xout_wire_x1"], xout_y))
        # XOUT wire seg2 (only when R present)
        if xtal["xout_wire2_x0"] is not None:
            sch.graphicalItems.append(_make_wire(
                xtal["xout_wire2_x0"], xout_y, xtal["xout_wire2_x1"], xout_y))

        # Crystal-to-wire vertical stubs (crystal pins are at ±_CAP_PIN_OFF from
        # center, while the wires sit at _XTAL_XIN_Y / _XTAL_XOUT_Y)
        sch.graphicalItems.append(_make_wire(stub_x, xtal["xtal_pin1_y"], stub_x, xin_y))
        sch.graphicalItems.append(_make_wire(stub_x, xtal["xtal_pin3_y"], stub_x, xout_y))

        # Junctions at T-intersections on the horizontal wires
        for jx, jy in xtal["xin_jcts"] + xtal["xout_jcts"]:
            j = Junction()
            j.position = Position(X=jx, Y=jy)
            j.uuid = str(uuid.uuid4())
            sch.junctions.append(j)

        # Horizontal GND stubs: bridge from GND bus to each cap's GND pin
        if xtal["gnd_stub_xin"] is not None:
            sch.graphicalItems.append(_make_wire(gnd_x, xin_y, xtal["gnd_stub_xin"], xin_y))
        if xtal["gnd_stub_xout"] is not None:
            sch.graphicalItems.append(_make_wire(gnd_x, xout_y, xtal["gnd_stub_xout"], xout_y))

        # Vertical GND bus (connects stub endpoints on each side)
        sch.graphicalItems.append(_make_wire(gnd_x, xin_y, gnd_x, xout_y))
        # GND symbol at bottom of bus
        _place_power_symbol(sch, "GND", gnd_x, xout_y,
                            kicad_lib_path, added_syms, pwr_counter)

        # GND symbol directly at crystal case GND pins (no stub to bus needed)
        _place_power_symbol(sch, "GND", xtal["case_gnd_x"], xtal["case_gnd_y"],
                            kicad_lib_path, added_syms, pwr_counter)

        # XIN GlobalLabel on RIGHT — shape "output" (crystal drives the MCU's XIN input)
        lbl_xin = GlobalLabel()
        lbl_xin.text  = xtal["xin_net"]
        lbl_xin.shape = "output"
        lbl_xin.position = Position(X=xtal["xin_lbl_x"], Y=xin_y, angle=0)
        lbl_xin.fieldsAutoplaced = True
        lbl_xin.effects = copy.deepcopy(label_effects)
        lbl_xin.effects.justify = Justify(horizontally="left")
        lbl_xin.uuid = str(uuid.uuid4())
        sch.globalLabels.append(lbl_xin)

        # XOUT GlobalLabel on RIGHT — shape "input" (MCU oscillator output drives crystal)
        lbl_xout = GlobalLabel()
        lbl_xout.text  = xtal["xout_net"]
        lbl_xout.shape = "input"
        lbl_xout.position = Position(X=xtal["xout_lbl_x"], Y=xout_y, angle=0)
        lbl_xout.fieldsAutoplaced = True
        lbl_xout.effects = copy.deepcopy(label_effects)
        lbl_xout.effects.justify = Justify(horizontally="left")
        lbl_xout.uuid = str(uuid.uuid4())
        sch.globalLabels.append(lbl_xout)

    # ── Filter group wires, symbols, and labels ──────────────────────────────
    # Horizontal L-filter:  from_net ──[R]──┬──── to_net
    #                                        │
    #                                       [C]
    #                                        │
    #                                       GND
    for filt in filter_specs:
        fy        = filt["filter_y"]
        jx        = filt["junction_x"]
        r_pin1_x  = filt["r_pin1_x"]
        r_pin2_x  = filt["r_pin2_x"]
        c_pin1_y  = filt["c_pin1_y"]
        c_pin2_y  = filt["c_pin2_y"]
        gnd_y     = filt["gnd_y"]

        pwr_sym_x = filt["pwr_sym_x"]

        # from_net power symbol at on-grid pwr_sym_x; wire to R's off-grid left pin
        if not _place_power_symbol(sch, filt["from_net"], pwr_sym_x, fy,
                                   kicad_lib_path, added_syms, pwr_counter):
            lbl = GlobalLabel()
            lbl.text  = filt["from_net"]
            lbl.shape = "passive"
            lbl.position = Position(X=pwr_sym_x, Y=fy, angle=180)
            lbl.fieldsAutoplaced = True
            lbl.effects = copy.deepcopy(label_effects)
            lbl.effects.justify = Justify(horizontally="right")
            lbl.uuid = str(uuid.uuid4())
            sch.globalLabels.append(lbl)
        # Wire: on-grid power symbol → off-grid R pin1
        sch.graphicalItems.append(_make_wire(pwr_sym_x, fy, r_pin1_x, fy))

        label_x = filt["label_x"]

        # Wire: off-grid R pin2 → on-grid junction
        sch.graphicalItems.append(_make_wire(r_pin2_x, fy, jx, fy))

        # Wire: junction → label connection point (one grid step right for readability)
        sch.graphicalItems.append(_make_wire(jx, fy, label_x, fy))

        # to_net GlobalLabel one grid step right of junction
        lbl = GlobalLabel()
        lbl.text  = filt["to_net"]
        lbl.shape = "passive"
        lbl.position = Position(X=label_x, Y=fy, angle=0)
        lbl.fieldsAutoplaced = True
        lbl.effects = copy.deepcopy(label_effects)
        lbl.effects.justify = Justify(horizontally="left")
        lbl.uuid = str(uuid.uuid4())
        sch.globalLabels.append(lbl)

        # Junction dot where horizontal wire meets vertical C stub
        j = Junction()
        j.position = Position(X=jx, Y=fy)
        j.uuid = str(uuid.uuid4())
        sch.junctions.append(j)

        # Vertical wire: junction down to C's top pin
        sch.graphicalItems.append(_make_wire(jx, fy, jx, c_pin1_y))

        # Vertical wire: C's bottom pin down to GND endpoint (on grid)
        sch.graphicalItems.append(_make_wire(jx, c_pin2_y, jx, gnd_y))

        # GND power symbol (or GlobalLabel) at the bottom
        if not _place_power_symbol(sch, filt["gnd_net"], jx, gnd_y,
                                   kicad_lib_path, added_syms, pwr_counter):
            lbl = GlobalLabel()
            lbl.text  = filt["gnd_net"]
            lbl.shape = "passive"
            lbl.position = Position(X=jx, Y=gnd_y, angle=270)
            lbl.fieldsAutoplaced = True
            lbl.effects = copy.deepcopy(label_effects)
            lbl.effects.justify = Justify(horizontally="left")
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

    doc = yaml.safe_load(args.circuit.read_text())

    try:
        if "project" in doc:
            out_dir = args.output or args.circuit.parent
            write_kicad_project(args.circuit, lib_paths, out_dir, kicad_lib_path)
        else:
            resolved = resolve(args.circuit, lib_paths)
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
    except GenerateError as e:
        print(f"✗ {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
