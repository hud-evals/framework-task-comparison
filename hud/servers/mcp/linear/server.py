"""Mock Linear MCP server that matches official Linear MCP schemas."""

from __future__ import annotations

import base64
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastmcp import FastMCP

from .data import MockLinearData, _paginate

PRIORITY_NAMES = {0: "None", 1: "Urgent", 2: "High", 3: "Medium", 4: "Low"}

# ---------------------------------------------------------------------------
# Official Linear MCP tool descriptions (from https://mcp.linear.app/mcp)
# ---------------------------------------------------------------------------
_TOOL_DESCRIPTIONS: dict[str, str] = {
    "get_attachment": "Retrieve an attachment's content by ID.",
    "create_attachment": "Create a new attachment on a specific Linear issue by uploading base64-encoded content.",
    "delete_attachment": "Delete an attachment by ID",
    "list_comments": "List comments for a specific Linear issue",
    "create_comment": "Create a comment on a specific Linear issue",
    "list_cycles": "Retrieve cycles for a specific Linear team",
    "get_document": "Retrieve a Linear document by ID or slug",
    "list_documents": "List documents in the user's Linear workspace",
    "create_document": "Create a new document in Linear",
    "update_document": "Update an existing Linear document",
    "extract_images": "Extract and fetch images from markdown content. Use this to view screenshots, diagrams, or other images embedded in Linear issues, comments, or documents. Pass the markdown content (e.g., issue description) and receive the images as viewable data.",
    "get_issue": "Retrieve detailed information about an issue by ID, including attachments and git branch name",
    "list_issues": 'List issues in the user\'s Linear workspace. For my issues, use "me" as the assignee.',
    "create_issue": "Create a new Linear issue",
    "update_issue": "Update an existing Linear issue",
    "list_issue_statuses": "List available issue statuses in a Linear team",
    "get_issue_status": "Retrieve detailed information about an issue status in Linear by name or ID",
    "list_issue_labels": "List available issue labels in a Linear workspace or team",
    "create_issue_label": "Create a new Linear issue label",
    "list_projects": "List projects in the user's Linear workspace",
    "get_project": "Retrieve details of a specific project in Linear",
    "create_project": "Create a new project in Linear",
    "update_project": "Update an existing Linear project",
    "list_project_labels": "List available project labels in the Linear workspace",
    "list_milestones": "List all milestones in a Linear project",
    "get_milestone": "Retrieve details of a specific milestone by ID or name",
    "create_milestone": "Create a new milestone in a Linear project",
    "update_milestone": "Update an existing milestone in a Linear project",
    "list_teams": "List teams in the user's Linear workspace",
    "get_team": "Retrieve details of a specific Linear team",
    "list_users": "Retrieve users in the Linear workspace",
    "get_user": "Retrieve details of a specific Linear user",
    "search_documentation": "Search Linear's documentation to learn about features and usage",
    "list_initiatives": "List initiatives in the user's Linear workspace",
    "get_initiative": "Retrieve detailed information about a specific initiative in Linear",
    "create_initiative": "Create a new initiative in Linear",
    "update_initiative": "Update an existing Linear initiative",
    "get_status_updates": "List or get project/initiative status updates. Pass `id` to get a specific update, or filter to list.",
    "save_status_update": "Create or update a project/initiative status update. Omit `id` to create, provide `id` to update.",
    "delete_status_update": "Delete (archive) a project or initiative status update.",
}

