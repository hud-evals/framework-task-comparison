"""Mock GitHub MCP service.

Usage::

    from servers.mcp.github import MockGitHubService

    github = MockGitHubService()
    env.connect_server(github.server)

    github.configure(
        bare_repo_path="/srv/git/project.git",
        data_dir="/mcp_server/github_data",
    )
"""

from .frontend import GitHubFrontend
from .mock_client import GitHubAPIError, MockGitHubClient, RepoAccessDenied
from .server import create_github_server
from .services import MockGitHubService

__all__ = [
    "GitHubAPIError",
    "GitHubFrontend",
    "MockGitHubClient",
    "MockGitHubService",
    "RepoAccessDenied",
    "create_github_server",
]
