# /// script
# requires-python = ">=3.12"
# dependencies = ["anthropic>=0.40.0", "httpx>=0.27.0", "jsonschema>=4.0.0", "pyyaml>=6.0.3"]
# ///
"""Headless V1 diagnosis, eligibility and reports with terminal run observability.

Exit codes: 0 for a validated diagnosis; 1 for an operational failure;
2 for invalid incident/configuration input; 3 for a safe UNKNOWN diagnosis.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import traceback
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anthropic
from anthropic.types import MessageParam
from jsonschema import Draft202012Validator

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

# These runtime imports must follow sys.path injection; Ruff versions classify them differently.
# isort: off
from adapters.registry import build_default_registry
from scripts.artifact_store import LocalArtifactStore
from scripts.diagnosis_validator import validate_diagnosis
from scripts.eligibility_gate import evaluate_eligibility
from scripts.execution_budget import ExecutionBudget, ExecutionBudgetExceeded
from scripts.incident_validator import (
    IncidentValidationError,
    validate_incident_input,
)
from scripts.model_backend import (
    ModelBackendConfigurationError,
    ModelSettings,
    openai_chat_completion,
    resolve_model_settings,
)
from scripts.redaction import redact_payload
from scripts.report_renderer import render_report
from scripts.run_manifest import (
    build_run_manifest,
    derive_manifest_run_id,
    load_run_manifest,
    persist_run_manifest,
)
from scripts.validate_config import ConfigValidationError, validate_config
# isort: on

AGENT_PATH = Path(__file__).resolve().parents[1] / "agents" / "diagnosis-agent.md"
_LOCAL_TOKEN_BOUND_OVERHEAD = 512


def load_agent_prompt(document: str | None = None) -> str:
    """Use the agent body, including its read-only diagnostic rules."""
    text = AGENT_PATH.read_text(encoding="utf-8") if document is None else document
    parts = text.split("---", maxsplit=2)
    if len(parts) != 3 or not parts[2].strip():
        raise ValueError(
            "diagnosis-agent must contain frontmatter and a non-empty prompt"
        )
    return parts[2].strip()


class DiagnosisRunner:
    """Use Anthropic or OpenAI-compatible model APIs with bounded requests."""

    def __init__(
        self,
        model: str | None = None,
        *,
        provider: str | None = None,
        project_config: Mapping[str, Any] | None = None,
        environment: Mapping[str, str] | None = None,
        settings: ModelSettings | None = None,
        client: Any = None,
    ) -> None:
        self.settings: ModelSettings = settings or resolve_model_settings(
            project_config,
            provider=provider,
            model=model,
            environment=environment,
        )
        self.model = self.settings.model
        self.provider = self.settings.profile
        self._client = client

    def __call__(
        self,
        payload: Mapping[str, Any],
        budget: ExecutionBudget,
        agent_prompt: str,
    ) -> object:
        if self.settings.provider == "openai-compatible":
            return self._call_openai_compatible(payload, budget, agent_prompt)
        return self._call_anthropic(payload, budget, agent_prompt)

    def _call_anthropic(
        self,
        payload: Mapping[str, Any],
        budget: ExecutionBudget,
        agent_prompt: str,
    ) -> object:
        client = self._client or _anthropic_client(self.settings)
        messages: list[MessageParam] = [
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}
        ]
        try:
            try:
                counted = budget.call_tool(
                    lambda: client.messages.count_tokens(
                        model=self.model,
                        system=agent_prompt,
                        messages=messages,
                        timeout=budget.remaining_seconds,
                    )
                )
                input_tokens = counted.input_tokens
            except anthropic.BadRequestError as error:
                if not _is_unavailable_unity_gateway_token_count(error):
                    raise
                input_tokens = _conservative_input_token_bound(agent_prompt, messages)
            budget.record_tokens(input_tokens)
            output_limit = budget.remaining_tokens - 1
            if output_limit < 1:
                raise ExecutionBudgetExceeded("max_tokens")
            response = budget.call_tool(
                lambda: client.messages.create(
                    model=self.model,
                    system=agent_prompt,
                    messages=messages,
                    max_tokens=output_limit,
                    timeout=budget.remaining_seconds,
                )
            )
        except anthropic.APITimeoutError as error:
            raise ExecutionBudgetExceeded("max_seconds") from error

        # Account for actual usage, including cached input, before accepting any output.
        usage = response.usage
        actual_input = (
            usage.input_tokens
            + (getattr(usage, "cache_creation_input_tokens", None) or 0)
            + (getattr(usage, "cache_read_input_tokens", None) or 0)
        )
        budget.record_tokens(max(0, actual_input - input_tokens) + usage.output_tokens)
        if response.stop_reason != "end_turn":
            return None
        content = "".join(
            block.text for block in response.content if block.type == "text"
        )
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return None

    def _call_openai_compatible(
        self,
        payload: Mapping[str, Any],
        budget: ExecutionBudget,
        agent_prompt: str,
    ) -> object:
        user_message = json.dumps(payload, ensure_ascii=False)
        messages: list[dict[str, str]] = [
            {"role": "system", "content": agent_prompt},
            {"role": "user", "content": user_message},
        ]
        input_bound = _conservative_input_token_bound(
            agent_prompt,
            [{"role": "user", "content": user_message}],
        )
        budget.record_tokens(input_bound)
        output_limit = budget.remaining_tokens - 1
        if output_limit < 1:
            raise ExecutionBudgetExceeded("max_tokens")
        response = budget.call_tool(
            lambda: openai_chat_completion(
                self.settings,
                {
                    "model": self.model,
                    "messages": messages,
                    "max_tokens": output_limit,
                    "stream": False,
                },
                timeout=budget.remaining_seconds,
                client=self._client,
            )
        )
        choice = _openai_choice(response)
        usage = response.get("usage")
        output_tokens = _openai_output_tokens(usage, choice.get("message"))
        budget.record_tokens(output_tokens)
        if choice.get("finish_reason") != "stop":
            return None
        message = choice.get("message")
        if not isinstance(message, Mapping) or not isinstance(
            message.get("content"), str
        ):
            return None
        try:
            return json.loads(message["content"])
        except json.JSONDecodeError:
            return None


def _openai_choice(response: Mapping[str, Any]) -> Mapping[str, Any]:
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        return {}
    choice = choices[0]
    return choice if isinstance(choice, Mapping) else {}


def _openai_output_tokens(usage: object, message: object) -> int:
    if isinstance(usage, Mapping):
        value = usage.get("completion_tokens")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    serialized = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    return len(serialized.encode("utf-8")) + _LOCAL_TOKEN_BOUND_OVERHEAD


def _is_unavailable_unity_gateway_token_count(error: anthropic.BadRequestError) -> bool:
    """Recognize Unity Gateway's explicit unsupported managed-path response."""
    message = str(error).lower()
    return (
        error.status_code == 400
        and "count_tokens" in message
        and "unmanaged api request" in message
        and "databricks-model-provider-service" in message
    )


