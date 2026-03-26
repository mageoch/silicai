#!/usr/bin/env python3
"""Validate SilicAI component or circuit YAML files against their schema."""

import sys
import json
import argparse
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

SCHEMA_DIR = Path(__file__).parent.parent / "schema"
DEFS_DIR = SCHEMA_DIR / "defs"

# Map $schema URI suffix to local schema file
SCHEMA_MAP = {
    "component.schema.json": SCHEMA_DIR / "component.schema.json",
    "circuit.schema.json":   SCHEMA_DIR / "circuit.schema.json",
}


def build_registry() -> Registry:
    """Load all sub-schemas from defs/ into a registry."""
    resources = []
    for schema_file in DEFS_DIR.glob("*.schema.json"):
        with open(schema_file) as f:
            sub = json.load(f)
        resources.append(
            (sub["$id"], Resource.from_contents(sub, default_specification=DRAFT202012))
        )
    return Registry().with_resources(resources)


def resolve_schema(schema_uri: str) -> Path:
    for suffix, path in SCHEMA_MAP.items():
        if schema_uri.endswith(suffix):
            return path
    raise ValueError(f"Unknown schema URI: {schema_uri!r}. Expected one of: {list(SCHEMA_MAP)}")


def validate(file_path: Path, registry: Registry) -> bool:
    with open(file_path) as f:
        doc = yaml.safe_load(f)

    schema_uri = doc.get("$schema", "")
    try:
        schema_path = resolve_schema(schema_uri)
    except ValueError as e:
        print(f"✗ {file_path}: {e}")
        return False

    with open(schema_path) as f:
        schema = json.load(f)

    validator = Draft202012Validator(schema, registry=registry)
    errors = list(validator.iter_errors(doc))

    if not errors:
        print(f"✓ {file_path} is valid")
        return True

    for error in errors:
        path = ".".join(str(p) for p in error.path) or "root"
        print(f"✗ {file_path}: {error.message} (at {path})")
    return False


def main():
    parser = argparse.ArgumentParser(description="Validate SilicAI component and circuit files")
    parser.add_argument("files", nargs="+", type=Path, help="YAML files to validate")
    args = parser.parse_args()

    registry = build_registry()
    results = [validate(f, registry) for f in args.files]
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
