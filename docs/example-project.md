# Example Project

[silicai-testproject](https://github.com/mageoch/silicai-testproject) is a complete worked example: an RP2350A microcontroller reading a TMP117 temperature sensor over I2C. It demonstrates the full SilicAI workflow from component definitions to a generated KiCad schematic.

## Setup

```bash
git clone git@github.com:mageoch/silicai-testproject.git
cd silicai-testproject
pip install silicai
```

Or with `uv`:

```bash
uv sync
```

## Project structure

```
silicai-testproject/
├── project.yaml           top-level project definition
├── circuits/
│   ├── tmp117.yaml        TMP117 temperature sensor circuit
│   └── rp2350a.yaml       RP2350A microcontroller circuit
└── kicad/                 generated KiCad files (not tracked)
```

## What the design does

- **MCU**: RP2350A running as I2C master on GPIO4 (SDA) and GPIO5 (SCL)
- **Sensor**: TMP117AIDRVR at I2C address 0x48 (ADD0 tied to GND)
- **Bus**: I2C at 400 kHz with 4.7 kΩ pull-ups to +3V3
- **Power**: single 3.3 V rail

## The project file

`project.yaml` declares shared resources — the power rails and the I2C bus — that are common to both circuits. Pull-up resistors for SDA and SCL are declared once here and placed in the first circuit sheet that uses `i2c_main`.

```yaml
project:
  name: TMP117 Sensor Board
  revision: "0.1"
  company: mageo services Sàrl
  description: RP2350A reading a TMP117 temperature sensor over I2C at 400 kHz

  shared:
    power_rails:
      - net: +3V3
      - net: GND
    buses:
      - id: i2c_main
        type: I2C
        speed: { value: 400, unit: kHz }
        pull_ups:
          sda: { resistance: { value: 4.7, unit: kΩ }, net: +3V3 }
          scl: { resistance: { value: 4.7, unit: kΩ }, net: +3V3 }

  circuits:
    - circuits/tmp117.yaml
    - circuits/rp2350a.yaml
```

## The sensor circuit

`circuits/tmp117.yaml` places one TMP117 instance. The `address: "0x48"` field tells SilicAI to connect ADD0 to GND (from the component's address-select options table). The pull-up on ALERT is placed automatically from the component's `required_external` definition.

```yaml
circuit:
  name: TMP117 temperature sensor

  instances:
    - ref: U1
      mpn: TMP117AIDRVR
      buses:
        - id: i2c_main
          interface: I2C
          role: slave
          address: "0x48"
          pins:
            scl: SCL
            sda: SDA
      rails:
        vplus: +3V3
```

## The MCU circuit

`circuits/rp2350a.yaml` places the RP2350A as I2C master. Because the RP2350A is a flexible-pin MCU, the pins used for SDA and SCL are declared explicitly. All power rails are mapped to `+3V3`.

```yaml
circuit:
  name: RP2350A microcontroller

  instances:
    - ref: U2
      mpn: RP2350A
      buses:
        - id: i2c_main
          interface: I2C
          role: master
          pins:
            sda: GPIO4
            scl: GPIO5
      rails:
        iovdd: +3V3
        qspi_iovdd: +3V3
        usb_otp_vdd: +3V3
        adc_avdd: +3V3
        vreg_vin: +3V3
```

## Generating the schematic

```bash
silicai-generate project.yaml --output kicad/
```

SilicAI resolves the full netlist and writes:

- `kicad/tmp117_sensor_board.kicad_pro` — KiCad project file
- `kicad/tmp117_sensor_board.kicad_sch` — Root schematic with links to sub-sheets
- `kicad/tmp117.kicad_sch` — TMP117 sheet (with decoupling cap, ALERT pull-up, address resistor)
- `kicad/rp2350a.kicad_sch` — RP2350A sheet (with per-rail decoupling caps)

Open `tmp117_sensor_board.kicad_pro` in KiCad to inspect the result.

## Using with Claude Code

The test project includes MCP server configuration. Add a `.mcp.json` at the project root:

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

Claude Code can then look up component definitions, check net assignments, and regenerate the schematic as you iterate on the design.
