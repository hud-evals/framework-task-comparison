"""Mock GitHub client — JSON + bare repo + in-memory state.

Instead of making real HTTP calls to ``api.github.com``, this client:

- **Static data** (issues, repo metadata, user): loaded from JSON files
- **Git-backed data** (branches, commits, file contents, diffs): reads
  a local bare git repo via ``subprocess``
- **In-memory mutable state** (PRs, new issues, comments): stored in
  Python dicts/lists, ephemeral per container run

Typical usage::

    from servers.mcp.github import MockGitHubService

    mock = MockGitHubService(
        data_dir="/mcp_server/github_data",
        bare_repo_path="/srv/git/project.git",
        read_only=False,
    )
    env.connect_server(mock.server)
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import tempfile
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class GitHubAPIError(Exception):
    """Raised when a GitHub API call fails."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"GitHub API error {status_code}: {message}")


class RepoAccessDenied(Exception):
    """Raised when trying to access a repo outside the allow-list."""

    def __init__(self, repo: str, allowed: list[str]) -> None:
        allowed_str = ", ".join(allowed) if allowed else "(none)"
        super().__init__(f"Repository '{repo}' is not accessible. Allowed repositories: {allowed_str}")


# ---------------------------------------------------------------------------
# Mock client
# ---------------------------------------------------------------------------


