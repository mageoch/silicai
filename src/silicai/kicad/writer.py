"""KiCad schematic writer for SilicAI resolved circuits."""

import sys
import copy
import math
import uuid
from pathlib import Path

from kiutils.schematic import Schematic, Junction
from kiutils.symbol import SymbolLib
from kiutils.items.schitems import (
    SchematicSymbol, GlobalLabel, LocalLabel, Connection,
)
from kiutils.items.common import Position, Effects, Font, Justify, Property

from silicai.generate import GenerateError, _net_priority
from silicai.kicad.layout import _place, _PASSIVE_SYM, _BUS_HALF_H, _CAP_PIN_OFF, _CAP_STEP

# ── Defaults ──────────────────────────────────────────────────────────────────
_DEFAULT_KICAD_SYM = Path("/usr/share/kicad/symbols")

# KiCad GlobalLabel shapes match the pin direction on the IC.
_DIR_TO_SHAPE = {
    "input":         "input",
    "output":        "output",
    "bidirectional": "bidirectional",
    "power_in":      "passive",
    "power_out":     "passive",
    "open_drain":    "output",
}

_WIRE_STUB_LEN = 15.24  # mm (12 × 1.27 mm grid units) — space between IC pin and power symbol

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


def _make_wire(x0: float, y0: float, x1: float, y1: float) -> Connection:
    """Create a schematic wire segment between two points."""
    wire = Connection(type="wire")
    wire.points = [Position(X=x0, Y=y0), Position(X=x1, Y=y1)]
    wire.uuid = str(uuid.uuid4())
    return wire


_FONT_H = 1.0  # mm — standard schematic font height


def _text_width_mm(text: str, font_height: float = _FONT_H) -> float:
    """Approximate rendered width of text using KiCad's Newstroke font metrics.
    Each character is ~0.8× font height; margins add ~0.2× on top."""
    return len(text) * 0.8 * font_height + 0.2 * font_height


def _make_local_label(text: str, x: float, y: float, lbl_angle: int) -> LocalLabel:
    """Create a LocalLabel (sheet-scoped net label) at the given position.
    Font is fixed at _FONT_H. Text floats above the wire (vertically=bottom)
    and is justified toward the wire direction (right for left-pointing, left for right-pointing).
    """
    justify_h = "left" if lbl_angle in (180, 270) else "right"
    lbl = LocalLabel()
    lbl.text = text
    lbl.position = Position(X=x, Y=y, angle=lbl_angle)
    lbl.fieldsAutoplaced = True
    lbl.effects = Effects(font=Font(height=_FONT_H, width=_FONT_H))
    lbl.effects.justify = Justify(horizontally=justify_h, vertically="bottom")
    lbl.uuid = str(uuid.uuid4())
    return lbl


_GRID = 1.27  # mm — schematic grid unit


def _place_local_label(sch, text: str, x: float, y: float, lbl_angle: int) -> None:
    """Place a local label + wire stub at (x, y).
    The stub extends from the anchor in the label direction by text_width rounded up
    to the nearest grid unit, plus one extra grid unit beyond the text.
    """
    raw = _text_width_mm(text)
    stub_len = math.ceil(raw / _GRID) * _GRID + _GRID
    rad = math.radians(lbl_angle)
    dx = math.cos(rad)
    dy = -math.sin(rad)  # Y-down screen coords
    end_x = x + dx * stub_len
    end_y = y + dy * stub_len
    sch.graphicalItems.append(_make_wire(x, y, end_x, end_y))
    sch.labels.append(_make_local_label(text, end_x, end_y, lbl_angle))


def _make_global_label(text: str, x: float, y: float, lbl_angle: int,
                       shape: str, label_effects: Effects) -> GlobalLabel:
    """Create a GlobalLabel (project-wide net label) at the given position."""
    justify = "right" if lbl_angle in (180, 270) else "left"
    lbl = GlobalLabel()
    lbl.text = text
    lbl.shape = shape
    lbl.position = Position(X=x, Y=y, angle=lbl_angle)
    lbl.fieldsAutoplaced = True
    lbl.effects = copy.deepcopy(label_effects)
    lbl.effects.justify = Justify(horizontally=justify)
    lbl.uuid = str(uuid.uuid4())
    return lbl


