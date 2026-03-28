# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies (use uv, not pip)
uv sync
uv sync --extra mcp      # With MCP server support
uv sync --extra docs     # With documentation tools

# Run tests
uv run pytest
uv run pytest tests/test_validate.py          # Single file
uv run pytest tests/test_generate.py::test_name  # Single test

# CLI tools
uv run silicai-validate <files...>
uv run silicai-generate <circuit|project> --output <dir>
uv run silicai-import <kicad_project>
uv run silicai-mcp --project-dir .

# YAML formatting
uv run yamlfix <file>

# Documentation
make docs-generate   # Regenerate schema docs from JSON schemas
make docs-serve      # Serve locally
make docs-build      # Build static site
```

## Architecture

SilicAI is a 3-layer system for AI-assisted circuit design:

### Layer 1: Schema (`src/silicai/schema/`)
JSON Schema 2020-12 definitions. `component.schema.json`, `circuit.schema.json`, and `project.schema.json` are the top-level schemas; `defs/` contains reusable sub-schemas (pin, rail, interface, specs, etc.). Schemas are loaded via `importlib.resources`.

### Layer 2: Core Tools (`src/silicai/`)

**`generate.py`** is the most complex module. The key function is `resolve()`, which takes a circuit YAML + component library paths and produces a flat parts list + netlist by:
- Looking up component definitions by MPN via `find_component()`
- Resolving power rails (shared standard nets from `nets/standard.yaml` + circuit-local)
- Expanding bus connections across instances
- Returning `{ parts, netlist, power_nets, bom }`

`write_kicad_sch()` and `write_kicad_project()` consume the resolved output to produce KiCad files using `kiutils`.

**`validate.py`** builds a `jsonschema.Registry` from all schema files and validates YAML against it.

**`mcp_server.py`** exposes the core tools via MCP for Claude Code integration.

**`import_kicad.py`** is the reverse path: reads existing KiCad projects to generate SilicAI circuit YAMLs.

### Layer 3: Component Library (`src/silicai/components/` — git submodule)
Git submodule pointing to `mageoch/silicai-components`. Always checkout with `--recurse-submodules`. Additional libraries can be registered in a project's `pyproject.toml`:
```toml
[tool.silicai]
component_libraries = [{ path = "path/to/extra-components" }]
```

### Data Flow
```
Component YAMLs + Circuit YAML
         ↓
    resolve() [generate.py]
         ↓
   { parts, netlist, power_nets }
         ↓
write_kicad_sch() / write_kicad_project()
         ↓
   .kicad_sch + .kicad_pro files
```

## Key Conventions

- **Python 3.11+** required (uses `tomllib` from stdlib)
- **2-space indentation** for all JSON and YAML files
- Schema changes must be backward-compatible and motivated by real components
- Tests in `tests/fixtures/` use real component definitions from the submodule (TMP117, NRF52840, AP2112K); run `silicai-validate` on any modified YAML before committing

## MCP Server Setup

To use the MCP server with Claude Code in a circuit design project, add `.mcp.json`:
```json
{
  "mcpServers": {
    "SilicAI": {
      "type": "stdio",
      "command": ".venv/bin/silicai-mcp",
      "args": ["--project-dir", "."]
    }
  }
}
```