class MockGitHubClient:
    """Mock GitHub client backed by JSON files, a bare repo, and in-memory state.

    Args:
        bare_repo_path: Path to the local bare git repo (set at runtime
            by ``setup_task()``).
        data_dir: Directory containing ``repo.json``, ``issues.json``,
            ``user.json``.
        repo_owner: The mock repository owner.
        repo_name: The mock repository name.
        default_branch: Default branch name for the mock repo.
        allowed_repos: List of ``"owner/repo"`` strings the agent can
            access.  Defaults to ``["{repo_owner}/{repo_name}"]``.
    """

    def __init__(
        self,
        *,
        bare_repo_path: str | None = None,
        data_dir: str | None = None,
        repo_owner: str = "hud-evals",
        repo_name: str = "mock-github-repo",
        default_branch: str = "baseline",
        allowed_repos: list[str] | None = None,
        hidden_branches: list[str] | None = None,
    ) -> None:
        self.bare_repo_path = bare_repo_path
        self.data_dir = data_dir
        self.repo_owner = repo_owner
        self.repo_name = repo_name
        self.default_branch = default_branch
        self.hidden_branches = [b.lower() for b in (hidden_branches or [])]

        full_name = f"{repo_owner}/{repo_name}"
        self.allowed_repos = [r.lower() for r in (allowed_repos or [full_name])]

        # Static data (loaded from JSON)
        self._user: dict[str, Any] = {}
        self._repo_meta: dict[str, Any] = {}
        self._issues: list[dict[str, Any]] = []

        # In-memory mutable state (agent-created at runtime)
        self._next_number: int = 100
        self._created_issues: list[dict[str, Any]] = []
        self._pull_requests: list[dict[str, Any]] = []
        self._comments: dict[int, list[dict[str, Any]]] = {}
        self._pr_reviews: dict[int, list[dict[str, Any]]] = {}
        self._pr_review_comments: dict[int, list[dict[str, Any]]] = {}
        self._milestones: dict[int, dict[str, Any]] = {}

        # Action log — every write operation the agent performs
        self._action_log: list[dict[str, Any]] = []

        # Auto-incrementing ID counter for generated objects
        self._next_id: int = 1000

    # ── Data loading ───────────────────────────────────────────────────

    def load(self, data_dir: str | None = None) -> None:
        """Load issues, repo metadata, and user profile from JSON files."""
        d = data_dir or self.data_dir
        if not d:
            logger.warning("MockGitHubClient: no data_dir set, skipping load")
            return

        self.data_dir = d

        repo_path = os.path.join(d, "repo.json")
        if os.path.isfile(repo_path):
            with open(repo_path) as f:
                self._repo_meta = json.load(f)
            logger.info("Loaded repo.json: %s", self._repo_meta.get("full_name"))
        else:
            # Build a sensible default
            self._repo_meta = {
                "id": 1,
                "name": self.repo_name,
                "full_name": f"{self.repo_owner}/{self.repo_name}",
                "description": "Mock repository",
                "default_branch": self.default_branch,
                "private": True,
                "html_url": f"https://github.com/{self.repo_owner}/{self.repo_name}",
                "language": "Python",
                "topics": [],
            }

        issues_path = os.path.join(d, "issues.json")
        if os.path.isfile(issues_path):
            with open(issues_path) as f:
                self._issues = json.load(f)
            logger.info("Loaded %d pre-populated issues", len(self._issues))

            if self._issues:
                max_num = max(i.get("number", 0) for i in self._issues)
                self._next_number = max(self._next_number, max_num + 1)

            for issue in self._issues:
                self._enrich_issue(issue)

        user_path = os.path.join(d, "user.json")
        if os.path.isfile(user_path):
            with open(user_path) as f:
                self._user = json.load(f)
            logger.info("Loaded user: %s", self._user.get("login"))
        else:
            self._user = {
                "login": "agent-bot",
                "name": "Agent Bot",
                "email": "agent@example.com",
                "id": 42,
            }

        # ── Pre-populated pull requests ────────────────────────────────
        prs_path = os.path.join(d, "pull_requests.json")
        if os.path.isfile(prs_path):
            with open(prs_path) as f:
                raw_prs: list[dict[str, Any]] = json.load(f)
            for pr in raw_prs:
                self._enrich_pr(pr)
                self._pull_requests.append(pr)
            if raw_prs:
                max_pr = max(p.get("number", 0) for p in raw_prs)
                self._next_number = max(self._next_number, max_pr + 1)
            logger.info("Loaded %d pre-populated pull requests", len(raw_prs))

        # ── Pre-populated comments (keyed by issue/PR number) ──────────
        comments_path = os.path.join(d, "comments.json")
        if os.path.isfile(comments_path):
            with open(comments_path) as f:
                raw_comments: dict[str, list[dict[str, Any]]] = json.load(f)
            total = 0
            for num_str, comment_list in raw_comments.items():
                issue_number = int(num_str)
                enriched = [self._enrich_comment(c, issue_number) for c in comment_list]
                self._comments.setdefault(issue_number, []).extend(enriched)
                total += len(enriched)
                comment_count = len(self._comments[issue_number])
                for issue in self._issues + self._created_issues:
                    if issue.get("number") == issue_number:
                        issue["comments"] = comment_count
                        break
                else:
                    for pr in self._pull_requests:
                        if pr.get("number") == issue_number:
                            pr["comments"] = comment_count
                            break
            logger.info("Loaded %d pre-populated comments", total)

        # ── Pre-populated reviews (keyed by PR number) ─────────────────
        reviews_path = os.path.join(d, "reviews.json")
        if os.path.isfile(reviews_path):
            with open(reviews_path) as f:
                raw_reviews: dict[str, list[dict[str, Any]]] = json.load(f)
            total = 0
            for num_str, review_list in raw_reviews.items():
                pr_number = int(num_str)
                enriched = [self._enrich_review(r, pr_number) for r in review_list]
                self._pr_reviews.setdefault(pr_number, []).extend(enriched)
                total += len(enriched)
            logger.info("Loaded %d pre-populated reviews", total)

        # ── Pre-populated milestones ───────────────────────────────────
        milestones_path = os.path.join(d, "milestones.json")
        if os.path.isfile(milestones_path):
            with open(milestones_path) as f:
                raw_milestones: list[dict[str, Any]] = json.load(f)
            for ms in raw_milestones:
                number = ms.get("number", len(self._milestones) + 1)
                built = self._make_milestone(number, ms.get("title"))
                for k, v in ms.items():
                    if k not in ("number",):
                        built[k] = v
                self._milestones[number] = built
            logger.info("Loaded %d pre-populated milestones", len(raw_milestones))

    def reload(
        self,
        *,
        data_dir: str | None = None,
        bare_repo_path: str | None = None,
        **overrides: Any,
    ) -> None:
        """Reload data and reset mutable state."""
        if bare_repo_path is not None:
            self.bare_repo_path = bare_repo_path
        self._created_issues.clear()
        self._pull_requests.clear()
        self._comments.clear()
        self._pr_reviews.clear()
        self._pr_review_comments.clear()
        self._milestones.clear()
        self._action_log.clear()
        self._next_number = 100
        for k, v in overrides.items():
            if hasattr(self, k):
                setattr(self, k, v)
        self.load(data_dir)

    # ── Validation ─────────────────────────────────────────────────────

    def _check_repo(self, owner: str, repo: str) -> None:
        """Raise ``RepoAccessDenied`` if the repo is not allowed."""
        if not self.allowed_repos:
            return
        full = f"{owner}/{repo}".lower()
        if full not in self.allowed_repos:
            raise RepoAccessDenied(full, self.allowed_repos)

    # ── Git helpers ────────────────────────────────────────────────────

    def _git(
        self,
        *args: str,
        check: bool = True,
        input_data: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run a git command against the bare repo."""
        if not self.bare_repo_path:
            raise GitHubAPIError(404, "No git repository configured for this mock")
        cmd = ["git", "-C", self.bare_repo_path, *args]
        env = {**os.environ, **extra_env} if extra_env else None
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=check,
            input=input_data,
            env=env,
        )

    def _resolve_ref(self, ref: str | None) -> str:
        """Resolve ref to use, defaulting to default_branch."""
        return ref or self.default_branch

    def _normalize_repo_path(self, path: str | None) -> str:
        """Normalize a repository-relative path.

        GitHub tools treat the repository root as ``"/"``. Internally we use
        the empty string for root and strip leading/trailing slashes from
        concrete paths.
        """
        if not path or path == "/":
            return ""
        return path.strip("/")

    def _parse_patch_sections(self, diff_text: str) -> dict[str, str]:
        """Parse unified diff text into a ``filename -> patch`` mapping."""
        patches: dict[str, str] = {}
        current_file = ""
        current_patch_lines: list[str] = []
        for line in diff_text.splitlines():
            if line.startswith("diff --git"):
                if current_file and current_patch_lines:
                    patches[current_file] = "\n".join(current_patch_lines)
                parts = line.split(" b/", 1)
                current_file = parts[1] if len(parts) == 2 else ""
                current_patch_lines = []
            elif line.startswith("@@"):
                current_patch_lines.append(line)
            elif current_patch_lines:
                current_patch_lines.append(line)
        if current_file and current_patch_lines:
            patches[current_file] = "\n".join(current_patch_lines)
        return patches

    def _list_tree_entries(
        self,
        treeish: str,
        *,
        prefix: str = "",
        recursive: bool = False,
    ) -> list[dict[str, Any]]:
        """Return parsed ``git ls-tree`` entries for a treeish."""
        args = ["ls-tree"]
        if recursive:
            args.append("-r")
        args.extend(["-l", treeish])
        result = self._git(*args, check=False)
        if result.returncode != 0:
            raise GitHubAPIError(404, f"Tree '{treeish}' not found")

        entries: list[dict[str, Any]] = []
        for line in result.stdout.strip().splitlines():
            if "\t" not in line:
                continue
            meta, rel_name = line.split("\t", 1)
            parts = meta.split()
            if len(parts) < 4:
                continue
            mode, git_type, entry_sha, size_raw = parts[:4]
            size = int(size_raw) if size_raw.isdigit() else 0
            full_path = f"{prefix}/{rel_name}" if prefix else rel_name
            entries.append(
                {
                    "path": full_path,
                    "name": rel_name.rsplit("/", 1)[-1],
                    "mode": mode,
                    "git_type": git_type,
                    "sha": entry_sha,
                    "size": size,
                }
            )
        return entries

    def _now_iso(self) -> str:
        """Return current time in ISO format."""
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── Git write helpers ─────────────────────────────────────────────

    def _git_env(self) -> dict[str, str]:
        """Environment variables for git commit operations."""
        name = self._user.get("name", "Agent Bot")
        email = self._user.get("email", "agent@example.com")
        return {
            "GIT_AUTHOR_NAME": name,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email,
        }

    def _commit_files(
        self,
        branch: str,
        file_changes: list[tuple[str, str]],
        message: str,
    ) -> tuple[str, dict[str, str]]:
        """Write files to a branch via git plumbing.

        Uses a temporary index so the bare repo's default index is untouched.
        Returns (commit_sha, {path: blob_sha}).
        """
        branch_ref = f"refs/heads/{branch}"
        parent_result = self._git("rev-parse", branch_ref, check=False)
        if parent_result.returncode != 0:
            raise GitHubAPIError(404, f"Branch '{branch}' not found")
        parent_sha = parent_result.stdout.strip()

        with tempfile.TemporaryDirectory() as tmpdir:
            idx_path = os.path.join(tmpdir, "index")
            env = {**os.environ, "GIT_INDEX_FILE": idx_path, **self._git_env()}
            git_cmd = ["git", "-C", self.bare_repo_path]

            subprocess.run(
                [*git_cmd, "read-tree", parent_sha],
                capture_output=True,
                text=True,
                check=True,
                env=env,
            )

            blob_shas: dict[str, str] = {}
            for fpath, content in file_changes:
                blob_result = subprocess.run(
                    [*git_cmd, "hash-object", "-w", "--stdin"],
                    input=content,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                blob_sha = blob_result.stdout.strip()
                blob_shas[fpath] = blob_sha
                subprocess.run(
                    [*git_cmd, "update-index", "--add", "--cacheinfo", f"100644,{blob_sha},{fpath}"],
                    capture_output=True,
                    text=True,
                    check=True,
                    env=env,
                )

            tree_result = subprocess.run(
                [*git_cmd, "write-tree"],
                capture_output=True,
                text=True,
                check=True,
                env=env,
            )
            tree_sha = tree_result.stdout.strip()

            commit_result = subprocess.run(
                [*git_cmd, "commit-tree", tree_sha, "-p", parent_sha, "-m", message],
                capture_output=True,
                text=True,
                check=True,
                env=env,
            )
            commit_sha = commit_result.stdout.strip()

        self._git("update-ref", branch_ref, commit_sha)
        return commit_sha, blob_shas

    def _merge_branches(
        self,
        base_branch: str,
        head_branch: str,
        message: str,
        method: str = "merge",
    ) -> str:
        """Merge head_branch into base_branch in the bare repo. Returns new commit SHA."""
        with tempfile.TemporaryDirectory() as tmpdir:
            worktree_path = os.path.join(tmpdir, "merge-work")
            env = {**os.environ, **self._git_env()}

            self._git("worktree", "add", worktree_path, base_branch)
            try:
                git_wt = ["git", "-C", worktree_path]

                if method == "squash":
                    subprocess.run(
                        [*git_wt, "merge", "--squash", head_branch],
                        capture_output=True,
                        text=True,
                        check=False,
                        env=env,
                    )
                    result = subprocess.run(
                        [*git_wt, "commit", "-m", message],
                        capture_output=True,
                        text=True,
                        check=False,
                        env=env,
                    )
                    if result.returncode != 0:
                        raise GitHubAPIError(405, f"Squash merge failed: {result.stderr.strip()}")
                else:
                    result = subprocess.run(
                        [*git_wt, "merge", "--no-edit", "-m", message, head_branch],
                        capture_output=True,
                        text=True,
                        check=False,
                        env=env,
                    )
                    if result.returncode != 0:
                        raise GitHubAPIError(405, f"Merge failed: {result.stderr.strip()}")

                sha_result = subprocess.run(
                    [*git_wt, "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                return sha_result.stdout.strip()
            finally:
                self._git("worktree", "remove", "--force", worktree_path, check=False)

    # ── Action logging ────────────────────────────────────────────────

    def _log_action(self, action: str, **details: Any) -> None:
        """Record a write operation in the action log."""
        entry = {"action": action, "timestamp": self._now_iso(), **details}
        self._action_log.append(entry)
        logger.info("MockGitHub action: %s", action)

    def get_action_log(self) -> list[dict[str, Any]]:
        """Return a copy of the full action log (used by graders)."""
        return list(self._action_log)

    def snapshot_refs(self) -> None:
        """Snapshot all bare-repo refs. Call at scenario setup time.

        Used by ``detect_pushes()`` at grading time to find branches
        the agent pushed via native ``git push``.
        """
        if not self.bare_repo_path:
            return
        result = self._git("for-each-ref", "--format=%(refname) %(objectname)", check=False)
        if result.returncode == 0:
            self._initial_refs: dict[str, str] = {}
            for line in result.stdout.strip().splitlines():
                parts = line.split()
                if len(parts) == 2:
                    self._initial_refs[parts[0]] = parts[1]

    def detect_pushes(self) -> list[dict[str, str]]:
        """Compare current bare-repo refs to the snapshot to find pushes.

        Call at grading time. Returns a list of push records and appends
        them to the action log.
        """
        if not self.bare_repo_path or not hasattr(self, "_initial_refs"):
            return []
        result = self._git("for-each-ref", "--format=%(refname) %(objectname)", check=False)
        if result.returncode != 0:
            return []

        current_refs: dict[str, str] = {}
        for line in result.stdout.strip().splitlines():
            parts = line.split()
            if len(parts) == 2:
                current_refs[parts[0]] = parts[1]

        pushes = []
        for ref, sha in current_refs.items():
            if not ref.startswith("refs/heads/"):
                continue
            branch = ref.removeprefix("refs/heads/")
            if branch.lower() in self.hidden_branches:
                continue
            old_sha = self._initial_refs.get(ref)
            if old_sha != sha:
                push = {"branch": branch, "old_sha": old_sha or "(new)", "new_sha": sha}
                pushes.append(push)
                self._log_action("push", **push)
        return pushes

    def _html_url(self, suffix: str = "") -> str:
        """Build a mock html_url."""
        base = f"https://github.com/{self.repo_owner}/{self.repo_name}"
        return f"{base}/{suffix}" if suffix else base

    @staticmethod
    def _parse_search_qualifiers(query: str) -> tuple[str, dict[str, list[str]]]:
        """Parse ``key:value`` qualifiers out of a GitHub search query.

        Returns ``(free_text, qualifiers)`` where *qualifiers* maps each
        key (lowercased) to a list of values found in the query.
        """
        import re

        qualifiers: dict[str, list[str]] = {}

        def _collect(match: re.Match[str]) -> str:
            qualifiers.setdefault(match.group(1).lower(), []).append(match.group(2))
            return ""

        free_text = re.sub(r"\b(\w+):([^\s]+)", _collect, query).strip()
        return free_text, qualifiers

    # ── Schema-conforming object builders ────────────────────────────

    def _api_url(self, path: str = "") -> str:
        """Build ``https://api.github.com/repos/{owner}/{repo}[/{path}]``."""
        base = f"https://api.github.com/repos/{self.repo_owner}/{self.repo_name}"
        return f"{base}/{path}" if path else base

    def _node_id(self, type_prefix: str, id_val: int) -> str:
        """Generate a GitHub-style node_id (base64-encoded type+id)."""
        return base64.b64encode(f"{type_prefix}{id_val}".encode()).decode()

    def _next_auto_id(self) -> int:
        """Return an auto-incrementing ID."""
        self._next_id += 1
        return self._next_id

    def _make_user(self, login: str | None = None) -> dict[str, Any]:
        """Build a full ``GitHubIssueAssigneeSchema``-conforming user object."""
        if login is None:
            login = self._user.get("login", "agent-bot")
        is_self = login == self._user.get("login")
        uid = self._user.get("id", 42) if is_self else abs(hash(login)) % 100000
        return {
            "login": login,
            "id": uid,
            "avatar_url": f"https://avatars.githubusercontent.com/u/{uid}?v=4",
            "url": f"https://api.github.com/users/{login}",
            "html_url": f"https://github.com/{login}",
        }

    def _make_owner(self, login: str) -> dict[str, Any]:
        """Build a full ``GitHubOwnerSchema``-conforming owner object."""
        is_self = login == self._user.get("login")
        uid = self._user.get("id", 42) if is_self else abs(hash(login)) % 100000
        return {
            "login": login,
            "id": uid,
            "node_id": self._node_id("U_", uid),
            "avatar_url": f"https://avatars.githubusercontent.com/u/{uid}?v=4",
            "url": f"https://api.github.com/users/{login}",
            "html_url": f"https://github.com/{login}",
            "type": "User",
        }

    def _make_label(self, name: str) -> dict[str, Any]:
        """Build a full ``GitHubLabelSchema``-conforming label object."""
        label_id = abs(hash(name)) % 1000000
        color_map = {
            "bug": "d73a4a",
            "enhancement": "a2eeef",
            "documentation": "0075ca",
            "help wanted": "008672",
            "question": "d876e3",
            "good first issue": "7057ff",
        }
        color = color_map.get(name.lower(), f"{abs(hash(name)) % 0xFFFFFF:06x}")
        return {
            "id": label_id,
            "node_id": self._node_id("LA_", label_id),
            "url": f"{self._api_url()}/labels/{name}",
            "name": name,
            "color": color,
            "default": name.lower() in color_map,
            "description": None,
        }

    def _make_milestone(self, number: int, title: str | None = None) -> dict[str, Any]:
        """Build a ``GitHubMilestoneSchema``-conforming milestone object and cache it."""
        mid = self._next_auto_id()
        now = self._now_iso()
        ms: dict[str, Any] = {
            "url": self._api_url(f"milestones/{number}"),
            "html_url": self._html_url(f"milestone/{number}"),
            "labels_url": self._api_url(f"milestones/{number}/labels"),
            "id": mid,
            "node_id": self._node_id("MI_", mid),
            "number": number,
            "title": title or f"Milestone {number}",
            "description": None,
            "creator": self._make_user(),
            "open_issues": 0,
            "closed_issues": 0,
            "state": "open",
            "created_at": now,
            "updated_at": now,
            "due_on": None,
            "closed_at": None,
        }
        self._milestones[number] = ms
        return ms

    def _make_repo_object(self, owner: str | None = None, name: str | None = None) -> dict[str, Any]:
        """Build a full ``GitHubRepositorySchema``-conforming repo object."""
        o = owner or self.repo_owner
        n = name or self.repo_name
        repo_id = self._repo_meta.get("id", 1)
        now = self._now_iso()
        return {
            "id": repo_id,
            "node_id": self._node_id("R_", repo_id),
            "name": n,
            "full_name": f"{o}/{n}",
            "private": self._repo_meta.get("private", True),
            "owner": self._make_owner(o),
            "html_url": f"https://github.com/{o}/{n}",
            "description": self._repo_meta.get("description"),
            "fork": self._repo_meta.get("fork", False),
            "url": f"https://api.github.com/repos/{o}/{n}",
            "created_at": self._repo_meta.get("created_at", now),
            "updated_at": self._repo_meta.get("updated_at", now),
            "pushed_at": self._repo_meta.get("pushed_at", now),
            "git_url": f"git://github.com/{o}/{n}.git",
            "ssh_url": f"git@github.com:{o}/{n}.git",
            "clone_url": f"https://github.com/{o}/{n}.git",
            "default_branch": self.default_branch,
        }

    def _make_pr_ref(self, ref: str, sha: str, owner: str | None = None) -> dict[str, Any]:
        """Build a full ``GitHubPullRequestRefSchema``-conforming ref object."""
        o = owner or self.repo_owner
        return {
            "label": f"{o}:{ref}",
            "ref": ref,
            "sha": sha,
            "user": self._make_user(o),
            "repo": self._make_repo_object(o),
        }

    def _enrich_pr(self, pr: dict[str, Any]) -> dict[str, Any]:
        """Ensure a PR dict has all required fields.

        Called on pre-loaded PRs from ``pull_requests.json``.  Users only
        need to provide ``number``, ``title``, ``head`` (branch name),
        and ``base`` (branch name).  Everything else is filled in.
        """
        number = pr.get("number", 0)
        pr_id = pr.get("id") or self._next_auto_id()
        now = self._now_iso()

        user = pr.get("user")
        if isinstance(user, dict) and "avatar_url" not in user:
            pr["user"] = self._make_user(user.get("login"))

        labels = pr.get("labels", [])
        pr["labels"] = [
            self._make_label(lbl.get("name", "")) if isinstance(lbl, dict) and "id" not in lbl else lbl
            for lbl in labels
        ]

        assignees = pr.get("assignees", [])
        pr["assignees"] = [
            self._make_user(a.get("login")) if isinstance(a, dict) and "avatar_url" not in a else a for a in assignees
        ]

        _raw_head = pr.get("head")
        head_ref: str = (
            _raw_head
            if isinstance(_raw_head, str)
            else (_raw_head.get("ref", "") if isinstance(_raw_head, dict) else "")
        ) or ""
        _raw_base = pr.get("base")
        base_ref: str = (
            _raw_base
            if isinstance(_raw_base, str)
            else (_raw_base.get("ref", self.default_branch) if isinstance(_raw_base, dict) else self.default_branch)
        ) or self.default_branch

        if isinstance(pr.get("head"), str) or (isinstance(pr.get("head"), dict) and "sha" not in pr["head"]):
            head_sha_r = self._git("rev-parse", f"refs/heads/{head_ref}", check=False)
            head_sha = head_sha_r.stdout.strip() if head_sha_r.returncode == 0 else ""
            pr["head"] = self._make_pr_ref(head_ref or "", head_sha)
        if isinstance(pr.get("base"), str) or (isinstance(pr.get("base"), dict) and "sha" not in pr["base"]):
            base_sha_r = self._git("rev-parse", f"refs/heads/{base_ref}", check=False)
            base_sha = base_sha_r.stdout.strip() if base_sha_r.returncode == 0 else ""
            pr["base"] = self._make_pr_ref(base_ref or self.default_branch, base_sha)

        total_add, total_del, n_files = self._diff_stats(base_ref, head_ref) if head_ref and base_ref else (0, 0, 0)

        pr.setdefault("id", pr_id)
        pr.setdefault("node_id", self._node_id("PR_", pr_id))
        pr.setdefault("url", self._api_url(f"pulls/{number}"))
        pr.setdefault("html_url", self._html_url(f"pull/{number}"))
        pr.setdefault("diff_url", self._html_url(f"pull/{number}.diff"))
        pr.setdefault("patch_url", self._html_url(f"pull/{number}.patch"))
        pr.setdefault("issue_url", self._api_url(f"issues/{number}"))
        pr.setdefault("state", "open")
        pr.setdefault("locked", False)
        pr.setdefault("user", self._make_user())
        pr.setdefault("body", None)
        pr.setdefault("created_at", now)
        pr.setdefault("updated_at", now)
        pr.setdefault("closed_at", None)
        pr.setdefault("merged_at", None)
        pr.setdefault("merge_commit_sha", None)
        pr.setdefault("assignee", pr["assignees"][0] if pr["assignees"] else None)
        pr.setdefault("requested_reviewers", [])
        pr.setdefault("draft", False)
        pr.setdefault("merged", False)
        pr.setdefault("additions", total_add)
        pr.setdefault("deletions", total_del)
        pr.setdefault("changed_files", n_files)
        pr.setdefault("comments", 0)
        pr.setdefault("review_comments", 0)

        return pr

    def _enrich_comment(self, comment: dict[str, Any], issue_number: int) -> dict[str, Any]:
        """Ensure a comment dict has all required fields."""
        cid = comment.get("id") or self._next_auto_id()
        now = self._now_iso()

        user = comment.get("user")
        if isinstance(user, dict) and "avatar_url" not in user:
            comment["user"] = self._make_user(user.get("login"))

        comment.setdefault("id", cid)
        comment.setdefault("node_id", self._node_id("IC_", cid))
        comment.setdefault("url", self._api_url(f"issues/comments/{cid}"))
        comment.setdefault("html_url", self._html_url(f"issues/{issue_number}#issuecomment-{cid}"))
        comment.setdefault("issue_url", self._api_url(f"issues/{issue_number}"))
        comment.setdefault("user", self._make_user())
        comment.setdefault("created_at", now)
        comment.setdefault("updated_at", now)
        comment.setdefault("author_association", "OWNER")

        return comment

    def _enrich_review(self, review: dict[str, Any], pr_number: int) -> dict[str, Any]:
        """Ensure a review dict has all required fields."""
        rid = review.get("id") or self._next_auto_id()
        now = self._now_iso()

        user = review.get("user")
        if isinstance(user, dict) and "avatar_url" not in user:
            review["user"] = self._make_user(user.get("login"))

        state_map = {"APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED", "COMMENT": "COMMENTED"}
        if review.get("state") in state_map:
            review["state"] = state_map[review["state"]]

        review.setdefault("id", rid)
        review.setdefault("node_id", self._node_id("PRR_", rid))
        review.setdefault("user", self._make_user())
        review.setdefault("body", "")
        review.setdefault("state", "COMMENTED")
        review.setdefault("html_url", self._html_url(f"pull/{pr_number}#pullrequestreview-{rid}"))
        review.setdefault("pull_request_url", self._api_url(f"pulls/{pr_number}"))
        review.setdefault("commit_id", "")
        review.setdefault("submitted_at", now)
        review.setdefault("author_association", "OWNER")

        return review

    def _enrich_issue(self, issue: dict[str, Any]) -> dict[str, Any]:
        """Ensure an issue dict has all ``GitHubIssueSchema`` fields.

        Called on pre-loaded issues from ``issues.json`` to fill in any
        fields that the JSON file didn't include.
        """
        number = issue.get("number", 0)
        issue_id = issue.get("id") or abs(hash(f"issue-{number}")) % 1000000

        user = issue.get("user")
        if isinstance(user, dict) and "avatar_url" not in user:
            issue["user"] = self._make_user(user.get("login"))

        labels = issue.get("labels", [])
        enriched_labels = []
        for lbl in labels:
            if isinstance(lbl, dict) and "id" not in lbl:
                enriched_labels.append(self._make_label(lbl.get("name", "")))
            else:
                enriched_labels.append(lbl)
        issue["labels"] = enriched_labels

        assignees = issue.get("assignees", [])
        enriched_assignees = []
        for a in assignees:
            if isinstance(a, dict) and "avatar_url" not in a:
                enriched_assignees.append(self._make_user(a.get("login")))
            else:
                enriched_assignees.append(a)
        issue["assignees"] = enriched_assignees

        issue.setdefault("id", issue_id)
        issue.setdefault("node_id", self._node_id("I_", issue_id))
        issue.setdefault("url", self._api_url(f"issues/{number}"))
        issue.setdefault("repository_url", self._api_url())
        issue.setdefault("labels_url", self._api_url(f"issues/{number}/labels{{/name}}"))
        issue.setdefault("comments_url", self._api_url(f"issues/{number}/comments"))
        issue.setdefault("events_url", self._api_url(f"issues/{number}/events"))
        issue.setdefault("html_url", self._html_url(f"issues/{number}"))
        issue.setdefault("locked", False)
        issue.setdefault("assignee", issue["assignees"][0] if issue["assignees"] else None)
        issue.setdefault("milestone", None)
        issue.setdefault("comments", 0)
        issue.setdefault("created_at", self._now_iso())
        issue.setdefault("updated_at", self._now_iso())
        issue.setdefault("closed_at", None)
        issue.setdefault("body", None)
        issue.setdefault("state", "open")

        return issue

    def _diff_stats(self, base: str, head: str) -> tuple[int, int, int]:
        """Return ``(total_additions, total_deletions, files_changed)`` between two refs."""
        result = self._git("diff", "--numstat", f"{base}..{head}", check=False)
        if result.returncode != 0 or not result.stdout.strip():
            return 0, 0, 0
        total_add = total_del = n_files = 0
        for line in result.stdout.strip().splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3:
                a, d, _ = parts
                total_add += int(a) if a != "-" else 0
                total_del += int(d) if d != "-" else 0
                n_files += 1
        return total_add, total_del, n_files

    # ── Identity & Discovery ───────────────────────────────────────────

    async def list_repos(self, *, org: str | None = None) -> list[dict[str, Any]]:
        """Return a single-item list with the mock repo as a full GitHubRepositorySchema."""
        return [self._make_repo_object()]

    async def get_repo(self, owner: str, repo: str) -> dict[str, Any]:
        """Return mock repo metadata."""
        self._check_repo(owner, repo)
        return self._repo_meta

    # ── Branches ───────────────────────────────────────────────────────

    async def list_branches(
        self,
        owner: str,
        repo: str,
        *,
        per_page: int = 30,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """List branches from the bare repo, filtering hidden ones."""
        self._check_repo(owner, repo)
        result = self._git("for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads/", check=False)
        if result.returncode != 0:
            return []

        branches = []
        for line in result.stdout.strip().splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2:
                name, sha = parts
                branches.append(
                    {
                        "name": name,
                        "commit": {
                            "sha": sha,
                            "url": self._api_url(f"commits/{sha}"),
                        },
                        "protected": False,
                    }
                )

        start = max(page - 1, 0) * per_page
        return branches[start : start + per_page]

    # ── File contents ──────────────────────────────────────────────────

    async def get_file_contents(
        self,
        owner: str,
        repo: str,
        path: str = "/",
        ref: str | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Get file/directory contents from the bare repo."""
        self._check_repo(owner, repo)
        if not self.bare_repo_path:
            raise GitHubAPIError(404, "Repository content not available")

        resolved = self._resolve_ref(ref)
        normalized_path = self._normalize_repo_path(path)
        treeish = resolved if not normalized_path else f"{resolved}:{normalized_path}"

        if not normalized_path:
            obj_type = "tree"
        else:
            result = self._git("cat-file", "-t", treeish, check=False)
            if result.returncode != 0:
                raise GitHubAPIError(404, f"Not found: {normalized_path} at ref {resolved}")
            obj_type = result.stdout.strip()

        if obj_type == "tree":
            entries = []
            for entry in self._list_tree_entries(treeish, prefix=normalized_path, recursive=False):
                full_path = entry["path"]
                entry_type = "dir" if entry["git_type"] == "tree" else "file"
                api_base = self._api_url(f"contents/{full_path}")
                entries.append(
                    {
                        "type": entry_type,
                        "size": entry["size"],
                        "name": entry["name"],
                        "path": full_path,
                        "sha": entry["sha"],
                        "url": f"{api_base}?ref={resolved}",
                        "git_url": self._api_url(f"git/{'trees' if entry_type == 'dir' else 'blobs'}/{entry['sha']}"),
                        "html_url": self._html_url(
                            f"{'tree' if entry_type == 'dir' else 'blob'}/{resolved}/{full_path}"
                        ),
                        "download_url": (
                            f"https://raw.githubusercontent.com/{self.repo_owner}/{self.repo_name}/{resolved}/{full_path}"
                            if entry_type == "file"
                            else None
                        ),
                    }
                )
            return entries

        # File: get content + blob SHA
        content_result = self._git("show", treeish)
        raw = content_result.stdout
        encoded = base64.b64encode(raw.encode()).decode()

        sha_result = self._git("rev-parse", treeish, check=False)
        blob_sha = sha_result.stdout.strip() if sha_result.returncode == 0 else ""

        api_content = self._api_url(f"contents/{normalized_path}")
        return {
            "name": os.path.basename(normalized_path),
            "path": normalized_path,
            "sha": blob_sha,
            "size": len(raw.encode()),
            "url": f"{api_content}?ref={resolved}",
            "html_url": self._html_url(f"blob/{resolved}/{normalized_path}"),
            "git_url": self._api_url(f"git/blobs/{blob_sha}"),
            "download_url": (
                f"https://raw.githubusercontent.com/{self.repo_owner}/{self.repo_name}/{resolved}/{normalized_path}"
            ),
            "type": "file",
            "content": encoded,
            "encoding": "base64",
            "_links": {
                "self": api_content,
                "git": self._api_url(f"git/blobs/{blob_sha}"),
                "html": self._html_url(f"blob/{resolved}/{normalized_path}"),
            },
        }

    async def get_file_text(
        self,
        owner: str,
        repo: str,
        path: str,
        ref: str | None = None,
    ) -> str:
        """Get decoded file text from the bare repo."""
        self._check_repo(owner, repo)

        resolved = self._resolve_ref(ref)
        result = self._git("show", f"{resolved}:{self._normalize_repo_path(path)}", check=False)
        if result.returncode != 0:
            raise GitHubAPIError(404, f"Not found: {path} at ref {resolved}")
        return result.stdout

    async def get_repository_tree(
        self,
        owner: str,
        repo: str,
        *,
        tree_sha: str | None = None,
        recursive: bool = False,
        path_filter: str | None = None,
    ) -> dict[str, Any]:
        """Return the repository tree for a ref or tree SHA."""
        self._check_repo(owner, repo)

        resolved = self._resolve_ref(tree_sha)
        normalized_filter = self._normalize_repo_path(path_filter)
        treeish = resolved if not normalized_filter else f"{resolved}:{normalized_filter}"
        entries = self._list_tree_entries(treeish, prefix=normalized_filter, recursive=recursive)

        tree_sha_result = self._git("rev-parse", f"{treeish}^{{tree}}", check=False)
        resolved_tree_sha = tree_sha_result.stdout.strip() if tree_sha_result.returncode == 0 else resolved

        return {
            "sha": resolved_tree_sha,
            "url": self._api_url(f"git/trees/{resolved_tree_sha}"),
            "tree": [
                {
                    "path": entry["path"],
                    "mode": entry["mode"],
                    "type": "tree" if entry["git_type"] == "tree" else "blob",
                    "sha": entry["sha"],
                    "size": entry["size"],
                    "url": self._api_url(f"git/{'trees' if entry['git_type'] == 'tree' else 'blobs'}/{entry['sha']}"),
                }
                for entry in entries
            ],
            "truncated": False,
        }

    # ── Commits ────────────────────────────────────────────────────────

    async def list_commits(
        self,
        owner: str,
        repo: str,
        *,
        sha: str | None = None,
        path: str | None = None,
        since: str | None = None,
        until: str | None = None,
        author: str | None = None,
        per_page: int = 20,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """List commits from the bare repo."""
        self._check_repo(owner, repo)

        ref = self._resolve_ref(sha)
        # sha, tree, author_name, author_email, author_date,
        # committer_name, committer_email, committer_date, subject
        fmt = "%H%n%T%n%an%n%ae%n%aI%n%cn%n%ce%n%cI%n%s"
        fields_per_commit = 9
        max_count = per_page * page
        args = ["log", f"--format={fmt}", f"-{max_count}", ref]

        if path:
            args.extend(["--", path])
        if author:
            args.append(f"--author={author}")
        if since:
            args.append(f"--since={since}")
        if until:
            args.append(f"--until={until}")

        result = self._git(*args, check=False)
        if result.returncode != 0:
            return []

        commits = []
        lines = result.stdout.strip().split("\n")
        i = 0
        while i + fields_per_commit - 1 < len(lines):
            commit_sha = lines[i]
            tree_sha = lines[i + 1]
            author_name = lines[i + 2]
            author_email = lines[i + 3]
            author_date = lines[i + 4]
            committer_name = lines[i + 5]
            committer_email = lines[i + 6]
            committer_date = lines[i + 7]
            message = lines[i + 8]
            cid = self._next_auto_id()
            commits.append(
                {
                    "sha": commit_sha,
                    "node_id": self._node_id("C_", cid),
                    "commit": {
                        "author": {
                            "name": author_name,
                            "email": author_email,
                            "date": author_date,
                        },
                        "committer": {
                            "name": committer_name,
                            "email": committer_email,
                            "date": committer_date,
                        },
                        "message": message,
                        "tree": {
                            "sha": tree_sha,
                            "url": self._api_url(f"git/trees/{tree_sha}"),
                        },
                        "url": self._api_url(f"git/commits/{commit_sha}"),
                        "comment_count": 0,
                    },
                    "url": self._api_url(f"commits/{commit_sha}"),
                    "html_url": self._html_url(f"commit/{commit_sha}"),
                    "comments_url": self._api_url(f"commits/{commit_sha}/comments"),
                }
            )
            i += fields_per_commit

        start = (page - 1) * per_page
        return commits[start : start + per_page]

    async def get_commit(
        self,
        owner: str,
        repo: str,
        sha: str,
        *,
        include_diff: bool = True,
        per_page: int = 30,
        page: int = 1,
    ) -> dict[str, Any]:
        """Get detailed commit information for a specific commit."""
        self._check_repo(owner, repo)

        resolved_result = self._git("rev-parse", sha, check=False)
        if resolved_result.returncode != 0:
            raise GitHubAPIError(404, f"Commit '{sha}' not found")
        commit_sha = resolved_result.stdout.strip()

        fmt = "%H%x00%T%x00%P%x00%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI%x00%s%x00%b"
        meta_result = self._git("show", "-s", f"--format={fmt}", commit_sha, check=False)
        if meta_result.returncode != 0:
            raise GitHubAPIError(404, f"Commit '{sha}' not found")

        parts = meta_result.stdout.split("\x00", 10)
        if len(parts) != 11:
            raise GitHubAPIError(500, f"Unexpected git show output for commit '{sha}'")
        (
            _commit_sha,
            tree_sha,
            parents_raw,
            author_name,
            author_email,
            author_date,
            committer_name,
            committer_email,
            committer_date,
            subject,
            body,
        ) = parts
        message = subject if not body.strip() else f"{subject}\n\n{body.rstrip()}"

        commit_num = int(commit_sha[:12], 16) % 1_000_000
        response: dict[str, Any] = {
            "sha": commit_sha,
            "node_id": self._node_id("C_", commit_num),
            "commit": {
                "author": {
                    "name": author_name,
                    "email": author_email,
                    "date": author_date,
                },
                "committer": {
                    "name": committer_name,
                    "email": committer_email,
                    "date": committer_date,
                },
                "message": message,
                "tree": {
                    "sha": tree_sha,
                    "url": self._api_url(f"git/trees/{tree_sha}"),
                },
                "url": self._api_url(f"git/commits/{commit_sha}"),
                "comment_count": 0,
            },
            "url": self._api_url(f"commits/{commit_sha}"),
            "html_url": self._html_url(f"commit/{commit_sha}"),
            "comments_url": self._api_url(f"commits/{commit_sha}/comments"),
            "parents": [
                {
                    "sha": parent_sha,
                    "url": self._api_url(f"commits/{parent_sha}"),
                    "html_url": self._html_url(f"commit/{parent_sha}"),
                }
                for parent_sha in parents_raw.split()
                if parent_sha
            ],
        }

        numstat_result = self._git("diff-tree", "--root", "--no-commit-id", "--numstat", "-r", commit_sha, check=False)
        stat_map: dict[str, tuple[int, int]] = {}
        if numstat_result.returncode == 0:
            for line in numstat_result.stdout.strip().splitlines():
                stat_parts = line.split("\t", 2)
                if len(stat_parts) == 3:
                    adds_raw, dels_raw, filename = stat_parts
                    stat_map[filename] = (
                        int(adds_raw) if adds_raw.isdigit() else 0,
                        int(dels_raw) if dels_raw.isdigit() else 0,
                    )

        total_additions = sum(adds for adds, _ in stat_map.values())
        total_deletions = sum(dels for _, dels in stat_map.values())
        response["stats"] = {
            "total": total_additions + total_deletions,
            "additions": total_additions,
            "deletions": total_deletions,
        }

        patches: dict[str, str] = {}
        if include_diff:
            diff_result = self._git("show", "--format=", "--patch", "--root", commit_sha, check=False)
            if diff_result.returncode == 0 and diff_result.stdout.strip():
                patches = self._parse_patch_sections(diff_result.stdout)

        files_result = self._git(
            "diff-tree", "--root", "--no-commit-id", "--name-status", "-r", commit_sha, check=False
        )
        files: list[dict[str, Any]] = []
        if files_result.returncode == 0:
            status_map = {"A": "added", "M": "modified", "D": "removed", "R": "renamed"}
            for line in files_result.stdout.strip().splitlines():
                parts = line.split("\t")
                if len(parts) < 2:
                    continue
                status_code = parts[0]
                filename = parts[-1]
                adds, dels = stat_map.get(filename, (0, 0))
                blob_sha = ""
                if status_code[0] != "D":
                    blob_result = self._git("rev-parse", f"{commit_sha}:{filename}", check=False)
                    blob_sha = blob_result.stdout.strip() if blob_result.returncode == 0 else ""
                entry: dict[str, Any] = {
                    "sha": blob_sha,
                    "filename": filename,
                    "status": status_map.get(status_code[0], "modified"),
                    "additions": adds,
                    "deletions": dels,
                    "changes": adds + dels,
                    "blob_url": self._html_url(f"blob/{commit_sha}/{filename}") if blob_sha else None,
                    "raw_url": self._html_url(f"raw/{commit_sha}/{filename}") if blob_sha else None,
                    "contents_url": self._api_url(f"contents/{filename}?ref={commit_sha}"),
                }
                if include_diff and filename in patches:
                    entry["patch"] = patches[filename]
                files.append(entry)

        start = max(page - 1, 0) * per_page
        response["files"] = files[start : start + per_page]
        return response

    async def compare_commits(
        self,
        owner: str,
        repo: str,
        base: str,
        head: str,
    ) -> dict[str, Any]:
        """Compare two refs in the bare repo."""
        self._check_repo(owner, repo)

        # Count commits
        log_result = self._git("rev-list", "--count", f"{base}..{head}", check=False)
        ahead = int(log_result.stdout.strip()) if log_result.returncode == 0 else 0
        behind_result = self._git("rev-list", "--count", f"{head}..{base}", check=False)
        behind = int(behind_result.stdout.strip()) if behind_result.returncode == 0 else 0

        # Status
        if ahead == 0 and behind == 0:
            status = "identical"
        elif ahead > 0 and behind == 0:
            status = "ahead"
        elif ahead == 0 and behind > 0:
            status = "behind"
        else:
            status = "diverged"

        # Commits
        commits_result = self._git(
            "log",
            "--format=%H %s",
            f"{base}..{head}",
            check=False,
        )
        commits = []
        if commits_result.returncode == 0:
            for cline in commits_result.stdout.strip().splitlines():
                parts = cline.split(" ", 1)
                if len(parts) == 2:
                    csha, cmsg = parts
                    commits.append(
                        {
                            "sha": csha,
                            "commit": {"message": cmsg},
                        }
                    )

        # Per-file stats via --numstat
        numstat_result = self._git(
            "diff",
            "--numstat",
            f"{base}..{head}",
            check=False,
        )
        stat_map: dict[str, tuple[int, int]] = {}
        if numstat_result.returncode == 0:
            for sline in numstat_result.stdout.strip().splitlines():
                parts = sline.split("\t", 2)
                if len(parts) == 3:
                    a_str, d_str, fname = parts
                    adds = int(a_str) if a_str != "-" else 0
                    dels = int(d_str) if d_str != "-" else 0
                    stat_map[fname] = (adds, dels)

        # File list with SHAs via --name-status
        files_result = self._git(
            "diff",
            "--name-status",
            f"{base}..{head}",
            check=False,
        )
        files = []
        if files_result.returncode == 0:
            for fline in files_result.stdout.strip().splitlines():
                parts = fline.split("\t", 1)
                if len(parts) == 2:
                    status_char, filename = parts
                    status_map = {"A": "added", "M": "modified", "D": "removed"}
                    adds, dels = stat_map.get(filename, (0, 0))
                    blob_sha = ""
                    if status_char[0] != "D":
                        sha_r = self._git(
                            "rev-parse",
                            f"{head}:{filename}",
                            check=False,
                        )
                        blob_sha = sha_r.stdout.strip() if sha_r.returncode == 0 else ""
                    files.append(
                        {
                            "sha": blob_sha,
                            "filename": filename,
                            "status": status_map.get(status_char[0], "modified"),
                            "additions": adds,
                            "deletions": dels,
                            "changes": adds + dels,
                        }
                    )

        patches: dict[str, str] = {}
        diff_result = self._git("diff", f"{base}..{head}", check=False)
        if diff_result.returncode == 0 and diff_result.stdout.strip():
            patches = self._parse_patch_sections(diff_result.stdout)

        return {
            "status": status,
            "ahead_by": ahead,
            "behind_by": behind,
            "commits": commits,
            "files": files,
            "patches": patches,
        }

    # Extension mapping for language: qualifier in code search
    _LANG_EXTENSIONS: dict[str, list[str]] = {
        "python": ["py", "pyi"],
        "javascript": ["js", "mjs", "cjs"],
        "typescript": ["ts", "tsx"],
        "java": ["java"],
        "go": ["go"],
        "ruby": ["rb"],
        "rust": ["rs"],
        "c": ["c", "h"],
        "cpp": ["cpp", "cc", "cxx", "hpp", "hh"],
        "csharp": ["cs"],
        "swift": ["swift"],
        "kotlin": ["kt", "kts"],
        "scala": ["scala"],
        "php": ["php"],
        "shell": ["sh", "bash"],
        "yaml": ["yaml", "yml"],
        "json": ["json"],
        "markdown": ["md"],
        "html": ["html", "htm"],
        "css": ["css"],
        "sql": ["sql"],
    }

    def _build_grep_args(self, query: str) -> tuple[list[str], bool]:
        """Parse a GitHub-style code search query into ``git grep`` arguments.

        Returns ``(args_list, has_pattern)`` where *args_list* are the extra
        arguments to pass after ``git grep -n -i`` and *has_pattern* indicates
        whether a search pattern was found (False means the caller should
        return empty results).

        Handles ``content:``, ``path:``, ``language:``, and ignores
        ``repo:``, ``org:``, ``is:``, ``NOT``.
        """
        free_text, qualifiers = self._parse_search_qualifiers(query)

        search_terms: list[str] = []
        if free_text:
            import shlex

            try:
                parts = shlex.split(free_text)
            except ValueError:
                parts = [free_text.replace('"', "")]
            search_terms.extend(parts)
        for val in qualifiers.get("content", []):
            search_terms.append(val)

        search_terms = [t.strip() for t in search_terms if t.strip()]
        if not search_terms:
            return [], False

        if len(search_terms) == 1:
            args: list[str] = [search_terms[0], self.default_branch]
        else:
            args: list[str] = []
            for i, term in enumerate(search_terms):
                if i > 0:
                    args.append("--and")
                args.extend(["-e", term])
            args.append(self.default_branch)

        path_filters: list[str] = []
        for p in qualifiers.get("path", []):
            path_filters.append(p)
        for lang in qualifiers.get("language", []):
            exts = self._LANG_EXTENSIONS.get(lang.lower(), [])
            for ext in exts:
                path_filters.append(f"*.{ext}")

        if path_filters:
            args.append("--")
            args.extend(path_filters)

        return args, True

    async def search_code(
        self,
        query: str,
        *,
        owner: str | None = None,
        repo: str | None = None,
        per_page: int = 20,
        page: int = 1,
    ) -> dict[str, Any]:
        """Search code using git grep on the bare repo, returning line-level fragments."""
        if not self.bare_repo_path:
            return {"total_count": 0, "incomplete_results": False, "items": []}

        extra_args, has_pattern = self._build_grep_args(query)
        if not has_pattern:
            return {"total_count": 0, "incomplete_results": False, "items": []}

        ref = self.default_branch
        file_matches: dict[str, list[str]] = {}
        ref_prefix = f"{ref}:"

        if "--and" in extra_args:
            # Multi-term query: file-level AND (matches GitHub semantics).
            # Extract individual terms and the shared suffix (branch + pathspecs).
            terms: list[str] = []
            suffix: list[str] = []
            i = 0
            while i < len(extra_args):
                if extra_args[i] == "-e":
                    terms.append(extra_args[i + 1])
                    i += 2
                elif extra_args[i] == "--and":
                    i += 1
                else:
                    suffix = extra_args[i:]
                    break

            # Intersect files matching each term independently.
            common_files: set[str] | None = None
            for term in terms:
                r = self._git("grep", "-l", "-i", term, *suffix, check=False)
                if r.returncode != 0:
                    return {"total_count": 0, "incomplete_results": False, "items": []}
                files = set(r.stdout.strip().splitlines())
                common_files = files if common_files is None else common_files & files

            if not common_files:
                return {"total_count": 0, "incomplete_results": False, "items": []}

            # Collect line-level matches from the first term in common files.
            for raw_file in sorted(common_files):
                path = raw_file.split(":", 1)[1] if ":" in raw_file else raw_file
                r = self._git("grep", "-n", "-i", terms[0], ref, "--", path, check=False)
                if r.returncode == 0:
                    for raw_line in r.stdout.strip().splitlines():
                        if raw_line.startswith(ref_prefix):
                            raw_line = raw_line[len(ref_prefix) :]
                        parts = raw_line.split(":", 2)
                        if len(parts) >= 3:
                            file_matches.setdefault(parts[0], []).append(parts[2])
        else:
            result = self._git("grep", "-n", "-i", *extra_args, check=False)
            if result.returncode != 0:
                return {"total_count": 0, "incomplete_results": False, "items": []}
            for raw_line in result.stdout.strip().splitlines():
                if raw_line.startswith(ref_prefix):
                    raw_line = raw_line[len(ref_prefix) :]
                parts = raw_line.split(":", 2)
                if len(parts) < 3:
                    continue
                file_matches.setdefault(parts[0], []).append(parts[2])

        items = []
        for fpath, _fragments in file_matches.items():
            fname = os.path.basename(fpath)
            sha_r = self._git("rev-parse", f"{ref}:{fpath}", check=False)
            blob_sha = sha_r.stdout.strip() if sha_r.returncode == 0 else ""
            items.append(
                {
                    "name": fname,
                    "path": fpath,
                    "sha": blob_sha,
                    "url": self._api_url(f"contents/{fpath}?ref={ref}"),
                    "git_url": self._api_url(f"git/blobs/{blob_sha}"),
                    "html_url": self._html_url(f"blob/{ref}/{fpath}"),
                    "repository": self._make_repo_object(),
                    "score": 1.0,
                }
            )

        start = max(page - 1, 0) * per_page
        return {"total_count": len(items), "incomplete_results": False, "items": items[start : start + per_page]}

    # ── Issues (JSON-backed + in-memory) ───────────────────────────────

    def _all_issues(self) -> list[dict[str, Any]]:
        """Return all issues: pre-populated + agent-created."""
        return self._issues + self._created_issues

    def _iter_issue_comment_bodies(self, issue: dict[str, Any]) -> list[str]:
        """Return searchable comment bodies for an issue.

        Includes both fixture-backed ``comments_data`` embedded on the issue
        object and comments added dynamically at runtime, which are stored in
        ``self._comments``.
        """
        issue_number = issue.get("number")
        comment_bodies: list[str] = []
        stored_comments: list[dict[str, Any]] = []

        for comment in issue.get("comments_data", []) or []:
            body = comment.get("body")
            if isinstance(body, str):
                comment_bodies.append(body)

        if isinstance(issue_number, int):
            stored_comments = self._comments.get(issue_number, [])

        for comment in stored_comments:
            body = comment.get("body")
            if isinstance(body, str):
                comment_bodies.append(body)

        return comment_bodies

    async def list_issues(
        self,
        owner: str,
        repo: str,
        *,
        state: str = "open",
        labels: str | None = None,
        assignee: str | None = None,
        sort: str = "created",
        direction: str = "desc",
        per_page: int = 20,
        page: int = 1,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        """List filtered issues."""
        self._check_repo(owner, repo)
        all_issues = self._all_issues()

        if state != "all":
            all_issues = [i for i in all_issues if i.get("state") == state]

        if labels:
            label_set = {lbl.strip().lower() for lbl in labels.split(",")}
            all_issues = [
                i for i in all_issues if label_set & {lbl.get("name", "").lower() for lbl in i.get("labels", [])}
            ]

        if since:
            all_issues = [i for i in all_issues if i.get("updated_at", "") >= since]

        reverse = direction == "desc"
        if sort == "comments":
            all_issues.sort(key=lambda x: x.get("comments", 0), reverse=reverse)
        else:
            sort_key = "updated_at" if sort == "updated" else "created_at"
            all_issues.sort(key=lambda x: x.get(sort_key, ""), reverse=reverse)

        start = (page - 1) * per_page
        return all_issues[start : start + per_page]

    async def get_issue(
        self,
        owner: str,
        repo: str,
        issue_number: int,
    ) -> dict[str, Any]:
        """Get a single issue by number."""
        self._check_repo(owner, repo)
        for issue in self._all_issues():
            if issue.get("number") == issue_number:
                return issue
        raise GitHubAPIError(404, f"Issue #{issue_number} not found")

    async def search_issues(
        self,
        query: str,
        *,
        owner: str | None = None,
        repo: str | None = None,
        per_page: int = 20,
    ) -> dict[str, Any]:
        """Search across all issues by title, body, and comment bodies.

        Supports GitHub search qualifiers (``is:``, ``state:``, ``label:``,
        ``author:``, ``assignee:``).  Unrecognized qualifiers are silently
        ignored but still make the query valid (i.e. qualifier-only queries
        no longer return empty).
        """
        free_text, qualifiers = self._parse_search_qualifiers(query)

        if not free_text and not qualifiers:
            return {"total_count": 0, "items": []}

        matches = list(self._all_issues())

        # --- Apply actionable qualifiers as filters ---

        state_values = {v.lower() for v in qualifiers.get("is", []) + qualifiers.get("state", [])}
        if "pr" in state_values:
            matches = []
        if state_values & {"open", "closed"}:
            matches = [i for i in matches if i.get("state") in state_values]

        if "label" in qualifiers:
            label_set = {v.lower() for v in qualifiers["label"]}
            matches = [i for i in matches if label_set & {l.get("name", "").lower() for l in i.get("labels", [])}]

        if "author" in qualifiers:
            authors = {v.lower() for v in qualifiers["author"]}
            matches = [i for i in matches if (i.get("user") or {}).get("login", "").lower() in authors]

        if "assignee" in qualifiers:
            assignees = {v.lower() for v in qualifiers["assignee"]}
            matches = [
                i for i in matches if any(a.get("login", "").lower() in assignees for a in i.get("assignees", []))
            ]

        # --- Apply free-text substring matching ---

        if free_text:
            q_lower = free_text.lower()
            matches = [
                i
                for i in matches
                if q_lower in (i.get("title") or "").lower()
                or q_lower in (i.get("body") or "").lower()
                or any(q_lower in c.lower() for c in self._iter_issue_comment_bodies(i))
            ]

        return {
            "total_count": len(matches),
            "incomplete_results": False,
            "items": matches[:per_page],
        }

    # ── Pull Requests (in-memory) ──────────────────────────────────────

    async def list_pull_requests(
        self,
        owner: str,
        repo: str,
        *,
        state: str = "open",
        sort: str = "created",
        direction: str = "desc",
        per_page: int = 20,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """List pull requests."""
        self._check_repo(owner, repo)
        prs = self._pull_requests

        if state != "all":
            prs = [p for p in prs if p.get("state") == state]

        reverse = direction == "desc"
        if sort == "popularity":
            prs = sorted(prs, key=lambda x: x.get("comments", 0) + x.get("review_comments", 0), reverse=reverse)
        elif sort == "long-running":
            prs = sorted(prs, key=lambda x: x.get("created_at", ""), reverse=not reverse)
        else:
            sort_key = "updated_at" if sort == "updated" else "created_at"
            prs = sorted(prs, key=lambda x: x.get(sort_key, ""), reverse=reverse)

        start = (page - 1) * per_page
        return prs[start : start + per_page]

    async def get_pull_request(
        self,
        owner: str,
        repo: str,
        pr_number: int,
    ) -> dict[str, Any]:
        """Get a single PR by number, refreshing diff stats from the repo."""
        self._check_repo(owner, repo)
        for pr in self._pull_requests:
            if pr.get("number") == pr_number:
                if pr["state"] == "open":
                    head_ref = pr["head"]["ref"]
                    base_ref = pr["base"]["ref"]
                    a, d, n = self._diff_stats(base_ref, head_ref)
                    pr["additions"] = a
                    pr["deletions"] = d
                    pr["changed_files"] = n
                    sha_r = self._git("rev-parse", f"refs/heads/{head_ref}", check=False)
                    if sha_r.returncode == 0:
                        pr["head"]["sha"] = sha_r.stdout.strip()
                return pr
        raise GitHubAPIError(404, f"PR #{pr_number} not found")

    async def list_pr_reviews(
        self,
        owner: str,
        repo: str,
        pr_number: int,
    ) -> list[dict[str, Any]]:
        """Return reviews for a PR."""
        self._check_repo(owner, repo)
        return self._pr_reviews.get(pr_number, [])

    # ── Write operations (in-memory) ───────────────────────────────────

    async def create_issue(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str | None = None,
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
        milestone: int | None = None,
    ) -> dict[str, Any]:
        """Create an issue in-memory."""
        self._check_repo(owner, repo)
        now = self._now_iso()
        number = self._next_number
        self._next_number += 1
        issue_id = self._next_auto_id()

        enriched_labels = [self._make_label(lbl) for lbl in (labels or [])]
        enriched_assignees = [self._make_user(a) for a in (assignees or [])]

        milestone_obj = None
        if milestone is not None:
            milestone_obj = self._milestones.get(milestone)
            if milestone_obj is None:
                milestone_obj = self._make_milestone(milestone)

        issue: dict[str, Any] = {
            "url": self._api_url(f"issues/{number}"),
            "repository_url": self._api_url(),
            "labels_url": self._api_url(f"issues/{number}/labels{{/name}}"),
            "comments_url": self._api_url(f"issues/{number}/comments"),
            "events_url": self._api_url(f"issues/{number}/events"),
            "html_url": self._html_url(f"issues/{number}"),
            "id": issue_id,
            "node_id": self._node_id("I_", issue_id),
            "number": number,
            "title": title,
            "user": self._make_user(),
            "labels": enriched_labels,
            "state": "open",
            "locked": False,
            "assignee": enriched_assignees[0] if enriched_assignees else None,
            "assignees": enriched_assignees,
            "milestone": milestone_obj,
            "comments": 0,
            "created_at": now,
            "updated_at": now,
            "closed_at": None,
            "body": body,
        }
        self._created_issues.append(issue)
        self._log_action("create_issue", number=number, title=title, body=body, labels=labels or [])
        return issue

    async def add_issue_comment(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        body: str,
    ) -> dict[str, Any]:
        """Add a comment to an issue/PR in-memory."""
        self._check_repo(owner, repo)
        now = self._now_iso()
        comment_id = self._next_auto_id()
        comment = {
            "id": comment_id,
            "node_id": self._node_id("IC_", comment_id),
            "url": self._api_url(f"issues/comments/{comment_id}"),
            "html_url": self._html_url(f"issues/{issue_number}#issuecomment-{comment_id}"),
            "issue_url": self._api_url(f"issues/{issue_number}"),
            "body": body,
            "user": self._make_user(),
            "created_at": now,
            "updated_at": now,
            "author_association": "OWNER",
        }
        self._comments.setdefault(issue_number, []).append(comment)
        comment_count = len(self._comments[issue_number])
        for issue in self._all_issues():
            if issue.get("number") == issue_number:
                issue["comments"] = comment_count
                break
        else:
            for pr in self._pull_requests:
                if pr.get("number") == issue_number:
                    pr["comments"] = comment_count
                    break
        self._log_action("add_comment", issue_number=issue_number, body=body)
        return comment

    async def update_issue(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        state: str | None = None,
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
        milestone: int | None = None,
    ) -> dict[str, Any]:
        """Update an issue in-memory."""
        self._check_repo(owner, repo)
        for issue in self._all_issues():
            if issue.get("number") == issue_number:
                if title is not None:
                    issue["title"] = title
                if body is not None:
                    issue["body"] = body
                if state is not None:
                    issue["state"] = state
                    if state == "closed" and issue.get("closed_at") is None:
                        issue["closed_at"] = self._now_iso()
                    elif state == "open":
                        issue["closed_at"] = None
                if labels is not None:
                    issue["labels"] = [self._make_label(lbl) for lbl in labels]
                if assignees is not None:
                    enriched = [self._make_user(a) for a in assignees]
                    issue["assignees"] = enriched
                    issue["assignee"] = enriched[0] if enriched else None
                if milestone is not None:
                    ms = self._milestones.get(milestone)
                    if ms is None:
                        ms = self._make_milestone(milestone)
                    issue["milestone"] = ms
                issue["updated_at"] = self._now_iso()
                self._log_action(
                    "update_issue",
                    issue_number=issue_number,
                    title=title,
                    body=body,
                    state=state,
                    labels=labels,
                    assignees=assignees,
                )
                return issue
        raise GitHubAPIError(404, f"Issue #{issue_number} not found")

    async def create_pull_request(
        self,
        owner: str,
        repo: str,
        title: str,
        head: str,
        base: str,
        body: str | None = None,
        draft: bool = False,
    ) -> dict[str, Any]:
        """Create a PR in-memory. Validates head branch exists in bare repo."""
        self._check_repo(owner, repo)

        # Verify the head branch exists in the bare repo
        result = self._git("rev-parse", "--verify", f"refs/heads/{head}", check=False)
        if result.returncode != 0:
            raise GitHubAPIError(
                422,
                f"Head branch '{head}' not found. Push it first with: git push -u origin {head}",
            )

        # Verify base branch exists
        result = self._git("rev-parse", "--verify", f"refs/heads/{base}", check=False)
        if result.returncode != 0:
            raise GitHubAPIError(422, f"Base branch '{base}' not found")

        now = self._now_iso()
        number = self._next_number
        self._next_number += 1
        pr_id = self._next_auto_id()

        head_sha_r = self._git("rev-parse", head, check=False)
        head_sha = head_sha_r.stdout.strip() if head_sha_r.returncode == 0 else ""
        base_sha_r = self._git("rev-parse", base, check=False)
        base_sha = base_sha_r.stdout.strip() if base_sha_r.returncode == 0 else ""

        total_add, total_del, n_files = self._diff_stats(base, head)

        pr: dict[str, Any] = {
            "url": self._api_url(f"pulls/{number}"),
            "id": pr_id,
            "node_id": self._node_id("PR_", pr_id),
            "html_url": self._html_url(f"pull/{number}"),
            "diff_url": self._html_url(f"pull/{number}.diff"),
            "patch_url": self._html_url(f"pull/{number}.patch"),
            "issue_url": self._api_url(f"issues/{number}"),
            "number": number,
            "state": "open",
            "locked": False,
            "title": title,
            "user": self._make_user(),
            "body": body,
            "created_at": now,
            "updated_at": now,
            "closed_at": None,
            "merged_at": None,
            "merge_commit_sha": None,
            "assignee": None,
            "assignees": [],
            "requested_reviewers": [],
            "labels": [],
            "head": self._make_pr_ref(head, head_sha),
            "base": self._make_pr_ref(base, base_sha),
            "draft": draft,
            "merged": False,
            "additions": total_add,
            "deletions": total_del,
            "changed_files": n_files,
            "comments": 0,
            "review_comments": 0,
        }

        self._pull_requests.append(pr)
        self._log_action(
            "create_pull_request", number=number, title=title, head=head, base=base, body=body, draft=draft
        )
        return pr

    async def create_branch(
        self,
        owner: str,
        repo: str,
        branch_name: str,
        from_sha: str,
    ) -> dict[str, Any]:
        """Create a branch in the bare repo."""
        self._check_repo(owner, repo)

        result = self._git("branch", branch_name, from_sha, check=False)
        if result.returncode != 0:
            raise GitHubAPIError(422, f"Failed to create branch: {result.stderr.strip()}")
        self._log_action("create_branch", branch_name=branch_name, from_sha=from_sha)
        ref_id = self._next_auto_id()
        return {
            "ref": f"refs/heads/{branch_name}",
            "node_id": self._node_id("REF_", ref_id),
            "url": self._api_url(f"git/refs/heads/{branch_name}"),
            "object": {
                "sha": from_sha,
                "type": "commit",
                "url": self._api_url(f"git/commits/{from_sha}"),
            },
        }

    # ── File write operations ──────────────────────────────────────────

    async def create_or_update_file(
        self,
        owner: str,
        repo: str,
        path: str,
        content: str,
        message: str,
        branch: str,
        sha: str | None = None,
    ) -> dict[str, Any]:
        """Create or update a single file via git plumbing (matches GitHub Contents API)."""
        self._check_repo(owner, repo)

        commit_sha, blob_shas = self._commit_files(branch, [(path, content)], message)

        self._log_action(
            "create_or_update_file",
            path=path,
            branch=branch,
            message=message,
        )

        blob_sha = blob_shas[path]
        api_content = self._api_url(f"contents/{path}")
        commit_id = self._next_auto_id()
        git_env = self._git_env()
        now = self._now_iso()
        author_info = {"name": git_env["GIT_AUTHOR_NAME"], "email": git_env["GIT_AUTHOR_EMAIL"], "date": now}

        tree_result = self._git("rev-parse", f"{commit_sha}^{{tree}}", check=False)
        tree_sha = tree_result.stdout.strip() if tree_result.returncode == 0 else ""

        parent_result = self._git("rev-parse", f"{commit_sha}^", check=False)
        parent_sha = parent_result.stdout.strip() if parent_result.returncode == 0 else ""

        encoded_content = base64.b64encode(content.encode()).decode()
        return {
            "content": {
                "name": os.path.basename(path),
                "path": path,
                "sha": blob_sha,
                "size": len(content),
                "url": f"{api_content}?ref={branch}",
                "html_url": self._html_url(f"blob/{branch}/{path}"),
                "git_url": self._api_url(f"git/blobs/{blob_sha}"),
                "download_url": (
                    f"https://raw.githubusercontent.com/{self.repo_owner}/{self.repo_name}/{branch}/{path}"
                ),
                "type": "file",
                "content": encoded_content,
                "encoding": "base64",
                "_links": {
                    "self": api_content,
                    "git": self._api_url(f"git/blobs/{blob_sha}"),
                    "html": self._html_url(f"blob/{branch}/{path}"),
                },
            },
            "commit": {
                "sha": commit_sha,
                "node_id": self._node_id("C_", commit_id),
                "url": self._api_url(f"git/commits/{commit_sha}"),
                "html_url": self._html_url(f"commit/{commit_sha}"),
                "author": author_info,
                "committer": author_info,
                "message": message,
                "tree": {
                    "sha": tree_sha,
                    "url": self._api_url(f"git/trees/{tree_sha}"),
                },
                "parents": [
                    {
                        "sha": parent_sha,
                        "url": self._api_url(f"git/commits/{parent_sha}"),
                        "html_url": self._html_url(f"commit/{parent_sha}"),
                    },
                ]
                if parent_sha
                else [],
            },
        }

    async def push_files(
        self,
        owner: str,
        repo: str,
        branch: str,
        files: list[dict[str, str]],
        message: str,
    ) -> dict[str, Any]:
        """Push multiple files in a single commit (matches createTree + createCommit + updateRef)."""
        self._check_repo(owner, repo)

        file_changes = [(f["path"], f["content"]) for f in files]
        commit_sha, _blob_shas = self._commit_files(branch, file_changes, message)

        self._log_action(
            "push_files",
            branch=branch,
            message=message,
            files_count=len(files),
        )
        ref_id = self._next_auto_id()
        return {
            "ref": f"refs/heads/{branch}",
            "node_id": self._node_id("REF_", ref_id),
            "url": self._api_url(f"git/refs/heads/{branch}"),
            "object": {
                "sha": commit_sha,
                "type": "commit",
                "url": self._api_url(f"git/commits/{commit_sha}"),
            },
        }

    # ── PR merge / update / review ─────────────────────────────────────

    async def merge_pull_request(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        commit_title: str | None = None,
        commit_message: str | None = None,
        merge_method: str | None = None,
    ) -> dict[str, Any]:
        """Merge a PR (merge, squash, or rebase) using an actual git worktree merge."""
        self._check_repo(owner, repo)
        pr = await self.get_pull_request(owner, repo, pr_number)

        if pr.get("state") != "open":
            raise GitHubAPIError(422, f"PR #{pr_number} is not open")
        if pr.get("merged"):
            raise GitHubAPIError(422, f"PR #{pr_number} is already merged")

        base_branch = pr["base"]["ref"]
        head_branch = pr["head"]["ref"]
        method = merge_method or "merge"
        title = commit_title or f"Merge pull request #{pr_number}"
        msg = f"{title}\n\n{commit_message}" if commit_message else title

        merge_sha = self._merge_branches(base_branch, head_branch, msg, method)

        now = self._now_iso()
        pr["state"] = "closed"
        pr["merged"] = True
        pr["merged_at"] = now
        pr["closed_at"] = now
        pr["merge_commit_sha"] = merge_sha
        pr["updated_at"] = now

        self._log_action(
            "merge_pull_request",
            pr_number=pr_number,
            merge_method=method,
            merge_sha=merge_sha,
        )
        return {
            "sha": merge_sha,
            "merged": True,
            "message": "Pull Request successfully merged",
        }

    async def update_pull_request_branch(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        expected_head_sha: str | None = None,
    ) -> dict[str, Any]:
        """Update a PR branch by merging the base branch into the head branch."""
        self._check_repo(owner, repo)
        pr = await self.get_pull_request(owner, repo, pr_number)

        head_branch = pr["head"]["ref"]
        base_branch = pr["base"]["ref"]

        if expected_head_sha and pr["head"]["sha"] != expected_head_sha:
            raise GitHubAPIError(422, "Expected head SHA does not match")

        msg = f"Merge branch '{base_branch}' into {head_branch}"
        self._merge_branches(head_branch, base_branch, msg)

        sha_result = self._git("rev-parse", f"refs/heads/{head_branch}")
        pr["head"]["sha"] = sha_result.stdout.strip()
        pr["updated_at"] = self._now_iso()

        self._log_action("update_pull_request_branch", pr_number=pr_number)
        return {
            "message": "Updating pull request branch.",
            "url": self._html_url(f"pull/{pr_number}"),
        }

    async def create_pull_request_review(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        body: str,
        event: str,
        commit_id: str | None = None,
        comments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Submit a pull request review, stored in mock state."""
        self._check_repo(owner, repo)
        pr = await self.get_pull_request(owner, repo, pr_number)

        now = self._now_iso()
        reviews = self._pr_reviews.setdefault(pr_number, [])
        review_id = self._next_auto_id()

        resolved_commit = commit_id or pr["head"]["sha"]
        state_map = {
            "APPROVE": "APPROVED",
            "REQUEST_CHANGES": "CHANGES_REQUESTED",
            "COMMENT": "COMMENTED",
        }
        review: dict[str, Any] = {
            "id": review_id,
            "node_id": self._node_id("PRR_", review_id),
            "user": self._make_user(),
            "body": body,
            "state": state_map.get(event, event),
            "html_url": self._html_url(
                f"pull/{pr_number}#pullrequestreview-{review_id}",
            ),
            "pull_request_url": self._api_url(f"pulls/{pr_number}"),
            "commit_id": resolved_commit,
            "submitted_at": now,
            "author_association": "OWNER",
        }
        reviews.append(review)

        if comments:
            rc_list = self._pr_review_comments.setdefault(pr_number, [])
            for c in comments:
                cid = self._next_auto_id()
                comment_url = self._api_url(f"pulls/comments/{cid}")
                comment_html = self._html_url(f"pull/{pr_number}#discussion_r{cid}")
                pr_url = self._api_url(f"pulls/{pr_number}")
                rc_list.append(
                    {
                        "url": comment_url,
                        "pull_request_review_id": review_id,
                        "id": cid,
                        "node_id": self._node_id("PRRC_", cid),
                        "diff_hunk": c.get("diff_hunk", ""),
                        "path": c.get("path"),
                        "position": c.get("position"),
                        "original_position": c.get("position"),
                        "commit_id": resolved_commit,
                        "original_commit_id": resolved_commit,
                        "user": self._make_user(),
                        "body": c.get("body", ""),
                        "created_at": now,
                        "updated_at": now,
                        "html_url": comment_html,
                        "pull_request_url": pr_url,
                        "author_association": "OWNER",
                        "_links": {
                            "self": {"href": comment_url},
                            "html": {"href": comment_html},
                            "pull_request": {"href": pr_url},
                        },
                    }
                )

        pr_obj = next((p for p in self._pull_requests if p["number"] == pr_number), None)
        if pr_obj is not None:
            pr_obj["review_comments"] = len(self._pr_review_comments.get(pr_number, []))

        self._log_action(
            "create_pull_request_review",
            pr_number=pr_number,
            event=event,
            body=body,
        )
        return review

    # ── Search users ───────────────────────────────────────────────────

    def _all_known_logins(self) -> set[str]:
        """Collect every user login mentioned anywhere in mock data."""
        logins: set[str] = set()
        logins.add(self._user.get("login", "agent-bot"))
        for issue in self._all_issues():
            if u := issue.get("user"):
                if lg := u.get("login"):
                    logins.add(lg)
            for a in issue.get("assignees", []):
                if lg := a.get("login"):
                    logins.add(lg)
        for pr in self._pull_requests:
            if u := pr.get("user"):
                if lg := u.get("login"):
                    logins.add(lg)
        for review_list in self._pr_reviews.values():
            for review in review_list:
                if u := review.get("user"):
                    if lg := u.get("login"):
                        logins.add(lg)
        for comment_list in self._comments.values():
            for comment in comment_list:
                if u := comment.get("user"):
                    if lg := u.get("login"):
                        logins.add(lg)
        return logins

    async def search_users(
        self,
        query: str,
        *,
        per_page: int = 30,
    ) -> dict[str, Any]:
        """Search users — matches against all users found in mock data."""
        q_lower, _ = self._parse_search_qualifiers(query)
        q_lower = q_lower.lower()
        if not q_lower:
            return {"total_count": 0, "incomplete_results": False, "items": []}

        matches = []
        for login in self._all_known_logins():
            if q_lower in login.lower():
                matches.append(self._make_user(login))

        return {
            "total_count": len(matches),
            "incomplete_results": False,
            "items": matches[:per_page],
        }

    # ── PR commit status ───────────────────────────────────────────────

    async def get_pull_request_status(
        self,
        owner: str,
        repo: str,
        pr_number: int,
    ) -> dict[str, Any]:
        """Return combined commit status + check runs for a PR's head SHA."""
        self._check_repo(owner, repo)
        pr = await self.get_pull_request(owner, repo, pr_number)
        sha = pr["head"]["sha"]
        return {
            "state": "pending",
            "statuses": [],
            "sha": sha,
            "total_count": 0,
        }

    # ── Repository creation / forking ──────────────────────────────────

    async def create_repository(
        self,
        name: str,
        description: str | None = None,
        private: bool = False,
        auto_init: bool = False,
    ) -> dict[str, Any]:
        """Create a new bare git repo on disk and track metadata."""
        user_login = self._user.get("login", "agent-bot")
        full_name = f"{user_login}/{name}"

        parent_dir = os.path.dirname(self.bare_repo_path) if self.bare_repo_path else tempfile.gettempdir()
        repo_path = os.path.join(parent_dir, f"{name}.git")
        subprocess.run(
            ["git", "init", "--bare", repo_path],
            capture_output=True,
            text=True,
            check=True,
        )

        if auto_init:
            with tempfile.TemporaryDirectory() as tmpdir:
                work_path = os.path.join(tmpdir, "init")
                env = {**os.environ, **self._git_env()}
                subprocess.run(
                    ["git", "clone", repo_path, work_path],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                readme_path = os.path.join(work_path, "README.md")
                with open(readme_path, "w") as f:
                    f.write(f"# {name}\n\n{description or ''}\n")
                subprocess.run(
                    ["git", "-C", work_path, "add", "."],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                subprocess.run(
                    ["git", "-C", work_path, "commit", "-m", "Initial commit"],
                    capture_output=True,
                    text=True,
                    check=True,
                    env=env,
                )
                subprocess.run(
                    ["git", "-C", work_path, "push"],
                    capture_output=True,
                    text=True,
                    check=True,
                )

        self.allowed_repos.append(full_name.lower())
        now = self._now_iso()
        repo_id = abs(hash(full_name)) % 100000
        repo_meta: dict[str, Any] = {
            "id": repo_id,
            "node_id": self._node_id("R_", repo_id),
            "name": name,
            "full_name": full_name,
            "private": private,
            "owner": self._make_owner(user_login),
            "html_url": f"https://github.com/{full_name}",
            "description": description,
            "fork": False,
            "url": f"https://api.github.com/repos/{full_name}",
            "created_at": now,
            "updated_at": now,
            "pushed_at": now,
            "git_url": f"git://github.com/{full_name}.git",
            "ssh_url": f"git@github.com:{full_name}.git",
            "clone_url": f"https://github.com/{full_name}.git",
            "default_branch": "main",
        }

        self._log_action(
            "create_repository",
            name=name,
            description=description,
            private=private,
        )
        return repo_meta

    async def fork_repository(
        self,
        owner: str,
        repo: str,
        organization: str | None = None,
    ) -> dict[str, Any]:
        """Fork (bare-clone) the current repo and track metadata."""
        self._check_repo(owner, repo)
        repo_data = await self.get_repo(owner, repo)

        user_login = organization or self._user.get("login", "agent-bot")
        fork_name = repo_data.get("name", repo)
        full_name = f"{user_login}/{fork_name}"

        parent_dir = os.path.dirname(self.bare_repo_path) if self.bare_repo_path else tempfile.gettempdir()
        fork_path = os.path.join(parent_dir, f"{fork_name}-fork.git")
        assert self.bare_repo_path is not None, "bare_repo_path must be set to fork"
        subprocess.run(
            ["git", "clone", "--bare", self.bare_repo_path, fork_path],
            capture_output=True,
            text=True,
            check=True,
        )

        self.allowed_repos.append(full_name.lower())
        now = self._now_iso()
        fork_id = abs(hash(full_name)) % 100000
        default_br = repo_data.get("default_branch", "main")
        fork_meta: dict[str, Any] = {
            "id": fork_id,
            "node_id": self._node_id("R_", fork_id),
            "name": fork_name,
            "full_name": full_name,
            "private": repo_data.get("private", False),
            "owner": self._make_owner(user_login),
            "html_url": f"https://github.com/{full_name}",
            "description": repo_data.get("description"),
            "fork": True,
            "url": f"https://api.github.com/repos/{full_name}",
            "created_at": now,
            "updated_at": now,
            "pushed_at": now,
            "git_url": f"git://github.com/{full_name}.git",
            "ssh_url": f"git@github.com:{full_name}.git",
            "clone_url": f"https://github.com/{full_name}.git",
            "default_branch": default_br,
            "parent": self._make_repo_object(owner, repo_data.get("name", repo)),
            "source": self._make_repo_object(owner, repo_data.get("name", repo)),
        }

        self._log_action(
            "fork_repository",
            owner=owner,
            repo=repo,
            organization=organization,
        )
        return fork_meta

    # ── README ─────────────────────────────────────────────────────────

    # ── PR review comments ────────────────────────────────────────────

    async def list_pr_review_comments(
        self,
        owner: str,
        repo: str,
        pr_number: int,
    ) -> list[dict[str, Any]]:
        """Return review comments for a PR."""
        self._check_repo(owner, repo)
        return self._pr_review_comments.get(pr_number, [])

    # ── Branch detail ─────────────────────────────────────────────────

    async def get_branch(
        self,
        owner: str,
        repo: str,
        branch: str,
    ) -> dict[str, Any]:
        """Get branch detail (name + head commit SHA)."""
        self._check_repo(owner, repo)

        result = self._git("rev-parse", f"refs/heads/{branch}", check=False)
        if result.returncode != 0:
            raise GitHubAPIError(404, f"Branch '{branch}' not found")
        sha = result.stdout.strip()
        return {
            "name": branch,
            "commit": {
                "sha": sha,
                "url": self._api_url(f"commits/{sha}"),
            },
            "protected": False,
        }
