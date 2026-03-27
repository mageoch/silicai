#!/usr/bin/env python3
"""Validate SilicAI component or circuit YAML files against their schema."""

import sys
import json
import argparse
from pathlib import Path
from importlib.resources import files

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

_SCHEMA_PKG = files("silicai").joinpath("schema")
SCHEMA_VERSION = "0.1.0"

SCHEMA_MAP = {
    "component.schema.json": "component.schema.json",
    "circuit.schema.json":   "circuit.schema.json",
    "project.schema.json":   "project.schema.json",
}


def build_registry() -> Registry:
    resources = []
    defs = _SCHEMA_PKG.joinpath("defs")
    for entry in defs.iterdir():
        if entry.name.endswith(".schema.json"):
            sub = json.loads(entry.read_text())
            resources.append(
                (sub["$id"], Resource.from_contents(sub, default_specification=DRAFT202012))
            )
    return Registry().with_resources(resources)


def resolve_schema(schema_uri: str) -> object:
    for suffix, filename in SCHEMA_MAP.items():
        if schema_uri.endswith(suffix):
            return _SCHEMA_PKG.joinpath(filename)
    raise ValueError(f"Unknown schema URI: {schema_uri!r}. Expected one of: {list(SCHEMA_MAP)}")


def validate(file_path: Path, registry: Registry) -> bool:
    with open(file_path) as f:
        doc = yaml.safe_load(f)

    schema_uri = doc.get("$schema", "")
    try:
        schema_ref = resolve_schema(schema_uri)
    except ValueError as e:
        print(f"✗ {file_path}: {e}")
        return False

    file_version = doc.get("$schema_version")
    if file_version is not None and file_version != SCHEMA_VERSION:
        print(f"✗ {file_path}: $schema_version {file_version!r} does not match expected {SCHEMA_VERSION!r}")
        return False

    schema = json.loads(schema_ref.read_text())

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
