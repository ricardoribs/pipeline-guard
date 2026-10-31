---
name: pipeline-guard-setup
description: Materializes a validated pipeline-guard project configuration from the generic or dbt preset. Use when installing pipeline-guard in a consumer project, configuring it for the first time, selecting its initial preset, or recreating config/pipeline-guard.yaml. Refuses to overwrite an existing configuration without explicit confirmation and validates the generated file before reporting success. Not for diagnosing incidents, repairing pipelines, or modifying the plugin's bundled presets.
triggers:
  - "setup do pipeline-guard"
  - "configurar pipeline-guard"
  - "instalar pipeline-guard no projeto"
  - "materializar configuração do pipeline-guard"
  - "escolher preset do pipeline-guard"
  - "criar config/pipeline-guard.yaml"
---

# Pipeline Guard Setup

Materialize a project-owned pipeline-guard configuration from one of the
plugin's bundled presets and validate it before reporting success.

## Scope

Use this skill only to initialize or replace the configuration of a consumer
project.

Do not use it to:

- diagnose a pipeline incident;
- remediate pipeline code;
- edit a bundled preset;
- edit the plugin schema;
- collect or persist literal credentials.

## Paths

Resolve the paths as follows:

- Plugin root: `${CLAUDE_PLUGIN_ROOT}`
- Generic preset: `${CLAUDE_PLUGIN_ROOT}/config/presets/generic.yaml`
- dbt preset: `${CLAUDE_PLUGIN_ROOT}/config/presets/dbt.yaml`
- Validator: `${CLAUDE_PLUGIN_ROOT}/scripts/validate_config.py`
- Consumer configuration: `<consumer-root>/config/pipeline-guard.yaml`

Treat `${CLAUDE_PLUGIN_ROOT}` as read-only. Write only inside the consumer
project selected by the user.

## Required inputs

Determine:

1. The consumer project root.
2. The preset: `generic` or `dbt`.
3. The project identifier.

Use the current Git repository root as the consumer root when the user does not
provide another path. If the current directory is not in a Git repository, use
the current working directory and state that choice.

Never silently choose a preset. If the user has not selected one, explain:

- `generic` uses the dependency-free `mock` adapter;
- `dbt` uses the `dbt` adapter and is intended for projects containing
  `dbt_project.yml`.

Then ask the user to choose.

Use the consumer directory name as the project identifier when the user does
not provide one. Use `.` as the default pipeline root.

## Existing configuration guard

Before writing, check whether
`<consumer-root>/config/pipeline-guard.yaml` already exists.

If it exists:

1. Stop before modifying any file.
2. Tell the user the exact existing path.
3. Ask whether that exact file may be replaced.
4. Continue only after explicit confirmation to replace it.

Do not interpret a general request to continue as overwrite confirmation.

After explicit confirmation, preserve the existing file as
`pipeline-guard.yaml.bak` before replacing it. Tell the user where the backup
was created.

## Materialization procedure

1. Verify that the selected preset is exactly `generic` or `dbt`.
2. Verify that the corresponding preset file exists below
   `${CLAUDE_PLUGIN_ROOT}/config/presets/`.
3. Create `<consumer-root>/config/` when it does not exist.
4. Copy the selected preset to
   `<consumer-root>/config/pipeline-guard.yaml`.
5. Edit only the copied file:
   - set `project.id` to the resolved project identifier;
   - set `pipeline.root` to the user-provided path or `.`;
   - preserve `pipeline.adapter` from the selected preset.
6. Never add a literal credential. Credential values must remain environment
   variable or secret references.
7. Do not modify any file below `${CLAUDE_PLUGIN_ROOT}`.

## Validation

Run:

```bash
uv run "${CLAUDE_PLUGIN_ROOT}/scripts/validate_config.py" \
  "<consumer-root>/config/pipeline-guard.yaml"
```

Treat exit code `0` as success.

For a nonzero exit code:

1. Do not report setup as complete.
2. Show the validator's actionable error.
3. Correct only the materialized consumer configuration when the correction is
   unambiguous and safe.
4. Run the validator again.
5. Ask the user before making a correction that changes their intended
   adapter, repository, evidence providers, validation commands, or safety
   policy.

## Completion report

Report setup as complete only after validation succeeds. Include:

- selected preset;
- consumer configuration path;
- resolved project identifier;
- resolved pipeline root;
- validator result;
- backup path, when an existing configuration was replaced.

State explicitly that the bundled preset and other plugin files were not
modified.
