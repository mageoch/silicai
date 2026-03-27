# Getting Started

## Installation

Clone the repository with its component library submodule:

```bash
git clone --recurse-submodules git@github.com:mageoch/silicai.git
cd silicai
uv sync
```

This installs the `silicai-validate` and `silicai-generate` CLI tools into your virtual environment.

## Validate a component

SilicAI ships with a built-in component library. Try validating one of the included components:

```bash
uv run silicai-validate src/silicai/components/components/sensor/ti/tmp117aidrvr.yaml
```

```
✓ src/silicai/components/components/sensor/ti/tmp117aidrvr.yaml is valid
```

You can validate multiple files at once:

```bash
uv run silicai-validate src/silicai/components/components/**/*.yaml
```

## Generate a KiCad schematic

Point `silicai-generate` at a circuit or project YAML:

```bash
uv run silicai-generate path/to/circuit.yaml --output kicad/
```

For a multi-sheet project:

```bash
uv run silicai-generate path/to/project.yaml --output kicad/
```

This produces:

- `kicad/{project-name}.kicad_pro` — KiCad project file
- `kicad/{project-name}.kicad_sch` — Root schematic with sheet links
- `kicad/{circuit-name}.kicad_sch` — One sub-sheet per circuit

See the [Example Project](example-project.md) for a complete walkthrough.

## Use the MCP server with Claude Code

The MCP server lets Claude Code read component definitions and generate schematics directly from a conversation. Add this to your project's `.mcp.json`:

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

Claude can then call `silicai-generate`, look up component definitions, and inspect circuit netlists without leaving the conversation.

## Next steps

- [Writing Components](writing-components.md) — add a new IC to the library
- [Writing Circuits](writing-circuits.md) — describe a circuit and generate a schematic
- [Example Project](example-project.md) — a complete worked example
