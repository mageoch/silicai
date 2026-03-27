# Getting Started

## Installation

```bash
pip install silicai
```

This installs the `silicai-validate`, `silicai-generate`, `silicai-import`, and `silicai-mcp` CLI tools. The bundled component library ([silicai-components](https://github.com/mageoch/silicai-components)) is included automatically.

To also install the MCP server dependencies:

```bash
pip install "silicai[mcp]"
```

## Validate a component

Create a component YAML file and validate it against the schema:

```bash
silicai-validate path/to/mycomponent.yaml
```

```
✓ path/to/mycomponent.yaml is valid
```

Validate multiple files at once:

```bash
silicai-validate components/**/*.yaml
```

Not sure where to start? See [Writing Components](writing-components.md) for an annotated example, or clone [silicai-testproject](https://github.com/mageoch/silicai-testproject) for a ready-to-run project.

## Generate a KiCad schematic

Point `silicai-generate` at a circuit or project YAML:

```bash
silicai-generate path/to/circuit.yaml --output kicad/
```

For a multi-sheet project:

```bash
silicai-generate path/to/project.yaml --output kicad/
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

## Contributing / development install

To work on SilicAI itself or contribute components, clone the repository:

```bash
git clone --recurse-submodules git@github.com:mageoch/silicai.git
cd silicai
uv sync
```

## Next steps

- [Writing Components](writing-components.md) — add a new IC to the library
- [Writing Circuits](writing-circuits.md) — describe a circuit and generate a schematic
- [Example Project](example-project.md) — a complete worked example
