SCHEMA_DIR  = src/silicai/schema
DOCS_DIR    = docs/schema
DEFS_DIR    = $(DOCS_DIR)/defs
GSD         = uv run generate-schema-doc

.PHONY: docs-generate docs-serve docs-build

docs-generate:
	mkdir -p $(DEFS_DIR)
	$(GSD) $(SCHEMA_DIR)/component.schema.json                $(DOCS_DIR)/component.md              --config-file docs-config.yaml
	$(GSD) $(SCHEMA_DIR)/circuit.schema.json                  $(DOCS_DIR)/circuit.md                --config-file docs-config.yaml
	$(GSD) $(SCHEMA_DIR)/defs/pin.schema.json                 $(DEFS_DIR)/pin.md                    --config-file docs-config.yaml
	$(GSD) $(SCHEMA_DIR)/defs/pin_function.schema.json        $(DEFS_DIR)/pin_function.md           --config-file docs-config.yaml
	$(GSD) $(SCHEMA_DIR)/defs/pin_electrical.schema.json      $(DEFS_DIR)/pin_electrical.md         --config-file docs-config.yaml
	$(GSD) $(SCHEMA_DIR)/defs/alternate_function.schema.json  $(DEFS_DIR)/alternate_function.md     --config-file docs-config.yaml
	$(GSD) $(SCHEMA_DIR)/defs/rail.schema.json                $(DEFS_DIR)/rail.md                   --config-file docs-config.yaml
	$(GSD) $(SCHEMA_DIR)/defs/interface.schema.json           $(DEFS_DIR)/interface.md              --config-file docs-config.yaml
	$(GSD) $(SCHEMA_DIR)/defs/passive_component.schema.json   $(DEFS_DIR)/passive_component.md      --config-file docs-config.yaml
	$(GSD) $(SCHEMA_DIR)/defs/specifications.schema.json      $(DEFS_DIR)/specifications.md         --config-file docs-config.yaml
	$(GSD) $(SCHEMA_DIR)/defs/rating.schema.json              $(DEFS_DIR)/rating.md                 --config-file docs-config.yaml
	$(GSD) $(SCHEMA_DIR)/defs/measured_value.schema.json      $(DEFS_DIR)/measured_value.md         --config-file docs-config.yaml
	$(GSD) $(SCHEMA_DIR)/defs/range_value.schema.json         $(DEFS_DIR)/range_value.md            --config-file docs-config.yaml
	$(GSD) $(SCHEMA_DIR)/defs/relative_threshold.schema.json  $(DEFS_DIR)/relative_threshold.md     --config-file docs-config.yaml
	$(GSD) $(SCHEMA_DIR)/defs/circuit_bus.schema.json         $(DEFS_DIR)/circuit_bus.md            --config-file docs-config.yaml
	$(GSD) $(SCHEMA_DIR)/defs/circuit_power_rail.schema.json  $(DEFS_DIR)/circuit_power_rail.md     --config-file docs-config.yaml
	$(GSD) $(SCHEMA_DIR)/defs/circuit_instance.schema.json    $(DEFS_DIR)/circuit_instance.md       --config-file docs-config.yaml

docs-serve: docs-generate
	uv run mkdocs serve

docs-build: docs-generate
	uv run mkdocs build
