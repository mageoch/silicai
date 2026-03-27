# Writing Circuits

A circuit file instantiates components, assigns them to nets and buses, and declares the power rails and communication buses used on that sheet. Multiple circuits can be grouped into a [project](#projects) for multi-sheet KiCad schematics.

## Basic structure

```yaml
$schema: "https://mageoch.github.io/silicai/schema/circuit.schema.json"
$schema_version: "0.1.0"

circuit:
  name: TMP117 temperature sensor
  description: TMP117 I2C temperature sensor at address 0x48

  power_rails:
    - net: +3V3
    - net: GND

  instances:
    - ref: U1
      mpn: TMP117AIDRVR
      rails:
        vplus: +3V3        # map component rail "vplus" to circuit net "+3V3"
```

## Instances

Each instance is a placed component. The required fields are `ref` (KiCad reference designator) and `mpn` (must match a component file in the library).

### Rail overrides

Component files define default rail net names (e.g. `VCC_3V3`). Override them per instance to match your actual power nets:

```yaml
rails:
  vplus: +3V3
  vreg_vin: +3V3
```

### Bus connections

Connect an instance to a circuit bus using `buses`. For components with fixed interface pins (sensors, logic ICs), the pin mapping is taken from the component's `interfaces` definition:

```yaml
buses:
  - id: i2c_main
    interface: I2C
    role: slave
    address: "0x48"        # selects ADD0 connection automatically
```

For flexible-pin components (MCUs), declare which GPIO serves each role explicitly:

```yaml
buses:
  - id: i2c_main
    interface: I2C
    role: master
    pins:
      sda: GPIO4
      scl: GPIO5
```

### Pin overrides

Override individual pin nets directly with `pin_config`. Useful for tying configuration pins to a fixed potential:

```yaml
pin_config:
  ALERT: GND             # tie ALERT low (disable interrupt)
```

## Buses

Declare buses at circuit level with their type, speed, and any pull-up requirements. For I2C and SMBus, SilicAI places the pull-up resistors automatically:

```yaml
buses:
  - id: i2c_main
    type: I2C
    speed: { value: 400, unit: kHz }
    pull_ups:
      sda: { resistance: { value: 4.7, unit: kΩ }, net: +3V3 }
      scl: { resistance: { value: 4.7, unit: kΩ }, net: +3V3 }
```

## Projects

A project groups multiple circuits into a multi-sheet KiCad schematic. Buses and power rails declared under `shared` are available to all circuits and their pull-ups are placed only once:

```yaml
$schema: "https://mageoch.github.io/silicai/schema/project.schema.json"
$schema_version: "0.1.0"

project:
  name: TMP117 Sensor Board
  revision: "0.1"
  company: mageo services Sàrl

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

Generate the full project:

```bash
uv run silicai-generate project.yaml --output kicad/
```

See the [Example Project](example-project.md) for a complete walkthrough of this exact design.
