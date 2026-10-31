# /// script
# requires-python = ">=3.12"
# dependencies = ["requests>=2.31"]
# ///
"""Publish or reuse a Pipeline Guard draft PR on Bitbucket or GitHub."""

from __future__ import annotations

import base64
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests

_SUPPORTED_PROVIDERS = {"bitbucket", "github"}
_OPEN_STATES = {"bitbucket": "OPEN", "github": "open"}
_REQUEST_TIMEOUT_SECONDS = 30


class RepairPRPublicationError(ValueError):
    """Raised when a repair draft PR cannot be published safely."""


class RepairPRClientError(OSError):
    """Raised when a draft PR provider client cannot be configured."""


@dataclass(frozen=True)
class RepairPRRequest:
    """Validated input for one Pipeline Guard draft PR."""

    provider: str
    repository: str
    repository_owner: str
    title: str
    body: str
    branch: str
    base: str
    incident_id: str
    run_id: str

    @classmethod
    def from_mapping(cls, value: object) -> RepairPRRequest:
        """Accept only the neutral fields produced by Pipeline Guard."""
        if not isinstance(value, Mapping):
            raise RepairPRPublicationError("repair PR request must be an object")
        expected = {
            "provider",
            "repository",
            "repository_owner",
            "title",
            "body",
            "branch",
            "base",
            "draft",
            "incident_id",
            "run_id",
        }
        if set(value) != expected:
            raise RepairPRPublicationError(
                "repair PR request contains unsupported or missing fields"
            )
        if value["draft"] is not True:
            raise RepairPRPublicationError("repair PR request must set draft=true")
        provider = _required_text(value["provider"], "provider").lower()
        if provider not in _SUPPORTED_PROVIDERS:
            raise RepairPRPublicationError(
                f"unsupported repair PR provider: {provider}"
            )
        return cls(
            provider=provider,
            repository=_required_text(value["repository"], "repository"),
            repository_owner=_required_text(
                value["repository_owner"], "repository_owner"
            ),
            title=_required_text(value["title"], "title"),
            body=_required_text(value["body"], "body"),
            branch=_required_text(value["branch"], "branch"),
            base=_required_text(value["base"], "base"),
            incident_id=_required_text(value["incident_id"], "incident_id"),
            run_id=_required_text(value["run_id"], "run_id"),
        )

    @property
    def visible_identity(self) -> tuple[str, str]:
        """Return the incident and run identity visible in the PR body."""
        identity = _visible_identity(self.body)
        if identity is None:
            raise RepairPRPublicationError(
                "repair PR body must contain non-empty 'ID:' and 'Run:' lines"
            )
        return identity


@dataclass(frozen=True)
class RepairPRReference:
    """Stable provider reference for a created or reused draft PR."""

    id: str
    url: str
    reused: bool

    def as_dict(self) -> dict[str, object]:
        return {"id": self.id, "url": self.url, "reused": self.reused}


