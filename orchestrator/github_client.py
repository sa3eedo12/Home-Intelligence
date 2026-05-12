from __future__ import annotations

from typing import Any

import httpx


class GitHubClientError(RuntimeError):
    """Raised when the GitHub REST API rejects a delivery request."""


class GitHubClient:
    api_base = "https://api.github.com"

    def __init__(self, token: str | None, repo: str | None) -> None:
        self.token = (token or "").strip() or None
        self.repo = (repo or "").strip() or None

    @property
    def is_configured(self) -> bool:
        return bool(self.token and self.repo)

    def _repo_parts(self) -> tuple[str, str]:
        if not self.repo or "/" not in self.repo:
            raise GitHubClientError("GITHUB_REPO must be set as owner/name")
        owner, repo = self.repo.split("/", 1)
        if not owner or not repo:
            raise GitHubClientError("GITHUB_REPO must be set as owner/name")
        return owner, repo

    def _headers(self) -> dict[str, str]:
        if not self.token:
            raise GitHubClientError("GITHUB_REPO_TOKEN is not configured")
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def open_issue(
        self, *, title: str, body: str, labels: list[str] | None = None
    ) -> dict[str, Any]:
        owner, repo = self._repo_parts()
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{self.api_base}/repos/{owner}/{repo}/issues",
                headers=self._headers(),
                json=payload,
            )
        self._raise_for_error(response)
        return response.json()

    async def dispatch_workflow(
        self, workflow_filename: str, ref: str, inputs: dict[str, Any]
    ) -> dict[str, bool]:
        owner, repo = self._repo_parts()
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{self.api_base}/repos/{owner}/{repo}/actions/workflows/"
                f"{workflow_filename}/dispatches",
                headers=self._headers(),
                json={"ref": ref, "inputs": inputs},
            )
        self._raise_for_error(response)
        if response.status_code != 204:
            raise GitHubClientError(
                f"GitHub workflow dispatch returned HTTP {response.status_code}: {response.text}"
            )
        return {"ok": True}

    @staticmethod
    def _raise_for_error(response: httpx.Response) -> None:
        if response.status_code >= 400:
            raise GitHubClientError(
                f"GitHub API error HTTP {response.status_code}: {response.text}"
            )