def _place_power_symbol(
    sch,
    net: str,
    px: float,
    py: float,
    kicad_lib_path: Path,
    added_syms: set[str],
    pwr_counter: list[int],
    lbl_angle: int = 0,
) -> bool:
    """
    Place a KiCad power symbol (power:{net}) at (px, py).
    Angle=0: GND body extends downward, VCC-like body extends upward.
    lbl_angle: if 90 or 270 (top/bottom IC pin), rotate and reposition the Value
    label text so it reads vertically (angle=90, bottom-to-top) at a fixed
    2.794 mm offset from the pin, with left/right justify respectively.
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
                prop.effects = Effects(font=Font(height=_FONT_H, width=_FONT_H))
            prop.effects.hide = True
        elif prop.key == "Value":
            prop.value = net
            if lbl_angle in (90, 270):
                prop.position.Y = py + (2.794 if lbl_angle == 270 else -2.794)
                prop.position.angle = 90
                if prop.effects is None:
                    prop.effects = Effects(font=Font(height=_FONT_H, width=_FONT_H))
                prop.effects.justify = Justify(
                    horizontally="right" if lbl_angle == 270 else "left"
                )
        else:
            if prop.effects is None:
                prop.effects = Effects(font=Font(height=_FONT_H, width=_FONT_H))
            prop.effects.hide = True

    all_lib_pins = _all_pins(lib_sym)
    for p in all_lib_pins:
        inst.pins[p.number] = str(uuid.uuid4())
    if not all_lib_pins:
        inst.pins["1"] = str(uuid.uuid4())

    sch.schematicSymbols.append(inst)
    return True


# ── KiCad schematic writer ────────────────────────────────────────────────────

def write_kicad_sch(
    resolved: dict,
    output: Path,
    kicad_lib_path: Path,
    global_nets: set[str] | None = None,
) -> None:
    from kiutils.items.common import PageSettings
    sch = Schematic.create_new()
    sch.paper = PageSettings(paperSize="A3")
    added_syms: set[str] = set()
    label_effects = Effects(font=Font(height=_FONT_H, width=_FONT_H))
    power_nets = resolved.get("power_nets", set())
    _global_nets = global_nets or set()
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
                    prop.effects = Effects(font=Font(height=_FONT_H, width=_FONT_H))
                prop.effects.hide = True

        # Crystal: Reference/Value to the right of crystal body, angle=90.
        # Positions verified against manually placed reference schematic.
        if part.get("type") == "crystal":
            for prop in inst.properties:
                if prop.key == "Reference":
                    if prop.effects is None:
                        prop.effects = Effects(font=Font(height=_FONT_H, width=_FONT_H))
                    prop.position = Position(X=cx + 4.318, Y=cy - 1.016, angle=90)
                    prop.effects.justify = Justify(horizontally="left")
                elif prop.key == "Value":
                    if prop.effects is None:
                        prop.effects = Effects(font=Font(height=_FONT_H, width=_FONT_H))
                    prop.position = Position(X=cx + 4.318, Y=cy + 1.016, angle=90)
                    prop.effects.justify = Justify(horizontally="left")

        # Crystal-group caps at 270°: Reference top-left, Value bottom-right, angle=90.
        # Positions verified against manually placed reference schematic.
        elif part.get("crystal_group") and part.get("type") == "capacitor":
            for prop in inst.properties:
                if prop.key == "Reference":
                    if prop.effects is None:
                        prop.effects = Effects(font=Font(height=_FONT_H, width=_FONT_H))
                    prop.position = Position(X=cx - 1.27, Y=cy - 0.508, angle=90)
                    prop.effects.justify = Justify(horizontally="right", vertically="bottom")
                elif prop.key == "Value":
                    if prop.effects is None:
                        prop.effects = Effects(font=Font(height=_FONT_H, width=_FONT_H))
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

        pin_to_local_net: dict[str, str] = {}  # future: map pin → local decoupling net name

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
                    _place_local_label(sch, local_net, pin_x, pin_y, lbl_angle)
                    _place_power_symbol(sch, net, end_x, end_y,
                                        kicad_lib_path, added_syms, pwr_counter,
                                        lbl_angle=lbl_angle)
                    continue  # handled — skip GlobalLabel fallback regardless
                else:
                    if _place_power_symbol(sch, net, pin_x, lbl_y,
                                           kicad_lib_path, added_syms, pwr_counter,
                                           lbl_angle=lbl_angle):
                        continue
                    # Symbol not found in library — fall through to GlobalLabel

            # Non-power nets: GlobalLabel if shared across sheets, LocalLabel otherwise.
            shape = pin_shapes.get(p.name, pin_shapes.get(p.number, "passive"))
            if net in _global_nets:
                sch.globalLabels.append(
                    _make_global_label(net, pin_x, lbl_y, lbl_angle, shape, label_effects)
                )
            else:
                _place_local_label(sch, net, pin_x, lbl_y, lbl_angle)

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
        # Supply symbol (or label fallback) at left end of top wire
        if not _place_power_symbol(sch, bus["net"], bus["x_pwr"], top_y,
                                   kicad_lib_path, added_syms, pwr_counter):
            if bus["net"] in _global_nets:
                sch.globalLabels.append(
                    _make_global_label(bus["net"], bus["x_pwr"], top_y, 180, "passive", label_effects)
                )
            else:
                _place_local_label(sch, bus["net"], bus["x_pwr"], top_y, 180)
        # GND symbol (or label fallback) at right end of bottom wire
        if not _place_power_symbol(sch, bus["gnd"], bus["x_gnd"], bot_y,
                                   kicad_lib_path, added_syms, pwr_counter):
            if bus["gnd"] in _global_nets:
                sch.globalLabels.append(
                    _make_global_label(bus["gnd"], bus["x_gnd"], bot_y, 0, "passive", label_effects)
                )
            else:
                _place_local_label(sch, bus["gnd"], bus["x_gnd"], bot_y, 0)

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

        # XIN label on RIGHT — shape "output" (crystal drives the MCU's XIN input)
        xin_net = xtal["xin_net"]
        if xin_net in _global_nets:
            sch.globalLabels.append(
                _make_global_label(xin_net, xtal["xin_lbl_x"], xin_y, 0, "output", label_effects)
            )
        else:
            _place_local_label(sch, xin_net, xtal["xin_lbl_x"], xin_y, 0)

        # XOUT label on RIGHT — shape "input" (MCU oscillator output drives crystal)
        xout_net = xtal["xout_net"]
        if xout_net in _global_nets:
            sch.globalLabels.append(
                _make_global_label(xout_net, xtal["xout_lbl_x"], xout_y, 0, "input", label_effects)
            )
        else:
            _place_local_label(sch, xout_net, xtal["xout_lbl_x"], xout_y, 0)

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
            if filt["from_net"] in _global_nets:
                sch.globalLabels.append(
                    _make_global_label(filt["from_net"], pwr_sym_x, fy, 180, "passive", label_effects)
                )
            else:
                _place_local_label(sch, filt["from_net"], pwr_sym_x, fy, 180)
        # Wire: on-grid power symbol → off-grid R pin1
        sch.graphicalItems.append(_make_wire(pwr_sym_x, fy, r_pin1_x, fy))

        label_x = filt["label_x"]

        # Wire: off-grid R pin2 → on-grid junction
        sch.graphicalItems.append(_make_wire(r_pin2_x, fy, jx, fy))

        # Wire: junction → label connection point (one grid step right for readability)
        sch.graphicalItems.append(_make_wire(jx, fy, label_x, fy))

        # to_net label one grid step right of junction
        if filt["to_net"] in _global_nets:
            sch.globalLabels.append(
                _make_global_label(filt["to_net"], label_x, fy, 0, "passive", label_effects)
            )
        else:
            _place_local_label(sch, filt["to_net"], label_x, fy, 0)

        # Junction dot where horizontal wire meets vertical C stub
        j = Junction()
        j.position = Position(X=jx, Y=fy)
        j.uuid = str(uuid.uuid4())
        sch.junctions.append(j)

        # Vertical wire: junction down to C's top pin
        sch.graphicalItems.append(_make_wire(jx, fy, jx, c_pin1_y))

        # Vertical wire: C's bottom pin down to GND endpoint (on grid)
        sch.graphicalItems.append(_make_wire(jx, c_pin2_y, jx, gnd_y))

        # GND power symbol (or label fallback) at the bottom
        if not _place_power_symbol(sch, filt["gnd_net"], jx, gnd_y,
                                   kicad_lib_path, added_syms, pwr_counter):
            if filt["gnd_net"] in _global_nets:
                sch.globalLabels.append(
                    _make_global_label(filt["gnd_net"], jx, gnd_y, 270, "passive", label_effects)
                )
            else:
                _place_local_label(sch, filt["gnd_net"], jx, gnd_y, 270)

    sch.to_file(str(output))
    print(f"✓ {output}")
