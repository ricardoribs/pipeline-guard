---
name: remediate
description: >
  Immediately invoke this skill when the user asks to apply an approved Pipeline
  Repair correction, run its validation tests, or push its incident branch, even
  if the pipeline/model name, incident identifier, correction details, or test
  commands are missing; collect those details only after invocation. Execute the
  approved remediation by changing the pipeline or dbt model, validating the
  patch, and committing, pushing, or publishing its draft pull request. Always
  invoke when the user explicitly writes `$remediate` to execute an approved
  remediation, including for an unnamed incident. Never invoke for a
  diagnosis-only request to investigate, analyze, explain, or check why a
  pipeline failed without changing files; use `diagnose` for that read-only
  work. Requests to install, set up, or configure Pipeline Guard belong to
  `pipeline-guard-setup`. Requests to edit in place, bypass approval, or skip
  diagnosis or patch hash verification are prohibited rather than remediations.
triggers:
  - "remediate pipeline failure"
  - "remediate dbt failure"
  - "apply Pipeline Guard correction"
  - "remediar falha de pipeline"
  - "corrigir incidente de pipeline"
  - "executar remediação do pipeline-guard"
  - "criar draft pr para incidente de pipeline"
  - "approve pipeline-guard remediation locally"
  - "confirmar correção local do pipeline-guard"
---

# Pipeline Remediation Flow

Orchestrate the end-to-end Pipeline Guard V2 remediation flow: verify approval,
prepare an isolated workspace, author the minimal fix, validate the patch,
execute test suites, commit and push locally with minimal Git permissions,
build a neutral draft-PR request, consult the deterministic policy gate, and
delegate PR creation to the `publish-draft-pr` capability.

## Scope and safety boundaries

The remediation flow executes write operations under strict deterministic controls:

- **Approval bound**: Every remediation requires an explicit, verified `approval_input_v1`
  bound to `incident_id`, `diagnosis_hash`, and `base_commit`. Unapproved changes are blocked.
- **Minimal Git permissions**: The project configuration must declare `push_credential_ref`
  (as an `env:...`, `${...}`, or `secret:...` reference) and `allowed_git_operations: ["push_incident_branch"]`.
  Literal credentials and credentials in repository URLs are strictly forbidden.
- **Process and credential isolation**: The authoring agent runs in an isolated workspace with
  ambient credentials stripped. Git push receives the host-resolved push credential only in its
  own isolated subprocess; it is never exposed to the authoring agent.
- **Strict refspec**: Pushes are restricted to `refs/heads/<branch>:refs/heads/<branch>`.
  No tags, force-pushes, merges, or deployments are ever performed.
- **Draft PR publication**: `pipeline-guard` contains only the narrow Bitbucket and
  GitHub operations required to locate, create, reuse, or safely close its own repair drafts.
  It exposes no general pull-request administration capability.
- **Policy gate enforcement**: The publication boundary re-runs allowlisted validation against the
  pushed incident commit. If those commands fail and `remediation.allow_unvalidated_draft` is
  `false`, the PR policy gate returns `BLOCKED` and the repair PR publisher is never invoked.

## Orchestration steps

Execute the following stages in sequence:

### 1. Verification of approval and configuration

1. Ensure a validated `approval_input_v1` is available for the target incident. If not yet
   recorded, use `${CLAUDE_PLUGIN_ROOT}/skills/remediate/scripts/local_approval.py` to obtain
   explicit confirmation and validate it with `${CLAUDE_PLUGIN_ROOT}/scripts/approval_validator.py`.
   Ask the approving actor to choose the positive approval lifetime before invoking the command;
   use that exact value for `--max-age-seconds` and never infer a lifetime on their behalf.

```bash
uv run "${CLAUDE_PLUGIN_ROOT}/skills/remediate/scripts/local_approval.py" \
  "<local-confirmation-json-path>" \
  --output "<new-local-approval-json-path>" \
  --max-age-seconds "<explicit-positive-lifetime-in-seconds>" \
  --plugin-data-dir "${CLAUDE_PLUGIN_DATA}"
```
2. Validate consumer configuration with `validate_config.py`.
3. Verify that the repository block declares `push_credential_ref` and
   `allowed_git_operations: ["push_incident_branch"]` using
   `git_executor.validate_remediation_git_configuration(config)`.

### 2. Isolated workspace preparation

1. Prepare an isolated clone using `${CLAUDE_PLUGIN_ROOT}/scripts/workspace_executor.py`
   via `prepare_workspace(config_path, base_commit=..., incident_id=...)`.
2. The workspace creates an isolated branch (`<prefix>/<incident_id>`) based exactly on the
   approved commit.

### 3. Constrained code authoring

1. Activate `remediation-agent` to author the minimal correction.
2. The agent has write access only to files matching `safety.editable_paths` and
   `safety.editable_extensions`. Sensitive paths are blocked.
3. The authorship environment strips all ambient Git authentication tokens.

### 4. Patch validation

