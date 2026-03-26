#!/usr/bin/env python3
"""Validate a SilicAI component YAML file against the schema."""

import sys
import json
import argparse
from pathlib import Path

import yaml
import jsonschema

SCHEMA_PATH = Path(__file__).parent.parent / "schema" / "component.schema.json"


def validate(component_path: Path) -> bool:
    with open(SCHEMA_PATH) as f:
        schema = json.load(f)

    with open(component_path) as f:
        component = yaml.safe_load(f)

    try:
        jsonschema.validate(instance=component, schema=schema)
        print(f"✓ {component_path} is valid")
        return True
    except jsonschema.ValidationError as e:
        print(f"✗ {component_path}: {e.message} (at {'.'.join(str(p) for p in e.path)})")
        return False


def main():
    parser = argparse.ArgumentParser(description="Validate SilicAI component files")
    parser.add_argument("files", nargs="+", type=Path, help="Component YAML files to validate")
    args = parser.parse_args()

    results = [validate(f) for f in args.files]
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
