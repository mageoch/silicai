#!/usr/bin/env python3
"""Resolve SilicAI circuit definitions into flat parts lists and netlists."""

import sys
import tomllib
import argparse
from pathlib import Path

import yaml
from importlib.resources import files

# ── Defaults ──────────────────────────────────────────────────────────────────
_DEFAULT_KICAD_SYM = Path("/usr/share/kicad/symbols")

_BUILTIN_COMPONENTS = Path(str(files("silicai").joinpath("components")))

# Bus types whose open-drain signals are managed by a circuit-level pull_ups definition.
# Component-level scope:bus pull-ups are skipped for these to avoid double-placement.
_BUS_LEVEL_PULL_UP_TYPES: frozenset[str] = frozenset({"I2C", "SMBus"})


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
                pin_net = pin_nets.get(p["name"]) or pin_nets.get(str(p["number"]))
                if pin_net is None:
                    continue
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
                other["crystal_group"]      = gid
                other["crystal_role"]       = "resistor"
                other["xtal_internal_pins"] = {"1", "2"}
                other["crystal_mcu_xout"]   = next(iter(nets - {xout_net}))

    return {"name": circuit["name"], "parts": parts, "netlist": netlist,
            "power_nets": power_nets}


# ── Value formatters ──────────────────────────────────────────────────────────

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


# ── Net priority ──────────────────────────────────────────────────────────────

def _net_priority(net: str, power_nets: set[str]) -> int:
    """Return ordering priority for passive pin assignment.
    Power supply (2) goes to the top pin, GND (0) to the bottom, signals in between."""
    if net in power_nets and net != "GND":
        return 2
    if net == "GND":
        return 0
    return 1


# ── CLI ───────────────────────────────────────────────────────────────────────

_FORMATS = ["kicad"]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate schematics from SilicAI circuit definitions"
    )
    parser.add_argument("circuit", type=Path)
    parser.add_argument("-o", "--output", type=Path, default=None)
    parser.add_argument("--project-dir", type=Path, default=None)
    parser.add_argument(
        "--format", choices=_FORMATS, default="kicad",
        help="Output format (default: kicad)",
    )
    args = parser.parse_args()

    project_dir = args.project_dir or Path.cwd()
    config = load_config(project_dir)
    lib_paths = [_BUILTIN_COMPONENTS] + [
        (project_dir / entry["path"]).resolve()
        for entry in config.get("component_libraries", [])
    ]

    doc = yaml.safe_load(args.circuit.read_text())

    try:
        if args.format == "kicad":
            from silicai.kicad.writer import write_kicad_sch
            from silicai.kicad.project import write_kicad_project
            kicad_lib_path = Path(config.get("kicad_library_path", str(_DEFAULT_KICAD_SYM)))
            if "project" in doc:
                out_dir = args.output or args.circuit.parent
                write_kicad_project(args.circuit, lib_paths, out_dir, kicad_lib_path)
            else:
                output = args.output or args.circuit.with_suffix(".kicad_sch")
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
