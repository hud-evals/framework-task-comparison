"""GitHub MCP service wrapper backed by local bare repos and JSON fixtures."""

from __future__ import annotations

import logging
import os
from servers.mcp.base import LocalService

from .frontend import GitHubFrontend
from .mock_client import MockGitHubClient
from .server import create_github_server

logger = logging.getLogger(__name__)


class MockGitHubService(LocalService):
    """Mock GitHub MCP service — JSON + bare repo + in-memory.

    Supports multiple repos via :meth:`add_repo`.  A single
    ``create_github_server`` instance routes each tool call to the
    correct ``MockGitHubClient`` based on ``owner/repo``.
    """

    def __init__(
        self,
        *,
        read_only: bool = False,
        frontend_port: int | None = None,
        bare_repo_path: str | None = None,
        data_dir: str | None = None,
        repo_owner: str | None = None,
        repo_name: str | None = None,
        default_branch: str | None = None,
        worktree_path: str | None = None,
    ) -> None:
        super().__init__()
        self.client = MockGitHubClient()
        self._clients: dict[str, MockGitHubClient] = {}
        self._read_only = read_only
        self._frontend: GitHubFrontend | None = None
        self._worktree_path = worktree_path

        if bare_repo_path is not None:
            self.client.bare_repo_path = bare_repo_path
        if repo_owner is not None:
            self.client.repo_owner = repo_owner
        if repo_name is not None:
            self.client.repo_name = repo_name
        if default_branch is not None:
            self.client.default_branch = default_branch
        if repo_owner is not None or repo_name is not None:
            full_name = f"{self.client.repo_owner}/{self.client.repo_name}"
            self.client.allowed_repos = [full_name.lower()]
        if data_dir is not None:
            self.client.load(data_dir)

        self._register_client(self.client)

        self._frontend_detach = False
        if frontend_port is None:
            raw = os.environ.get("GITHUB_FRONTEND_PORT")
            if raw is not None:
                try:
                    frontend_port = int(raw)
                except ValueError:
                    raise ValueError(f"GITHUB_FRONTEND_PORT must be an integer, got: {raw!r}") from None
                self._frontend_detach = True
        if frontend_port is not None and not (1 <= frontend_port <= 65535):
            raise ValueError(f"GITHUB_FRONTEND_PORT must be 1-65535, got: {frontend_port}")
        self._frontend_port = frontend_port

    # ── Client registry ───────────────────────────────────────────────

    def _register_client(self, c: MockGitHubClient) -> None:
        key = f"{c.repo_owner}/{c.repo_name}".lower()
        self._clients[key] = c

    def _resolve_client(self, owner: str, repo: str) -> MockGitHubClient:
        """Route an owner/repo pair to the right ``MockGitHubClient``.

        Falls back to the primary client for tools that don't carry
        owner/repo (search_repositories, search_users, etc.).
        """
        if owner and repo:
            key = f"{owner}/{repo}".lower()
            if key in self._clients:
                return self._clients[key]
        return self.client

    @property
    def all_clients(self) -> list[MockGitHubClient]:
        seen: dict[int, MockGitHubClient] = {}
        for c in self._clients.values():
            if id(c) not in seen:
                seen[id(c)] = c
        return list(seen.values())

    # ── Configuration ─────────────────────────────────────────────────

    def add_repo(
        self,
        *,
        bare_repo_path: str | None = None,
        data_dir: str | None = None,
        repo_owner: str,
        repo_name: str,
        default_branch: str = "main",
        hidden_branches: list[str] | None = None,
    ) -> MockGitHubClient:
        """Register an additional repository.

        Returns the new ``MockGitHubClient`` so callers can reference it
        directly for grading etc.
        """
        c = MockGitHubClient(
            bare_repo_path=bare_repo_path,
            repo_owner=repo_owner,
            repo_name=repo_name,
            default_branch=default_branch,
            hidden_branches=hidden_branches,
        )
        full_name = f"{repo_owner}/{repo_name}".lower()
        c.allowed_repos = [full_name]
        if data_dir is not None:
            c.load(data_dir)
        self._register_client(c)
        logger.info("Registered additional repo: %s", full_name)
        return c

    def configure(
        self,
        *,
        bare_repo_path: str | None = None,
        data_dir: str | None = None,
        repo_owner: str | None = None,
        repo_name: str | None = None,
        default_branch: str | None = None,
        worktree_path: str | None = None,
    ) -> None:
        """Reconfigure the primary repo for a new task and reload data.

        Updates the underlying ``MockGitHubClient`` properties in place
        (so already-registered MCP tools see the new state), then calls
        ``client.reload()`` to clear mutable state and load fresh JSON
        data from *data_dir*.
        """
        if bare_repo_path is not None:
            self.client.bare_repo_path = bare_repo_path
        if repo_owner is not None:
            self.client.repo_owner = repo_owner
        if repo_name is not None:
            self.client.repo_name = repo_name
        if default_branch is not None:
            self.client.default_branch = default_branch
        if repo_owner is not None or repo_name is not None:
            full_name = f"{self.client.repo_owner}/{self.client.repo_name}"
            self.client.allowed_repos = [full_name.lower()]
        if worktree_path is not None:
            self._worktree_path = worktree_path
        self.client.reload(data_dir=data_dir)
        self._register_client(self.client)
        self.start_frontend()

    # ── Frontend ──────────────────────────────────────────────────────

    def start_frontend(self) -> None:
        """Start the frontend if a port is configured (idempotent)."""
        if self._frontend_port is None or self._frontend is not None:
            return
        clients = self.all_clients
        self._frontend = GitHubFrontend(
            clients if len(clients) > 1 else clients[0],
            port=self._frontend_port,
            worktree_path=self._worktree_path,
            detach=self._frontend_detach,
        )
        self._frontend.start()

    def stop_frontend(self) -> None:
        """Stop the frontend if running."""
        if self._frontend is not None:
            self._frontend.stop()
            self._frontend = None

    # ── Server factory ────────────────────────────────────────────────

    def _create_server(self):
        return create_github_server(
            self._resolve_client,
            read_only=self._read_only,
            all_clients=lambda: self.all_clients,
        )