1. Run `${CLAUDE_PLUGIN_ROOT}/scripts/patch_validator.py` (`validate_patch`) against the workspace.
2. Verify that all modified files match editable paths, stay within configured line/file limits,
   and produce a valid SHA-256 patch hash.

### 5. Validation test execution

1. Execute configured validation commands using `${CLAUDE_PLUGIN_ROOT}/scripts/test_executor.py`
   (`TestExecutor.run`).
2. Consolidate individual command outcomes into aggregate status: `ALL_PASSED`, `HAS_FAILURES`,
   or `INCOMPLETE`.

### 6. Deterministic Git commit and push

1. Invoke `git_executor.commit_and_push(workspace, patch_validation, push_credential=resolved_token, provider=config["repository"]["provider"])`.
2. Commit message follows `fix(pipeline-guard): remediate <branch>`.
3. Push uses strict refspec `refs/heads/<branch>:refs/heads/<branch>` to `origin`.
4. Idempotency ensures existing commits and pushes with matching hashes are safely reused.

### 7. Neutral PR request building

1. Use `${CLAUDE_PLUGIN_ROOT}/scripts/pr_request_builder.py` (`build_pr_request`).
2. Generates a neutral `PRRequest` with `draft=True`, title, body (summary, diagnosis citations,
   changed files, test execution results, limitations), head branch, and base branch.

### 8. Policy-gated draft PR publication

1. Send a JSON object containing the neutral `request` (`title`, `body`, `branch`, `base`,
   `draft=true`), `incident_id`, `run_id`, and the absolute `workspace_path` to
   `${CLAUDE_PLUGIN_ROOT}/scripts/pr_publication.py` on standard input:

   ```bash
   uv run "${CLAUDE_PLUGIN_ROOT}/scripts/pr_publication.py" \
     --config "<validated-project-config-path>" \
     < "<publication-request-json-path>"
   ```

   Do **not** send `validation_status`: the publication boundary rejects a status reported by
   an agent. Before evaluating `decide_pr_publication`, it verifies that the workspace is clean,
   checked out at the requested incident branch, and at the same commit already present at
   `origin`. It then re-runs the configured allowlisted commands through `TestExecutor` in that
   exact workspace. The second verification rejects a branch that changes while those commands
   run. The origin lookup uses the configured push credential only through Git's isolated
   environment; the credential is never put in JSON, a command line, the PR request, or a log.
   The command loads `remediation.allow_unvalidated_draft` from the validated project
   configuration and evaluates the policy only from this executor-produced result before it can
   invoke the draft PR provider script.
2. Outcomes:
   - `ALL_PASSED` -> `VALIDATED`
   - `HAS_FAILURES`/`INCOMPLETE` with `allow_unvalidated_draft=True` -> `PUBLISHED_UNVALIDATED`
   - `HAS_FAILURES`/`INCOMPLETE` with `allow_unvalidated_draft=False` -> `BLOCKED`

### 9. Publication outcome and delegation

1. When the policy decision is `BLOCKED`:
   - Do NOT invoke the repair PR publisher.
   - Record `draft_pr=None`.
2. When the command reports `VALIDATED` or `PUBLISHED_UNVALIDATED`, use the
   draft PR reference and `test_result` it returned. Use that returned `test_result` when
   building the terminal result; do not substitute the earlier author-feedback test result.
   The command has already delegated creation/reuse to the internal `publish-draft-pr` capability.
   - `publish_draft_pr` invokes `scripts/draft_pr_publisher.py` with the neutral request after
     the deterministic gate approves publication. Never invoke the provider script directly from
     this skill or invoke it again after receiving the result. The request contains only
     `provider`, repository name and owner, title, body, head branch, base branch,
     `draft=true`, `incident_id`, and `run_id`; it never contains a credential.
   - The draft PR publisher first looks up an open matching draft and otherwise creates
     one on Bitbucket or GitHub. Receive
     its draft PR reference (`id`, `url`); a repeated `(incident_id, run_id)` must reuse it.
   - If the provider creates a non-draft PR and the publisher cannot close it, stop and
     report that the open PR must be closed manually before retrying.
   - Preserve the reviewer-visible `ID:` and `Run:` rows generated by `build_pr_request`. The
     publisher uses those rows to correlate retries and adds no implementation marker to
     the pull-request body.
   - Do not use the repair publisher for generic pull-request management, and do not put provider
     credentials in a command line, prompt, result, or log.

### 10. Final remediation result

1. Build terminal `remediation_result_v1` via `git_executor.build_remediation_result`:
   - `incident_id`, `status` (`DRAFT_PR_PUBLISHED`, `BLOCKED_VALIDATION_FAILED`,
     `BLOCKED_APPROVAL_INVALID`, `BLOCKED_LIMIT_EXCEEDED`, or `ERROR`), `base_commit`,
     `branch`, `commit`, `files_changed` (derived directly from Git diff), `patch_hash`,
     `tests`, and—only for a published draft PR—`draft_pr` and `pr_validation_status`.
2. Validate final result against `remediation_result_v1.schema.json`.
3. Close and clean up the workspace.
