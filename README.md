# SilicAI

Copyright 2026 mageo services Ltd — Licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE). Commercial use requires a [separate license](COMMERCIAL_LICENSE.md).

Structured electronic component descriptions for AI-assisted circuit design.

## Concept

Electronic component information lives in PDF datasheets — unstructured, hard for AI to consume. SilicAI defines a YAML schema that captures everything needed to design circuits correctly:

- Pin directions and functions (primary + all alternate functions)
- Power rails with per-pin and bulk decoupling requirements
- Required external components (filters, pull-ups, decoupling)
- Standard net definitions and electrical characteristics

The goal is to give AI tools the structured context they need to generate correct, production-ready schematics.

## Structure

```
src/silicai/
  validate.py        Schema validation CLI
  generate.py        KiCad schematic generator
  mcp_server.py      MCP server for Claude Code integration
  schema/            JSON Schema definitions
    component.schema.json
    circuit.schema.json
    project.schema.json
    defs/            Shared schema definitions
  components/        Built-in component library (silicai-components submodule)
nets/                Standard net definitions (GND, VCC_3V3, ...)
tests/               Test suite
```

The component library is bundled as a git submodule ([mageoch/silicai-components](https://github.com/mageoch/silicai-components)) and loaded automatically — no extra configuration needed. Additional component libraries can be declared in a project's `pyproject.toml`:

```toml
[tool.silicai]
component_libraries = [
    { path = "path/to/extra-components" },
]
```

## Usage

```bash
# Clone with submodules
git clone --recurse-submodules git@github.com:mageoch/silicai.git

uv sync

# Validate a component or circuit file
silicai-validate path/to/component.yaml

# Generate a KiCad schematic from a circuit definition
silicai-generate path/to/circuit.yaml --output kicad/

# Run the MCP server
silicai-mcp --project-dir .
```

## MCP Server

SilicAI exposes a [Model Context Protocol](https://modelcontextprotocol.io) server that lets Claude Code read component definitions and generate schematics. Add to your project's `.mcp.json`:

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

## Status

Early development. Schema and component format subject to change.
