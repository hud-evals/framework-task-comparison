"""Minimal in-memory Linear data layer for the 3-tool mock server."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class MockLinearData:
    """Loads issues and workflow states from JSON files.

    Mutations (comments, state changes) are kept in memory for the
    duration of a session.
    """

    def __init__(self, data_dir: str | None = None) -> None:
        self.data_dir = data_dir or ""
        self._loaded = False
        self.issues: list[dict[str, Any]] = []
        self.workflow_states: list[dict[str, Any]] = []
        self._created_comments: dict[str, list[dict[str, Any]]] = {}

    def _load_json_list(self, filename: str) -> list[dict[str, Any]]:
        path = os.path.join(self.data_dir, filename)
        if os.path.exists(path):
            with open(path) as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        return []

    def load(self) -> None:
        self.issues = self._load_json_list("issues.json")
        self.workflow_states = self._load_json_list("workflow_states.json")
        self._loaded = True

    def reload(self, data_dir: str | None = None) -> None:
        if data_dir:
            self.data_dir = data_dir
        self._created_comments.clear()
        self._loaded = False
        self.load()

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def get_issue(self, id_or_identifier: str) -> dict[str, Any] | None:
        self._ensure_loaded()
        for issue in self.issues:
            if issue.get("id") == id_or_identifier or issue.get("identifier") == id_or_identifier:
                return issue
        return None

    def done_state(self) -> dict[str, Any]:
        self._ensure_loaded()
        for state in self.workflow_states:
            if state.get("type") == "completed":
                return state
        raise ValueError("No completed workflow state found")
