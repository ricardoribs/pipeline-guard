# Airflow setup

> Smoke validation pending: select DockerOperator or KubernetesPodOperator in the Astro sandbox,
> run the DAG failure path, and copy the exact validated DAG/operator configuration here.

## Prerequisites and installation

Build a non-root Stage B image containing `pipeline-guard`, Python 3.12+, `uv`, and Git. Include
Claude Code 2.1.295+ only for the `anthropic` backend. Commit and validate
`config/pipeline-guard.yaml`; prove report-only mode before enabling remediation. Airflow must
provide shared retained storage for Stage A input and
Stage B output, durable create-once authorization state across task retries, and an ephemeral
workspace parent inside the Stage B container.

Project-declared validation commands must use isolated, non-production targets and least-privilege
credentials whenever they can modify an external system. Pipeline Guard executes those commands as
written and does not infer their side effects.

Use separate Airflow Connections or secret-backend entries for the Stage A read identity, model
access, restricted Git push, draft-PR publication, and policy signing. Do not render their values in
templated commands, XCom, task logs, or DAG parameters.

## DAG pattern

Keep collection and repair separate:

1. The pipeline task fails normally.
2. A lightweight Stage A task with `trigger_rule="one_failed"` reads the DagRun/task instances,
   chooses the upstream-most failed task, and writes incident 1.1 plus artifacts to shared storage.
3. Stage B runs `unattended_cli.py` in a new unprivileged container with no interactive stdin.
4. A sentinel preserves the failed DagRun state. A successful repair never changes the original
   task or DagRun to success.

Do not combine this pattern with `fail_fast`/`fail_stop`; those modes constrain non-default trigger
rules. Tasks downstream of the original failure should remain `upstream_failed`.

```python
collect_context = PythonOperator(
    task_id="pipeline_guard_context",
    python_callable=collect_failed_context,
    trigger_rule="one_failed",
)

pipeline_guard = DockerOperator(  # or KubernetesPodOperator after sandbox validation
    task_id="pipeline_guard",
    image="<validated-unprivileged-image>",
    command=[
        "uv",
        "run",
        "/plugin/scripts/unattended_cli.py",
        "--mode",
        "unattended",
        "--config",
        "/checkout/config/pipeline-guard.yaml",
        "--project-root",
        "/checkout",
        "--output-dir",
        "/retained/output",
        "--authorization-state-dir",
        "/retained/authorization-state",
        "--workspace-parent",
        "/tmp/pipeline-guard-workspaces",
        "/retained/input/incident.json",
    ],
    user="65532:65532",
    do_xcom_push=False,
)
```

For Airflow 2.x, Stage A may use the stable REST API or scheduler-side ORM in a tightly scoped
collector. Prefer the stable REST API in Airflow 3.x/task-sdk deployments; do not depend on private
ORM internals from a task container. Record the chosen version and API in sandbox evidence.

With Cosmos, copy `manifest.json` and `run_results.json` to shared storage in a callback that runs
before worker cleanup. The incident contains relative artifact references and hashes, never XCom
payloads with raw logs. XCom may carry only small paths/identifiers.

Provide secrets through Airflow Connections/secret backend or Kubernetes secrets. Airflow masking
does not protect arbitrary values passed to subprocesses, so never echo the command environment.
The model request layer receives only the selected model credential. Model-controlled file tools
never receive Airflow, Git push, PR, or policy credentials.

The Stage B container needs shared read/write output, durable authorization state across retries,
and a disposable workspace. Interpret the exit code using the [common outcome table](README.md#outcomes)
while leaving the DagRun failure unchanged.

For the smoke run, record the Airflow/Astro version, selected operator, container user, failed task
and DagRun identifiers, final `result.json` outcome, final DagRun state, and proof that a retry uses
the same authorization-state location without exposing it to the authoring process.
