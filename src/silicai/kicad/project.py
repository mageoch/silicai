"""KiCad project generation for SilicAI multi-circuit projects."""

import uuid
from pathlib import Path

import yaml
from kiutils.schematic import Schematic
from kiutils.items.schitems import (
    HierarchicalSheet, HierarchicalSheetInstance,
    HierarchicalSheetProjectInstance, HierarchicalSheetProjectPath,
)
from kiutils.items.common import Position, Effects, Font, Justify, Property

from silicai.generate import resolve, _BUILTIN_COMPONENTS
from silicai.kicad.writer import write_kicad_sch, _DEFAULT_KICAD_SYM


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

    # ── Pass 1: resolve all circuits ──────────────────────────────────────────
    all_resolved: list[tuple[Path, dict, dict]] = []
    for circuit_rel in proj["circuits"]:
        circuit_path = (project_path.parent / circuit_rel).resolve()
        circuit_doc = yaml.safe_load(circuit_path.read_text())
        resolved = resolve(circuit_path, lib_paths,
                           shared=shared, placed_bus_pullups=placed_bus_pullups)
        all_resolved.append((circuit_path, circuit_doc, resolved))

    # Nets that appear in more than one circuit must use GlobalLabel so KiCad
    # connects them across sheets.
    from collections import Counter
    net_counts: Counter = Counter()
    for _, _, resolved in all_resolved:
        net_counts.update(resolved["netlist"].keys())
    global_nets: set[str] = {net for net, count in net_counts.items() if count > 1}

    # ── Pass 2: write each circuit sub-sheet ──────────────────────────────────
    sub_sheets: list[dict] = []
    for circuit_path, circuit_doc, resolved in all_resolved:
        circuit_name = circuit_doc["circuit"]["name"]
        out_name = circuit_path.stem + ".kicad_sch"
        out_path = output_dir / out_name
        write_kicad_sch(resolved, out_path, kicad_lib_path, global_nets=global_nets)
        sub_sheets.append({"name": circuit_name, "file": out_name, "uuid": str(uuid.uuid4())})

    # ── Generate root schematic ────────────────────────────────────────────────
    root_path = output_dir / f"{slug}.kicad_sch"
    from kiutils.items.common import PageSettings
    sch = Schematic.create_new()
    sch.paper = PageSettings(paperSize="A3")

    _BOX_W, _BOX_H, _BOX_GAP = 120.0, 40.0, 20.0
    _ORIGIN_X, _ORIGIN_Y = 30.0, 30.0
    _NAME_OFFSET, _FILE_OFFSET = 2.5, 2.5
    _FONT = Font(height=1.0, width=1.0)

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
