"""Schematic placement engine for KiCad output.

Assigns (x, y, angle) coordinates to all parts in a resolved circuit,
grouping them into rail bus rows, crystal H-layouts, L-filter sections,
and regular passive columns.
"""

# ── Symbol name mapping ───────────────────────────────────────────────────────

_PASSIVE_SYM = {
    "resistor": "Device:R",
    "capacitor": "Device:C",
    "inductor": "Device:L",
    "crystal":  "Device:Crystal_GND24",
}

# ── Grid constants (all in mm, on 1.27 mm KiCad grid) ────────────────────────

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
_XTAL_GND_X    = _BUS_PWR_X   # 25.4 — GND bus X for crystal H-layout (fixed)


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
