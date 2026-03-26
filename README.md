# SilicAI

Copyright 2026 mageo services Ltd — Licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE). Commercial use requires a [separate license](COMMERCIAL_LICENSE.md).

Structured electronic component descriptions for AI-assisted circuit design.

## Concept

Electronic component information lives in PDF datasheets — unstructured, hard for AI to consume. SilicAI defines a YAML schema that captures everything needed to design circuits correctly:

- Pin directions and functions (primary + all alternate functions)
- Power rails with per-pin and bulk decoupling requirements
- Required external components (filters, pull-ups, decoupling)
- Standard net definitions

The goal is to give AI tools the structured context they need to generate correct, production-ready schematics.

## Structure

```
schema/          JSON Schema for component validation
nets/            Standard net definitions (GND, VCC_3V3, ...)
components/      Component library (organized by category/manufacturer)
tools/           CLI tools (validate, generate)
```

## Usage

```bash
pip install -e .
silicai-validate components/mcu/st/stm32f405rgt6.yaml
```

## Status

Early development. Schema and component format subject to change.