class BitbucketRepairPRClient:
    """Bitbucket Cloud client limited to Pipeline Guard draft PR operations."""

    def __init__(self, *, workspace: str) -> None:
        (token,) = _required_environment("BITBUCKET_API_TOKEN")
        base_url = os.environ.get("BITBUCKET_URL")
        oauth_key = os.environ.get("BITBUCKET_EMAIL")
        _validate_owner_segment(workspace, "Bitbucket workspace")
        if (
            base_url is not None
            and _owner_from_url(base_url, "BITBUCKET_URL").casefold()
            != workspace.casefold()
        ):
            raise RepairPRClientError("Bitbucket workspace differs from BITBUCKET_URL")

        self.workspace = workspace
        self.api_url = "https://api.bitbucket.org/2.0"
        self.session = requests.Session()
        if oauth_key and "@" in oauth_key:
            credentials = base64.b64encode(f"{oauth_key}:{token}".encode()).decode()
            authorization = f"Basic {credentials}"
        elif oauth_key:
            authorization = f"Bearer {self._fetch_oauth_token(oauth_key, token)}"
        else:
            authorization = f"Bearer {token}"
        self.session.headers.update(
            {"Accept": "application/json", "Authorization": authorization}
        )

    @staticmethod
    def _fetch_oauth_token(consumer_key: str, consumer_secret: str) -> str:
        response = requests.post(
            "https://bitbucket.org/site/oauth2/access_token",
            auth=(consumer_key, consumer_secret),
            data={"grant_type": "client_credentials"},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        value = _mapping_response(_response_json(response))
        token = value.get("access_token")
        if not isinstance(token, str) or not token:
            raise RepairPRClientError("Bitbucket OAuth response omitted access_token")
        return token

    def _request(self, method: str, endpoint: str, **kwargs: Any) -> dict[str, object]:
        response = self.session.request(
            method,
            f"{self.api_url}{endpoint}",
            timeout=_REQUEST_TIMEOUT_SECONDS,
            **kwargs,
        )
        return _mapping_response(_response_json(response))

    def list_pull_requests(
        self, repository: str, state: str
    ) -> list[dict[str, object]]:
        endpoint = f"/repositories/{self.workspace}/{repository}/pullrequests"
        values: list[dict[str, object]] = []
        url: str | None = f"{self.api_url}{endpoint}"
        params: Mapping[str, str] = {"state": state}
        while url:
            response = self.session.get(
                url,
                params=params,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
            page = _mapping_response(_response_json(response))
            records = page.get("values", [])
            if not isinstance(records, list) or not all(
                isinstance(item, Mapping) for item in records
            ):
                raise requests.RequestException("Bitbucket returned an invalid PR page")
            values.extend(dict(item) for item in records)
            next_url = page.get("next")
            url = next_url if isinstance(next_url, str) and next_url else None
            params = {}
        return values

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
        payload = {
            "title": title,
            "description": description,
            "source": {
                "branch": {"name": from_branch},
                "repository": {"full_name": f"{self.workspace}/{repository}"},
            },
            "destination": {"branch": {"name": to_branch}},
            "close_source_branch": False,
            "draft": draft,
        }
        return self._request(
            "POST",
            f"/repositories/{self.workspace}/{repository}/pullrequests",
            json=payload,
        )

    def close_pull_request(self, repository: str, pull_request_id: str) -> None:
        self._request(
            "POST",
            f"/repositories/{self.workspace}/{repository}/pullrequests/{pull_request_id}/decline",
        )


class GitHubRepairPRClient:
    """GitHub client limited to Pipeline Guard draft PR operations."""

    def __init__(self, *, owner: str) -> None:
        (token,) = _required_environment("GITHUB_API_TOKEN")
        base_url = os.environ.get("GITHUB_URL")
        _validate_owner_segment(owner, "GitHub owner")
        if (
            base_url is not None
            and _owner_from_url(base_url, "GITHUB_URL").casefold() != owner.casefold()
        ):
            raise RepairPRClientError("GitHub owner differs from GITHUB_URL")

        self.owner = owner
        self.api_url = "https://api.github.com"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

    def _request(self, method: str, endpoint: str, **kwargs: Any) -> dict[str, object]:
        response = self.session.request(
            method,
            f"{self.api_url}{endpoint}",
            timeout=_REQUEST_TIMEOUT_SECONDS,
            **kwargs,
        )
        return _mapping_response(_response_json(response))

    def list_pull_requests(
        self, repository: str, state: str
    ) -> list[dict[str, object]]:
        values: list[dict[str, object]] = []
        url: str | None = f"{self.api_url}/repos/{self.owner}/{repository}/pulls"
        params: dict[str, str | int] = {"state": state, "per_page": 100}
        while url:
            response = self.session.get(
                url,
                params=params,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
            page = _response_json(response)
            if not isinstance(page, list) or not all(
                isinstance(item, Mapping) for item in page
            ):
                raise requests.RequestException("GitHub returned an invalid PR page")
            values.extend(dict(item) for item in page)
            url = response.links.get("next", {}).get("url")
            params = {}
        return values

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
        return self._request(
            "POST",
            f"/repos/{self.owner}/{repository}/pulls",
            json={
                "title": title,
                "body": description,
                "head": from_branch,
                "base": to_branch,
                "draft": draft,
            },
        )

    def close_pull_request(self, repository: str, pull_request_id: str) -> None:
        self._request(
            "PATCH",
            f"/repos/{self.owner}/{repository}/pulls/{pull_request_id}",
            json={"state": "closed"},
        )


def publish_request(
    value: object, *, client: object | None = None
) -> dict[str, object]:
    """Publish a validated repair request and return a stable draft reference."""
    request = RepairPRRequest.from_mapping(value)
    return publish_or_reuse_draft(request, client=client).as_dict()


def publish_or_reuse_draft(
    request: RepairPRRequest,
    *,
    client: object | None = None,
) -> RepairPRReference:
    """Reuse a matching open repair draft or create exactly one draft PR."""
    identity = request.visible_identity
    try:
        active_client = client if client is not None else _create_client(request)
        _verify_repository_owner(active_client, request)
        existing = _find_existing_draft(active_client, request, identity)
        if existing is not None:
            return existing
        try:
            created = _create_draft(active_client, request)
        except requests.HTTPError as error:
            if not _is_conflict(error):
                raise RepairPRPublicationError(
                    "Git provider rejected repair draft PR publication"
                ) from error
            existing = _find_existing_draft(active_client, request, identity)
            if existing is not None:
                return existing
            raise RepairPRPublicationError(
                "Git provider reported a duplicate repair PR but no matching draft was found"
            ) from error
    except RepairPRPublicationError:
        raise
    except (OSError, requests.RequestException, ValueError) as error:
        raise RepairPRPublicationError("repair draft PR publication failed") from error

    if not _is_draft(created):
        _close_non_draft(active_client, request, created)
        raise RepairPRPublicationError(
            "Git provider created a repair PR that is not a draft; it was closed"
        )
    return _reference_from_record(created, request.provider, reused=False)


def _create_client(request: RepairPRRequest) -> object:
    if request.provider == "bitbucket":
        return BitbucketRepairPRClient(workspace=request.repository_owner)
    if request.provider == "github":
        return GitHubRepairPRClient(owner=request.repository_owner)
    raise RepairPRClientError(f"unsupported repair PR provider: {request.provider}")


def _find_existing_draft(
    client: object,
    request: RepairPRRequest,
    identity: tuple[str, str],
) -> RepairPRReference | None:
    records = _list_open_pull_requests(client, request)
    matching_non_draft = False
    for record in records:
        if _matches_request_identity(record, request, identity):
            if _is_draft(record):
                return _reference_from_record(record, request.provider, reused=True)
            matching_non_draft = True
    if matching_non_draft:
        raise RepairPRPublicationError(
            "matching open repair PR exists but is not a draft; refusing to create another"
        )
    return None


def _verify_repository_owner(client: object, request: RepairPRRequest) -> None:
    attribute = "workspace" if request.provider == "bitbucket" else "owner"
    configured = getattr(client, attribute, None)
    if not isinstance(configured, str) or (
        configured.casefold() != request.repository_owner.casefold()
    ):
        raise RepairPRPublicationError(
            "repository owner differs from the configured repair PR provider owner"
        )


def _list_open_pull_requests(
    client: object,
    request: RepairPRRequest,
) -> list[Mapping[str, object]]:
    method = getattr(client, "list_pull_requests", None)
    if not callable(method):
        raise RepairPRPublicationError(
            f"{request.provider} repair PR client does not implement list_pull_requests"
        )
    response = method(request.repository, _OPEN_STATES[request.provider])
    if not isinstance(response, list) or not all(
        isinstance(item, Mapping) for item in response
    ):
        raise RepairPRPublicationError(
            f"{request.provider} repair PR client returned an invalid PR list"
        )
    return list(response)


def _create_draft(client: object, request: RepairPRRequest) -> Mapping[str, object]:
    method = getattr(client, "create_pull_request", None)
    if not callable(method):
        raise RepairPRPublicationError(
            f"{request.provider} repair PR client does not implement create_pull_request"
        )
    response = method(
        request.repository,
        request.title,
        request.branch,
        request.base,
        request.body,
        draft=True,
    )
    if not isinstance(response, Mapping):
        raise RepairPRPublicationError(
            f"{request.provider} repair PR client returned an invalid created PR"
        )
    return response


def _matches_request_identity(
    record: Mapping[str, object],
    request: RepairPRRequest,
    identity: tuple[str, str],
) -> bool:
    source, destination = _branches(record, request.provider)
    return (
        _record_text(record, "state") == _OPEN_STATES[request.provider]
        and _source_repository_matches(record, request)
        and source == request.branch
        and destination == request.base
        and _visible_identity(_description(record, request.provider)) == identity
    )


def _source_repository_matches(
    record: Mapping[str, object],
    request: RepairPRRequest,
) -> bool:
    if request.provider == "bitbucket":
        source_repository = _nested_text(record, "source", "repository", "full_name")
    else:
        source_repository = _nested_text(record, "head", "repo", "full_name")
    expected_repository = f"{request.repository_owner}/{request.repository}"
    return (
        source_repository is not None
        and source_repository.casefold() == expected_repository.casefold()
    )


def _branches(
    record: Mapping[str, object], provider: str
) -> tuple[str | None, str | None]:
    if provider == "bitbucket":
        return (
            _nested_text(record, "source", "branch", "name"),
            _nested_text(record, "destination", "branch", "name"),
        )
    return _nested_text(record, "head", "ref"), _nested_text(record, "base", "ref")


def _is_draft(record: Mapping[str, object]) -> bool:
    return record.get("draft") is True


def _description(record: Mapping[str, object], provider: str) -> str:
    field = "body" if provider == "github" else "description"
    return _record_text(record, field) or ""


def _visible_identity(description: str) -> tuple[str, str] | None:
    incident: str | None = None
    run: str | None = None
    for line in description.splitlines():
        if line.startswith("ID: "):
            incident = _visible_identity_value(line.removeprefix("ID: "))
        elif line.startswith("Run: "):
            run = _visible_identity_value(line.removeprefix("Run: "))
    if not incident or not run:
        return None
    return incident, run


def _visible_identity_value(value: str) -> str:
    normalized = value.strip()
    if normalized.startswith("`") and normalized.endswith("`") and len(normalized) >= 2:
        return normalized[1:-1]
    return normalized


def _reference_from_record(
    record: Mapping[str, object],
    provider: str,
    *,
    reused: bool,
) -> RepairPRReference:
    identifier = _record_identifier(record, provider)
    url = _reference_url(record, provider)
    if identifier is None or url is None:
        raise RepairPRPublicationError(
            f"{provider} response is missing a repair PR id or URL"
        )
    return RepairPRReference(id=identifier, url=url, reused=reused)


def _record_identifier(record: Mapping[str, object], provider: str) -> str | None:
    field = "id" if provider == "bitbucket" else "number"
    return _first_text(record, field)


def _reference_url(record: Mapping[str, object], provider: str) -> str | None:
    if provider == "bitbucket":
        return _nested_text(record, "links", "html", "href")
    return _record_text(record, "html_url")


def _close_non_draft(
    client: object,
    request: RepairPRRequest,
    record: Mapping[str, object],
) -> None:
    identifier = _record_identifier(record, request.provider)
    method = getattr(client, "close_pull_request", None)
    if identifier is None or not callable(method):
        raise RepairPRPublicationError(
            "Git provider created a non-draft repair PR; close it manually before retrying"
        )
    try:
        method(request.repository, identifier)
    except requests.RequestException as error:
        raise RepairPRPublicationError(
            "Git provider created a non-draft repair PR; close it manually before retrying"
        ) from error


def _is_conflict(error: requests.HTTPError) -> bool:
    return error.response is not None and error.response.status_code in {400, 409, 422}


def _required_environment(*names: str) -> tuple[str, ...]:
    values = tuple(os.environ.get(name) for name in names)
    missing = [name for name, value in zip(names, values, strict=True) if not value]
    if missing:
        raise RepairPRClientError(
            f"Missing required environment variables: {', '.join(missing)}"
        )
    return tuple(value for value in values if value)


def _validate_owner_segment(value: str, field: str) -> None:
    if not value or "/" in value or value != value.strip():
        raise RepairPRClientError(f"{field} must be one non-empty path segment")


def _owner_from_url(value: str, field: str) -> str:
    parts = [part for part in urlparse(value.rstrip("/")).path.split("/") if part]
    if not parts:
        raise RepairPRClientError(f"Could not extract repository owner from {field}")
    return parts[0]


def _response_json(response: requests.Response) -> object:
    if not response.ok:
        raise requests.HTTPError(
            f"Git provider API error {response.status_code}",
            response=response,
        )
    if response.status_code == 204 or not response.text:
        return {}
    return response.json()


def _mapping_response(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise requests.RequestException("Git provider returned a non-object response")
    return dict(value)


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RepairPRPublicationError(f"{field} must be a non-empty string")
    return value.strip()


def _record_text(record: Mapping[str, object], field: str) -> str | None:
    value = record.get(field)
    return value if isinstance(value, str) and value else None


def _nested_text(record: Mapping[str, object], *fields: str) -> str | None:
    current: object = record
    for field in fields:
        if not isinstance(current, Mapping):
            return None
        current = current.get(field)
    return current if isinstance(current, str) and current else None


def _first_text(record: Mapping[str, object], *fields: str) -> str | None:
    for field in fields:
        value = record.get(field)
        if isinstance(value, str | int) and str(value):
            return str(value)
    return None


def main() -> int:
    """Read one neutral request from stdin and emit one repair PR reference."""
    try:
        payload = json.load(sys.stdin)
        result = publish_request(payload)
    except (json.JSONDecodeError, RepairPRPublicationError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
