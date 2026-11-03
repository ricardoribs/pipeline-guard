# Unattended Pipeline Guard

Pipeline Guard can diagnose a failed pipeline and, when versioned project policy explicitly
allows it, prepare a bounded correction and publish a draft pull request without waiting for a
person. The failed pipeline remains failed; the plugin never merges, deploys, reruns a pipeline,
or changes data.

> Host validation status: the local implementation is available. Databricks end-to-end and the
> Bitbucket Pipelines and Airflow smoke validations described in the
> [unattended specification](../specs/pipeline-guard/unattended/spec.md) must still be recorded
> before these host guides are treated as production-validated.

## Choose a guide

- [Databricks Jobs](databricks.md)
- [Bitbucket Pipelines](bitbucket-pipelines.md)
- [Airflow](airflow.md)
- [Reference prompt for host-side context collection](reference-prompt.md)

## Complete flow

The host owns two stages:

1. **Stage A — collect:** after a pipeline task fails, a read-only service identity identifies the
   upstream-most failed step, writes `incident_input_v1` 1.1, places declared artifacts beside the
   checkout, calculates their SHA-256 hashes, and preserves the original failed status.
2. **Stage B — repair:** an isolated, unprivileged environment checks out the failed commit and
   invokes `unattended_cli.py` once. It supplies model, push, draft-PR, and authorization credentials
   through the host secret store, with a persistent authorization-state directory outside the
   checkout and workspace.

The plugin then performs preflight, validates the incident, checks recursion, diagnoses the failure,
applies the eligibility policy, consumes a signed policy authorization, checks for an existing
matching draft, authors in an isolated clone when needed, validates the patch, runs allowlisted
tests, pushes one deterministic branch, and creates or reuses one draft PR.

```bash
uv run "$PIPELINE_GUARD_ROOT/scripts/unattended_cli.py" \
  --mode unattended \
  --config "$CHECKOUT/config/pipeline-guard.yaml" \
  --project-root "$CHECKOUT" \
  --output-dir "$RETAINED_OUTPUT" \
  --authorization-state-dir "$DURABLE_AUTHORIZATION_STATE" \
  --workspace-parent "$DISPOSABLE_WORKSPACES" \
  "$RETAINED_INPUT/incident.json"
```

Omit `--authorization-state-dir` only when `unattended.remediation_enabled` is `false`.

## Model backend

The provider profile defaults to Anthropic and preserves the existing Claude Code authoring path;
its model, endpoint, and credential still need to be configured. The same entrypoint can
explicitly select an OpenAI-compatible backend for both diagnosis and authoring:

```yaml
runtime:
  model:
    provider: ollama # anthropic, databricks, openai, gemini, ollama, or openai-compatible
    name: llama3.3
    base_url: http://127.0.0.1:11434/v1
    # credential_ref: env:PIPELINE_GUARD_MODEL_API_KEY
```

CLI overrides use `--model-provider` and `--model`. Configuration values take precedence over the
generic and provider-specific variables below.

| Profile | Model | Base URL | Credential |
|---|---|---|---|
| `anthropic` | `ANTHROPIC_MODEL` | `ANTHROPIC_BASE_URL` (required by unattended preflight) | `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` |
| `databricks` | `DATABRICKS_MODEL`, otherwise `system.ai.meta-llama-3-3-70b-instruct` | `DATABRICKS_MODEL_BASE_URL`, otherwise `DATABRICKS_HOST/ai-gateway/mlflow/v1` | `DATABRICKS_TOKEN`, or both `DATABRICKS_MODEL_CLIENT_ID` and `DATABRICKS_MODEL_CLIENT_SECRET` for OAuth M2M |
| `openai` | `OPENAI_MODEL` | `OPENAI_BASE_URL`, otherwise `https://api.openai.com/v1` | `OPENAI_API_KEY` |
| `gemini` | `GEMINI_MODEL` | `GEMINI_BASE_URL`, otherwise the Gemini OpenAI-compatible endpoint | `GEMINI_API_KEY` |
| `ollama` | `OLLAMA_MODEL` | `OLLAMA_BASE_URL`, otherwise `http://127.0.0.1:11434/v1` | Optional `OLLAMA_API_KEY` |
| `openai-compatible` | `PIPELINE_GUARD_MODEL` | Required `PIPELINE_GUARD_MODEL_BASE_URL` | Optional `PIPELINE_GUARD_MODEL_API_KEY` |

All profiles also accept `PIPELINE_GUARD_MODEL_PROVIDER`, `PIPELINE_GUARD_MODEL`,
`PIPELINE_GUARD_MODEL_BASE_URL`, and `PIPELINE_GUARD_MODEL_API_KEY`. A custom service must
implement Chat Completions; unattended remediation also requires function-tool calls. The model
never receives shell, Git, provider, or authorization tools.

## Project policy

```yaml
unattended:
  remediation_enabled: false
  service_identity: sp:pipeline-guard
  authorization_secret_ref: env:PIPELINE_GUARD_POLICY_SECRET
  authorization_max_age_seconds: 900
  target_branch: null
  max_seconds: 3600
  max_model_tokens: 200000
```

Start with remediation disabled. Enabling it is a reviewed project change. `target_branch: null`
uses the branch whose commit failed. The authorization secret must contain at least 32 bytes and is
never passed to the authoring agent. For an enabled remediation, the file supplied through
`--config` must be tracked inside `--project-root` and must match its blob at
`code_version.commit`; otherwise the run stops with `INVALID_INPUT` before remediation.

## Local and unattended flows

Both modes use the same incident, diagnosis, eligibility, patch, test, Git, and draft-publication
contracts. They differ only at the authorization and orchestration boundaries:

