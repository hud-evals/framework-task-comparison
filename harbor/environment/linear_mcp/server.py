"""Minimal file-backed Linear MCP server for the Harbor demo."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastmcp import FastMCP

STATE_DIR = Path("/app/linear_data")
ISSUES_PATH = STATE_DIR / "issues.json"
WORKFLOW_STATES_PATH = STATE_DIR / "workflow_states.json"
COMMENTS_PATH = STATE_DIR / "comments.json"

VIEWER = {
    "id": "user-001",
    "name": "Agent Bot",
    "displayName": "Agent",
    "email": "agent@example.com",
}

mcp = FastMCP("orders-incident-linear")


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _save_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _load_issue(issue_identifier: str) -> tuple[list[dict], dict]:
    issues = _load_json(ISSUES_PATH)
    for issue in issues:
        if issue_identifier in {issue.get("identifier"), issue.get("id")}:
            return issues, issue
    raise ValueError(f"Linear issue {issue_identifier!r} not found")


def _done_state() -> dict:
    for state in _load_json(WORKFLOW_STATES_PATH):
        if state.get("type") == "completed":
            return state
    raise ValueError("No completed workflow state found")


@mcp.tool()
def get_linear_issue(issue_identifier: str = "ENG-450") -> str:
    """Return the seeded Linear issue details plus any created comments."""
    _issues, issue = _load_issue(issue_identifier)
    payload = {**issue, "created_comments": _load_json(COMMENTS_PATH)}
    return json.dumps(payload, indent=2)


@mcp.tool()
def leave_linear_comment(body: str, issue_identifier: str = "ENG-450") -> str:
    """Leave a comment on the seeded Linear issue."""
    issues, issue = _load_issue(issue_identifier)
    comments = _load_json(COMMENTS_PATH)
    comment = {
        "id": str(uuid.uuid4()),
        "body": body,
        "createdAt": _utc_now(),
        "user": VIEWER,
    }
    comments.append(comment)
    issue["updatedAt"] = comment["createdAt"]
    _save_json(COMMENTS_PATH, comments)
    _save_json(ISSUES_PATH, issues)
    return json.dumps(comment, indent=2)


@mcp.tool()
def mark_linear_done(issue_identifier: str = "ENG-450") -> str:
    """Mark the seeded Linear issue as Done."""
    issues, issue = _load_issue(issue_identifier)
    issue["state"] = _done_state()
    issue["completedAt"] = _utc_now()
    issue["updatedAt"] = issue["completedAt"]
    _save_json(ISSUES_PATH, issues)
    return json.dumps(issue["state"], indent=2)


if __name__ == "__main__":
    mcp.run(transport="stdio")
