"""Mock Linear MCP data layer — loads and queries Linear data from JSON files.

Implements the data backend for mock Linear MCP tools. Loads issues, teams,
projects, users, and other entities from static JSON files and provides
query, filter, and mutation methods that match real Linear API behavior.

No live Linear instance or API key is required.

Typical usage:
    from servers.mcp.linear import MockLinearData

    data = MockLinearData(data_dir="linear_data/")
    data.load()
    issues = data.filter_issues(team_id="...", state_type="started")
"""

from __future__ import annotations

import base64
import json
import logging
import os
import uuid
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pagination helper
# ---------------------------------------------------------------------------

PRIORITY_LABELS = {0: "None", 1: "Urgent", 2: "High", 3: "Medium", 4: "Low"}


def _paginate(
    items: list[dict[str, Any]],
    limit: int = 50,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Apply cursor-based pagination to a list.

    Returns:
        {"nodes": [...], "pageInfo": {"hasNextPage": bool, "endCursor": str | None}}
    """
    start = 0
    if cursor:
        try:
            start = int(base64.b64decode(cursor).decode()) + 1
        except (ValueError, Exception):
            start = 0

    limit = min(limit, 250)
    end = start + limit
    page = items[start:end]
    has_next = end < len(items)

    return {
        "nodes": page,
        "pageInfo": {
            "hasNextPage": has_next,
            "endCursor": base64.b64encode(str(end - 1).encode()).decode() if has_next else None,
        },
    }


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _new_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Data layer
# ---------------------------------------------------------------------------


class MockLinearData:
    """Loads and queries mock Linear data from JSON files.

    Static data comes from JSON files in ``data_dir``. In-memory mutations
    (create/update/delete) persist within a session and are visible to
    subsequent reads.
    """

    def __init__(self, data_dir: str | None = None) -> None:
        self.data_dir = data_dir or self._find_data_dir()
        self._loaded = False

        # Static data (from JSON files)
        self.issues: list[dict[str, Any]] = []
        self.teams: list[dict[str, Any]] = []
        self.projects: list[dict[str, Any]] = []
        self.users: list[dict[str, Any]] = []
        self.cycles: list[dict[str, Any]] = []
        self.documents: list[dict[str, Any]] = []
        self.labels: list[dict[str, Any]] = []
        self.project_labels: list[dict[str, Any]] = []
        self.workflow_states: list[dict[str, Any]] = []
        self.viewer: dict[str, Any] = {}

        # In-memory mutations (session-scoped)
        self._created_issues: list[dict[str, Any]] = []
        self._created_comments: dict[str, list[dict[str, Any]]] = {}  # issue_id -> comments
        self._created_projects: list[dict[str, Any]] = []
        self._created_documents: list[dict[str, Any]] = []
        self._created_labels: list[dict[str, Any]] = []
        self._updated: dict[str, dict[str, Any]] = {}  # entity_id -> field overrides
        self._deleted_ids: set[str] = set()

        # Auto-increment counter for issue identifiers
        self._next_issue_number: int = 1

    # -- discovery -----------------------------------------------------------

    @staticmethod
    def _find_data_dir() -> str:
        """Try common locations for the linear data directory."""
        candidates = [
            os.environ.get("LINEAR_DATA_DIR", ""),
            os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
                "linear_data",
            ),
            "/mcp_server/linear_data",
        ]
        for path in candidates:
            if path and os.path.isdir(path):
                return path
        return candidates[1]  # default to bundled location

    # -- loading -------------------------------------------------------------

    def _load_json(self, filename: str) -> list[dict[str, Any]] | dict[str, Any]:
        """Load a JSON file from data_dir. Returns [] or {} on missing file."""
        path = os.path.join(self.data_dir, filename)
        if os.path.exists(path):
            with open(path) as f:
                data = json.load(f)
            logger.info("Loaded %s from %s", filename, path)
            return data
        logger.debug("File not found (optional): %s", path)
        return []

    def _load_json_list(self, filename: str) -> list[dict[str, Any]]:
        """Load a JSON file expected to contain a list. Returns [] on missing/non-list."""
        data = self._load_json(filename)
        return data if isinstance(data, list) else []

    def load(self) -> None:
        """Load all entity data from JSON files in data_dir."""
        self.issues = self._load_json_list("issues.json")
        self.teams = self._load_json_list("teams.json")
        self.projects = self._load_json_list("projects.json")
        self.users = self._load_json_list("users.json")
        self.cycles = self._load_json_list("cycles.json")
        self.documents = self._load_json_list("documents.json")
        self.labels = self._load_json_list("labels.json")
        self.project_labels = self._load_json_list("project_labels.json")
        self.workflow_states = self._load_json_list("workflow_states.json")

        viewer = self._load_json("viewer.json")
        if isinstance(viewer, dict) and viewer:
            self.viewer = viewer
        elif self.users:
            self.viewer = dict(self.users[0])
        else:
            self.viewer = {"id": _new_id(), "name": "agent", "email": "agent@example.com"}

        # Set auto-increment counter based on existing issues
        self._init_issue_counter()
        self._loaded = True

    def reload(self, data_dir: str | None = None) -> None:
        """Reload data, optionally from a different directory.

        Resets all in-memory mutations.
        """
        if data_dir:
            self.data_dir = data_dir
        self._reset_mutable()
        self._loaded = False
        self.load()

    def _reset_mutable(self) -> None:
        """Clear all in-memory mutations."""
        self._created_issues.clear()
        self._created_comments.clear()
        self._created_projects.clear()
        self._created_documents.clear()
        self._created_labels.clear()
        self._updated.clear()
        self._deleted_ids.clear()

    def _init_issue_counter(self) -> None:
        """Set next issue number based on highest existing identifier."""
        max_num = 0
        for issue in self.issues:
            identifier = issue.get("identifier", "")
            parts = identifier.rsplit("-", 1)
            if len(parts) == 2:
                try:
                    max_num = max(max_num, int(parts[1]))
                except ValueError:
                    pass
        self._next_issue_number = max_num + 1

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    # -- helpers: apply updates & filter deleted ----------------------------

    def _apply_updates(self, entity: dict[str, Any]) -> dict[str, Any]:
        """Apply in-memory field overrides to an entity."""
        entity_id = entity.get("id", "")
        if entity_id in self._updated:
            merged = dict(entity)
            merged.update(self._updated[entity_id])
            return merged
        return entity

    def _is_deleted(self, entity: dict[str, Any]) -> bool:
        return entity.get("id", "") in self._deleted_ids

    # ======================================================================
    # QUERIES: Issues
    # ======================================================================

    def all_issues(self) -> list[dict[str, Any]]:
        """All issues (static + created), with updates applied, deleted filtered."""
        self._ensure_loaded()
        combined = self.issues + self._created_issues
        return [self._apply_updates(i) for i in combined if not self._is_deleted(i)]

    def get_issue(self, id_or_identifier: str) -> dict[str, Any] | None:
        """Lookup by UUID or identifier (e.g. 'HUD-42')."""
        for issue in self.all_issues():
            if issue.get("id") == id_or_identifier or issue.get("identifier") == id_or_identifier:
                return issue
        return None

    def filter_issues(
        self,
        *,
        team_id: str | None = None,
        assignee_id: str | None = None,
        state_type: str | None = None,
        project_id: str | None = None,
        priority: int | None = None,
        query: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
        order_by: str | None = None,
    ) -> dict[str, Any]:
        """Filter issues by criteria. Returns paginated result."""
        results = self.all_issues()

        if team_id:
            results = [i for i in results if (i.get("team") or {}).get("id") == team_id]
        if assignee_id:
            results = [i for i in results if (i.get("assignee") or {}).get("id") == assignee_id]
        if state_type:
            results = [i for i in results if (i.get("state") or {}).get("type") == state_type]
        if project_id:
            results = [i for i in results if (i.get("project") or {}).get("id") == project_id]
        if priority is not None:
            results = [i for i in results if i.get("priority") == priority]
        if query:
            kw = query.lower()
            results = [
                i for i in results if kw in (i.get("title") or "").lower() or kw in (i.get("description") or "").lower()
            ]

        # Sort
        if order_by == "updatedAt":
            results.sort(key=lambda i: i.get("updatedAt", ""), reverse=True)
        else:
            results.sort(key=lambda i: i.get("createdAt", ""), reverse=True)

        return _paginate(results, limit=limit, cursor=cursor)

    # ======================================================================
    # MUTATIONS: Issues
    # ======================================================================

    def create_issue(
        self,
        *,
        title: str,
        team_id: str,
        description: str | None = None,
        assignee_id: str | None = None,
        state_id: str | None = None,
        project_id: str | None = None,
        priority: int | None = None,
        label_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create an issue in-memory. Auto-assigns identifier."""
        team = self.get_team(team_id)
        team_key = (team or {}).get("key", "TEAM")

        identifier = f"{team_key}-{self._next_issue_number}"
        self._next_issue_number += 1

        now = _now_iso()
        issue: dict[str, Any] = {
            "id": _new_id(),
            "identifier": identifier,
            "title": title,
            "description": description,
            "priority": priority or 0,
            "url": f"https://linear.app/issue/{identifier}",
            "createdAt": now,
            "updatedAt": now,
            "completedAt": None,
            "state": self._resolve_state_obj(state_id, team_id) if state_id else self._default_state(team_id),
            "assignee": self._resolve_user_obj(assignee_id) if assignee_id else None,
            "team": {"id": team_id, "name": (team or {}).get("name", ""), "key": team_key},
            "project": self._resolve_project_obj(project_id) if project_id else None,
            "labels": {"nodes": self._resolve_label_objs(label_ids) if label_ids else []},
        }
        self._created_issues.append(issue)
        return issue

    def update_issue(self, issue_id: str, **fields: Any) -> dict[str, Any] | None:
        """Update issue fields. Returns updated issue or None."""
        issue = self.get_issue(issue_id)
        if not issue:
            return None

        real_id = issue["id"]
        updates = dict(self._updated.get(real_id, {}))
        updates["updatedAt"] = _now_iso()

        for key, value in fields.items():
            if value is not None:
                updates[key] = value

        self._updated[real_id] = updates
        return self.get_issue(issue_id)

    def delete_issue(self, issue_id: str) -> bool:
        """Mark issue as deleted."""
        issue = self.get_issue(issue_id)
        if not issue:
            return False
        self._deleted_ids.add(issue["id"])
        return True

    # ======================================================================
    # QUERIES & MUTATIONS: Comments
    # ======================================================================

    def get_comments(self, issue_id: str, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        """Get comments for an issue (static + created). Returns paginated result."""
        self._ensure_loaded()
        issue = self.get_issue(issue_id)
        if not issue:
            return _paginate([], limit=limit, cursor=cursor)

        real_id = issue["id"]

        # Static comments from issue data
        static_comments = []
        labels_or_comments = issue.get("comments")
        if isinstance(labels_or_comments, dict):
            static_comments = labels_or_comments.get("nodes", [])
        elif isinstance(labels_or_comments, list):
            static_comments = labels_or_comments

        # In-memory created comments
        created = self._created_comments.get(real_id, [])
        all_comments = static_comments + created
        all_comments.sort(key=lambda c: c.get("createdAt", ""), reverse=True)

        return _paginate(all_comments, limit=limit, cursor=cursor)

    def create_comment(self, issue_id: str, body: str) -> dict[str, Any] | None:
        """Add a comment to an issue."""
        issue = self.get_issue(issue_id)
        if not issue:
            return None

        real_id = issue["id"]
        comment: dict[str, Any] = {
            "id": _new_id(),
            "body": body,
            "createdAt": _now_iso(),
            "user": dict(self.viewer),
        }
        self._created_comments.setdefault(real_id, []).append(comment)
        return comment

    # ======================================================================
    # QUERIES: Teams
    # ======================================================================

    def list_teams(self, query: str | None = None) -> list[dict[str, Any]]:
        """List teams, optionally filtered by name."""
        self._ensure_loaded()
        results = list(self.teams)
        if query:
            kw = query.lower()
            results = [t for t in results if kw in t.get("name", "").lower() or kw in t.get("key", "").lower()]
        return results

    def get_team(self, id_or_key: str) -> dict[str, Any] | None:
        """Lookup by UUID or key (e.g. 'HUD')."""
        self._ensure_loaded()
        for team in self.teams:
            if team.get("id") == id_or_key or team.get("key", "").upper() == id_or_key.upper():
                return team
            if team.get("name", "").lower() == id_or_key.lower():
                return team
        return None

    # ======================================================================
    # QUERIES: Users
    # ======================================================================

    def list_users(
        self,
        query: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List users, optionally filtered by name/email. Returns paginated result."""
        self._ensure_loaded()
        results = list(self.users)
        if query:
            kw = query.lower()
            results = [
                u
                for u in results
                if kw in u.get("name", "").lower()
                or kw in (u.get("email") or "").lower()
                or kw in (u.get("displayName") or "").lower()
            ]
        return _paginate(results, limit=limit, cursor=cursor)

    def get_user(self, id_or_name: str) -> dict[str, Any] | None:
        """Lookup by UUID, name, email, or 'me'."""
        self._ensure_loaded()
        if id_or_name.lower() == "me":
            return dict(self.viewer)
        for user in self.users:
            if user.get("id") == id_or_name:
                return user
            if user.get("name", "").lower() == id_or_name.lower():
                return user
            if (user.get("email") or "").lower() == id_or_name.lower():
                return user
        return None

    def get_viewer(self) -> dict[str, Any]:
        """Get the authenticated user."""
        self._ensure_loaded()
        return dict(self.viewer)

    # ======================================================================
    # QUERIES: Workflow States
    # ======================================================================

    def list_workflow_states(self, team_id: str | None = None) -> list[dict[str, Any]]:
        """List workflow states, optionally filtered by team."""
        self._ensure_loaded()
        results = list(self.workflow_states)
        if team_id:
            results = [s for s in results if s.get("teamId") == team_id]
        return results

    def get_workflow_state(self, id_or_name: str, team_id: str | None = None) -> dict[str, Any] | None:
        """Lookup by UUID, name, or type (e.g. 'started')."""
        self._ensure_loaded()
        states = self.list_workflow_states(team_id=team_id)

        # Exact ID match
        for state in states:
            if state.get("id") == id_or_name:
                return state

        # Name match
        for state in states:
            if state.get("name", "").lower() == id_or_name.lower():
                return state

        # Type match (returns first matching)
        state_types = {"backlog", "unstarted", "started", "completed", "canceled"}
        if id_or_name.lower() in state_types:
            for state in states:
                if state.get("type", "").lower() == id_or_name.lower():
                    return state

        return None

    # ======================================================================
    # QUERIES & MUTATIONS: Labels
    # ======================================================================

    def all_labels(self) -> list[dict[str, Any]]:
        """All issue labels (static + created)."""
        self._ensure_loaded()
        return self.labels + self._created_labels

    def list_labels(
        self,
        team_id: str | None = None,
        query: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List issue labels. Returns paginated result."""
        results = self.all_labels()
        if team_id:
            results = [l for l in results if l.get("teamId") == team_id]
        if query:
            kw = query.lower()
            results = [l for l in results if kw in l.get("name", "").lower()]
        return _paginate(results, limit=limit, cursor=cursor)

    def get_label(self, id_or_name: str, team_id: str | None = None) -> dict[str, Any] | None:
        """Lookup by UUID or name."""
        labels = self.all_labels()
        if team_id:
            labels = [l for l in labels if l.get("teamId") == team_id]
        for label in labels:
            if label.get("id") == id_or_name:
                return label
            if label.get("name", "").lower() == id_or_name.lower():
                return label
        return None

    def create_issue_label(
        self,
        *,
        name: str,
        color: str | None = None,
        description: str | None = None,
        team_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a new issue label in-memory."""
        label: dict[str, Any] = {
            "id": _new_id(),
            "name": name,
            "color": color or "#95a2b3",
            "description": description,
        }
        if team_id:
            label["teamId"] = team_id
        self._created_labels.append(label)
        return label

    # ======================================================================
    # QUERIES & MUTATIONS: Projects
    # ======================================================================

    def all_projects(self) -> list[dict[str, Any]]:
        """All projects (static + created), with updates applied."""
        self._ensure_loaded()
        combined = self.projects + self._created_projects
        return [self._apply_updates(p) for p in combined]

    def list_projects(
        self,
        state: str | None = None,
        team_id: str | None = None,
        query: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List projects. Returns paginated result."""
        results = self.all_projects()
        if state:
            results = [p for p in results if p.get("state") == state]
        if team_id:
            results = [
                p for p in results if any(t.get("id") == team_id for t in (p.get("teams") or {}).get("nodes", []))
            ]
        if query:
            kw = query.lower()
            results = [p for p in results if kw in p.get("name", "").lower()]
        return _paginate(results, limit=limit, cursor=cursor)

    def get_project(self, id_or_name: str) -> dict[str, Any] | None:
        """Lookup by UUID or name."""
        for project in self.all_projects():
            if project.get("id") == id_or_name:
                return project
            if project.get("name", "").lower() == id_or_name.lower():
                return project
            if project.get("slugId") == id_or_name:
                return project
        return None

    def create_project(self, *, name: str, team_ids: list[str], **fields: Any) -> dict[str, Any]:
        """Create a project in-memory."""
        now = _now_iso()
        teams_list = [
            {
                "id": tid,
                "name": (self.get_team(tid) or {}).get("name", ""),
                "key": (self.get_team(tid) or {}).get("key", ""),
            }
            for tid in team_ids
        ]
        project: dict[str, Any] = {
            "id": _new_id(),
            "name": name,
            "description": fields.get("description"),
            "slugId": name.lower().replace(" ", "-"),
            "icon": fields.get("icon"),
            "color": fields.get("color"),
            "state": "planned",
            "progress": 0,
            "health": "onTrack",
            "priority": fields.get("priority", 0),
            "url": f"https://linear.app/project/{name.lower().replace(' ', '-')}",
            "startDate": fields.get("startDate"),
            "targetDate": fields.get("targetDate"),
            "lead": self._resolve_user_obj(fields["lead"]) if fields.get("lead") else None,
            "teams": {"nodes": teams_list},
            "members": {"nodes": []},
            "createdAt": now,
            "updatedAt": now,
        }
        self._created_projects.append(project)
        return project

    def update_project(self, project_id: str, **fields: Any) -> dict[str, Any] | None:
        """Update project fields."""
        project = self.get_project(project_id)
        if not project:
            return None
        real_id = project["id"]
        updates = dict(self._updated.get(real_id, {}))
        updates["updatedAt"] = _now_iso()
        for key, value in fields.items():
            if value is not None:
                updates[key] = value
        self._updated[real_id] = updates
        return self.get_project(project_id)

    # ======================================================================
    # QUERIES: Cycles
    # ======================================================================

    def list_cycles(
        self,
        team_id: str,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List cycles for a team. Returns paginated result."""
        self._ensure_loaded()
        results = [c for c in self.cycles if c.get("teamId") == team_id]
        results.sort(key=lambda c: c.get("startsAt", ""), reverse=True)
        return _paginate(results, limit=limit, cursor=cursor)

    # ======================================================================
    # QUERIES & MUTATIONS: Documents
    # ======================================================================

    def all_documents(self) -> list[dict[str, Any]]:
        """All documents (static + created), with updates applied."""
        self._ensure_loaded()
        combined = self.documents + self._created_documents
        return [self._apply_updates(d) for d in combined]

    def list_documents(
        self,
        project_id: str | None = None,
        team_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List documents. Returns paginated result."""
        results = self.all_documents()
        if project_id:
            results = [d for d in results if (d.get("project") or {}).get("id") == project_id]
        if team_id:
            results = [d for d in results if (d.get("team") or {}).get("id") == team_id]
        return _paginate(results, limit=limit, cursor=cursor)

    def get_document(self, id_or_slug: str) -> dict[str, Any] | None:
        """Lookup by UUID, slugId, or title (case-insensitive)."""
        for doc in self.all_documents():
            if doc.get("id") == id_or_slug or doc.get("slugId") == id_or_slug:
                return doc
            if doc.get("title", "").lower() == id_or_slug.lower():
                return doc
        return None

    def create_document(self, *, title: str, **fields: Any) -> dict[str, Any]:
        """Create a document in-memory."""
        now = _now_iso()
        doc: dict[str, Any] = {
            "id": _new_id(),
            "title": title,
            "icon": fields.get("icon"),
            "color": fields.get("color"),
            "content": fields.get("content"),
            "slugId": title.lower().replace(" ", "-"),
            "url": f"https://linear.app/document/{title.lower().replace(' ', '-')}",
            "createdAt": now,
            "updatedAt": now,
            "creator": dict(self.viewer),
            "project": self._resolve_project_obj(fields["project"]) if fields.get("project") else None,
            "team": self._build_team_obj(fields["team"]) if fields.get("team") else None,
        }
        self._created_documents.append(doc)
        return doc

    def update_document(self, doc_id: str, **fields: Any) -> dict[str, Any] | None:
        """Update document fields."""
        doc = self.get_document(doc_id)
        if not doc:
            return None
        real_id = doc["id"]
        updates = dict(self._updated.get(real_id, {}))
        updates["updatedAt"] = _now_iso()
        for key, value in fields.items():
            if value is not None:
                updates[key] = value
        self._updated[real_id] = updates
        return self.get_document(doc_id)

    # ======================================================================
    # QUERIES: Project Labels
    # ======================================================================

    def list_project_labels(
        self,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List project labels. Returns paginated result."""
        self._ensure_loaded()
        return _paginate(self.project_labels, limit=limit, cursor=cursor)

    # ======================================================================
    # Resolution helpers (name/key/email → UUID)
    # ======================================================================

    def resolve_team(self, name_or_id: str) -> str:
        """Resolve team name/key/UUID to UUID. Raises if not found."""
        team = self.get_team(name_or_id)
        if team:
            return team["id"]
        raise ValueError(f"Team '{name_or_id}' not found")

    def resolve_user(self, name_or_id: str) -> str:
        """Resolve user name/email/UUID/'me' to UUID."""
        user = self.get_user(name_or_id)
        if user:
            return user["id"]
        raise ValueError(f"User '{name_or_id}' not found")

    def resolve_state(self, name_or_id: str, team_id: str | None = None) -> str:
        """Resolve state name/type/UUID to UUID."""
        state = self.get_workflow_state(name_or_id, team_id=team_id)
        if state:
            return state["id"]
        raise ValueError(f"State '{name_or_id}' not found")

    def resolve_project(self, name_or_id: str) -> str:
        """Resolve project name/UUID to UUID."""
        project = self.get_project(name_or_id)
        if project:
            return project["id"]
        raise ValueError(f"Project '{name_or_id}' not found")

    def resolve_label(self, name_or_id: str, team_id: str | None = None) -> str:
        """Resolve label name/UUID to UUID."""
        label = self.get_label(name_or_id, team_id=team_id)
        if label:
            return label["id"]
        raise ValueError(f"Label '{name_or_id}' not found")

    def resolve_labels(self, names_or_ids: list[str], team_id: str | None = None) -> list[str]:
        """Resolve list of label names/UUIDs."""
        return [self.resolve_label(n, team_id=team_id) for n in names_or_ids]

    # -- internal object builders -------------------------------------------

    def _resolve_user_obj(self, user_id: str) -> dict[str, Any] | None:
        """Return a user summary dict for embedding in issue/project."""
        user = self.get_user(user_id)
        if not user:
            return None
        return {"id": user["id"], "name": user.get("name", ""), "email": user.get("email")}

    def _resolve_state_obj(self, state_id: str, team_id: str | None = None) -> dict[str, Any] | None:
        """Return a state summary dict."""
        state = self.get_workflow_state(state_id, team_id=team_id)
        if not state:
            return None
        return {
            "id": state["id"],
            "name": state.get("name", ""),
            "type": state.get("type", ""),
            "color": state.get("color"),
        }

    def _default_state(self, team_id: str | None = None) -> dict[str, Any]:
        """Return the default (first backlog) state for a team."""
        states = self.list_workflow_states(team_id=team_id)
        for s in states:
            if s.get("type") == "backlog":
                return {"id": s["id"], "name": s["name"], "type": "backlog", "color": s.get("color")}
        if states:
            s = states[0]
            return {"id": s["id"], "name": s["name"], "type": s.get("type", ""), "color": s.get("color")}
        return {"id": _new_id(), "name": "Backlog", "type": "backlog", "color": "#bec2c8"}

    def _resolve_project_obj(self, project_id: str) -> dict[str, Any] | None:
        """Return a project summary dict."""
        project = self.get_project(project_id)
        if not project:
            return None
        return {"id": project["id"], "name": project.get("name", ""), "state": project.get("state")}

    def _resolve_label_objs(self, label_ids: list[str]) -> list[dict[str, Any]]:
        """Return label summary dicts."""
        result = []
        for lid in label_ids:
            label = self.get_label(lid)
            if label:
                result.append({"id": label["id"], "name": label["name"], "color": label.get("color")})
        return result

    def _build_team_obj(self, team_id: str) -> dict[str, Any] | None:
        """Return a team summary dict."""
        team = self.get_team(team_id)
        if not team:
            return None
        return {"id": team["id"], "name": team.get("name", ""), "key": team.get("key", "")}
