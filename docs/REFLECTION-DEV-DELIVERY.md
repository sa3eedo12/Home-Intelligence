# Reflection dev-delivery channels

The Morning Brief proposal cards support three ways to move an idea into development:

1. **Clipboard** — copies the same markdown prompt that the other channels use, so you can paste it into any IDE or Copilot CLI session.
2. **GitHub Issue** — opens a real issue in `GITHUB_REPO` with the proposal title, rationale, evidence ids, and implementation prompt. The proposal row records the issue URL and dispatch timestamp.
3. **Copilot Auto-PR** — opens the same GitHub issue, then triggers `copilot-auto-pr.yml` via `workflow_dispatch`. Today the workflow creates a draft PR containing the proposal prompt as a tracking artifact.

## Configuration

Set these variables for the orchestrator service:

```env
GITHUB_REPO_TOKEN=
GITHUB_REPO=sa3eedo12/Home-Intelligence
```

Use a fine-grained GitHub PAT scoped to this repository only. It needs:

- `issues: write`
- `actions: write`

The workflow itself uses `GITHUB_TOKEN` with `contents: write`, `issues: write`, and `pull-requests: write` permissions.

## Current auto-PR limitation

The auto-PR workflow is a documented stub until the official Copilot CLI GitHub Action is available. It opens a draft PR with the proposal body so the idea has a durable branch, issue, and review surface; a future update can replace the placeholder step with the real Copilot CLI action.
