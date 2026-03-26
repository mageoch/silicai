#!/usr/bin/env python3
"""Validate a SilicAI component YAML file against the schema."""

import sys
import json
import argparse
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

SCHEMA_PATH = Path(__file__).parent.parent / "schema" / "component.schema.json"
DEFS_DIR = SCHEMA_PATH.parent / "defs"


def load_schema():
    with open(SCHEMA_PATH) as f:
        main_schema = json.load(f)

    resources = []
    for schema_file in DEFS_DIR.glob("*.schema.json"):
        with open(schema_file) as f:
            sub = json.load(f)
        resources.append(
            (sub["$id"], Resource.from_contents(sub, default_specification=DRAFT202012))
        )

    registry = Registry().with_resources(resources)
    return main_schema, registry


def validate(component_path: Path, schema, registry) -> bool:
    with open(component_path) as f:
        component = yaml.safe_load(f)

    validator = Draft202012Validator(schema, registry=registry)
    errors = list(validator.iter_errors(component))

    if not errors:
        print(f"✓ {component_path} is valid")
        return True

    for error in errors:
        path = ".".join(str(p) for p in error.path) or "root"
        print(f"✗ {component_path}: {error.message} (at {path})")
    return False


def main():
    parser = argparse.ArgumentParser(description="Validate SilicAI component files")
    parser.add_argument("files", nargs="+", type=Path, help="Component YAML files to validate")
    args = parser.parse_args()

    schema, registry = load_schema()
    results = [validate(f, schema, registry) for f in args.files]
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
