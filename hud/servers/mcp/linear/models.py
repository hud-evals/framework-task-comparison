"""Pydantic models for Linear API request/response data."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# ============ Response Models ============


class User(BaseModel):
    """Linear user representation."""

    id: str
    name: str
    email: str | None = None
    display_name: str | None = Field(None, alias="displayName")

    model_config = {"populate_by_name": True}


class WorkflowState(BaseModel):
    """Issue workflow state."""

    id: str
    name: str
    type: str  # "backlog", "unstarted", "started", "completed", "canceled"
    color: str | None = None


class Team(BaseModel):
    """Linear team representation."""

    id: str
    name: str
    key: str  # Team prefix like "ENG"


class Project(BaseModel):
    """Linear project representation."""

    id: str
    name: str
    state: str | None = None


class Label(BaseModel):
    """Issue label."""

    id: str
    name: str
    color: str | None = None


class Issue(BaseModel):
    """Full issue representation."""

    id: str
    identifier: str  # Human-readable ID like "ENG-123"
    title: str
    description: str | None = None
    priority: int | None = None  # 0=none, 1=urgent, 2=high, 3=medium, 4=low
    state: WorkflowState | None = None
    assignee: User | None = None
    team: Team | None = None
    project: Project | None = None
    labels: list[Label] = Field(default_factory=list)
    created_at: datetime | None = Field(None, alias="createdAt")
    updated_at: datetime | None = Field(None, alias="updatedAt")
    completed_at: datetime | None = Field(None, alias="completedAt")
    url: str | None = None

    model_config = {"populate_by_name": True}


class Comment(BaseModel):
    """Issue comment representation."""

    id: str
    body: str
    user: User | None = None
    created_at: datetime | None = Field(None, alias="createdAt")

    model_config = {"populate_by_name": True}


# ============ Input Models ============


class IssueCreateInput(BaseModel):
    """Input for creating a new issue."""

    title: str
    team_id: str = Field(alias="teamId")
    description: str | None = None
    assignee_id: str | None = Field(None, alias="assigneeId")
    state_id: str | None = Field(None, alias="stateId")
    project_id: str | None = Field(None, alias="projectId")
    priority: int | None = None
    label_ids: list[str] | None = Field(None, alias="labelIds")

    model_config = {"populate_by_name": True}


class IssueUpdateInput(BaseModel):
    """Input for updating an issue."""

    title: str | None = None
    description: str | None = None
    assignee_id: str | None = Field(None, alias="assigneeId")
    state_id: str | None = Field(None, alias="stateId")
    project_id: str | None = Field(None, alias="projectId")
    priority: int | None = None
    label_ids: list[str] | None = Field(None, alias="labelIds")

    model_config = {"populate_by_name": True}


class IssueSearchFilter(BaseModel):
    """Filter parameters for searching issues."""

    query: str | None = None  # Text search
    team_id: str | None = None
    assignee_id: str | None = None
    state_id: str | None = None
    state_type: str | None = None  # "backlog", "unstarted", "started", "completed", "canceled"
    project_id: str | None = None
    label_ids: list[str] | None = None
    priority: int | None = None

    def to_graphql_filter(self) -> dict[str, Any]:
        """Convert to Linear GraphQL IssueFilter format."""
        filter_dict: dict[str, Any] = {}

        if self.query:
            # Linear uses 'searchableContent' for text search
            filter_dict["searchableContent"] = {"contains": self.query}

        if self.team_id:
            filter_dict["team"] = {"id": {"eq": self.team_id}}

        if self.assignee_id:
            filter_dict["assignee"] = {"id": {"eq": self.assignee_id}}

        if self.state_id:
            filter_dict["state"] = {"id": {"eq": self.state_id}}
        elif self.state_type:
            filter_dict["state"] = {"type": {"eq": self.state_type}}

        if self.project_id:
            filter_dict["project"] = {"id": {"eq": self.project_id}}

        if self.label_ids:
            filter_dict["labels"] = {"id": {"in": self.label_ids}}

        if self.priority is not None:
            filter_dict["priority"] = {"eq": self.priority}

        return filter_dict


class CommentCreateInput(BaseModel):
    """Input for creating a comment."""

    issue_id: str = Field(alias="issueId")
    body: str

    model_config = {"populate_by_name": True}