1. In the **local flow**, a maintainer asks Claude Code to run the `diagnose` skill for an incident.
   An eligible diagnosis may proceed through the `remediate` skill only after that person explicitly
   confirms a local `approval_input_v1`. The skill prepares an isolated workspace, proposes and
   validates the patch, runs the configured tests, pushes the repair branch, and publishes a draft.
2. In the **unattended flow**, Stage A produces incident 1.1 and Stage B invokes `unattended_cli.py`.
   Project configuration replaces the interactive choices, and a signed, single-use policy
   authorization replaces the local confirmation. Every unresolved choice stops safely.

Neither mode edits the primary checkout, merges the draft, deploys, or reruns the failed pipeline.
See the plugin [local workflow](../../README.md#3-run-a-diagnosis) for the interactive commands and
approval contract.

## Outcomes

| Exit | Outcome | Reason codes |
|---:|---|---|
| 0 | `DRAFT_PR_PUBLISHED` | Draft created or reused |
| 3 | `REPORT_ONLY` | `NOT_ELIGIBLE`, `UNKNOWN_CAUSE`, `REMEDIATION_DISABLED`, `MISSING_INPUT` |
| 4 | `BLOCKED` | `AUTHORIZATION_INVALID`, `VALIDATION_FAILED`, `CHANGE_LIMIT_EXCEEDED`, `PR_TARGET_REFUSED`, `RECURSION_GUARD` |
| 5 | `LIMIT_REACHED` | `TIME_LIMIT`, `TOKEN_LIMIT`, `TOOL_CALL_LIMIT` |
| 1 | `ERROR` | `MODEL_UNAVAILABLE`, `CREDENTIAL_UNAVAILABLE`, `OPERATIONAL_FAILURE` |
| 2 | `ERROR` | `INVALID_INPUT`, `PREREQUISITE_MISSING` |

The host retains `result.json`, `summary.md`, and `artifacts/`. A result after push includes
`pushed_branch` and starts no further external operation.

## Decision catalog

| Decision | Resolution |
|---|---|
| Configuration and incident paths | `INPUT` from CLI arguments |
| Failed commit | `INPUT` from `code_version.commit`; absent → `SAFE_STOP` |
| Draft target | `CONFIG` from `unattended.target_branch`, otherwise `INPUT` from `code_version.branch`; absent → `SAFE_STOP` |
| Permission to repair | `CONFIG` opt-in plus signed policy authorization |
| Continue after diagnosis | `DOCUMENTED_DEFAULT` only for `ELIGIBLE_FOR_REMEDIATION` |
| Publish with failed or absent tests | `CONFIG` from `remediation.allow_unvalidated_draft`, default `false` |
| Retry the same failure | `DOCUMENTED_DEFAULT` to reuse the matching open draft |
| Several plausible fixes | `SAFE_STOP` with `BLOCKED` |
| Discard isolated workspace | `DOCUMENTED_DEFAULT` after success or when no commit exists; retain a clean committed workspace until its branch and draft PR are both published so the next invocation can resume without authoring again |
| Setup overwrite | Outside unattended mode; setup remains interactive |

Security, scope, authorization, and publication destination never use an implicit default.

## Support matrix

| Pipeline technology | Diagnosis | Unattended repair | Notes |
|---|---|---|---|
| dbt | Supported from declared `manifest.json` and `run_results.json` | Supported for eligible code changes within configured paths | Primary reference profile |
| Generic pipeline | Supported from normalized incident evidence | Supported when exact source paths and validation commands are declared | Uses the `mock` adapter for integration |
| Python | Generic diagnosis only | Not a specialized adapter | Configure exact `.py` paths and validation conservatively |
| Terraform | Generic diagnosis only | Not supported as a specialized repair profile | Plans, state, credentials, and applies remain out of scope |

Host support is separate from pipeline technology. The same host-neutral CLI runs on Databricks,
Bitbucket Pipelines, Airflow, or another isolated host after that host prepares the contract.

## Troubleshooting

- `PREREQUISITE_MISSING`: verify `uv`, Git, the selected model variables, provider token, writable
  output/workspace paths, and durable authorization state. Also verify Claude Code 2.1.295+ when
  `runtime.model.provider` is `anthropic`.
- `MISSING_INPUT`: Stage A did not provide both failed commit and branch.
- `AUTHORIZATION_INVALID`: rotate/check the policy secret and ensure the state directory persists
  atomically across retries. Never delete a consumption marker merely to force a retry.
- `CHANGE_LIMIT_EXCEEDED`: inspect the retained patch validation artifact; narrow evidence or have a
  person handle the change rather than expanding safety limits automatically.
- `VALIDATION_FAILED`: fix the project validation command or review the proposed change manually.
- `RECURSION_GUARD`: the failed branch already uses the configured remediation prefix. The host must
  exclude those branches from repair triggers.
- `PR_TARGET_REFUSED`: the branch was pushed but the provider rejected the target. Inspect
  `pushed_branch`, repository target policy, and provider permissions; the CLI does not retry PR
  creation after this terminal result.

## Security boundary

- Use separate service identities for host read, model use, restricted push, and draft publication.
- Stage A treats logs and artifacts as untrusted data. The reference prompt is convenience, not a
  security control.
- The model request layer receives only the selected model credential. File tools never receive
  model, push, provider, or policy credentials; all non-model secrets remain in deterministic
  executor processes.
- Project-declared validation commands execute outside the model and run exactly as configured.
  Commands that can modify a warehouse or another service must use an isolated, non-production
  target with least-privilege credentials; they must not approve or merge pull requests, deploy to
  production, or rerun the failed host pipeline. Pipeline Guard does not infer command side effects.
- Results, artifacts, standard output, and PR content are sanitized. Do not place arbitrary
  unlabeled credentials in source data; no redactor can identify every opaque secret reliably.
- Draft review and merge remain the human gate.
