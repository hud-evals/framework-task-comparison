"""GitHub MCP server for HUD evaluation environments.

Provides ``create_github_server(client)`` which returns a FastMCP
server instance with all GitHub tools registered.  The tool schemas
match the official ``@modelcontextprotocol/server-github`` server
(https://github.com/modelcontextprotocol/servers — 26 tools).

Typical usage::

    from servers.mcp.github import MockGitHubService

    github = MockGitHubService(
        bare_repo_path="/srv/git/project.git",
        data_dir="/mcp_server/github_data",
    )
    env.connect_server(github.server, prefix="github")
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from fastmcp import FastMCP

if TYPE_CHECKING:
    from .mock_client import MockGitHubClient

ClientResolver = Callable[[str, str], "MockGitHubClient"]
MAX_RESPONSE_LEN = 1_000_000

# ---------------------------------------------------------------------------
# Official tool descriptions (from @modelcontextprotocol/server-github)
# ---------------------------------------------------------------------------
_TOOL_DESCRIPTIONS: dict[str, str] = {
    "create_or_update_file": "Create or update a single file in a GitHub repository",
    "search_repositories": "Search for GitHub repositories",
    "create_repository": "Create a new GitHub repository in your account",
    "get_file_contents": "Get the contents of a file or directory from a GitHub repository",
    "push_files": "Push multiple files to a GitHub repository in a single commit",
    "create_issue": "Create a new issue in a GitHub repository",
    "create_pull_request": "Create a new pull request in a GitHub repository",
    "fork_repository": "Fork a GitHub repository to your account or specified organization",
    "create_branch": "Create a new branch in a GitHub repository",
    "list_branches": "List branches in a GitHub repository",
    "list_commits": (
        "Get list of commits of a branch in a GitHub repository. Returns at least 30 results per page by "
        "default, but can return more if specified using the perPage parameter (up to 100)."
    ),
    "get_commit": "Get details for a commit from a GitHub repository",
    "get_repository_tree": "Get the tree structure (files and directories) of a GitHub repository at a specific ref or SHA",
    "list_issues": "List issues in a GitHub repository with filtering options",
    "update_issue": "Update an existing issue in a GitHub repository",
    "add_issue_comment": "Add a comment to an existing issue",
    "search_code": (
        "Fast and precise code search across ALL GitHub repositories using GitHub's native search engine. "
        "Best for finding exact symbols, functions, classes, or specific code patterns."
    ),
    "search_issues": "Search for issues and pull requests across GitHub repositories",
    "search_users": "Search for users on GitHub",
    "get_issue": "Get details of a specific issue in a GitHub repository.",
    "get_pull_request": "Get details of a specific pull request",
    "pull_request_read": "Get information on a specific pull request in GitHub repository.",
    "list_pull_requests": "List and filter repository pull requests",
    "create_pull_request_review": "Create a review on a pull request",
    "merge_pull_request": "Merge a pull request",
    "get_pull_request_files": "Get the list of files changed in a pull request",
    "get_pull_request_status": "Get the combined status of all status checks for a pull request",
    "update_pull_request_branch": "Update a pull request branch with the latest changes from the base branch",
    "get_pull_request_comments": "Get the review comments on a pull request",
    "get_pull_request_reviews": "Get the reviews on a pull request",
}

# ---------------------------------------------------------------------------
# Official parameter descriptions — only for tools/params that have them.
# Tools whose params have NO descriptions in the official schema (create_issue,
# get_issue, list_issues, update_issue, add_issue_comment,
# list_commits, search_code, search_issues, search_users) are absent.
# ---------------------------------------------------------------------------
_PARAM_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "create_or_update_file": {
        "owner": "Repository owner (username or organization)",
        "repo": "Repository name",
        "path": "Path where to create/update the file",
        "content": "Content of the file",
        "message": "Commit message",
        "branch": "Branch to create/update the file in",
        "sha": "SHA of the file being replaced (required when updating existing files)",
    },
    "search_repositories": {
        "query": "Search query (see GitHub search syntax)",
        "page": "Page number for pagination (default: 1)",
        "perPage": "Number of results per page (default: 30, max: 100)",
    },
    "create_repository": {
        "name": "Repository name",
        "description": "Repository description",
        "private": "Whether the repository should be private",
        "autoInit": "Initialize with README.md",
    },
    "push_files": {
        "owner": "Repository owner (username or organization)",
        "repo": "Repository name",
        "branch": "Branch to push to (e.g., 'main' or 'master')",
        "files": "Array of files to push",
        "message": "Commit message",
    },
    "create_pull_request": {
        "owner": "Repository owner (username or organization)",
        "repo": "Repository name",
        "title": "Pull request title",
        "body": "Pull request body/description",
        "head": "The name of the branch where your changes are implemented",
        "base": "The name of the branch you want the changes pulled into",
        "draft": "Whether to create the pull request as a draft",
        "maintainer_can_modify": "Whether maintainers can modify the pull request",
    },
    "fork_repository": {
        "owner": "Repository owner (username or organization)",
        "repo": "Repository name",
        "organization": "Optional: organization to fork to (defaults to your personal account)",
    },
    "create_branch": {
        "owner": "Repository owner (username or organization)",
        "repo": "Repository name",
        "branch": "Name for the new branch",
        "from_branch": "Optional: source branch to create from (defaults to the repository's default branch)",
    },
    "list_branches": {
        "owner": "Repository owner",
        "page": "Page number for pagination (min 1)",
        "perPage": "Results per page for pagination (min 1, max 100)",
        "repo": "Repository name",
    },
    "get_pull_request": {
        "owner": "Repository owner (username or organization)",
        "repo": "Repository name",
        "pull_number": "Pull request number",
    },
    "get_commit": {
        "include_diff": "Whether to include file diffs and stats in the response. Default is true.",
        "owner": "Repository owner",
        "page": "Page number for pagination (min 1)",
        "perPage": "Results per page for pagination (min 1, max 100)",
        "repo": "Repository name",
        "sha": "Commit SHA, branch name, or tag name",
    },
    "get_repository_tree": {
        "owner": "Repository owner (username or organization)",
        "path_filter": "Optional path prefix to filter the tree results (e.g., 'src/' to only show files in the src directory)",
        "recursive": "Setting this parameter to true returns the objects or subtrees referenced by the tree. Default is false",
        "repo": "Repository name",
        "tree_sha": "The SHA1 value or ref (branch or tag) name of the tree. Defaults to the repository's default branch",
    },
    "list_pull_requests": {
        "owner": "Repository owner (username or organization)",
        "repo": "Repository name",
        "state": "State of the pull requests to return",
        "head": "Filter by head user or head organization and branch name",
        "base": "Filter by base branch name",
        "sort": "What to sort results by",
        "direction": "The direction of the sort",
        "per_page": "Results per page (max 100)",
        "page": "Page number of the results",
    },
    "create_pull_request_review": {
        "owner": "Repository owner (username or organization)",
        "repo": "Repository name",
        "pull_number": "Pull request number",
        "commit_id": "The SHA of the commit that needs a review",
        "body": "The body text of the review",
        "event": "The review action to perform",
        "comments": "Comments to post as part of the review (specify either position or line, not both)",
    },
    "merge_pull_request": {
        "owner": "Repository owner (username or organization)",
        "repo": "Repository name",
        "pull_number": "Pull request number",
        "commit_title": "Title for the automatic commit message",
        "commit_message": "Extra detail to append to automatic commit message",
        "merge_method": "Merge method to use",
    },
    "get_pull_request_files": {
        "owner": "Repository owner (username or organization)",
        "repo": "Repository name",
        "pull_number": "Pull request number",
    },
    "get_pull_request_status": {
        "owner": "Repository owner (username or organization)",
        "repo": "Repository name",
        "pull_number": "Pull request number",
    },
    "update_pull_request_branch": {
        "owner": "Repository owner (username or organization)",
        "repo": "Repository name",
        "pull_number": "Pull request number",
        "expected_head_sha": "The expected SHA of the pull request's HEAD ref",
    },
    "get_pull_request_comments": {
        "owner": "Repository owner (username or organization)",
        "repo": "Repository name",
        "pull_number": "Pull request number",
    },
    "get_pull_request_reviews": {
        "owner": "Repository owner (username or organization)",
        "repo": "Repository name",
        "pull_number": "Pull request number",
    },
    "pull_request_read": {
        "method": (
            "Action to specify what pull request data needs to be retrieved from GitHub. \n"
            "Possible options: \n"
            " 1. get - Get details of a specific pull request.\n"
            " 2. get_diff - Get the diff of a pull request.\n"
            " 3. get_status - Get combined commit status of a head commit in a pull request.\n"
            " 4. get_files - Get the list of files changed in a pull request. Use with pagination parameters to control the number of results returned.\n"
            " 5. get_review_comments - Get review threads on a pull request. Each thread contains logically grouped review comments made on the same code location during pull request reviews. Returns threads with metadata (isResolved, isOutdated, isCollapsed) and their associated comments. Use cursor-based pagination (perPage, after) to control results.\n"
            " 6. get_reviews - Get the reviews on a pull request. When asked for review comments, use get_review_comments method.\n"
            " 7. get_comments - Get comments on a pull request. Use this if user doesn't specifically want review comments. Use with pagination parameters to control the number of results returned.\n"
            " 8. get_check_runs - Get check runs for the head commit of a pull request. Check runs are the individual CI/CD jobs and checks that run on the PR.\n"
        ),
        "owner": "Repository owner",
        "page": "Page number for pagination (min 1)",
        "perPage": "Results per page for pagination (min 1, max 100)",
        "pullNumber": "Pull request number",
        "repo": "Repository name",
    },
    "get_file_contents": {
        "owner": "Repository owner (username or organization)",
        "path": "Path to file/directory",
        "ref": "Accepts optional git refs such as `refs/tags/{tag}`, `refs/heads/{branch}` or `refs/pull/{pr_number}/head`",
        "repo": "Repository name",
        "sha": "Accepts optional commit SHA. If specified, it will be used instead of ref",
    },
    "search_code": {
        "order": "Sort order for results",
        "page": "Page number for pagination (min 1)",
        "perPage": "Results per page for pagination (min 1, max 100)",
        "query": (
            "Search query using GitHub's powerful code search syntax. Examples: "
            "'content:Skill language:Java org:github', "
            "'NOT is:archived language:Python OR language:go', "
            "'repo:github/github-mcp-server'. Supports exact matching, language filters, path filters, and more."
        ),
        "sort": "Sort field ('indexed' only)",
    },
    "list_commits": {
        "author": "Author username or email address to filter commits by",
        "owner": "Repository owner",
        "page": "Page number for pagination (min 1)",
        "perPage": "Results per page for pagination (min 1, max 100)",
        "repo": "Repository name",
        "sha": (
            "Commit SHA, branch or tag name to list commits of. If not provided, uses the default branch of the "
            "repository. If a commit SHA is provided, will list commits up to that SHA."
        ),
    },
}

_OFFICIAL_TOOLS: set[str] = set(_TOOL_DESCRIPTIONS)

_NUMBER_PARAMS: set[str] = {
    "issue_number",
    "pull_number",
    "pullNumber",
    "milestone",
    "page",
    "perPage",
    "per_page",
}

_PARAM_CONSTRAINTS: dict[tuple[str, str], dict[str, Any]] = {
    ("search_code", "page"): {"minimum": 1},
    ("search_code", "perPage"): {"minimum": 1, "maximum": 100},
    ("search_issues", "page"): {"minimum": 1},
    ("search_issues", "per_page"): {"minimum": 1, "maximum": 100},
    ("search_users", "page"): {"minimum": 1},
    ("search_users", "per_page"): {"minimum": 1, "maximum": 100},
    ("list_branches", "page"): {"minimum": 1},
    ("list_branches", "perPage"): {"minimum": 1, "maximum": 100},
    ("list_commits", "page"): {"minimum": 1},
    ("list_commits", "perPage"): {"minimum": 1, "maximum": 100},
    ("get_commit", "page"): {"minimum": 1},
    ("get_commit", "perPage"): {"minimum": 1, "maximum": 100},
    ("pull_request_read", "page"): {"minimum": 1},
    ("pull_request_read", "perPage"): {"minimum": 1, "maximum": 100},
}

_PROPERTY_OVERRIDES: dict[str, dict[str, Any]] = {
    "push_files": {
        "files": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            "description": "Array of files to push",
        },
    },
    "create_pull_request_review": {
        "comments": {
            "type": "array",
            "items": {
                "anyOf": [
                    {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "The relative path to the file being commented on",
                            },
                            "position": {
                                "type": "number",
                                "description": "The position in the diff where you want to add a review comment",
                            },
                            "body": {
                                "type": "string",
                                "description": "Text of the review comment",
                            },
                        },
                        "required": ["path", "position", "body"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "The relative path to the file being commented on",
                            },
                            "line": {
                                "type": "number",
                                "description": "The line number in the file where you want to add a review comment",
                            },
                            "body": {
                                "type": "string",
                                "description": "Text of the review comment",
                            },
                        },
                        "required": ["path", "line", "body"],
                        "additionalProperties": False,
                    },
                ],
            },
            "description": "Comments to post as part of the review (specify either position or line, not both)",
        },
    },
}


_REPO_QUALIFIER_RE = re.compile(r"\brepo:([^\s]+)")


def _extract_repo_qualifier(q: str) -> tuple[str, str] | None:
    """Return ``(owner, repo)`` if the query contains a ``repo:owner/name`` qualifier."""
    m = _REPO_QUALIFIER_RE.search(q)
    if m:
        parts = m.group(1).split("/", 1)
        if len(parts) == 2:
            return parts[0], parts[1]
    return None


def _truncate(text: str, max_len: int = MAX_RESPONSE_LEN) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"\n\n... (response truncated, {len(text) - max_len} chars omitted)"


# ---------------------------------------------------------------------------
# Schema fixup helpers (applied after all tools are registered)
# ---------------------------------------------------------------------------


async def _ensure_tool_descriptions(server: FastMCP) -> None:
    """Set tool descriptions to match the official GitHub MCP API."""
    for tool in await server.list_tools():
        if tool.name in _TOOL_DESCRIPTIONS:
            tool.description = _TOOL_DESCRIPTIONS[tool.name]


async def _fix_schemas(server: FastMCP) -> None:
    """Post-process tool schemas to match the official GitHub MCP API.

    Handles: additionalProperties, nullable stripping, integer->number,
    param descriptions, enum/min/max constraints, and complex items schemas.
    """
    for tool in await server.list_tools():
        tool_name = tool.name
        if tool_name not in _OFFICIAL_TOOLS:
            continue

        params = tool.parameters
        if not params or not isinstance(params, dict):
            continue

        params["additionalProperties"] = False

        props = params.get("properties", {})
        param_descs = _PARAM_DESCRIPTIONS.get(tool_name, {})

        if tool_name in _PROPERTY_OVERRIDES:
            for prop_name, override in _PROPERTY_OVERRIDES[tool_name].items():
                props[prop_name] = override

        for prop_name, prop_schema in list(props.items()):
            if tool_name in _PROPERTY_OVERRIDES and prop_name in _PROPERTY_OVERRIDES[tool_name]:
                continue

            # Strip anyOf nullable wrapping
            if "anyOf" in prop_schema:
                non_null = [t for t in prop_schema["anyOf"] if t.get("type") != "null"]
                if len(non_null) == 1:
                    prop_schema.clear()
                    prop_schema.update(non_null[0])

            if prop_schema.get("default") is None and "default" in prop_schema:
                del prop_schema["default"]

            if prop_name in _NUMBER_PARAMS and prop_schema.get("type") == "integer":
                prop_schema["type"] = "number"

            constraints = _PARAM_CONSTRAINTS.get((tool_name, prop_name))
            if constraints:
                prop_schema.update(constraints)

            if prop_name in param_descs:
                prop_schema["description"] = param_descs[prop_name]
            elif "description" in prop_schema:
                del prop_schema["description"]


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


async def create_github_server(
    client: MockGitHubClient | ClientResolver,
    *,
    read_only: bool = True,
    all_clients: Callable[[], list[MockGitHubClient]] | None = None,
) -> FastMCP:
    """Create a FastMCP server with GitHub tools.

    *client* can be a single ``MockGitHubClient`` (for backward
    compatibility) or a callable ``(owner, repo) -> MockGitHubClient``
    that resolves the right client for multi-repo setups.

    *all_clients*, when provided, is called by global search tools
    (``search_repositories``, ``search_code``, etc.) to aggregate
    results across every registered repo.
    """
    if callable(client):
        _resolve: ClientResolver = client
    else:
        _c = client
        _resolve = lambda _o, _r: _c  # noqa: E731

    _all: Callable[[], list[MockGitHubClient]]
    if all_clients is not None:
        _all = all_clients
    else:

        def _all() -> list[MockGitHubClient]:
            return [_resolve("", "")]

    server = FastMCP("GitHub")

    async def _build_pull_request_files(owner: str, repo: str, pull_number: int) -> list[dict[str, Any]]:
        c = _resolve(owner, repo)
        pr = await c.get_pull_request(owner, repo, pull_number)
        base_ref = pr["base"]["ref"]
        head_ref = pr["head"]["ref"]
        comparison = await c.compare_commits(owner, repo, base_ref, head_ref)
        raw_files = comparison.get("files", [])
        patches = comparison.get("patches", {})
        api_base = f"https://api.github.com/repos/{owner}/{repo}"
        html_base = f"https://github.com/{owner}/{repo}"
        files = []
        for f in raw_files:
            blob_sha = f.get("sha", "")
            fname = f.get("filename", "")
            entry: dict[str, Any] = {
                "sha": blob_sha,
                "filename": fname,
                "status": f.get("status", "modified"),
                "additions": f.get("additions", 0),
                "deletions": f.get("deletions", 0),
                "changes": f.get("changes", 0),
                "blob_url": f"{html_base}/blob/{blob_sha}/{fname}",
                "raw_url": f"{html_base}/raw/{blob_sha}/{fname}",
                "contents_url": f"{api_base}/contents/{fname}?ref={blob_sha}",
            }
            patch = patches.get(fname)
            if patch is not None:
                entry["patch"] = patch
            files.append(entry)
        return files

    async def _get_pull_request_diff(owner: str, repo: str, pull_number: int) -> str:
        c = _resolve(owner, repo)
        pr = await c.get_pull_request(owner, repo, pull_number)
        diff_result = c._git("diff", f"{pr['base']['ref']}..{pr['head']['ref']}", check=False)
        if diff_result.returncode != 0:
            raise ValueError(f"Unable to compute diff for PR #{pull_number}")
        return diff_result.stdout

    # ================================================================
    # Read tools (always registered)
    # ================================================================

    @server.tool
    async def get_file_contents(
        owner: str,
        repo: str,
        path: str = "/",
        ref: str | None = None,
        sha: str | None = None,
    ) -> str:
        """Get the contents of a file or directory from a GitHub repository."""
        data = await _resolve(owner, repo).get_file_contents(owner, repo, path, sha or ref)
        if not isinstance(data, list) and data.get("content"):
            try:
                data["content"] = base64.b64decode(data["content"]).decode("utf-8")
            except Exception:
                pass
        return _truncate(json.dumps(data, indent=2))

    @server.tool
    async def list_branches(
        owner: str,
        repo: str,
        page: int | None = None,
        perPage: int | None = None,
    ) -> str:
        """List branches in a GitHub repository."""
        data = await _resolve(owner, repo).list_branches(
            owner,
            repo,
            page=page or 1,
            per_page=min(perPage or 30, 100),
        )
        return _truncate(json.dumps(data, indent=2))

    @server.tool
    async def list_commits(
        owner: str,
        repo: str,
        sha: str | None = None,
        author: str | None = None,
        page: int | None = None,
        perPage: int | None = None,
    ) -> str:
        """Get list of commits of a branch in a GitHub repository."""
        per_page = perPage or 20
        commits = await _resolve(owner, repo).list_commits(
            owner,
            repo,
            sha=sha,
            author=author,
            per_page=min(per_page, 100),
            page=page or 1,
        )
        return _truncate(json.dumps(commits, indent=2))

    @server.tool
    async def get_commit(
        owner: str,
        repo: str,
        sha: str,
        include_diff: bool = True,
        page: int | None = None,
        perPage: int | None = None,
    ) -> str:
        """Get details for a commit from a GitHub repository."""
        data = await _resolve(owner, repo).get_commit(
            owner,
            repo,
            sha,
            include_diff=include_diff,
            per_page=min(perPage or 30, 100),
            page=page or 1,
        )
        return _truncate(json.dumps(data, indent=2))

    @server.tool
    async def get_repository_tree(
        owner: str,
        repo: str,
        tree_sha: str | None = None,
        recursive: bool = False,
        path_filter: str | None = None,
    ) -> str:
        """Get the tree structure (files and directories) of a GitHub repository at a specific ref or SHA."""
        data = await _resolve(owner, repo).get_repository_tree(
            owner,
            repo,
            tree_sha=tree_sha,
            recursive=recursive,
            path_filter=path_filter,
        )
        return _truncate(json.dumps(data, indent=2))

    @server.tool
    async def list_issues(
        owner: str,
        repo: str,
        direction: Literal["asc", "desc"] | None = None,
        labels: list[str] | None = None,
        page: int | None = None,
        per_page: int | None = None,
        since: str | None = None,
        sort: Literal["created", "updated", "comments"] | None = None,
        state: Literal["open", "closed", "all"] | None = None,
    ) -> str:
        """List issues in a GitHub repository with filtering options."""
        label_str = ",".join(labels) if labels else None
        issues = await _resolve(owner, repo).list_issues(
            owner,
            repo,
            state=state or "open",
            labels=label_str,
            sort=sort or "created",
            direction=direction or "desc",
            per_page=min(per_page or 20, 100),
            page=page or 1,
            since=since,
        )
        return _truncate(json.dumps(issues, indent=2))

    @server.tool
    async def get_issue(owner: str, repo: str, issue_number: int) -> str:
        """Get details of a specific issue in a GitHub repository."""
        data = await _resolve(owner, repo).get_issue(owner, repo, issue_number)
        return _truncate(json.dumps(data, indent=2))

    @server.tool
    async def search_code(
        query: str,
        order: Literal["asc", "desc"] | None = None,
        page: int | None = None,
        perPage: int | None = None,
        sort: str | None = None,
    ) -> str:
        """Search for code across GitHub repositories."""
        del order, sort
        rq = _extract_repo_qualifier(query)
        limit = perPage or 20
        page_num = page or 1
        if rq:
            data = await _resolve(*rq).search_code(query, per_page=limit, page=page_num)
        else:
            all_items: list[dict[str, Any]] = []
            for c in _all():
                d = await c.search_code(query, per_page=10_000, page=1)
                all_items.extend(d.get("items", []))
            start = max(page_num - 1, 0) * limit
            data = {
                "total_count": len(all_items),
                "incomplete_results": False,
                "items": all_items[start : start + limit],
            }
        return _truncate(json.dumps(data, indent=2))

    @server.tool
    async def search_issues(
        q: str,
        order: Literal["asc", "desc"] | None = None,
        page: int | None = None,
        per_page: int | None = None,
        sort: Literal[
            "comments",
            "reactions",
            "reactions-+1",
            "reactions--1",
            "reactions-smile",
            "reactions-thinking_face",
            "reactions-heart",
            "reactions-tada",
            "interactions",
            "created",
            "updated",
        ]
        | None = None,
    ) -> str:
        """Search for issues and pull requests across GitHub repositories."""
        rq = _extract_repo_qualifier(q)
        limit = per_page or 20
        if rq:
            data = await _resolve(*rq).search_issues(q, per_page=limit)
        else:
            all_items: list[dict[str, Any]] = []
            for c in _all():
                d = await c.search_issues(q, per_page=limit)
                all_items.extend(d.get("items", []))
            data = {"total_count": len(all_items), "incomplete_results": False, "items": all_items[:limit]}
        return _truncate(json.dumps(data, indent=2))

    @server.tool
    async def search_users(
        q: str,
        order: Literal["asc", "desc"] | None = None,
        page: int | None = None,
        per_page: int | None = None,
        sort: Literal["followers", "repositories", "joined"] | None = None,
    ) -> str:
        """Search for users on GitHub."""
        data = await _resolve("", "").search_users(q, per_page=per_page or 30)
        return json.dumps(data, indent=2)

    @server.tool
    async def search_repositories(
        query: str,
        page: int | None = None,
        perPage: int | None = None,
    ) -> str:
        """Search for GitHub repositories."""
        repos: list[dict[str, Any]] = []
        for c in _all():
            repos.extend(await c.list_repos())
        q_lower = query.lower()
        matches = [
            r
            for r in repos
            if q_lower in (r.get("full_name") or "").lower() or q_lower in (r.get("description") or "").lower()
        ]
        result = {
            "total_count": len(matches),
            "incomplete_results": False,
            "items": matches,
        }
        return _truncate(json.dumps(result, indent=2))

    @server.tool
    async def list_pull_requests(
        owner: str,
        repo: str,
        state: Literal["open", "closed", "all"] | None = None,
        head: str | None = None,
        base: str | None = None,
        sort: Literal["created", "updated", "popularity", "long-running"] | None = None,
        direction: Literal["asc", "desc"] | None = None,
        per_page: int | None = None,
        page: int | None = None,
    ) -> str:
        """List and filter repository pull requests."""
        prs = await _resolve(owner, repo).list_pull_requests(
            owner,
            repo,
            state=state or "open",
            sort=sort or "created",
            direction=direction or "desc",
            per_page=min(per_page or 20, 100),
            page=page or 1,
        )
        if head is not None:
            prs = [p for p in prs if p.get("head", {}).get("ref") == head]
        if base is not None:
            prs = [p for p in prs if p.get("base", {}).get("ref") == base]
        return _truncate(json.dumps(prs, indent=2))

    @server.tool
    async def get_pull_request(owner: str, repo: str, pull_number: int) -> str:
        """Get details of a specific pull request."""
        data = await _resolve(owner, repo).get_pull_request(owner, repo, pull_number)
        return _truncate(json.dumps(data, indent=2))

    @server.tool
    async def get_pull_request_files(owner: str, repo: str, pull_number: int) -> str:
        """Get the list of files changed in a pull request."""
        files = await _build_pull_request_files(owner, repo, pull_number)
        return _truncate(json.dumps(files, indent=2))

    @server.tool
    async def get_pull_request_status(owner: str, repo: str, pull_number: int) -> str:
        """Get the combined status of all status checks for a pull request."""
        data = await _resolve(owner, repo).get_pull_request_status(owner, repo, pull_number)
        return json.dumps(data, indent=2)

    @server.tool
    async def get_pull_request_comments(owner: str, repo: str, pull_number: int) -> str:
        """Get the review comments on a pull request."""
        comments = await _resolve(owner, repo).list_pr_review_comments(owner, repo, pull_number)
        return _truncate(json.dumps(comments, indent=2))

    @server.tool
    async def get_pull_request_reviews(owner: str, repo: str, pull_number: int) -> str:
        """Get the reviews on a pull request."""
        reviews = await _resolve(owner, repo).list_pr_reviews(owner, repo, pull_number)
        return _truncate(json.dumps(reviews, indent=2))

    @server.tool
    async def pull_request_read(
        method: Literal[
            "get",
            "get_diff",
            "get_status",
            "get_files",
            "get_review_comments",
            "get_reviews",
            "get_comments",
            "get_check_runs",
        ],
        owner: str,
        repo: str,
        pullNumber: int,
        page: int | None = None,
        perPage: int | None = None,
    ) -> str:
        """Get information on a specific pull request in GitHub repository."""
        if method == "get":
            data = await _resolve(owner, repo).get_pull_request(owner, repo, pullNumber)
        elif method == "get_diff":
            data = {"diff": await _get_pull_request_diff(owner, repo, pullNumber)}
        elif method == "get_status":
            data = await _resolve(owner, repo).get_pull_request_status(owner, repo, pullNumber)
        elif method == "get_files":
            files = await _build_pull_request_files(owner, repo, pullNumber)
            page_num = page or 1
            per_page = perPage or 30
            start = max(page_num - 1, 0) * per_page
            data = files[start : start + per_page]
        elif method == "get_review_comments":
            comments = await _resolve(owner, repo).list_pr_review_comments(owner, repo, pullNumber)
            page_num = page or 1
            per_page = perPage or 30
            start = max(page_num - 1, 0) * per_page
            data = comments[start : start + per_page]
        elif method == "get_reviews":
            reviews = await _resolve(owner, repo).list_pr_reviews(owner, repo, pullNumber)
            page_num = page or 1
            per_page = perPage or 30
            start = max(page_num - 1, 0) * per_page
            data = reviews[start : start + per_page]
        elif method == "get_comments":
            raise ValueError("pull_request_read method 'get_comments' is not implemented in the vendored mock")
        elif method == "get_check_runs":
            raise ValueError("pull_request_read method 'get_check_runs' is not implemented in the vendored mock")
        else:
            raise ValueError(f"Unsupported pull_request_read method: {method}")
        return _truncate(json.dumps(data, indent=2))

    # ================================================================
    # Write tools (only registered when read_only=False)
    # ================================================================

    if not read_only:

        @server.tool
        async def create_or_update_file(
            owner: str,
            repo: str,
            path: str,
            content: str,
            message: str,
            branch: str,
            sha: str | None = None,
        ) -> str:
            """Create or update a single file in a GitHub repository."""
            data = await _resolve(owner, repo).create_or_update_file(
                owner,
                repo,
                path,
                content,
                message,
                branch,
                sha,
            )
            return json.dumps(data, indent=2)

        @server.tool
        async def create_repository(
            name: str,
            description: str | None = None,
            private: bool | None = None,
            autoInit: bool | None = None,
        ) -> str:
            """Create a new GitHub repository in your account."""
            data = await _resolve("", "").create_repository(
                name,
                description=description,
                private=bool(private),
                auto_init=bool(autoInit),
            )
            return json.dumps(data, indent=2)

        @server.tool
        async def push_files(
            owner: str,
            repo: str,
            branch: str,
            files: list[dict[str, str]],
            message: str,
        ) -> str:
            """Push multiple files to a GitHub repository in a single commit."""
            data = await _resolve(owner, repo).push_files(owner, repo, branch, files, message)
            return json.dumps(data, indent=2)

        @server.tool
        async def create_issue(
            owner: str,
            repo: str,
            title: str,
            body: str | None = None,
            assignees: list[str] | None = None,
            milestone: int | None = None,
            labels: list[str] | None = None,
        ) -> str:
            """Create a new issue in a GitHub repository."""
            data = await _resolve(owner, repo).create_issue(
                owner,
                repo,
                title,
                body=body,
                labels=labels,
                assignees=assignees,
                milestone=milestone,
            )
            return json.dumps(data, indent=2)

        @server.tool
        async def create_pull_request(
            owner: str,
            repo: str,
            title: str,
            head: str,
            base: str,
            body: str | None = None,
            draft: bool | None = None,
            maintainer_can_modify: bool | None = None,
        ) -> str:
            """Create a new pull request in a GitHub repository."""
            data = await _resolve(owner, repo).create_pull_request(
                owner,
                repo,
                title,
                head,
                base,
                body=body,
                draft=bool(draft),
            )
            return json.dumps(data, indent=2)

        @server.tool
        async def fork_repository(
            owner: str,
            repo: str,
            organization: str | None = None,
        ) -> str:
            """Fork a GitHub repository to your account or specified organization."""
            data = await _resolve(owner, repo).fork_repository(owner, repo, organization)
            return json.dumps(data, indent=2)

        @server.tool
        async def create_branch(
            owner: str,
            repo: str,
            branch: str,
            from_branch: str | None = None,
        ) -> str:
            """Create a new branch in a GitHub repository."""
            c = _resolve(owner, repo)
            if not from_branch:
                repo_data = await c.get_repo(owner, repo)
                default_branch = repo_data.get("default_branch", "main")
                branch_data = await c.get_branch(owner, repo, default_branch)
                sha = branch_data["commit"]["sha"]
            elif len(from_branch) < 40:
                branch_data = await c.get_branch(owner, repo, from_branch)
                sha = branch_data["commit"]["sha"]
            else:
                sha = from_branch

            data = await c.create_branch(owner, repo, branch, sha)
            return json.dumps(data, indent=2)

        @server.tool
        async def update_issue(
            owner: str,
            repo: str,
            issue_number: int,
            title: str | None = None,
            body: str | None = None,
            assignees: list[str] | None = None,
            milestone: int | None = None,
            labels: list[str] | None = None,
            state: Literal["open", "closed"] | None = None,
        ) -> str:
            """Update an existing issue in a GitHub repository."""
            data = await _resolve(owner, repo).update_issue(
                owner,
                repo,
                issue_number,
                title=title,
                body=body,
                state=state,
                labels=labels,
                assignees=assignees,
                milestone=milestone,
            )
            return json.dumps(data, indent=2)

        @server.tool
        async def add_issue_comment(
            owner: str,
            repo: str,
            issue_number: int,
            body: str,
        ) -> str:
            """Add a comment to an existing issue."""
            data = await _resolve(owner, repo).add_issue_comment(
                owner,
                repo,
                issue_number,
                body,
            )
            return json.dumps(data, indent=2)

        @server.tool
        async def create_pull_request_review(
            owner: str,
            repo: str,
            pull_number: int,
            body: str,
            event: Literal["APPROVE", "REQUEST_CHANGES", "COMMENT"],
            commit_id: str | None = None,
            comments: list | None = None,
        ) -> str:
            """Create a review on a pull request."""
            data = await _resolve(owner, repo).create_pull_request_review(
                owner,
                repo,
                pull_number,
                body=body,
                event=event,
                commit_id=commit_id,
                comments=comments,
            )
            return json.dumps(data, indent=2)

        @server.tool
        async def merge_pull_request(
            owner: str,
            repo: str,
            pull_number: int,
            commit_title: str | None = None,
            commit_message: str | None = None,
            merge_method: Literal["merge", "squash", "rebase"] | None = None,
        ) -> str:
            """Merge a pull request."""
            data = await _resolve(owner, repo).merge_pull_request(
                owner,
                repo,
                pull_number,
                commit_title=commit_title,
                commit_message=commit_message,
                merge_method=merge_method,
            )
            return json.dumps(data, indent=2)

        @server.tool
        async def update_pull_request_branch(
            owner: str,
            repo: str,
            pull_number: int,
            expected_head_sha: str | None = None,
        ) -> str:
            """Update a pull request branch with the latest changes from the base branch."""
            await _resolve(owner, repo).update_pull_request_branch(
                owner,
                repo,
                pull_number,
                expected_head_sha=expected_head_sha,
            )
            return json.dumps({"success": True}, indent=2)

    await _ensure_tool_descriptions(server)
    await _fix_schemas(server)
    return server
