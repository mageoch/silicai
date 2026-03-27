# SilicAI

SilicAI is a structured format and toolchain for describing electronic components and circuits in YAML — purpose-built for AI-assisted circuit design.

Electronic component information lives in PDF datasheets: unstructured, inconsistent, and hard for AI tools to consume reliably. SilicAI captures exactly what an AI needs to design circuits correctly:

- **Pin directions and functions** — input, output, bidirectional, open-drain; primary function (I2C_SCL, RESET, GPIO…) and all alternate functions
- **Power rails** — supply nets, per-pin and bulk decoupling requirements, placement rules
- **Required externals** — pull-ups, filters, address-select resistors, and other passive components mandated by the datasheet
- **Interfaces** — I2C, SPI, UART and others with speed, pin mapping, and electrical characteristics

From a set of component definitions and a circuit description, SilicAI can generate a production-ready KiCad schematic — placing power symbols, decoupling capacitors, pull-up resistors, and net labels automatically.

## How it works

```
components/          Circuit YAML           KiCad schematic
  tmp117.yaml   +   (instances,       →     (symbols, nets,
  rp2350a.yaml       buses, rails)           passives, labels)
```

1. **Component files** describe each IC once — pins, rails, interfaces, required externals
2. **Circuit files** instantiate components, assign nets and buses, override pins
3. **Project files** group circuits into a multi-sheet KiCad project
4. **`silicai-generate`** resolves everything and writes `.kicad_sch` / `.kicad_pro` files
5. **`silicai-mcp`** exposes the same capabilities as an MCP server for Claude Code

## Where to start

- New to SilicAI? → [Getting Started](getting-started.md)
- Adding a component to the library? → [Writing Components](writing-components.md)
- Designing a circuit? → [Writing Circuits](writing-circuits.md)
- See a complete example? → [Example Project](example-project.md)
- Need the schema details? → [Schema Reference](schema/component.md)