def _anthropic_client(settings: ModelSettings) -> anthropic.Anthropic:
    arguments: dict[str, Any] = {"max_retries": 0}
    if settings.base_url is not None:
        arguments["base_url"] = settings.base_url
    if settings.api_key is not None:
        credential_argument = (
            "api_key" if settings.credential_kind == "api-key" else "auth_token"
        )
        arguments[credential_argument] = settings.api_key
    return anthropic.Anthropic(**arguments)


def _conservative_input_token_bound(
    agent_prompt: str,
    messages: Sequence[Mapping[str, object]],
) -> int:
    """Bound visible UTF-8 input plus a fixed allowance for protocol tokens."""
    serialized = json.dumps(
        {"system": agent_prompt, "messages": messages},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return len(serialized.encode("utf-8")) + _LOCAL_TOKEN_BOUND_OVERHEAD


Runner = Callable[[Mapping[str, Any], ExecutionBudget, str], object]


def run_diagnosis(
    incident: object,
    *,
    project_config: Mapping[str, Any],
    project_root: Path,
    runner: Runner,
    agent_document: str | None = None,
    budget: ExecutionBudget | None = None,
) -> dict[str, Any]:
    """Return validated contracts and reports, never editing the consumer project."""
    validated_incident = validate_incident_input(incident)
    active_budget = budget or ExecutionBudget(project_config)
    registry = build_default_registry()
    adapter_name = project_config["pipeline"]["adapter"]
    factory = registry.resolve(adapter_name, allowed_names=registry.registered_names)
    root = project_root / project_config["pipeline"].get("root", ".")
    adapter: Any = (
        factory(artifacts_dir=root / "target") if adapter_name == "dbt" else factory()
    )
    evidences: list[dict[str, Any]] = []
    raw_diagnosis: object = None
    exhausted = False
    try:
        collected = active_budget.call_tool(lambda: adapter.run(validated_incident))
        schema = json.loads(
            (
                PLUGIN_ROOT / "contracts/schema/pipeline_adapter_interface.schema.json"
            ).read_text(encoding="utf-8")
        )
        Draft202012Validator(schema).validate(collected)
        safe_collection = redact_payload(collected)
        evidences = safe_collection["evidences"]
        for evidence in evidences:
            if "content" in evidence:
                evidence["hash"] = (
                    "sha256:"
                    + hashlib.sha256(evidence["content"].encode("utf-8")).hexdigest()
                )
        if evidences:
            raw_diagnosis = runner(
                {
                    "incident": redact_payload(validated_incident),
                    "evidences": evidences,
                    "adapter_details": safe_collection["adapter_details"],
                },
                active_budget,
                load_agent_prompt(agent_document),
            )
        else:
            raw_diagnosis = {
                "schema_version": "1.0",
                "incident_id": validated_incident["incident_id"],
                "failure_family": "UNKNOWN",
                "confidence": "LOW",
                "cause_status": "UNKNOWN",
                "cause": "No evidence was available for a grounded diagnosis.",
                "evidence_citations": [],
                "limitations": ["insufficient_evidence"],
            }
        active_budget.check()
    except ExecutionBudgetExceeded:
        exhausted = True

    diagnosis = validate_diagnosis(
        redact_payload(raw_diagnosis),
        incident_id=validated_incident["incident_id"],
        evidences=evidences,
        investigation_budget_exhausted=exhausted,
    )
    decision = evaluate_eligibility(diagnosis, project_config=project_config)
    markdown, report = render_report(diagnosis, decision)
    return {
        "exit_code": 3 if diagnosis["cause_status"] == "UNKNOWN" else 0,
        "diagnosis": diagnosis,
        "eligibility_decision": decision,
        "report": report,
        "report_markdown": markdown,
        "evidences": evidences,
    }


def _persist_terminal_manifest(
    incident: object,
    *,
    attempt_payload: object,
    status: str,
    started_at: datetime,
    limitation: str,
) -> None:
    if not isinstance(incident, dict):
        return

    incident_id = incident.get("incident_id")
    source_run_id = incident.get("run_id")
    if not isinstance(incident_id, str) or not (
        source_run_id is None or isinstance(source_run_id, str)
    ):
        return
    manifest_run_id = derive_manifest_run_id(
        incident_id=incident_id,
        source_run_id=source_run_id,
        attempt_payload=attempt_payload,
    )

    manifest = build_run_manifest(
        run_id=manifest_run_id,
        incident_id=incident_id,
        status=status,
        started_at=started_at,
        finished_at=datetime.now(UTC),
        limitations=(limitation,),
    )
    persist_run_manifest(manifest, artifact_store=LocalArtifactStore())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Diagnose a pipeline incident without prompts."
    )
    parser.add_argument(
        "input_file",
        nargs="?",
        type=Path,
        help="JSON input file; reads stdin when omitted.",
    )
    parser.add_argument(
        "--config", type=Path, help="materialized project configuration"
    )
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--model-provider",
        choices=(
            "anthropic",
            "databricks",
            "openai",
            "gemini",
            "ollama",
            "openai-compatible",
        ),
        help="model API profile; defaults to runtime.model or anthropic",
    )
    parser.add_argument(
        "--model",
        help="diagnostic model; defaults to runtime.model or provider environment",
    )
    args = parser.parse_args(argv)
    started_at = datetime.now(UTC)

    try:
        if args.input_file is None:
            payload = json.load(sys.stdin)
        else:
            with args.input_file.open(encoding="utf-8") as input_file:
                payload = json.load(input_file)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        print(f"Invalid incident_input_v1: {type(error).__name__}", file=sys.stderr)
        return 2

    try:
        incident = validate_incident_input(payload)
    except IncidentValidationError as error:
        try:
            _persist_terminal_manifest(
                payload,
                attempt_payload=payload,
                status="FAILED",
                started_at=started_at,
                limitation="Incident validation failed before evidence collection.",
            )
        except (OSError, ValueError, TypeError) as manifest_error:
            print(
                f"Could not persist run_manifest_v1: {type(manifest_error).__name__}",
                file=sys.stderr,
            )
        print(f"Invalid incident_input_v1: {type(error).__name__}", file=sys.stderr)
        return 2

    if args.config is None:
        print("Invalid project configuration: --config is required", file=sys.stderr)
        return 2
    try:
        config = validate_config(args.config)
    except ConfigValidationError as error:
        print(
            f"Invalid project configuration: {error.code} at {error.field}",
            file=sys.stderr,
        )
        return 2

    try:
        runner = DiagnosisRunner(
            args.model,
            provider=args.model_provider,
            project_config=config,
        )
    except ModelBackendConfigurationError as error:
        print(
            f"Invalid project configuration: {error}",
            file=sys.stderr,
        )
        return 2

    run_id: str | None = None
    store: LocalArtifactStore | None = None
    attempt: dict = {"incident": incident, "config": config}
    try:
        store = LocalArtifactStore()
        if config.get("runtime", {}).get("artifact_store", "local") != "local":
            raise ValueError("the headless CLI requires the local artifact store")
        if config.get("evidence_providers"):
            raise ValueError(
                "the headless CLI currently collects adapter evidence only"
            )
        attempt = {
            "incident": incident,
            "config": config,
            "project_root": str(args.project_root.resolve()),
            "model_provider": runner.provider,
            "model": runner.model,
            "agent_hash": hashlib.sha256(AGENT_PATH.read_bytes()).hexdigest(),
        }
        run_id = derive_manifest_run_id(
            incident_id=incident["incident_id"],
            source_run_id=incident["run_id"],
            attempt_payload=attempt,
        )
        if store.exists(run_id, "manifest.json"):
            manifest = load_run_manifest(run_id=run_id, artifact_store=store)
            if manifest["status"] in {"COMPLETED", "STOPPED_UNKNOWN"}:
                result = json.loads(store.get(run_id, "result.json"))
                print(json.dumps(result, ensure_ascii=False))
                return result["exit_code"]

        # Save the complete result first so retries can finish interrupted artifact writes.
        if store.exists(run_id, "result.json"):
            result = json.loads(store.get(run_id, "result.json"))
        else:
            result = run_diagnosis(
                incident,
                project_config=config,
                project_root=args.project_root,
                runner=runner,
            )
        for reference, document in (
            ("result.json", result),
            ("diagnosis.json", result["diagnosis"]),
            ("eligibility.json", result["eligibility_decision"]),
            ("report.json", result["report"]),
        ):
            serialized = json.dumps(
                document, ensure_ascii=False, sort_keys=True
            ).encode("utf-8")
            if (
                not store.put(run_id, reference, serialized)
                and store.get(run_id, reference) != serialized
            ):
                raise ValueError(
                    "an immutable result differs for this diagnosis attempt"
                )
        markdown = result["report_markdown"].encode("utf-8")
        if (
            not store.put(run_id, "report.md", markdown)
            and store.get(run_id, "report.md") != markdown
        ):
            raise ValueError("an immutable report differs for this diagnosis attempt")
        manifest = build_run_manifest(
            run_id=run_id,
            incident_id=incident["incident_id"],
            status="STOPPED_UNKNOWN" if result["exit_code"] == 3 else "COMPLETED",
            started_at=started_at,
            finished_at=datetime.now(UTC),
            evidences=result["evidences"],
            limitations=result["diagnosis"].get("limitations", []),
        )
        persist_run_manifest(manifest, artifact_store=store)
    except Exception as error:  # noqa: BLE001
        # SDK/source/persistence failures are operational errors, not invalid incidents.
        # Do not echo provider responses, credentials or evidence into stderr.
        print(f"Diagnosis failed: {type(error).__name__}", file=sys.stderr)
        # Log locations only; source lines and exception text may contain secrets.
        for frame, line_number in traceback.walk_tb(error.__traceback__):
            print(f"  {frame.f_code.co_filename}:{line_number}", file=sys.stderr)
        if store is not None:
            try:
                failure_id = derive_manifest_run_id(
                    incident_id=incident["incident_id"],
                    source_run_id=incident["run_id"],
                    attempt_payload={
                        "attempt": attempt,
                        "operational_failure": type(error).__name__,
                    },
                )
                failed_manifest = build_run_manifest(
                    run_id=failure_id,
                    incident_id=incident["incident_id"],
                    status="FAILED",
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                    limitations=(
                        "Operational diagnosis failure: " + type(error).__name__,
                    ),
                )
                persist_run_manifest(failed_manifest, artifact_store=store)
            except (OSError, ValueError, TypeError) as manifest_error:
                print(
                    "Could not persist failed run_manifest_v1: "
                    + type(manifest_error).__name__,
                    file=sys.stderr,
                )
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
