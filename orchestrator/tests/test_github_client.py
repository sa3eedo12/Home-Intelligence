from __future__ import annotations

import json

import httpx
import pytest
import respx

from orchestrator.github_client import GitHubClient, GitHubClientError


@respx.mock
@pytest.mark.asyncio
async def test_open_issue_success() -> None:
    route = respx.post("https://api.github.com/repos/owner/repo/issues").mock(
        return_value=httpx.Response(
            201,
            json={"number": 12, "html_url": "https://github.com/owner/repo/issues/12"},
        )
    )
    client = GitHubClient("token-123", "owner/repo")

    issue = await client.open_issue(title="Hello", body="Body", labels=["reflection"])

    assert issue["number"] == 12
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer token-123"
    assert request.headers["Accept"] == "application/vnd.github+json"
    assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"
    assert json.loads(request.content) == {
        "title": "Hello",
        "body": "Body",
        "labels": ["reflection"],
    }


@respx.mock
@pytest.mark.asyncio
async def test_open_issue_raises_on_422() -> None:
    respx.post("https://api.github.com/repos/owner/repo/issues").mock(
        return_value=httpx.Response(422, json={"message": "Validation Failed"})
    )
    client = GitHubClient("token-123", "owner/repo")

    with pytest.raises(GitHubClientError, match="Validation Failed"):
        await client.open_issue(title="", body="Body")


@respx.mock
@pytest.mark.asyncio
async def test_dispatch_workflow_success() -> None:
    route = respx.post(
        "https://api.github.com/repos/owner/repo/actions/workflows/"
        "copilot-auto-pr.yml/dispatches"
    ).mock(return_value=httpx.Response(204))
    client = GitHubClient("token-123", "owner/repo")

    result = await client.dispatch_workflow(
        "copilot-auto-pr.yml",
        "main",
        {"proposal_id": "7"},
    )

    assert result == {"ok": True}
    assert json.loads(route.calls.last.request.content) == {
        "ref": "main",
        "inputs": {"proposal_id": "7"},
    }


@respx.mock
@pytest.mark.asyncio
async def test_dispatch_workflow_raises_on_404() -> None:
    respx.post(
        "https://api.github.com/repos/owner/repo/actions/workflows/"
        "copilot-auto-pr.yml/dispatches"
    ).mock(return_value=httpx.Response(404, json={"message": "Not Found"}))
    client = GitHubClient("token-123", "owner/repo")

    with pytest.raises(GitHubClientError, match="Not Found"):
        await client.dispatch_workflow("copilot-auto-pr.yml", "main", {})
