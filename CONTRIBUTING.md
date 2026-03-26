# Contributing to SilicAI

Thank you for your interest in contributing to SilicAI.

## Licensing

By submitting a contribution you agree that your work will be licensed under
the same terms as this project (PolyForm Noncommercial License 1.0.0) and
that mageo services Ltd may offer it under commercial licenses as described
in [COMMERCIAL_LICENSE.md](COMMERCIAL_LICENSE.md).

## What you can contribute

- **Schema improvements** — new fields, better descriptions, additional enum
  values, or fixes to existing definitions
- **Tooling** — improvements to `silicai-validate` or `silicai-generate`
- **Bug reports** — open an issue with a minimal reproducing example
- **Documentation** — corrections or additions to `docs/`

## Schema changes

The schema is the core contract of this project. Changes should be:

- **Backward compatible** where possible — existing valid YAML files should
  remain valid after the change
- **Motivated by a real component** — if adding a new field or enum value,
  reference the datasheet or component that requires it
- **Documented** — add or update `description` fields in the schema JSON

## Workflow

1. Fork the repository and create a branch from `main`
2. Make your changes
3. Run validation on any affected example files: `silicai-validate <file>`
4. Open a pull request with a clear description of what changed and why

## Code style

- Python code follows standard PEP 8
- JSON schema files use 2-space indentation
- YAML files use 2-space indentation

## Questions

Open an issue or reach out at [info@mageo.ch](mailto:info@mageo.ch).
