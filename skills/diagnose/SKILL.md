---
name: diagnose
description: >
  Use this skill for every request whose primary intent is to diagnose,
  investigate, analyze, explain, or check a pipeline incident or failed
  pipeline/dbt run. This includes requests for an evidence-backed diagnosis,
  verified citations, or a safe UNKNOWN result when evidence is insufficient.
  Invoke it immediately before asking any questions or requesting files,
  manifests, run results, logs, or other evidence. Missing inputs are handled
  inside the skill and must never delay invocation. ALWAYS invoke it when the
  user explicitly mentions $diagnose. It runs the complete read-only Pipeline
  Guard flow and returns a validated diagnosis, deterministic eligibility
  decision, and neutral report. NEVER invoke it when the primary intent is
  plugin setup, configuration-only validation, fixing or editing code,
  remediation, Git operations, merges, or deployments.
triggers:
  - "diagnose pipeline failure"
  - "diagnose dbt run failure"
  - "investigate why dbt run failed"
  - "dbt manifest and run results"
  - "analyze pipeline incident"
  - "explain failed pipeline run"
  - "check pipeline incident"
  - "return unknown when evidence is insufficient"
  - "diagnosticar falha de pipeline"
  - "investigar incidente de pipeline"
  - "executar diagnóstico do pipeline-guard"
---

# Diagnose Pipeline Incident

Run the complete Pipeline Guard V1 diagnosis flow in read-only mode and return
the canonical result produced by the headless entrypoint.

## Scope and safety

Use this skill only to diagnose an incident. The flow may read the incident,
project configuration, and adapter artifacts needed as evidence. It must not:

- modify project files, source code, Git state, pipeline data, or a warehouse;
- create a branch, commit, pull request, merge, deployment, or remediation;
- infer a cause that is not supported by cited evidence;
- expose credentials, secrets, personal email addresses, or unsanitized logs;
- wait for human input after execution has started.

If the user asks to configure Pipeline Guard, use `pipeline-guard-setup`
instead. Remediation is outside the V1 diagnosis flow.

## Canonical execution path

Do not reconstruct the diagnostic stages with separate tool calls. Delegate the
entire execution to:

`${CLAUDE_PLUGIN_ROOT}/scripts/diagnose_cli.py`

That entrypoint is the single source of truth and runs, in order:

1. `incident_validator`;
2. the configured pipeline adapter;
3. `diagnosis-agent` with sanitized evidence;
4. `diagnosis_validator`;
5. `eligibility_gate`;
6. `report_renderer`;
7. sanitized result and run-manifest persistence.

Using the same entrypoint in interactive and headless execution guarantees the
same contracts, decisions, reports, artifacts, and exit codes for the same
incident and configuration.

The separately registered `diagnosis-agent` remains available for direct
plugin-agent invocation outside this skill. That compatibility path is not a
replacement for the canonical entrypoint above. It has no tools and accepts only
evidence supplied inline in its `Agent` input, which is covered by the same
registered redaction hook. It cannot open an evidence or artifact path.

## Resolve inputs

Determine these values before execution:

1. Consumer project root. Use the current Git repository root when available;
   otherwise use the current working directory and state that choice.
2. Project configuration. Default to
   `<consumer-root>/config/pipeline-guard.yaml` unless the user supplies a
   different path.
3. Incident input. In an interactive Claude Code session, require a path to a
   UTF-8 JSON `incident_input_v1`. The CLI also accepts the JSON document
   through standard input for external headless orchestrators.
4. Optional model override. Omit it unless the user or environment requires a
   specific configured model.

If the configuration or incident file is missing during an interactive
session, stop before execution and ask only for the missing path. Do not create
configuration or a temporary incident file as part of this skill, and do not
place incident contents directly in a shell command. The path itself must not
contain credentials or personal data. If the prompt guard blocks the request,
tell the user to retry with only a safe relative or absolute incident path.

## Execute

For an incident file, run:

```bash
uv run "${CLAUDE_PLUGIN_ROOT}/scripts/diagnose_cli.py" \
  --config "<consumer-root>/config/pipeline-guard.yaml" \
  --project-root "<consumer-root>" \
  "<incident-input-path>"
```

External headless orchestrators may instead omit the final file argument and
pass the document unchanged through standard input. The interactive skill must
not synthesize this form with a heredoc, inline command, or temporary file. Add
`--model "<model>"` only for an explicit model override.

The registered fail-closed `UserPromptSubmit` hook blocks diagnosis prompts with
recognized sensitive data before model processing and suppresses the original
prompt from model context. The `PreToolUse` hook is filtered to diagnosis-bound
`Agent`, `Bash`, and `PowerShell` calls and sanitizes their input before the tool
runs. Unrelated tool calls are not intercepted. A direct headless invocation
does not run inside a Claude Code hook lifecycle, so the entrypoint independently
applies the same sanitizer to the incident and collected evidence before the
model call and before any result, report, or manifest is persisted.

## Interpret the result

Treat the process exit code as authoritative:

- `0`: validated evidence-backed diagnosis completed;
- `1`: operational failure;
- `2`: invalid incident input or project configuration;
- `3`: safe stop with `cause_status=UNKNOWN` because available evidence was
  insufficient or an investigation limit was reached.

For exit codes `0` and `3`, parse the JSON document written to standard output
and present its diagnosis, eligibility decision, limitations, and neutral
report without changing or reclassifying them. For exit codes `1` and `2`,
report the sanitized error from standard error and the corresponding category.

Do not convert `UNKNOWN` into a guessed cause. Do not describe an eligibility
decision as permission to modify code or deploy.

## Completion report

Return:

- the incident identifier;
- cause status, failure family, confidence, and cited evidence;
- eligibility decision and reasons;
- limitations and the rendered neutral report;
- the process exit code;
- confirmation that the flow made no project, Git, warehouse, or deployment
  changes.

Preserve the canonical JSON result when the caller requests machine-readable
output. Do not add fields to or remove fields from that JSON.
