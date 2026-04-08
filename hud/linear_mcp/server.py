"""Minimal 3-tool mock Linear MCP server."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from fastmcp import FastMCP

from .data import MockLinearData

VIEWER = {
    "id": "user-001",
    "name": "Agent Bot",
    "displayName": "Agent",
    "email": "agent@example.com",
}


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def create_linear_server(data: MockLinearData) -> FastMCP:
    server = FastMCP("orders-incident-linear")

    @server.tool()
    def get_linear_issue(issue_identifier: str = "ENG-450") -> str:
        """Return the seeded Linear issue details plus any created comments."""
        issue = data.get_issue(issue_identifier)
        if not issue:
            raise ValueError(f"Linear issue {issue_identifier!r} not found")
        issue_id = issue.get("id", "")
        comments = data._created_comments.get(issue_id, [])
        payload: dict[str, Any] = {**issue, "created_comments": comments}
        return json.dumps(payload, indent=2)

    @server.tool()
    def leave_linear_comment(body: str, issue_identifier: str = "ENG-450") -> str:
        """Leave a comment on the seeded Linear issue."""
        issue = data.get_issue(issue_identifier)
        if not issue:
            raise ValueError(f"Linear issue {issue_identifier!r} not found")
        issue_id = issue.get("id", "")
        comment = {
            "id": str(uuid.uuid4()),
            "body": body,
            "createdAt": _utc_now(),
            "user": VIEWER,
        }
        data._created_comments.setdefault(issue_id, []).append(comment)
        issue["updatedAt"] = comment["createdAt"]
        return json.dumps(comment, indent=2)

    @server.tool()
    def mark_linear_done(issue_identifier: str = "ENG-450") -> str:
        """Mark the seeded Linear issue as Done."""
        issue = data.get_issue(issue_identifier)
        if not issue:
            raise ValueError(f"Linear issue {issue_identifier!r} not found")
        issue["state"] = data.done_state()
        issue["completedAt"] = _utc_now()
        issue["updatedAt"] = issue["completedAt"]
        return json.dumps(issue["state"], indent=2)

    return server
