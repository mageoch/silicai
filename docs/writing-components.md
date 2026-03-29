# Writing Components

A component file describes one IC or passive component in enough detail for SilicAI to place it correctly in a schematic — without needing to parse its datasheet at generation time.

Files live in `components/<category>/<manufacturer>/<mpn-lowercase>.yaml` inside [silicai-components](https://github.com/mageoch/silicai-components).

## Minimal structure

Every component file needs at minimum: `$schema`, `$schema_version`, and the five required fields under `component`.

```yaml
$schema: "https://mageoch.github.io/silicai/schema/component.schema.json"
$schema_version: "0.1.0"

component:
  mpn: TMP117AIDRVR
  manufacturer: Texas Instruments
  category: sensor
  package: WSON-6
  pins:
    - number: 1
      name: SCL
      direction: input
```

## Pins

Each pin needs a number, name, and direction. Direction must match the datasheet perspective (not the system perspective):

| Direction | Meaning |
|-----------|---------|
| `input` | Signal flows into the IC |
| `output` | Signal flows out of the IC |
| `bidirectional` | Configurable direction (e.g. GPIO, I2C SDA) |
| `power_in` | Supply or ground pin |
| `power_out` | Regulated output (e.g. LDO output) |
| `open_drain` | Open-drain output — add `open_drain: true` and use `output` or `bidirectional` |

For pins the datasheet says must be connected, add `must_connect: true`. For pins with a fixed net (GND, internal reference), set `net: GND` directly.

```yaml
pins:
  - number: 2
    name: GND
    direction: power_in
    net: GND

  - number: 4
    name: ADD0
    direction: input
    must_connect: true        # datasheet says: do not leave floating
```

### Pin functions

Use `primary_function` to declare what a pin does. This lets SilicAI and AI tools understand the signal type without parsing pin names:

```yaml
  - number: 1
    name: SCL
    direction: input
    open_drain: true
    primary_function:
      type: i2c_scl
```

Common function types include `i2c_scl`, `i2c_sda`, `spi_clk`, `spi_mosi`, `uart_tx`, `reset`, `interrupt`, `alert`, `gpio`, `address_select`.

### Required externals

If the datasheet requires an external component on a pin (pull-up, filter capacitor, address resistor), declare it under `externals` with `required: true`. SilicAI will place it automatically:

```yaml
  - number: 3
    name: ALERT
    direction: output
    open_drain: true
    externals:
      - type: resistor
        required: true
        resistance: { value: 10, unit: kΩ }
        from: ALERT
        to: VCC_3V3
        scope: component   # place once per component instance
```

For crystal oscillator circuits, declare the crystal with its load capacitors and optional series resistor:

```yaml
  - number: 30
    name: XIN
    direction: input
    externals:
      - type: crystal
        required: true
        frequency: { value: 12, unit: MHz }
        to: XOUT             # net name of the XOUT pin
        xin_cap: { value: 15, unit: pF }
        xout_cap: { value: 15, unit: pF }
        series_r: { value: 33, unit: Ω }   # optional drive-current limiting resistor
```

For address-select pins, declare the address options so the generator can pick the right connection:

```yaml
  - number: 4
    name: ADD0
    direction: input
    primary_function:
      type: address_select
      options:
        - connect_to: GND
          i2c_address: "0x48"
        - connect_to: "V+"
          i2c_address: "0x49"
```

## Power rails

Rails group all power-supply pins and their decoupling requirements:

```yaml
rails:
  - id: vplus
    net: VCC_3V3           # default net name (can be overridden per circuit instance)
    bulk_decoupling:
      - type: capacitor
        capacitance: { value: 10, unit: µF }
        placement: nearby
```

The `id` is how the circuit references this rail (e.g. `rails: {vplus: +3V3}`). The `net` is the default net name used if the circuit doesn't override it.

Per-pin decoupling caps (one cap per supply pin, placed close) are declared on the pin itself under `decoupling`:

```yaml
pins:
  - number: 5
    name: VDD
    direction: power_in
    rail: vplus
    decoupling:
      - type: capacitor
        capacitance: { value: 100, unit: nF }
        voltage_rating: { value: 10, unit: V }
        dielectric: [X5R, X7R]
        placement: close
```

For rails that require an RC or LC input filter (e.g. an SMPS output feeding a sensitive analog rail), declare the filter chain under `input_filter`. List components in order from source to rail: series elements first, then shunt elements to GND:

```yaml
rails:
  - id: vreg_avdd
    net: VREG_AVDD
    input_filter:
      - type: resistor
        resistance: { value: 33, unit: Ω }
        from: +3V3            # source net
        to: VREG_AVDD         # rail net (output node)
      - type: capacitor
        capacitance: { value: 4.7, unit: µF }
        to: GND               # shunt to GND
```

SilicAI renders this as a horizontal L-filter: `+3V3 ──[R]──┬── VREG_AVDD` with the capacitor dropping to GND at the junction.

## Interfaces

Declare communication interfaces with their speed range and pin mapping:

```yaml
interfaces:
  - type: I2C
    instance: 1
    speed_min: { value: 1, unit: kHz }
    speed_max: { value: 400, unit: kHz }
    pins:
      scl: SCL
      sda: SDA
```

## Operating conditions and ratings

Include absolute maximum ratings and recommended operating conditions from the datasheet — these are used for design-rule checks and documentation:

```yaml
absolute_maximum_ratings:
  - parameter: supply_voltage
    pins: ["V+"]
    unit: V
    max: 6

recommended_operating_conditions:
  - parameter: supply_voltage
    pins: ["V+"]
    unit: V
    min: 1.7
    nom: 3.3
    max: 5.5
```

## Validating your file

```bash
uv run silicai-validate components/sensor/ti/tmp117aidrvr.yaml
```

All files must pass validation before being added to the library. See [CONTRIBUTING](https://github.com/mageoch/silicai-components/blob/main/CONTRIBUTING.md) for the full guidelines.