# ---------------------------------------------------------------------------
# Official Linear MCP parameter descriptions (from https://mcp.linear.app/mcp)
# ---------------------------------------------------------------------------
_PARAM_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "get_attachment": {
        "id": "Attachment ID",
    },
    "create_attachment": {
        "issue": "Issue ID or identifier (e.g., LIN-123)",
        "base64Content": "Base64-encoded file content to upload",
        "filename": "Filename for the upload (e.g., 'screenshot.png')",
        "contentType": "MIME type for the upload (e.g., 'image/png', 'application/pdf')",
        "title": "Optional title for the attachment",
        "subtitle": "Optional subtitle for the attachment",
    },
    "delete_attachment": {
        "id": "Attachment ID",
    },
    "list_comments": {
        "issueId": "Issue ID",
    },
    "create_comment": {
        "issueId": "Issue ID",
        "body": "Content as Markdown",
        "parentId": "Parent comment ID (for replies)",
    },
    "list_cycles": {
        "teamId": "Team ID",
        "type": "Filter: current, previous, next, or all",
    },
    "get_document": {
        "id": "Document ID or slug",
    },
    "list_documents": {
        "limit": "Max results (default 50, max 250)",
        "cursor": "Next page cursor",
        "orderBy": "Sort: createdAt | updatedAt",
        "query": "Search query",
        "projectId": "Filter by project ID",
        "initiativeId": "Filter by initiative ID",
        "creatorId": "Filter by creator ID",
        "createdAt": "Created after: ISO-8601 date/duration (e.g., -P1D)",
        "updatedAt": "Updated after: ISO-8601 date/duration (e.g., -P1D)",
        "includeArchived": "Include archived items",
    },
    "create_document": {
        "title": "Document title",
        "content": "Content as Markdown",
        "project": "Project name or ID",
        "issue": "Issue ID or identifier (e.g., LIN-123)",
        "icon": "Icon emoji",
        "color": "Hex color",
    },
    "update_document": {
        "id": "Document ID or slug",
        "title": "Document title",
        "content": "Content as Markdown",
        "project": "Project name or ID",
        "icon": "Icon emoji",
        "color": "Hex color",
    },
    "extract_images": {
        "markdown": "Markdown content containing image references (e.g., issue description, comment body)",
    },
    "get_issue": {
        "id": "Issue ID",
        "includeRelations": "Include blocking/related/duplicate relations",
    },
    "list_issues": {
        "limit": "Max results (default 50, max 250)",
        "cursor": "Next page cursor",
        "orderBy": "Sort: createdAt | updatedAt",
        "query": "Search issue title or description",
        "team": "Team name or ID",
        "state": "State type, name, or ID",
        "cycle": "Cycle name, number, or ID",
        "label": "Label name or ID",
        "assignee": 'User ID, name, email, or "me"',
        "delegate": "Agent name or ID",
        "project": "Project name or ID",
        "priority": "0=None, 1=Urgent, 2=High, 3=Normal, 4=Low",
        "parentId": "Parent issue ID",
        "createdAt": "Created after: ISO-8601 date/duration (e.g., -P1D)",
        "updatedAt": "Updated after: ISO-8601 date/duration (e.g., -P1D)",
        "includeArchived": "Include archived items",
    },
    "create_issue": {
        "title": "Issue title",
        "team": "Team name or ID",
        "description": "Content as Markdown",
        "assignee": 'User ID, name, email, or "me"',
        "state": "State type, name, or ID",
        "priority": "0=None, 1=Urgent, 2=High, 3=Normal, 4=Low",
        "project": "Project name or ID",
        "labels": "Label names or IDs",
        "cycle": "Cycle name, number, or ID",
        "milestone": "Milestone name or ID",
        "parentId": "Parent issue ID",
        "dueDate": "Due date (ISO format)",
        "estimate": "Issue estimate value",
        "delegate": "Agent name or ID",
        "blockedBy": "Issue IDs/identifiers blocking this",
        "blocks": "Issue IDs/identifiers this blocks",
        "relatedTo": "Related issue IDs/identifiers",
        "duplicateOf": "Duplicate of issue ID/identifier",
        "links": "Link attachments [{url, title}]",
    },
    "update_issue": {
        "id": "Issue ID",
        "title": "Issue title",
        "team": "Team name or ID",
        "description": "Content as Markdown",
        "assignee": 'User ID, name, email, or "me". Null to remove',
        "state": "State type, name, or ID",
        "priority": "0=None, 1=Urgent, 2=High, 3=Normal, 4=Low",
        "project": "Project name or ID",
        "labels": "Label names or IDs",
        "cycle": "Cycle name, number, or ID",
        "milestone": "Milestone name or ID",
        "parentId": "Parent issue ID. Null to remove",
        "dueDate": "Due date (ISO format)",
        "estimate": "Issue estimate value",
        "delegate": "Agent name or ID. Null to remove",
        "blockedBy": "Issue IDs/identifiers blocking this. Replaces existing; omit to keep unchanged",
        "blocks": "Issue IDs/identifiers this blocks. Replaces existing; omit to keep unchanged",
        "relatedTo": "Related issue IDs/identifiers. Replaces existing; omit to keep unchanged",
        "duplicateOf": "Duplicate of issue ID/identifier. Null to remove",
        "links": "Link attachments [{url, title}]",
    },
    "list_issue_statuses": {
        "team": "Team name or ID",
    },
    "get_issue_status": {
        "id": "Status ID",
        "name": "Status name",
        "team": "Team name or ID",
    },
    "list_issue_labels": {
        "cursor": "Next page cursor",
        "limit": "Max results (default 50, max 250)",
        "name": "Filter by name",
        "orderBy": "Sort: createdAt | updatedAt",
        "team": "Team name or ID",
    },
    "create_issue_label": {
        "name": "Label name",
        "color": "Hex color code",
        "description": "Label description",
        "isGroup": "Is label group (not directly applicable)",
        "parentId": "Parent label group UUID",
        "teamId": "Team UUID (omit for workspace label)",
    },
    "list_projects": {
        "limit": "Max results (default 50, max 250)",
        "cursor": "Next page cursor",
        "orderBy": "Sort: createdAt | updatedAt",
        "query": "Search project name",
        "team": "Team name or ID",
        "state": "State type, name, or ID",
        "initiative": "Initiative name or ID",
        "member": 'User ID, name, email, or "me"',
        "createdAt": "Created after: ISO-8601 date/duration (e.g., -P1D)",
        "updatedAt": "Updated after: ISO-8601 date/duration (e.g., -P1D)",
        "includeArchived": "Include archived items",
        "includeMembers": "Include project members",
        "includeMilestones": "Include milestones",
    },
    "get_project": {
        "query": "Project ID or name",
        "includeMembers": "Include project members",
        "includeMilestones": "Include milestones",
        "includeResources": "Include resources (documents, links, attachments)",
    },
    "create_project": {
        "name": "Project name",
        "team": "Team name or ID",
        "description": "Content as Markdown",
        "color": "Hex color",
        "icon": "Icon emoji (e.g., :eagle:)",
        "initiative": "Initiative name or ID",
        "labels": "Label names or IDs",
        "lead": 'User ID, name, email, or "me"',
        "priority": "0=None, 1=Urgent, 2=High, 3=Medium, 4=Low",
        "startDate": "Start date (ISO format)",
        "state": "Project state",
        "summary": "Short summary (max 255 chars)",
        "targetDate": "Target date (ISO format)",
    },
    "update_project": {
        "id": "Project ID",
        "name": "Project name",
        "description": "Content as Markdown",
        "color": "Hex color",
        "icon": "Icon emoji (e.g., :eagle:)",
        "initiatives": "Initiative IDs or names",
        "labels": "Label names or IDs",
        "lead": 'User ID, name, email, or "me". Null to remove',
        "priority": "0=None, 1=Urgent, 2=High, 3=Medium, 4=Low",
        "startDate": "Start date (ISO format)",
        "state": "Project state",
        "summary": "Short summary (max 255 chars)",
        "targetDate": "Target date (ISO format)",
    },
    "list_project_labels": {
        "cursor": "Next page cursor",
        "limit": "Max results (default 50, max 250)",
        "name": "Filter by name",
        "orderBy": "Sort: createdAt | updatedAt",
    },
    "list_milestones": {
        "project": "Project name or ID",
    },
    "get_milestone": {
        "project": "Project name or ID",
        "query": "Milestone name or ID",
    },
    "create_milestone": {
        "project": "Project name or ID",
        "name": "Milestone name",
        "description": "Milestone description",
        "targetDate": "Target completion date (ISO format)",
    },
    "update_milestone": {
        "project": "Project name or ID",
        "id": "Milestone name or ID",
        "name": "Milestone name",
        "description": "Milestone description",
        "targetDate": "Target completion date (ISO format, null to remove)",
    },
    "list_teams": {
        "limit": "Max results (default 50, max 250)",
        "cursor": "Next page cursor",
        "orderBy": "Sort: createdAt | updatedAt",
        "query": "Search query",
        "createdAt": "Created after: ISO-8601 date/duration (e.g., -P1D)",
        "updatedAt": "Updated after: ISO-8601 date/duration (e.g., -P1D)",
        "includeArchived": "Include archived items",
    },
    "get_team": {
        "query": "Team UUID, key, or name",
    },
    "list_users": {
        "limit": "Max results (default 50, max 250)",
        "cursor": "Next page cursor",
        "orderBy": "Sort: createdAt | updatedAt",
        "query": "Filter by name or email",
        "team": "Team name or ID",
    },
    "get_user": {
        "query": 'User ID, name, email, or "me"',
    },
    "search_documentation": {
        "query": "Search query",
        "page": "Page number",
    },
    "list_initiatives": {
        "limit": "Max results (default 50, max 250)",
        "cursor": "Next page cursor",
        "orderBy": "Sort: createdAt | updatedAt",
        "query": "Search initiative name",
        "status": "Status of the initiative",
        "owner": 'User ID, name, email, or "me"',
        "parentInitiative": "Parent initiative name or ID",
        "createdAt": "Created after: ISO-8601 date/duration (e.g., -P1D)",
        "updatedAt": "Updated after: ISO-8601 date/duration (e.g., -P1D)",
        "includeArchived": "Include archived items",
        "includeProjects": "Include projects",
        "includeSubInitiatives": "Include sub-initiatives",
    },
    "get_initiative": {
        "query": "Initiative ID or name",
        "includeProjects": "Include projects",
        "includeSubInitiatives": "Include sub-initiatives",
    },
    "create_initiative": {
        "name": "Initiative name",
        "description": "Content as Markdown",
        "color": "Hex color",
        "icon": "Icon emoji or name",
        "status": "Initiative status (Planned, Active, Completed)",
        "summary": "Short summary (max 255 chars)",
        "targetDate": "Target date (ISO format)",
        "owner": 'User ID, name, email, or "me"',
    },
    "update_initiative": {
        "id": "Initiative ID",
        "name": "Initiative name",
        "description": "Content as Markdown",
        "color": "Hex color",
        "icon": "Icon emoji or name",
        "status": "Initiative status (Planned, Active, Completed)",
        "summary": "Short summary (max 255 chars)",
        "targetDate": "Target date (ISO format)",
        "owner": 'User ID, name, email, or "me". Null to remove',
        "parentInitiative": "Parent initiative name or ID. Null to remove",
    },
    "get_status_updates": {
        "type": "Type of status update",
        "limit": "Max results (default 50, max 250)",
        "cursor": "Next page cursor",
        "orderBy": "Sort: createdAt | updatedAt",
        "id": "Status update ID - if provided, returns this specific update",
        "project": "Project name or ID",
        "initiative": "Initiative name or ID",
        "user": 'User ID, name, email, or "me"',
        "createdAt": "Created after: ISO-8601 date/duration (e.g., -P1D)",
        "updatedAt": "Updated after: ISO-8601 date/duration (e.g., -P1D)",
        "includeArchived": "Include archived items",
    },
    "save_status_update": {
        "type": "Type of status update",
        "id": "Status update ID - if provided, updates this existing update",
        "project": "Project name or ID",
        "initiative": "Initiative name or ID",
        "body": "Content as Markdown",
        "health": "onTrack | atRisk | offTrack",
        "isDiffHidden": "Hide diff with previous update",
    },
    "delete_status_update": {
        "type": "Type of status update",
        "id": "Status update ID",
    },
}


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _new_id() -> str:
    return str(uuid.uuid4())


