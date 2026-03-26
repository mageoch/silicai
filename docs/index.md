# SilicAI Schema Reference

SilicAI defines a YAML-based format for describing electronic components in a structured,
machine-readable way — enabling AI-assisted circuit design and automatic KiCad schematic generation.

## Top-level schemas

| Schema | Description |
|--------|-------------|
| [Component](schema/component.md) | A single electronic component with pins, power rails, and interfaces |
| [Circuit](schema/circuit.md) | A circuit design composed of component instances connected by buses and power rails |

## Definition schemas

These are reusable building blocks referenced by the top-level schemas.

| Schema | Description |
|--------|-------------|
| [Pin](schema/defs/pin.md) | A single pin with direction, electrical characteristics, and functions |
| [Pin Function](schema/defs/pin_function.md) | Primary logical function of a pin (GPIO, I2C_SCL, RESET, …) |
| [Pin Electrical](schema/defs/pin_electrical.md) | Switching thresholds and current limits (V_IH, V_OL, I_OL, …) |
| [Alternate Function](schema/defs/alternate_function.md) | Alternate peripheral function available on a GPIO pin |
| [Rail](schema/defs/rail.md) | Power supply rail with decoupling requirements |
| [Interface](schema/defs/interface.md) | Communication interface (I2C, SPI, UART, …) with speed and pin mapping |
| [Passive Component](schema/defs/passive_component.md) | Capacitor, resistor, ferrite bead, or other passive with placement rules |
| [Specifications](schema/defs/specifications.md) | Primary specifications for passive components (R, C, L, tolerance, …) |
| [Rating](schema/defs/rating.md) | One row of an absolute maximum or recommended operating conditions table |
| [Measured Value](schema/defs/measured_value.md) | A numeric value with an explicit unit (e.g. `{value: 100, unit: nF}`) |
| [Range Value](schema/defs/range_value.md) | A min/max range with a unit (e.g. `{min: -40, max: 85, unit: °C}`) |
| [Relative Threshold](schema/defs/relative_threshold.md) | A voltage threshold as a fraction of a reference (e.g. `0.7 × V+`) |
| [Circuit Bus](schema/defs/circuit_bus.md) | A communication bus instantiated in a circuit |
| [Circuit Power Rail](schema/defs/circuit_power_rail.md) | A power rail instantiated in a circuit |
| [Circuit Instance](schema/defs/circuit_instance.md) | A component instance in a circuit with ref designator and pin config |

## Quick example

```yaml
$schema: "https://github.com/mageoch/silicai/schema/component.schema.json"
component:
  mpn: TMP117AIDRVR
  manufacturer: Texas Instruments
  category: sensor
  package: WSON-6
  rails:
    - id: vplus
      net: VCC_3V3
      per_pin_decoupling:
        - type: capacitor
          capacitance: {value: 100, unit: nF}
          placement: close
  pins:
    - number: 1
      name: SCL
      direction: input
      open_drain: true
      primary_function: {type: i2c_scl}
  interfaces:
    - type: I2C
      speed_max: {value: 400, unit: kHz}
      pins: {scl: SCL, sda: SDA}
```
