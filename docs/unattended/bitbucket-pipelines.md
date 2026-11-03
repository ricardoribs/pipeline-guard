# Bitbucket Pipelines setup

> Smoke validation pending: copy the final example from the sandbox only after recording the
> failure trigger, recursion exclusion, retained result, and minimum token permissions.

## Prerequisites and installation

Build an immutable non-root pipeline image containing `pipeline-guard`, Python 3.12+, `uv`, and
Git. Include Claude Code 2.1.295+ only for the `anthropic` backend. Commit a validated
`config/pipeline-guard.yaml`, first with `unattended.remediation_enabled: false`; enable it only
after a report-only smoke run and review of the editable paths, test commands, branch prefix, and
limits. The image exposes the installed plugin directory as `PIPELINE_GUARD_ROOT` and accepts no
interactive stdin.

Project-declared validation commands must use isolated, non-production targets and least-privilege
credentials whenever they can modify a warehouse or another external system. Pipeline Guard runs
the commands as written and does not infer their side effects.

Configure Bitbucket secured variables or an external secret manager for model access, read-only
collection, restricted branch push, draft-PR publication, and the policy secret. Do not pass secret
values as command arguments. Provide retained input/output storage and a create-once-capable
authorization-state store that survives separate pipeline containers.

## Required boundaries

- A repository/read token for Stage A pipeline and step APIs.
- A branch-restricted repository write token for the repair push.
- A Bitbucket API token that can list and create draft PRs but cannot merge them.
- A model service identity and a separate 32-byte policy secret.
- Persistent authorization state outside the clone. A fresh container-local directory is not
  sufficient across pipeline retries.

The repair step must run as a non-root user. Do not use a deployment environment with a manual
approval, because unattended execution cannot wait for it.

## Pipeline pattern

The sandbox must use a native failed-pipeline trigger when available, filter status `FAILED`, and
exclude `pipeline-guard/**`. Also add no-op branch and pull-request entries for that prefix so a
repair branch cannot recursively trigger another repair.

```yaml
definitions:
  steps:
    - step: &pipeline-guard
        name: Pipeline Guard
        image: <validated-unprivileged-image>
        max-time: 65 # minutes; exceeds the default 3600-second repair cap plus setup
        script:
          - ./sandbox/collect-bitbucket-failure.sh
          - uv run "$PIPELINE_GUARD_ROOT/scripts/unattended_cli.py" --mode unattended
            --config "$BITBUCKET_CLONE_DIR/config/pipeline-guard.yaml"
            --project-root "$BITBUCKET_CLONE_DIR"
            --output-dir "$PIPELINE_GUARD_OUTPUT"
            --authorization-state-dir "$PIPELINE_GUARD_STATE"
            --workspace-parent "$PIPELINE_GUARD_WORKSPACES"
            "$PIPELINE_GUARD_INPUT/incident.json"
        artifacts:
          - pipeline-guard-output/**

pipelines:
  custom:
    pipeline-guard:
      - step: *pipeline-guard
  branches:
    "pipeline-guard/**":
      - step:
          name: Recursion guard
          script: ["echo repair branch excluded"]
  pull-requests:
    "pipeline-guard/**":
      - step:
          name: Recursion guard
          script: ["echo repair PR excluded"]
```

The exact trigger stanza depends on workspace capabilities and must be copied from the validated
sandbox. Where the native completed-pipeline trigger is unavailable, use `after-script` only to
call a small authenticated dispatcher/API endpoint; do not run the full repair in `after-script`,
and pass the failed pipeline and step UUIDs rather than log content.

Stage A reads `BITBUCKET_TRIGGER_PIPELINE_UUID` and
`BITBUCKET_TRIGGER_PIPELINE_STEP_UUID`, obtains the failed commit/branch/steps/log using the
read-only token, explicitly checks out that commit, regenerates declared dbt artifacts, hashes
them, and validates incident 1.1. It must not print API responses or environment variables.

`max-time` is measured in minutes and must exceed `unattended.max_seconds` plus environment setup
while remaining within the workspace plan limit. The original pipeline remains failed. Retain the
output artifacts and use the common [outcome troubleshooting](README.md#troubleshooting).

Start with a failure that is expected to return `REPORT_ONLY`, then validate one eligible code
failure. Confirm the repair image runs non-root, the result validates, credentials are absent from
logs and retained files, and both the trigger filter and the CLI recursion guard reject
`pipeline-guard/**`.
