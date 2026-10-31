"""Contracts for Pipeline Guard's narrow Bitbucket/GitHub draft publication."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import requests

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from scripts import draft_pr_publisher
from scripts.draft_pr_publisher import (
    BitbucketRepairPRClient,
    GitHubRepairPRClient,
    RepairPRPublicationError,
    publish_request,
)
from scripts.pr_publication import (
    PipelineGuardDraftPRPublisher,
    PushedIncidentBranch,
    publish_draft_pr,
)
from scripts.pr_request_builder import PRRequest
from scripts.test_executor import (
    CommandResult,
)
from scripts.test_executor import (
    TestExecutorResult as ValidationExecutionResult,
)


def _reviewer_visible_body(incident_id: str, run_id: str) -> str:
    return f"## Incident\n\nID: `{incident_id}`\nRun: `{run_id}`\n"


def _payload(
    provider: str,
    *,
    incident_id: str = "INC-42",
    run_id: str = "run-42",
) -> dict[str, object]:
    return {
        "provider": provider,
        "repository": "delivery-dashboard",
        "repository_owner": "example-org",
        "title": "fix: remediate dbt failure",
        "body": _reviewer_visible_body(incident_id, run_id),
        "branch": "pipeline-guard/INC-42",
        "base": "main",
        "draft": True,
        "incident_id": incident_id,
        "run_id": run_id,
    }


def _provider_record(
    provider: str,
    *,
    identifier: int,
    incident_id: str = "INC-42",
    run_id: str = "run-42",
    draft: bool = True,
    source_repository: str | None = "example-org/delivery-dashboard",
) -> dict[str, object]:
    if provider == "bitbucket":
        source: dict[str, object] = {"branch": {"name": "pipeline-guard/INC-42"}}
        if source_repository is not None:
            source["repository"] = {"full_name": source_repository}
        return {
            "id": identifier,
            "state": "OPEN",
            "draft": draft,
            "description": _reviewer_visible_body(incident_id, run_id),
            "source": source,
            "destination": {"branch": {"name": "main"}},
            "links": {
                "html": {
                    "href": (
                        "https://bitbucket.org/example-org/delivery-dashboard/"
                        f"pull-requests/{identifier}"
                    )
                }
            },
        }
    head: dict[str, object] = {"ref": "pipeline-guard/INC-42"}
    if source_repository is not None:
        head["repo"] = {"full_name": source_repository}
    return {
        "number": identifier,
        "state": "open",
        "draft": draft,
        "body": _reviewer_visible_body(incident_id, run_id),
        "head": head,
        "base": {"ref": "main"},
        "html_url": f"https://github.com/example-org/delivery-dashboard/pull/{identifier}",
    }


@dataclass
class InMemoryRepairPRClient:
    provider: str
    records: list[dict[str, object]] = field(default_factory=list)
    create_calls: int = 0
    list_calls: int = 0
    closed: list[str] = field(default_factory=list)
    create_as_draft: bool = True
    conflict_after_create: bool = False

    @property
    def workspace(self) -> str | None:
        return "example-org" if self.provider == "bitbucket" else None

    @property
    def owner(self) -> str | None:
        return "example-org" if self.provider == "github" else None

    def list_pull_requests(
        self, repository: str, state: str
    ) -> list[dict[str, object]]:
        assert repository == "delivery-dashboard"
        assert state == ("OPEN" if self.provider == "bitbucket" else "open")
        self.list_calls += 1
        return list(self.records)

    def create_pull_request(
        self,
        repository: str,
        title: str,
        from_branch: str,
        to_branch: str,
        description: str,
        *,
        draft: bool,
    ) -> dict[str, object]:
        assert repository == "delivery-dashboard"
        assert title == "fix: remediate dbt failure"
        assert from_branch == "pipeline-guard/INC-42"
        assert to_branch == "main"
        assert description == _reviewer_visible_body("INC-42", "run-42").strip()
        assert draft is True
        self.create_calls += 1
        record = _provider_record(
            self.provider,
            identifier=len(self.records) + 1,
            draft=self.create_as_draft,
        )
        self.records.append(record)
        if self.conflict_after_create:
            response = requests.Response()
            response.status_code = 409
            raise requests.HTTPError("conflict", response=response)
        return record

    def close_pull_request(self, repository: str, pull_request_id: str) -> None:
        assert repository == "delivery-dashboard"
        self.closed.append(pull_request_id)


@pytest.mark.parametrize("provider", ["bitbucket", "github"])
def test_second_publication_reuses_the_same_repair_draft(provider: str) -> None:
    client = InMemoryRepairPRClient(provider)

    first = publish_request(_payload(provider), client=client)
    second = publish_request(_payload(provider), client=client)

    assert first["reused"] is False
    assert second == {**first, "reused": True}
    assert client.create_calls == 1
    assert client.list_calls == 2


@pytest.mark.parametrize("provider", ["bitbucket", "github"])
def test_different_run_does_not_reuse_another_repair_draft(provider: str) -> None:
    client = InMemoryRepairPRClient(
        provider,
        records=[_provider_record(provider, identifier=1, run_id="run-other")],
    )

    result = publish_request(_payload(provider), client=client)

    assert result["reused"] is False
    assert client.create_calls == 1


@pytest.mark.parametrize("provider", ["bitbucket", "github"])
@pytest.mark.parametrize("source_repository", ["external/delivery-dashboard", None])
def test_matching_identity_from_another_or_unknown_repository_is_not_reused(
    provider: str,
    source_repository: str | None,
) -> None:
    client = InMemoryRepairPRClient(
        provider,
        records=[
            _provider_record(
                provider,
                identifier=1,
                source_repository=source_repository,
            )
        ],
    )

    result = publish_request(_payload(provider), client=client)

    assert result["reused"] is False
    assert result["id"] == "2"
    assert client.create_calls == 1


@pytest.mark.parametrize("provider", ["bitbucket", "github"])
def test_matching_non_draft_blocks_duplicate_publication(provider: str) -> None:
    client = InMemoryRepairPRClient(
        provider,
        records=[_provider_record(provider, identifier=1, draft=False)],
    )

    with pytest.raises(RepairPRPublicationError, match="is not a draft"):
        publish_request(_payload(provider), client=client)

    assert client.create_calls == 0


@pytest.mark.parametrize("provider", ["bitbucket", "github"])
def test_create_conflict_is_reconciled_with_the_created_draft(provider: str) -> None:
    client = InMemoryRepairPRClient(provider, conflict_after_create=True)

    result = publish_request(_payload(provider), client=client)

    assert result["reused"] is True
    assert client.create_calls == 1
    assert client.list_calls == 2


@pytest.mark.parametrize("provider", ["bitbucket", "github"])
def test_provider_created_non_draft_is_closed(provider: str) -> None:
    client = InMemoryRepairPRClient(provider, create_as_draft=False)

    with pytest.raises(RepairPRPublicationError, match="it was closed"):
        publish_request(_payload(provider), client=client)

    assert client.closed == ["1"]


@pytest.mark.parametrize("provider", ["gitlab", "azure-devops"])
def test_unsupported_provider_is_rejected_before_credentials(provider: str) -> None:
    with pytest.raises(
        RepairPRPublicationError, match="unsupported repair PR provider"
    ):
        publish_request(_payload(provider))


def test_repository_owner_mismatch_stops_before_lookup() -> None:
    client = InMemoryRepairPRClient("github")
    payload = _payload("github")
    payload["repository_owner"] = "different-owner"

    with pytest.raises(RepairPRPublicationError, match="repository owner differs"):
        publish_request(payload, client=client)

    assert client.list_calls == 0


def test_body_without_visible_identity_is_rejected() -> None:
    payload = _payload("bitbucket")
    payload["body"] = "No incident identity"

    with pytest.raises(RepairPRPublicationError, match="must contain"):
        publish_request(payload, client=InMemoryRepairPRClient("bitbucket"))


def _request() -> PRRequest:
    return PRRequest(
        title="fix: remediate dbt failure",
        body=_reviewer_visible_body("INC-42", "run-42"),
        branch="pipeline-guard/INC-42",
        base="main",
        draft=True,
    )


def _test_result() -> ValidationExecutionResult:
    return ValidationExecutionResult(
        run_id="run-42",
        results=[CommandResult("test", "PASSED", 1.0, None)],
        validation_status="ALL_PASSED",
    )


def test_pipeline_guard_publisher_passes_only_the_neutral_request() -> None:
    calls: list[object] = []

    def request_publisher(value: object) -> dict[str, object]:
        calls.append(value)
        return {
            "id": "1",
            "url": "https://bitbucket.org/example-org/delivery-dashboard/pull-requests/1",
            "reused": False,
        }

    publisher = PipelineGuardDraftPRPublisher(
        provider="bitbucket",
        repository="delivery-dashboard",
        repository_owner="example-org",
        request_publisher=request_publisher,
    )

    result = publish_draft_pr(
        _request(),
        incident_id="INC-42",
        run_id="run-42",
        test_result=_test_result(),
        pushed_branch=PushedIncidentBranch(
            branch="pipeline-guard/INC-42",
            commit="a" * 40,
        ),
        allow_unvalidated_draft=False,
        publisher=publisher,
    )

    assert result.draft_pr == {
        "id": "1",
        "url": "https://bitbucket.org/example-org/delivery-dashboard/pull-requests/1",
    }
    assert calls == [_payload("bitbucket")]


@dataclass
class StubResponse:
    payload: object
    status_code: int = 200
    links: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def text(self) -> str:
        return "" if self.status_code == 204 else json.dumps(self.payload)

    def json(self) -> object:
        return self.payload


@dataclass
class RecordingSession:
    responses: list[StubResponse]
    headers: dict[str, str] = field(default_factory=dict)
    calls: list[tuple[str, str, dict[str, object]]] = field(default_factory=list)

    def get(self, url: str, **kwargs: object) -> StubResponse:
        return self._record("GET", url, kwargs)

    def request(self, method: str, url: str, **kwargs: object) -> StubResponse:
        return self._record(method, url, kwargs)

    def _record(self, method: str, url: str, kwargs: dict[str, object]) -> StubResponse:
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


@pytest.mark.parametrize("provider", ["bitbucket", "github"])
def test_real_provider_clients_cover_list_create_and_close(
    provider: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = _provider_record(provider, identifier=7)
    list_payload: object = {"values": []} if provider == "bitbucket" else []
    session = RecordingSession(
        [
            StubResponse(list_payload),
            StubResponse(created, status_code=201),
            StubResponse({}, status_code=200),
        ]
    )
    monkeypatch.setattr(draft_pr_publisher.requests, "Session", lambda: session)

    if provider == "bitbucket":
        monkeypatch.setenv("BITBUCKET_API_TOKEN", "test-token")
        monkeypatch.setenv("BITBUCKET_URL", "https://bitbucket.org/example-org")
        monkeypatch.delenv("BITBUCKET_EMAIL", raising=False)
        client: BitbucketRepairPRClient | GitHubRepairPRClient = (
            BitbucketRepairPRClient(workspace="example-org")
        )
        state = "OPEN"
    else:
        monkeypatch.setenv("GITHUB_API_TOKEN", "test-token")
        monkeypatch.setenv("GITHUB_URL", "https://github.com/example-org")
        client = GitHubRepairPRClient(owner="example-org")
        state = "open"

    assert client.list_pull_requests("delivery-dashboard", state) == []
    assert (
        client.create_pull_request(
            "delivery-dashboard",
            "fix: remediate dbt failure",
            "pipeline-guard/INC-42",
            "main",
            _reviewer_visible_body("INC-42", "run-42"),
            draft=True,
        )
        == created
    )
    client.close_pull_request("delivery-dashboard", "7")

    assert session.headers["Authorization"] == "Bearer test-token"
    assert [call[0] for call in session.calls] == (
        ["GET", "POST", "POST"] if provider == "bitbucket" else ["GET", "POST", "PATCH"]
    )
    create_payload = session.calls[1][2]["json"]
    assert isinstance(create_payload, Mapping)
    assert create_payload["draft"] is True


@pytest.mark.parametrize("provider", ["bitbucket", "github"])
@pytest.mark.parametrize("conflict_status", [409, 422])
def test_real_provider_clients_reconcile_create_conflict(
    provider: str,
    conflict_status: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matching_draft = _provider_record(provider, identifier=7)
    empty_list: object = {"values": []} if provider == "bitbucket" else []
    matching_list: object = (
        {"values": [matching_draft]} if provider == "bitbucket" else [matching_draft]
    )
    session = RecordingSession(
        [
            StubResponse(empty_list),
            StubResponse({"error": "conflict"}, status_code=conflict_status),
            StubResponse(matching_list),
        ]
    )
    monkeypatch.setattr(draft_pr_publisher.requests, "Session", lambda: session)

    if provider == "bitbucket":
        monkeypatch.setenv("BITBUCKET_API_TOKEN", "test-token")
        monkeypatch.setenv("BITBUCKET_URL", "https://bitbucket.org/example-org")
        monkeypatch.delenv("BITBUCKET_EMAIL", raising=False)
        client: BitbucketRepairPRClient | GitHubRepairPRClient = (
            BitbucketRepairPRClient(workspace="example-org")
        )
    else:
        monkeypatch.setenv("GITHUB_API_TOKEN", "test-token")
        monkeypatch.setenv("GITHUB_URL", "https://github.com/example-org")
        client = GitHubRepairPRClient(owner="example-org")

    result = publish_request(_payload(provider), client=client)

    assert result == {
        "id": "7",
        "url": (
            "https://bitbucket.org/example-org/delivery-dashboard/pull-requests/7"
            if provider == "bitbucket"
            else "https://github.com/example-org/delivery-dashboard/pull/7"
        ),
        "reused": True,
    }
    assert [call[0] for call in session.calls] == ["GET", "POST", "GET"]


def test_standalone_script_rejects_unsupported_provider() -> None:
    completed = subprocess.run(
        ["uv", "run", str(PLUGIN_ROOT / "scripts" / "draft_pr_publisher.py")],
        input=json.dumps(_payload("gitlab")),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 1
    assert "unsupported repair PR provider" in completed.stderr
    assert completed.stdout == ""
