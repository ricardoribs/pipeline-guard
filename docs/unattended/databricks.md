# Databricks Jobs setup

> End-to-end unattended remediation validated in a Databricks Free Edition sandbox on 2026-11-04.
> The recorded flow collected a controlled dbt failure, invoked an isolated runner identity,
> diagnosed and automatically authored the bounded fix, passed project validation, pushed the
> restricted incident branch, and published a Bitbucket draft PR. A person marked the draft ready
> and merged it; a later reference run passed. This is sandbox evidence, not permission to merge,
> deploy, or rerun automatically.

## Prerequisites and installation

- Make `pipeline-guard` available to Stage B from an immutable image or a checkout pinned to an
  immutable commit, and expose its root as `PIPELINE_GUARD_ROOT`; the job must also provide Python
  3.12+, `uv`, and Git. The validated serverless runner used a pinned plugin commit. Claude Code
  2.1.295+ is required only when `runtime.model.provider` is `anthropic`.
- Commit a validated `config/pipeline-guard.yaml` to the consumer repository. Begin with
  `unattended.remediation_enabled: false`, verify report-only execution, then enable remediation in
  a reviewed configuration change.
- Use a Databricks runtime or custom container that passed the selected model-backend compute
  spike. The validated reference used serverless Default Automated Compute, Python 3.12, outbound
  access to the declared Git and model endpoints, and least-privilege service principals. Repeat
  the sandbox validation when changing runtime, access mode, egress, or identity boundaries.
- Create one retained Volume location for inputs/results, one durable authorization-state location,
  and an ephemeral local-disk workspace parent. They must be distinct from the checkout.

## Architecture

Use a Databricks Asset Bundle with a reference job and a separately permissioned runner job:

- `pipeline_guard_context` (Stage A) runs only after failure, uses a read-only service principal,
  calls Jobs APIs, identifies the upstream-most failed task, and writes incident/artifacts to a
  retained Volume.
- `launch_pipeline_guard` depends on Stage A and passes only non-secret run identifiers to the
  separate runner job.
- `pipeline_guard` (Stage B) runs as a distinct least-privilege service principal in approved
  isolated compute and invokes `unattended_cli.py` once.

Disable retries, including serverless auto-optimization retries, for Stage A and Stage B. Exclude
branches under `repository.remediation_branch_prefix` in the job trigger or collector. The failed
workload task remains failed even when downstream collection and remediation succeed; depending on
the job graph, Databricks may summarize the parent run as `Succeeded with failures`.

## Identities and permissions

Create non-personal identities with minimum independent grants:

1. Collector: `jobs/read`, run output/log read, repository read, and Volume write for inputs.
2. Model: invoke only the configured Anthropic or OpenAI-compatible serving endpoint.
3. Git push: write only the remediation branch namespace.
4. Bitbucket/GitHub publisher: repository read plus draft PR create/read; no merge or approval.
5. Policy secret: read from a Databricks secret scope by Stage B only.

Stage B also needs read/write on the output Volume, a durable state directory, and a disposable
workspace directory. Never expose secret values through task parameters or notebook output.
Store the model credential, Git push credential, draft-PR token, and policy secret in a Databricks
secret scope and inject only references or environment variables into Stage B. Stage A receives
only its read identity.

For a Databricks-hosted Llama service with function calling enabled, use the Unity Catalog AI
Gateway OpenAI-compatible route. The OAuth client credentials are injected by the job from the
secret scope; they are not stored in YAML:

```yaml
runtime:
  model:
    provider: databricks
    name: system.ai.meta-llama-3-3-70b-instruct
    max_output_tokens: 8192
```

`max_output_tokens` limits each model response independently from the shared investigation
budget. Set it no higher than the selected model's documented output limit.

Set `DATABRICKS_HOST` to the HTTPS workspace origin and inject
`DATABRICKS_MODEL_CLIENT_ID` plus `DATABRICKS_MODEL_CLIENT_SECRET`. At startup, Pipeline Guard
exchanges them at `/oidc/v1/token` with only the `ai-gateway` scope and keeps the short-lived access
token in memory. The `databricks` profile derives the base URL
`DATABRICKS_HOST/ai-gateway/mlflow/v1` and appends `/chat/completions` for requests. A pre-issued
`DATABRICKS_TOKEN` remains supported, and an explicit `runtime.model.base_url` or
`DATABRICKS_MODEL_BASE_URL` can target a legacy serving endpoint when required. `runtime.model.name`
may likewise be replaced by `DATABRICKS_MODEL`; when neither is set, the system model shown above
is used. Confirm that any overridden model supports function calling before enabling remediation;
report-only diagnosis needs Chat Completions but does not invoke file tools.

## Bundle outline

```yaml
resources:
  jobs:
    reference_pipeline:
      tasks:
        - task_key: dbt_build
          dbt_task:
            commands: ["dbt build --target ${bundle.target}"]
        - task_key: pipeline_guard_context
          depends_on:
            - task_key: dbt_build
          run_if: AT_LEAST_ONE_FAILED
          spark_python_task:
            python_file: ./scripts/collect_failed_run.py
        - task_key: launch_pipeline_guard
          depends_on:
            - task_key: pipeline_guard_context
          run_if: ALL_SUCCESS
          run_job_task:
            job_id: ${resources.jobs.pipeline_guard_runner.id}
    pipeline_guard_runner:
      tasks:
        - task_key: pipeline_guard
          spark_python_task:
            python_file: ./scripts/run_pipeline_guard.py
```

This abbreviated outline omits run parameters, permissions, environments, and compute settings.
The deployed bundle must pin those values and keep the collector and runner identities separate.
Stage A records `{{job.run_id}}`, repair count, task run identity, failed commit, and branch in
host-neutral fields rather than adding Databricks-specific fields to the contract.
Any project-declared validation command used by the repair must target an isolated, non-production
catalog and schema with least-privilege credentials. This is separate from the failed workload's
target: Pipeline Guard executes declared commands as written and does not infer their side effects.

## Stage B invocation

Use a Volume layout such as:

```text
/Volumes/<catalog>/<schema>/pipeline_guard/<job-run>/input
/Volumes/<catalog>/<schema>/pipeline_guard/<job-run>/output
/Volumes/<catalog>/<schema>/pipeline_guard/authorization-state
```

The authorization-state directory is shared across retries and must not be under the checkout or
disposable workspace parent. Pipeline Guard obtains one OAuth M2M model token during preflight.
Keep `unattended.max_seconds` at or below 3000 so execution finishes before that one-hour token
expires; automatic renewal is not yet part of the runtime.

The collector must write a schema 1.1 incident with the failed commit and branch, the upstream-most
failed task in `failed_step`, any other failures in `host_run.related_failed_steps`, and relative
artifact paths plus SHA-256 hashes. Validate that document before starting Stage B; do not place
Databricks tokens, signed URLs, or raw environment dumps in it.

```bash
uv run "$PIPELINE_GUARD_ROOT/scripts/unattended_cli.py" \
  --mode unattended \
  --config "$CHECKOUT/config/pipeline-guard.yaml" \
  --project-root "$CHECKOUT" \
  --output-dir "$RUN_VOLUME/output" \
  --authorization-state-dir "$STATE_VOLUME" \
  --workspace-parent "$LOCAL_DISK/pipeline-guard-workspaces" \
  "$RUN_VOLUME/input/incident.json"
```

Retain `result.json`, `summary.md`, and `artifacts/`. Stage B reports the repair exit code while the
original workload task remains failed in the reference run. See [the common guide](README.md) for
outcomes and troubleshooting.