def _to_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _list_payload(key: str, page: dict[str, Any]) -> str:
    payload: dict[str, Any] = {
        key: page.get("nodes", []),
        "hasNextPage": bool((page.get("pageInfo") or {}).get("hasNextPage")),
    }
    cursor = (page.get("pageInfo") or {}).get("endCursor")
    if cursor:
        payload["cursor"] = cursor
    return _to_json(payload)


def _match_text(haystack: str | None, needle: str | None) -> bool:
    if not needle:
        return True
    return needle.lower() in (haystack or "").lower()


_ISO_DURATION_RE = re.compile(r"^-?P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?$", re.IGNORECASE)

_TIMESTAMP_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
)


def _parse_timestamp(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp string into a timezone-aware datetime."""
    for fmt in _TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _parse_date_threshold(value: str) -> datetime | None:
    """Parse a date filter into a threshold datetime.

    Supports ISO-8601 dates (``"2026-01-15"``) and durations
    (``"-P7D"``, ``"-P1M"``).  The official Linear MCP interprets
    these values as "on or after".
    """
    value = value.strip()
    m = _ISO_DURATION_RE.match(value)
    if m:
        years, months, weeks, days = (int(g or 0) for g in m.groups())
        total_days = years * 365 + months * 30 + weeks * 7 + days
        return datetime.now(UTC) - timedelta(days=total_days)
    return _parse_timestamp(value)


def _match_date(entity_value: str | None, filter_value: str | None) -> bool:
    """Return True if *entity_value* (a timestamp) is on or after *filter_value*.

    ``filter_value`` may be an ISO-8601 date or a negative ISO-8601 duration.
    """
    if not filter_value:
        return True
    threshold = _parse_date_threshold(filter_value)
    if threshold is None:
        return True
    entity_dt = _parse_timestamp(entity_value) if entity_value else None
    if entity_dt is None:
        return False
    return entity_dt >= threshold


def _shape_team(team: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": team.get("id"),
        "icon": team.get("icon"),
        "name": team.get("name"),
        "createdAt": team.get("createdAt"),
        "updatedAt": team.get("updatedAt"),
    }


def _shape_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": user.get("id"),
        "name": user.get("name"),
        "email": user.get("email"),
        "displayName": user.get("displayName") or user.get("name"),
        "isAdmin": bool(user.get("admin", False)),
        "isGuest": bool(user.get("guest", False)),
        "isActive": bool(user.get("active", True)),
        "createdAt": user.get("createdAt"),
        "updatedAt": user.get("updatedAt"),
        "status": user.get("status"),
    }


def _shape_project(project: dict[str, Any]) -> dict[str, Any]:
    lead = project.get("lead") or {}
    status_name = project.get("status")
    if not status_name:
        status_name = project.get("state")
    status = (
        {
            "id": (project.get("statusData") or {}).get("id") or f"project-status-{project.get('id')}",
            "name": status_name,
        }
        if status_name
        else None
    )
    labels = project.get("labels") or []
    initiatives = project.get("initiatives") or []
    return {
        "id": project.get("id"),
        "icon": project.get("icon"),
        "color": project.get("color"),
        "name": project.get("name"),
        "summary": project.get("summary"),
        "description": project.get("description"),
        "url": project.get("url"),
        "createdAt": project.get("createdAt"),
        "updatedAt": project.get("updatedAt"),
        "startDate": project.get("startDate"),
        "targetDate": project.get("targetDate"),
        "priority": {
            "value": project.get("priority", 0),
            "name": PRIORITY_NAMES.get(project.get("priority", 0), str(project.get("priority"))),
        },
        "labels": labels,
        "initiatives": initiatives,
        "lead": {"id": lead.get("id"), "name": lead.get("name")} if lead else None,
        "status": status,
    }


def _shape_issue(issue: dict[str, Any], *, include_relations: bool = False) -> dict[str, Any]:
    project = issue.get("project") or {}
    assignee = issue.get("assignee") or {}
    team = issue.get("team") or {}
    creator = issue.get("creator") or {}
    milestone = issue.get("milestone") or {}
    labels = issue.get("labels") or {}
    if isinstance(labels, dict):
        label_nodes = labels.get("nodes", [])
    else:
        label_nodes = labels

    payload: dict[str, Any] = {
        "id": issue.get("id"),
        "identifier": issue.get("identifier"),
        "title": issue.get("title"),
        "description": issue.get("description"),
        "projectMilestone": {"id": milestone.get("id"), "name": milestone.get("name")} if milestone else None,
        "priority": {
            "value": issue.get("priority", 0),
            "name": PRIORITY_NAMES.get(issue.get("priority", 0), str(issue.get("priority"))),
        },
        "url": issue.get("url"),
        "gitBranchName": issue.get("gitBranchName") or issue.get("identifier", "").lower().replace(" ", "-"),
        "createdAt": issue.get("createdAt"),
        "updatedAt": issue.get("updatedAt"),
        "archivedAt": issue.get("archivedAt"),
        "completedAt": issue.get("completedAt"),
        "dueDate": issue.get("dueDate"),
        "status": (issue.get("state") or {}).get("name"),
        "labels": [l.get("name") for l in label_nodes if l.get("name")],
        "attachments": issue.get("attachments", []),
        "documents": issue.get("documents", []),
        "createdBy": creator.get("name"),
        "createdById": creator.get("id"),
        "assignee": assignee.get("name"),
        "assigneeId": assignee.get("id"),
        "project": project.get("name"),
        "projectId": project.get("id"),
        "team": team.get("name"),
        "teamId": team.get("id"),
        "parentId": issue.get("parentId"),
    }
    if include_relations:
        payload["blockedBy"] = issue.get("blockedBy", [])
        payload["blocks"] = issue.get("blocks", [])
        payload["relatedTo"] = issue.get("relatedTo", [])
        payload["duplicateOf"] = issue.get("duplicateOf")
    return payload


# Params that SHOULD remain nullable (official uses anyOf[string, null]).
# All other optional params should be plain {type: string} (not nullable).
_NULLABLE_PARAMS: dict[str, set[str]] = {
    "update_issue": {"assignee", "delegate", "parentId", "duplicateOf"},
    "update_project": {"lead"},
    "update_milestone": {"targetDate"},
    "update_initiative": {"owner", "parentInitiative"},
}

# Official links item schema for create_issue / update_issue
_LINK_ITEMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "format": "uri"},
        "title": {"type": "string", "minLength": 1},
    },
    "required": ["url", "title"],
}

# Tools whose `limit` parameter should have maximum: 250
_LIMIT_TOOLS: set[str] = {
    "list_documents",
    "list_issues",
    "list_issue_labels",
    "list_projects",
    "list_project_labels",
    "list_teams",
    "list_users",
    "list_initiatives",
    "get_status_updates",
}

# Tools whose `priority` parameter should have minimum/maximum
_PRIORITY_CONSTRAINED_TOOLS: set[str] = {"create_project", "update_project"}

# Params that official types as "number" but Python `int` generates "integer"
_NUMBER_PARAMS: set[str] = {"limit", "priority", "estimate", "page"}


async def _fix_schemas(server: FastMCP) -> None:
    """Post-process tool schemas to match the official Linear MCP API.

    Handles four categories of fixes that cannot be expressed through
    Python type annotations alone:
    1. Nullable: Convert ``anyOf[type, null]`` to plain ``type`` for params
       that the official API does NOT treat as nullable.
    2. Constraints: Add ``maximum: 250`` on ``limit``, ``minimum/maximum``
       on ``priority`` for project tools.
    3. Links items: Set the correct object items schema for ``links``.
    4. Parameter descriptions: Inject official descriptions from
       ``_PARAM_DESCRIPTIONS``.
    """
    for tool in await server.list_tools():
        tool_name = tool.name
        params = tool.parameters
        if not params or not isinstance(params, dict):
            continue

        # Official Linear schemas omit additionalProperties; strip it for parity
        params.pop("additionalProperties", None)

        props = params.get("properties", {})
        nullable_set = _NULLABLE_PARAMS.get(tool_name, set())
        param_descs = _PARAM_DESCRIPTIONS.get(tool_name, {})

        for prop_name, prop_schema in list(props.items()):
            # --- Fix nullable ---
            if "anyOf" in prop_schema and prop_name not in nullable_set:
                types = prop_schema["anyOf"]
                non_null = [t for t in types if t.get("type") != "null"]
                if len(non_null) == 1:
                    saved_default = prop_schema.get("default")
                    prop_schema.clear()
                    prop_schema.update(non_null[0])
                    if saved_default is not None:
                        prop_schema["default"] = saved_default

            # Remove leftover default=None (official never has default: null)
            if prop_schema.get("default") is None and "default" in prop_schema:
                del prop_schema["default"]

            # --- Fix limit maximum ---
            if prop_name == "limit" and tool_name in _LIMIT_TOOLS:
                prop_schema["maximum"] = 250

            # --- Fix priority min/max ---
            if prop_name == "priority" and tool_name in _PRIORITY_CONSTRAINED_TOOLS:
                prop_schema["minimum"] = 0
                prop_schema["maximum"] = 4

            # --- Fix links items schema ---
            if prop_name == "links":
                prop_schema["items"] = _LINK_ITEMS_SCHEMA

            # --- Fix integer → number for parity with official ---
            # (except priority in project tools, which official types as integer)
            if (
                prop_name in _NUMBER_PARAMS
                and prop_schema.get("type") == "integer"
                and not (prop_name == "priority" and tool_name in _PRIORITY_CONSTRAINED_TOOLS)
            ):
                prop_schema["type"] = "number"

            # --- Inject official parameter descriptions ---
            if prop_name in param_descs:
                prop_schema["description"] = param_descs[prop_name]


async def _ensure_tool_descriptions(server: FastMCP) -> None:
    """Set tool descriptions to match the official Linear MCP API."""
    for tool in await server.list_tools():
        if tool.name in _TOOL_DESCRIPTIONS:
            tool.description = _TOOL_DESCRIPTIONS[tool.name]
        elif not getattr(tool, "description", None):
            tool.description = f"Linear MCP tool: {tool.name.replace('_', ' ')}."


async def create_linear_server(data: MockLinearData) -> FastMCP:
    """Create a FastMCP server with official Linear MCP tool contracts."""
    server = FastMCP("Linear")

    # Session-scoped entities not currently modeled in MockLinearData.
    attachments: list[dict[str, Any]] = []
    milestones: list[dict[str, Any]] = []
    initiative_store: list[dict[str, Any]] = []
    status_updates: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Attachments
    # ------------------------------------------------------------------
    @server.tool
    async def get_attachment(id: str) -> str:
        for item in attachments:
            if item.get("id") == id:
                return _to_json(item)
        raise ValueError("Entity not found: Attachment - Could not find referenced Attachment.")

    @server.tool
    async def create_attachment(
        issue: str,
        base64Content: str,
        filename: str,
        contentType: str,
        subtitle: str | None = None,
        title: str | None = None,
    ) -> str:
        issue_obj = data.get_issue(issue)
        if not issue_obj:
            raise ValueError("Entity not found: Issue - Could not find referenced Issue.")
        try:
            decoded = base64.b64decode(base64Content, validate=True)
        except Exception as exc:  # pragma: no cover - defensive
            raise ValueError("Invalid base64 content.") from exc
        item = {
            "id": _new_id(),
            "issueId": issue_obj.get("id"),
            "filename": filename,
            "contentType": contentType,
            "title": title,
            "subtitle": subtitle,
            "size": len(decoded),
            "url": f"https://linear.app/attachment/{_new_id()}",
            "createdAt": _now_iso(),
        }
        attachments.append(item)
        return _to_json(item)

    @server.tool
    async def delete_attachment(id: str) -> str:
        for i, item in enumerate(attachments):
            if item.get("id") == id:
                del attachments[i]
                return _to_json({"success": True, "id": id})
        raise ValueError("Entity not found: Attachment - Could not find referenced Attachment.")

    # ------------------------------------------------------------------
    # Comments
    # ------------------------------------------------------------------
    @server.tool
    async def list_comments(issueId: str) -> str:
        result = data.get_comments(issueId, limit=250, cursor=None)
        return _list_payload("comments", result)

    @server.tool
    async def create_comment(issueId: str, body: str, parentId: str | None = None) -> str:
        comment = data.create_comment(issueId, body)
        if not comment:
            raise ValueError("Entity not found: Issue - Could not find referenced Issue.")
        if parentId:
            comment["parentId"] = parentId
        return _to_json(comment)

    # ------------------------------------------------------------------
    # Cycles
    # ------------------------------------------------------------------
    @server.tool
    async def list_cycles(teamId: str, type: Literal["current", "previous", "next"] | None = None) -> str:
        team_id = data.resolve_team(teamId)
        result = data.list_cycles(team_id=team_id, limit=250, cursor=None)
        nodes = result.get("nodes", [])
        if type:
            nodes = [c for c in nodes if (c.get("type") or "").lower() == type.lower()]
        return _to_json({"cycles": nodes, "hasNextPage": False})

    # ------------------------------------------------------------------
    # Documents
    # ------------------------------------------------------------------
    @server.tool
    async def get_document(id: str) -> str:
        doc = data.get_document(id)
        if not doc:
            raise ValueError("Entity not found: Document - Could not find referenced Document.")
        return _to_json(doc)

    @server.tool
    async def list_documents(
        createdAt: str | None = None,
        creatorId: str | None = None,
        cursor: str | None = None,
        includeArchived: bool = False,
        initiativeId: str | None = None,
        limit: int = 50,
        orderBy: Literal["createdAt", "updatedAt"] = "updatedAt",
        projectId: str | None = None,
        query: str | None = None,
        updatedAt: str | None = None,
    ) -> str:
        docs = data.all_documents()
        if projectId:
            try:
                pid = data.resolve_project(projectId)
                docs = [d for d in docs if ((d.get("project") or {}).get("id") == pid)]
            except ValueError:
                docs = []
        if creatorId:
            docs = [d for d in docs if ((d.get("creator") or {}).get("id") == creatorId)]
        if initiativeId:
            docs = [d for d in docs if ((d.get("initiative") or {}).get("id") == initiativeId)]
        if query:
            docs = [d for d in docs if _match_text(d.get("title"), query) or _match_text(d.get("content"), query)]
        if createdAt:
            docs = [d for d in docs if _match_date(d.get("createdAt"), createdAt)]
        if updatedAt:
            docs = [d for d in docs if _match_date(d.get("updatedAt"), updatedAt)]
        docs.sort(key=lambda d: d.get("updatedAt" if orderBy == "updatedAt" else "createdAt", ""), reverse=True)
        page = _paginate(docs, limit=limit, cursor=cursor)
        return _list_payload("documents", page)

    @server.tool
    async def create_document(
        title: str,
        color: str | None = None,
        content: str | None = None,
        icon: str | None = None,
        issue: str | None = None,
        project: str | None = None,
    ) -> str:
        project_id = data.resolve_project(project) if project else None
        doc = data.create_document(
            title=title,
            content=content,
            icon=icon,
            color=color,
            project=project_id,
        )
        if issue:
            issue_obj = data.get_issue(issue)
            if issue_obj:
                doc["issue"] = {"id": issue_obj.get("id"), "identifier": issue_obj.get("identifier")}
        return _to_json(doc)

    @server.tool
    async def update_document(
        id: str,
        color: str | None = None,
        content: str | None = None,
        icon: str | None = None,
        project: str | None = None,
        title: str | None = None,
    ) -> str:
        updates: dict[str, Any] = {}
        if color is not None:
            updates["color"] = color
        if content is not None:
            updates["content"] = content
        if icon is not None:
            updates["icon"] = icon
        if title is not None:
            updates["title"] = title
        if project is not None:
            updates["project"] = data._resolve_project_obj(data.resolve_project(project))
        doc = data.update_document(id, **updates)
        if not doc:
            raise ValueError("Entity not found: Document - Could not find referenced Document.")
        return _to_json(doc)

    @server.tool
    async def extract_images(markdown: str) -> str:
        matches = re.findall(r"!\[([^\]]*)\]\(([^)]+)\)", markdown)
        images = [{"alt": alt, "url": url} for alt, url in matches]
        return _to_json({"images": images})

    # ------------------------------------------------------------------
    # Issues
    # ------------------------------------------------------------------
    @server.tool
    async def get_issue(id: str, includeRelations: bool = False) -> str:
        issue = data.get_issue(id)
        if not issue:
            raise ValueError("Entity not found: Issue - Could not find referenced Issue.")
        return _to_json(_shape_issue(issue, include_relations=includeRelations))

    @server.tool
    async def list_issues(
        assignee: str | None = None,
        createdAt: str | None = None,
        cursor: str | None = None,
        cycle: str | None = None,
        delegate: str | None = None,
        includeArchived: bool = True,
        label: str | None = None,
        limit: int = 50,
        orderBy: Literal["createdAt", "updatedAt"] = "updatedAt",
        parentId: str | None = None,
        priority: int | None = None,
        project: str | None = None,
        query: str | None = None,
        state: str | None = None,
        team: str | None = None,
        updatedAt: str | None = None,
    ) -> str:
        issues = data.all_issues()
        if team:
            team_id = data.resolve_team(team)
            issues = [i for i in issues if ((i.get("team") or {}).get("id") == team_id)]
        if assignee:
            user_id = data.resolve_user(assignee)
            issues = [i for i in issues if ((i.get("assignee") or {}).get("id") == user_id)]
        if delegate:
            user_id = data.resolve_user(delegate)
            issues = [i for i in issues if ((i.get("delegate") or {}).get("id") == user_id)]
        if state:
            st = data.get_workflow_state(state)
            if st:
                state_type = st.get("type")
            else:
                state_type = state.lower()
            issues = [
                i
                for i in issues
                if (i.get("state") or {}).get("type", "").lower() == state_type
                or (i.get("state") or {}).get("name", "").lower() == state.lower()
                or (i.get("state") or {}).get("id") == state
            ]
        if project:
            project_id = data.resolve_project(project)
            issues = [i for i in issues if ((i.get("project") or {}).get("id") == project_id)]
        if priority is not None and priority != 0:
            issues = [i for i in issues if i.get("priority") == priority]
        if label:
            issues = [
                i
                for i in issues
                if any(
                    l.get("id") == label or (l.get("name") or "").lower() == label.lower()
                    for l in ((i.get("labels") or {}).get("nodes", []))
                )
            ]
        if cycle:
            issues = [
                i
                for i in issues
                if ((i.get("cycle") or {}).get("id") == cycle or (i.get("cycle") or {}).get("name") == cycle)
            ]
        if parentId:
            issues = [i for i in issues if i.get("parentId") == parentId]
        if query:
            issues = [
                i for i in issues if _match_text(i.get("title"), query) or _match_text(i.get("description"), query)
            ]
        if createdAt:
            issues = [i for i in issues if _match_date(i.get("createdAt"), createdAt)]
        if updatedAt:
            issues = [i for i in issues if _match_date(i.get("updatedAt"), updatedAt)]
        if includeArchived is False:
            issues = [i for i in issues if not i.get("archivedAt")]

        issues.sort(key=lambda i: i.get("updatedAt" if orderBy == "updatedAt" else "createdAt", ""), reverse=True)
        page = _paginate(issues, limit=limit, cursor=cursor)
        return _list_payload(
            "issues", {"nodes": [_shape_issue(i) for i in page["nodes"]], "pageInfo": page["pageInfo"]}
        )

    @server.tool
    async def create_issue(
        title: str,
        team: str,
        assignee: str | None = None,
        blockedBy: list[str] | None = None,
        blocks: list[str] | None = None,
        cycle: str | None = None,
        delegate: str | None = None,
        description: str | None = None,
        dueDate: str | None = None,
        duplicateOf: str | None = None,
        estimate: int | None = None,
        labels: list[str] | None = None,
        links: list[dict[str, str]] | None = None,
        milestone: str | None = None,
        parentId: str | None = None,
        priority: int | None = None,
        project: str | None = None,
        relatedTo: list[str] | None = None,
        state: str | None = None,
    ) -> str:
        team_id = data.resolve_team(team)
        assignee_id = data.resolve_user(assignee) if assignee else None
        state_id = data.resolve_state(state, team_id=team_id) if state else None
        project_id = data.resolve_project(project) if project else None
        label_ids = data.resolve_labels(labels, team_id=team_id) if labels else None
        issue = data.create_issue(
            title=title,
            team_id=team_id,
            description=description,
            assignee_id=assignee_id,
            state_id=state_id,
            project_id=project_id,
            priority=priority,
            label_ids=label_ids,
        )
        updates: dict[str, Any] = {
            "blockedBy": blockedBy or [],
            "blocks": blocks or [],
            "dueDate": dueDate,
            "duplicateOf": duplicateOf,
            "estimate": estimate,
            "links": links or [],
            "parentId": parentId,
            "relatedTo": relatedTo or [],
        }
        if cycle:
            updates["cycle"] = {"id": cycle, "name": cycle}
        if delegate:
            delegate_id = data.resolve_user(delegate)
            updates["delegate"] = data._resolve_user_obj(delegate_id)
        if milestone:
            updates["milestone"] = {"id": milestone, "name": milestone}
        issue = data.update_issue(issue["id"], **updates) or issue
        return _to_json(_shape_issue(issue, include_relations=True))

    @server.tool
    async def update_issue(
        id: str,
        assignee: str | None = None,
        blockedBy: list[str] | None = None,
        blocks: list[str] | None = None,
        cycle: str | None = None,
        delegate: str | None = None,
        description: str | None = None,
        dueDate: str | None = None,
        duplicateOf: str | None = None,
        estimate: int | None = None,
        labels: list[str] | None = None,
        links: list[dict[str, str]] | None = None,
        milestone: str | None = None,
        parentId: str | None = None,
        priority: int | None = None,
        project: str | None = None,
        relatedTo: list[str] | None = None,
        state: str | None = None,
        team: str | None = None,
        title: str | None = None,
    ) -> str:
        existing = data.get_issue(id)
        if not existing:
            raise ValueError("Entity not found: Issue - Could not find referenced Issue.")
        team_id = (existing.get("team") or {}).get("id")
        if team is not None:
            team_id = data.resolve_team(team)
        updates: dict[str, Any] = {}
        if title is not None:
            updates["title"] = title
        if description is not None:
            updates["description"] = description
        if priority is not None:
            updates["priority"] = priority
        if assignee is not None:
            updates["assignee"] = data._resolve_user_obj(data.resolve_user(assignee))
        if state is not None:
            updates["state"] = data._resolve_state_obj(data.resolve_state(state, team_id=team_id), team_id=team_id)
        if project is not None:
            updates["project"] = data._resolve_project_obj(data.resolve_project(project))
        if labels is not None:
            updates["labels"] = {"nodes": data._resolve_label_objs(data.resolve_labels(labels, team_id=team_id))}
        if blockedBy is not None:
            updates["blockedBy"] = blockedBy
        if blocks is not None:
            updates["blocks"] = blocks
        if cycle is not None:
            updates["cycle"] = {"id": cycle, "name": cycle}
        if delegate is not None:
            updates["delegate"] = data._resolve_user_obj(data.resolve_user(delegate))
        if dueDate is not None:
            updates["dueDate"] = dueDate
        if duplicateOf is not None:
            updates["duplicateOf"] = duplicateOf
        if estimate is not None:
            updates["estimate"] = estimate
        if links is not None:
            updates["links"] = links
        if milestone is not None:
            updates["milestone"] = {"id": milestone, "name": milestone}
        if parentId is not None:
            updates["parentId"] = parentId
        if relatedTo is not None:
            updates["relatedTo"] = relatedTo
        issue = data.update_issue(id, **updates)
        if not issue:
            raise ValueError("Entity not found: Issue - Could not find referenced Issue.")
        return _to_json(_shape_issue(issue, include_relations=True))

    @server.tool
    async def list_issue_statuses(team: str) -> str:
        team_id = data.resolve_team(team)
        states = data.list_workflow_states(team_id=team_id)
        payload = [
            {
                "id": s.get("id"),
                "name": s.get("name"),
                "type": s.get("type"),
                "color": s.get("color"),
            }
            for s in states
        ]
        return _to_json({"statuses": payload})

    @server.tool
    async def get_issue_status(id: str, name: str, team: str) -> str:
        team_id = data.resolve_team(team)
        state = data.get_workflow_state(name or id, team_id=team_id)
        if not state and id:
            state = data.get_workflow_state(id, team_id=team_id)
        if not state:
            raise ValueError("Entity not found: IssueStatus - Could not find referenced IssueStatus.")
        return _to_json(
            {
                "id": state.get("id"),
                "name": state.get("name"),
                "type": state.get("type"),
                "color": state.get("color"),
                "teamId": team_id,
            }
        )

    @server.tool
    async def list_issue_labels(
        cursor: str | None = None,
        limit: int = 50,
        name: str | None = None,
        orderBy: Literal["createdAt", "updatedAt"] = "updatedAt",
        team: str | None = None,
    ) -> str:
        team_id = data.resolve_team(team) if team else None
        labels = data.list_labels(team_id=team_id, query=name, limit=250, cursor=None)["nodes"]
        labels.sort(key=lambda l: l.get("updatedAt" if orderBy == "updatedAt" else "createdAt", ""), reverse=True)
        page = _paginate(labels, limit=limit, cursor=cursor)
        return _list_payload("labels", page)

    @server.tool
    async def create_issue_label(
        name: str,
        color: str | None = None,
        description: str | None = None,
        isGroup: bool = False,
        parentId: str | None = None,
        teamId: str | None = None,
    ) -> str:
        team_id = data.resolve_team(teamId) if teamId else None
        label = data.create_issue_label(name=name, color=color, description=description, team_id=team_id)
        if isGroup is not None:
            label["isGroup"] = isGroup
        if parentId is not None:
            label["parentId"] = parentId
        return _to_json(label)

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------
    @server.tool
    async def list_projects(
        createdAt: str | None = None,
        cursor: str | None = None,
        includeArchived: bool = False,
        includeMembers: bool = False,
        includeMilestones: bool = False,
        initiative: str | None = None,
        limit: int = 50,
        member: str | None = None,
        orderBy: Literal["createdAt", "updatedAt"] = "updatedAt",
        query: str | None = None,
        state: str | None = None,
        team: str | None = None,
        updatedAt: str | None = None,
    ) -> str:
        team_id = data.resolve_team(team) if team else None
        projects = data.all_projects()
        if team_id:
            projects = [
                p for p in projects if any(t.get("id") == team_id for t in ((p.get("teams") or {}).get("nodes", [])))
            ]
        if state:
            projects = [p for p in projects if (p.get("state") or "").lower() == state.lower()]
        if query:
            projects = [
                p for p in projects if _match_text(p.get("name"), query) or _match_text(p.get("description"), query)
            ]
        if initiative:
            projects = [
                p
                for p in projects
                if any(
                    (i.get("id") == initiative or (i.get("name") or "").lower() == initiative.lower())
                    for i in (p.get("initiatives") or [])
                )
            ]
        if member:
            member_id = data.resolve_user(member)
            projects = [
                p
                for p in projects
                if any((m.get("id") == member_id) for m in ((p.get("members") or {}).get("nodes", [])))
            ]
        if createdAt:
            projects = [p for p in projects if _match_date(p.get("createdAt"), createdAt)]
        if updatedAt:
            projects = [p for p in projects if _match_date(p.get("updatedAt"), updatedAt)]
        if includeArchived is False:
            projects = [p for p in projects if not p.get("archivedAt")]
        projects.sort(key=lambda p: p.get("updatedAt" if orderBy == "updatedAt" else "createdAt", ""), reverse=True)
        page = _paginate(projects, limit=limit, cursor=cursor)
        return _list_payload(
            "projects", {"nodes": [_shape_project(p) for p in page["nodes"]], "pageInfo": page["pageInfo"]}
        )

    @server.tool
    async def get_project(
        query: str,
        includeMembers: bool = False,
        includeMilestones: bool = False,
        includeResources: bool = False,
    ) -> str:
        project = data.get_project(query)
        if not project:
            raise ValueError("Entity not found: Project - Could not find referenced Project.")
        payload = _shape_project(project)
        if includeMembers:
            payload["members"] = (project.get("members") or {}).get("nodes", [])
        if includeMilestones:
            payload["milestones"] = [m for m in milestones if m.get("projectId") == project.get("id")]
        if includeResources:
            payload["resources"] = project.get("resources", [])
        return _to_json(payload)

    @server.tool
    async def create_project(
        name: str,
        team: str,
        color: str | None = None,
        description: str | None = None,
        icon: str | None = None,
        initiative: str | None = None,
        labels: list[str] | None = None,
        lead: str | None = None,
        priority: int | None = None,
        startDate: str | None = None,
        state: str | None = None,
        summary: str | None = None,
        targetDate: str | None = None,
    ) -> str:
        team_id = data.resolve_team(team)
        lead_id = data.resolve_user(lead) if lead else None
        project = data.create_project(
            name=name,
            team_ids=[team_id],
            description=description,
            lead=lead_id,
            startDate=startDate,
            targetDate=targetDate,
            priority=priority,
            icon=icon,
            color=color,
        )
        updates: dict[str, Any] = {}
        if state is not None:
            updates["state"] = state
        if summary is not None:
            updates["summary"] = summary
        if labels is not None:
            updates["labels"] = labels
        if initiative:
            init_obj = next(
                (
                    i
                    for i in initiative_store
                    if i.get("id") == initiative or (i.get("name") or "").lower() == initiative.lower()
                ),
                None,
            )
            if init_obj:
                updates["initiatives"] = [{"id": init_obj["id"], "name": init_obj.get("name")}]
        if updates:
            project = data.update_project(project["id"], **updates) or project
        return _to_json(_shape_project(project))

    @server.tool
    async def update_project(
        id: str,
        color: str | None = None,
        description: str | None = None,
        icon: str | None = None,
        initiatives: list[str] | None = None,
        labels: list[str] | None = None,
        lead: str | None = None,
        name: str | None = None,
        priority: int | None = None,
        startDate: str | None = None,
        state: str | None = None,
        summary: str | None = None,
        targetDate: str | None = None,
    ) -> str:
        existing = data.get_project(id)
        if not existing:
            raise ValueError("Entity not found: Project - Could not find referenced Project.")
        updates: dict[str, Any] = {}
        if color is not None:
            updates["color"] = color
        if description is not None:
            updates["description"] = description
        if icon is not None:
            updates["icon"] = icon
        if lead is not None:
            updates["lead"] = data._resolve_user_obj(data.resolve_user(lead))
        if name is not None:
            updates["name"] = name
        if priority is not None:
            updates["priority"] = priority
        if startDate is not None:
            updates["startDate"] = startDate
        if state is not None:
            updates["state"] = state
        if summary is not None:
            updates["summary"] = summary
        if targetDate is not None:
            updates["targetDate"] = targetDate
        if labels is not None:
            updates["labels"] = labels
        if initiatives is not None:
            resolved = []
            for value in initiatives:
                init_obj = next(
                    (
                        i
                        for i in initiative_store
                        if i.get("id") == value or (i.get("name") or "").lower() == value.lower()
                    ),
                    None,
                )
                if init_obj:
                    resolved.append({"id": init_obj["id"], "name": init_obj.get("name")})
            updates["initiatives"] = resolved
        project = data.update_project(existing["id"], **updates)
        if not project:
            raise ValueError("Entity not found: Project - Could not find referenced Project.")
        return _to_json(_shape_project(project))

    @server.tool
    async def list_project_labels(
        cursor: str | None = None,
        limit: int = 50,
        name: str | None = None,
        orderBy: Literal["createdAt", "updatedAt"] = "updatedAt",
    ) -> str:
        labels = data.list_project_labels(limit=250, cursor=None)["nodes"]
        if name:
            labels = [l for l in labels if _match_text(l.get("name"), name)]
        labels.sort(key=lambda l: l.get("updatedAt" if orderBy == "updatedAt" else "createdAt", ""), reverse=True)
        page = _paginate(labels, limit=limit, cursor=cursor)
        return _list_payload("labels", page)

    # ------------------------------------------------------------------
    # Milestones
    # ------------------------------------------------------------------
    @server.tool
    async def list_milestones(project: str) -> str:
        project_id = data.resolve_project(project)
        rows = [m for m in milestones if m.get("projectId") == project_id]
        return _to_json({"milestones": rows})

    @server.tool
    async def get_milestone(project: str, query: str) -> str:
        project_id = data.resolve_project(project)
        item = next(
            (
                m
                for m in milestones
                if m.get("projectId") == project_id
                and (m.get("id") == query or (m.get("name") or "").lower() == query.lower())
            ),
            None,
        )
        if not item:
            raise ValueError("Entity not found: Milestone - Could not find referenced Milestone.")
        return _to_json(item)

    @server.tool
    async def create_milestone(
        project: str,
        name: str,
        description: str | None = None,
        targetDate: str | None = None,
    ) -> str:
        project_obj = data.get_project(project)
        if not project_obj:
            raise ValueError("Entity not found: Project - Could not find referenced Project.")
        item = {
            "id": _new_id(),
            "projectId": project_obj.get("id"),
            "project": {"id": project_obj.get("id"), "name": project_obj.get("name")},
            "name": name,
            "description": description,
            "targetDate": targetDate,
            "createdAt": _now_iso(),
            "updatedAt": _now_iso(),
        }
        milestones.append(item)
        return _to_json(item)

    @server.tool
    async def update_milestone(
        project: str,
        id: str,
        description: str | None = None,
        name: str | None = None,
        targetDate: str | None = None,
    ) -> str:
        project_id = data.resolve_project(project)
        item = next((m for m in milestones if m.get("projectId") == project_id and m.get("id") == id), None)
        if not item:
            raise ValueError("Entity not found: Milestone - Could not find referenced Milestone.")
        if description is not None:
            item["description"] = description
        if name is not None:
            item["name"] = name
        if targetDate is not None:
            item["targetDate"] = targetDate
        item["updatedAt"] = _now_iso()
        return _to_json(item)

    # ------------------------------------------------------------------
    # Teams & Users
    # ------------------------------------------------------------------
    @server.tool
    async def list_teams(
        createdAt: str | None = None,
        cursor: str | None = None,
        includeArchived: bool = False,
        limit: int = 50,
        orderBy: Literal["createdAt", "updatedAt"] = "updatedAt",
        query: str | None = None,
        updatedAt: str | None = None,
    ) -> str:
        teams = data.list_teams(query=query)
        if createdAt:
            teams = [t for t in teams if _match_date(t.get("createdAt"), createdAt)]
        if updatedAt:
            teams = [t for t in teams if _match_date(t.get("updatedAt"), updatedAt)]
        if includeArchived is False:
            teams = [t for t in teams if not t.get("archivedAt")]
        teams.sort(key=lambda t: t.get("updatedAt" if orderBy == "updatedAt" else "createdAt", ""), reverse=True)
        page = _paginate(teams, limit=limit, cursor=cursor)
        return _list_payload("teams", {"nodes": [_shape_team(t) for t in page["nodes"]], "pageInfo": page["pageInfo"]})

    @server.tool
    async def get_team(query: str) -> str:
        team = data.get_team(query)
        if not team:
            raise ValueError("Entity not found: Team - Could not find referenced Team.")
        return _to_json(_shape_team(team))

    @server.tool
    async def list_users(
        cursor: str | None = None,
        limit: int = 50,
        orderBy: Literal["createdAt", "updatedAt"] = "updatedAt",
        query: str | None = None,
        team: str | None = None,
    ) -> str:
        team_id = data.resolve_team(team) if team else None
        result = data.list_users(query=query, limit=250, cursor=None)
        users = result["nodes"]
        if team_id:
            users = [
                u
                for u in users
                if any(
                    ((i.get("team") or {}).get("id") == team_id and (i.get("assignee") or {}).get("id") == u.get("id"))
                    for i in data.all_issues()
                )
            ]
        users.sort(key=lambda u: u.get("updatedAt" if orderBy == "updatedAt" else "createdAt", ""), reverse=True)
        page = _paginate(users, limit=limit, cursor=cursor)
        return _list_payload("users", {"nodes": [_shape_user(u) for u in page["nodes"]], "pageInfo": page["pageInfo"]})

    @server.tool
    async def get_user(query: str) -> str:
        user = data.get_user(query)
        if not user:
            raise ValueError("Entity not found: User - Could not find referenced User.")
        return _to_json(_shape_user(user))

    # ------------------------------------------------------------------
    # Documentation Search
    # ------------------------------------------------------------------
    @server.tool
    async def search_documentation(query: str, page: int = 0) -> str:
        docs = data.all_documents()
        matches = [
            {
                "id": d.get("id"),
                "title": d.get("title"),
                "slugId": d.get("slugId"),
                "url": d.get("url"),
                "snippet": (d.get("content") or "")[:300],
            }
            for d in docs
            if _match_text(d.get("title"), query) or _match_text(d.get("content"), query)
        ]
        page_num = max(page or 1, 1)
        start = (page_num - 1) * 10
        chunk = matches[start : start + 10]
        return _to_json({"results": chunk, "page": page_num, "total": len(matches)})

    # ------------------------------------------------------------------
    # Initiatives
    # ------------------------------------------------------------------
    @server.tool
    async def list_initiatives(
        createdAt: str | None = None,
        cursor: str | None = None,
        includeArchived: bool = False,
        includeProjects: bool = False,
        includeSubInitiatives: bool = False,
        limit: int = 50,
        orderBy: Literal["createdAt", "updatedAt"] = "updatedAt",
        owner: str | None = None,
        parentInitiative: str | None = None,
        query: str | None = None,
        status: str | None = None,
        updatedAt: str | None = None,
    ) -> str:
        items = list(initiative_store)
        if owner:
            owner_id = data.resolve_user(owner)
            items = [i for i in items if ((i.get("owner") or {}).get("id") == owner_id)]
        if parentInitiative:
            items = [i for i in items if ((i.get("parentInitiative") or {}).get("id") == parentInitiative)]
        if query:
            items = [i for i in items if _match_text(i.get("name"), query) or _match_text(i.get("description"), query)]
        if status:
            items = [i for i in items if (i.get("status") or "").lower() == status.lower()]
        if createdAt:
            items = [i for i in items if _match_date(i.get("createdAt"), createdAt)]
        if updatedAt:
            items = [i for i in items if _match_date(i.get("updatedAt"), updatedAt)]
        if includeArchived is False:
            items = [i for i in items if not i.get("archivedAt")]
        if not includeProjects:
            items = [{k: v for k, v in i.items() if k != "projects"} for i in items]
        if not includeSubInitiatives:
            items = [{k: v for k, v in i.items() if k != "subInitiatives"} for i in items]
        items.sort(key=lambda i: i.get("updatedAt" if orderBy == "updatedAt" else "createdAt", ""), reverse=True)
        page_data = _paginate(items, limit=limit, cursor=cursor)
        return _list_payload("initiatives", page_data)

    @server.tool
    async def get_initiative(
        query: str,
        includeProjects: bool = False,
        includeSubInitiatives: bool = False,
    ) -> str:
        item = next(
            (i for i in initiative_store if i.get("id") == query or (i.get("name") or "").lower() == query.lower()),
            None,
        )
        if not item:
            raise ValueError("Entity not found: Initiative - Could not find referenced Initiative.")
        payload = dict(item)
        if not includeProjects:
            payload.pop("projects", None)
        if not includeSubInitiatives:
            payload.pop("subInitiatives", None)
        return _to_json(payload)

    @server.tool
    async def create_initiative(
        name: str,
        color: str | None = None,
        description: str | None = None,
        icon: str | None = None,
        owner: str | None = None,
        status: str | None = None,
        summary: str | None = None,
        targetDate: str | None = None,
    ) -> str:
        owner_obj = data._resolve_user_obj(data.resolve_user(owner)) if owner else None
        item = {
            "id": _new_id(),
            "name": name,
            "color": color,
            "description": description,
            "icon": icon,
            "owner": owner_obj,
            "status": status or "planned",
            "summary": summary,
            "targetDate": targetDate,
            "projects": [],
            "subInitiatives": [],
            "createdAt": _now_iso(),
            "updatedAt": _now_iso(),
        }
        initiative_store.append(item)
        return _to_json(item)

    @server.tool
    async def update_initiative(
        id: str,
        color: str | None = None,
        description: str | None = None,
        icon: str | None = None,
        name: str | None = None,
        owner: str | None = None,
        parentInitiative: str | None = None,
        status: str | None = None,
        summary: str | None = None,
        targetDate: str | None = None,
    ) -> str:
        item = next((i for i in initiative_store if i.get("id") == id), None)
        if not item:
            raise ValueError("Entity not found: Initiative - Could not find referenced Initiative.")
        if color is not None:
            item["color"] = color
        if description is not None:
            item["description"] = description
        if icon is not None:
            item["icon"] = icon
        if name is not None:
            item["name"] = name
        if owner is not None:
            item["owner"] = data._resolve_user_obj(data.resolve_user(owner))
        if parentInitiative is not None:
            parent = next((i for i in initiative_store if i.get("id") == parentInitiative), None)
            item["parentInitiative"] = {"id": parent.get("id"), "name": parent.get("name")} if parent else None
        if status is not None:
            item["status"] = status
        if summary is not None:
            item["summary"] = summary
        if targetDate is not None:
            item["targetDate"] = targetDate
        item["updatedAt"] = _now_iso()
        return _to_json(item)

    # ------------------------------------------------------------------
    # Status updates
    # ------------------------------------------------------------------
    @server.tool
    async def get_status_updates(
        type: Literal["project", "initiative"],
        createdAt: str | None = None,
        cursor: str | None = None,
        id: str | None = None,
        includeArchived: bool = False,
        initiative: str | None = None,
        limit: int = 50,
        orderBy: Literal["createdAt", "updatedAt"] = "updatedAt",
        project: str | None = None,
        updatedAt: str | None = None,
        user: str | None = None,
    ) -> str:
        rows = [s for s in status_updates if (s.get("type") or "").lower() == type.lower()]
        if id:
            rows = [s for s in rows if s.get("id") == id]
        if initiative:
            rows = [s for s in rows if ((s.get("initiative") or {}).get("id") == initiative)]
        if project:
            rows = [s for s in rows if ((s.get("project") or {}).get("id") == project)]
        if user:
            uid = data.resolve_user(user)
            rows = [s for s in rows if ((s.get("user") or {}).get("id") == uid)]
        if createdAt:
            rows = [s for s in rows if _match_date(s.get("createdAt"), createdAt)]
        if updatedAt:
            rows = [s for s in rows if _match_date(s.get("updatedAt"), updatedAt)]
        if includeArchived is False:
            rows = [s for s in rows if not s.get("archivedAt")]
        rows.sort(key=lambda s: s.get("updatedAt" if orderBy == "updatedAt" else "createdAt", ""), reverse=True)
        page_data = _paginate(rows, limit=limit, cursor=cursor)
        return _list_payload("statusUpdates", page_data)

    @server.tool
    async def save_status_update(
        type: Literal["project", "initiative"],
        body: str | None = None,
        health: Literal["onTrack", "atRisk", "offTrack"] | None = None,
        id: str | None = None,
        initiative: str | None = None,
        isDiffHidden: bool | None = None,
        project: str | None = None,
    ) -> str:
        project_obj = data.get_project(project) if project else None
        initiative_obj = next((i for i in initiative_store if i.get("id") == initiative), None) if initiative else None
        if id:
            item = next(
                (s for s in status_updates if s.get("id") == id and (s.get("type") or "").lower() == type.lower()), None
            )
            if not item:
                raise ValueError("Entity not found: StatusUpdate - Could not find referenced StatusUpdate.")
            if body is not None:
                item["body"] = body
            if health is not None:
                item["health"] = health
            if isDiffHidden is not None:
                item["isDiffHidden"] = isDiffHidden
            if project_obj is not None:
                item["project"] = {"id": project_obj.get("id"), "name": project_obj.get("name")}
            if initiative_obj is not None:
                item["initiative"] = {"id": initiative_obj.get("id"), "name": initiative_obj.get("name")}
            item["updatedAt"] = _now_iso()
            return _to_json(item)

        viewer = data.get_viewer()
        new_item = {
            "id": _new_id(),
            "type": type,
            "body": body,
            "health": health,
            "isDiffHidden": bool(isDiffHidden),
            "project": {"id": project_obj.get("id"), "name": project_obj.get("name")} if project_obj else None,
            "initiative": {"id": initiative_obj.get("id"), "name": initiative_obj.get("name")}
            if initiative_obj
            else None,
            "user": {"id": viewer.get("id"), "name": viewer.get("name")},
            "createdAt": _now_iso(),
            "updatedAt": _now_iso(),
        }
        status_updates.append(new_item)
        return _to_json(new_item)

    @server.tool
    async def delete_status_update(type: Literal["project", "initiative"], id: str) -> str:
        for i, row in enumerate(status_updates):
            if row.get("id") == id and (row.get("type") or "").lower() == type.lower():
                del status_updates[i]
                return _to_json({"success": True, "id": id, "type": type})
        raise ValueError("Entity not found: StatusUpdate - Could not find referenced StatusUpdate.")

    await _ensure_tool_descriptions(server)
    await _fix_schemas(server)
    return server
