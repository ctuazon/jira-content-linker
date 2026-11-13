"""Read-only GitHub access, signed in the same way as the GitHub Code Scanner:

  * GITHUB_AUTH=auto (default): your Git Credential Manager login first, GITHUB_TOKEN as fallback
  * GITHUB_AUTH=git: Git login only
  * GITHUB_AUTH=token: GITHUB_TOKEN only

The Git login is read with `git credential fill`, the same helper `git clone` uses. GCM keeps an
OAuth token that is usually SSO-authorized for the org, so private repos are visible without
creating a personal access token.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import requests

from linker import Settings

PR_URL_RE = re.compile(r"https?://[^/\s]+/([\w.-]+)/([\w.-]+)/pull/(\d+)")
CREATE_NO_WINDOW = 0x08000000


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def web_host(api_url: str) -> str:
    host = urlparse(api_url).hostname or "github.com"
    return "github.com" if host == "api.github.com" else host


def git_credential_token(api_url: str, interactive: bool = False) -> str:
    """The GitHub login git's credential helper already stores ("" when there is none).

    interactive=True lets Git Credential Manager open its sign-in window."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if not interactive:
        env["GCM_INTERACTIVE"] = "never"
    kwargs: dict[str, Any] = {"creationflags": CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    try:
        completed = subprocess.run(
            ["git", "credential", "fill"],
            input=f"protocol=https\nhost={web_host(api_url)}\n\n".encode(),
            capture_output=True, timeout=300 if interactive else 30, env=env, check=False, **kwargs,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if completed.returncode != 0:
        return ""
    fields = dict(
        line.split("=", 1) for line in completed.stdout.decode("utf-8", "replace").splitlines() if "=" in line
    )
    return fields.get("password", "")


def resolve_token(settings: Settings, interactive: bool = False) -> tuple[str, str]:
    """(token, where it came from). Token is "" when nothing is available."""
    if settings.github_auth in ("auto", "git"):
        token = git_credential_token(settings.github_api_url, interactive=interactive)
        if token:
            return token, "Git login"
    if settings.github_auth in ("auto", "token") and settings.github_token:
        return settings.github_token, "GITHUB_TOKEN"
    return "", ""


@dataclass
class PullRequest:
    repo: str  # owner/name
    number: int
    title: str
    url: str
    state: str = ""
    author: str = ""
    created: str = ""
    merged: str = ""
    base: str = ""
    head: str = ""
    body: str = ""
    files: list[tuple[str, int, int]] = field(default_factory=list)  # (path, additions, deletions)
    patches: list[tuple[str, str]] = field(default_factory=list)  # (path, clipped patch)
    sources: list[str] = field(default_factory=list)


class GitHubClient:
    def __init__(self, token: str, api_url: str = "https://api.github.com") -> None:
        self.api = api_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "jira-context-linker",
        })
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        self._repo_cache: dict[str, list[str]] = {}

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = path if path.startswith("http") else self.api + path
        for attempt in range(4):
            try:
                response = self.session.get(url, params=params, timeout=30)
            except requests.RequestException as exc:
                if attempt == 3:
                    raise GitHubError(0, f"Could not reach GitHub: {exc}") from exc
                time.sleep(2 ** attempt)
                continue
            if response.status_code in (403, 429) and "rate limit" in response.text.lower():
                # Search allows 30 requests a minute; wait it out briefly rather than failing.
                reset = response.headers.get("Retry-After") or response.headers.get("X-RateLimit-Reset")
                delay = 60.0
                if reset and reset.isdigit():
                    delay = float(reset) if int(reset) < 10_000 else max(1.0, int(reset) - time.time())
                if delay > 90 or attempt == 3:
                    raise GitHubError(response.status_code, "GitHub rate limit reached; try again in a few minutes.")
                time.sleep(delay + 1)
                continue
            if response.status_code >= 500 and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            if not response.ok:
                raise GitHubError(response.status_code, f"GitHub {response.status_code} for {url}: {response.text[:200]}")
            return response.json()
        raise GitHubError(0, f"GitHub gave up on {url}")

    def whoami(self) -> str:
        return str(self.get("/user").get("login") or "")

    # --- repos ---------------------------------------------------------------

    def find_repos(self, org: str, name: str, limit: int = 3) -> list[str]:
        """Repos in the org whose name best matches `name` (exact name first)."""
        cache_key = f"{org}/{name}".lower()
        if cache_key in self._repo_cache:
            return self._repo_cache[cache_key]
        found: list[str] = []
        try:
            exact = self.get(f"/repos/{org}/{name}")
            found.append(str(exact["full_name"]))
        except GitHubError:
            pass
        if len(found) < limit:
            try:
                result = self.get("/search/repositories", {"q": f"{name} in:name org:{org}", "per_page": 10})
                for item in result.get("items") or []:
                    full = str(item["full_name"])
                    if full not in found and not item.get("archived"):
                        found.append(full)
            except GitHubError:
                pass
        self._repo_cache[cache_key] = found[:limit]
        return found[:limit]

    # --- pull requests ---------------------------------------------------------

    def search_prs(self, query: str, limit: int = 10) -> list[PullRequest]:
        result = self.get("/search/issues", {"q": f"{query} is:pr", "per_page": limit, "sort": "updated"})
        prs = []
        for item in result.get("items") or []:
            match = PR_URL_RE.search(str(item.get("html_url") or ""))
            if not match:
                continue
            prs.append(PullRequest(
                repo=f"{match.group(1)}/{match.group(2)}",
                number=int(match.group(3)),
                title=str(item.get("title") or ""),
                url=str(item["html_url"]),
                state=str(item.get("state") or ""),
                author=str((item.get("user") or {}).get("login") or ""),
                created=str(item.get("created_at") or "")[:10],
            ))
        return prs

    def load_pr(self, pr: PullRequest, *, max_files: int = 40, max_patches: int = 6, patch_chars: int = 1500) -> PullRequest:
        """Fill in body, branches, changed files and a few clipped diffs."""
        data = self.get(f"/repos/{pr.repo}/pulls/{pr.number}")
        pr.title = str(data.get("title") or pr.title)
        pr.body = str(data.get("body") or "")
        pr.state = "merged" if data.get("merged_at") else str(data.get("state") or pr.state)
        pr.author = str((data.get("user") or {}).get("login") or pr.author)
        pr.created = str(data.get("created_at") or "")[:10]
        pr.merged = str(data.get("merged_at") or "")[:10]
        pr.base = str((data.get("base") or {}).get("ref") or "")
        pr.head = str((data.get("head") or {}).get("ref") or "")
        files = self.get(f"/repos/{pr.repo}/pulls/{pr.number}/files", {"per_page": 100})
        pr.files = _file_stats(files)[:max_files]
        pr.patches = _pick_patches(files, max_patches, patch_chars)
        return pr

    def pr_files(self, repo: str, number: int) -> list[str]:
        """Just the changed paths of a PR (one request; used to find which files to trace)."""
        return [str(f.get("filename")) for f in self.get(f"/repos/{repo}/pulls/{number}/files", {"per_page": 100})]

    # --- commits ---------------------------------------------------------------

    def search_commits(self, query: str, limit: int = 10) -> list["Commit"]:
        """Commit-message search. GitHub only indexes each repo's default branch."""
        result = self.get("/search/commits", {"q": query, "per_page": limit, "sort": "committer-date"})
        return [_commit_from_api(item, str((item.get("repository") or {}).get("full_name") or ""))
                for item in result.get("items") or []]

    def path_commits(self, repo: str, path: str, limit: int = 5) -> list["Commit"]:
        """Most recent commits touching one file on the default branch."""
        return [_commit_from_api(item, repo) for item in self.get(f"/repos/{repo}/commits", {"path": path, "per_page": limit})]

    def load_commit(self, commit: "Commit", *, max_files: int = 30, max_patches: int = 4, patch_chars: int = 1200) -> "Commit":
        data = self.get(f"/repos/{commit.repo}/commits/{commit.sha}")
        files = data.get("files") or []
        commit.message = str((data.get("commit") or {}).get("message") or commit.message)
        commit.files = _file_stats(files)[:max_files]
        commit.patches = _pick_patches(files, max_patches, patch_chars)
        return commit


def _file_stats(files: list[dict[str, Any]]) -> list[tuple[str, int, int]]:
    return [(str(f.get("filename")), int(f.get("additions") or 0), int(f.get("deletions") or 0)) for f in files]


def _pick_patches(files: list[dict[str, Any]], max_patches: int, patch_chars: int) -> list[tuple[str, str]]:
    """A few clipped diffs. Config/manifests usually show how services are wired together, so
    those come first, then the smallest changes."""
    def priority(f: dict[str, Any]) -> tuple[int, int]:
        name = str(f.get("filename") or "").lower()
        wiring = any(k in name for k in (
            "composer.json", "package.json", ".env", "config", ".yml", ".yaml", "docker", "routes",
            "client", "api", "service", "webhook", "queue", "event", "terraform", ".tf",
        ))
        return (0 if wiring else 1, int(f.get("changes") or 0))
    out = []
    for f in sorted((f for f in files if f.get("patch")), key=priority)[:max_patches]:
        patch = str(f["patch"])
        out.append((str(f["filename"]), patch if len(patch) <= patch_chars else patch[:patch_chars] + "\n…[clipped]"))
    return out


@dataclass
class Commit:
    repo: str  # owner/name
    sha: str
    url: str
    message: str = ""
    author: str = ""
    date: str = ""
    files: list[tuple[str, int, int]] = field(default_factory=list)
    patches: list[tuple[str, str]] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    @property
    def headline(self) -> str:
        return (self.message.strip().splitlines() or [""])[0][:120]


def _commit_from_api(item: dict[str, Any], repo: str) -> Commit:
    info = item.get("commit") or {}
    author = info.get("author") or {}
    login = (item.get("author") or {}).get("login") if isinstance(item.get("author"), dict) else ""
    return Commit(
        repo=repo,
        sha=str(item.get("sha") or ""),
        url=str(item.get("html_url") or ""),
        message=str(info.get("message") or ""),
        author=str(login or author.get("name") or ""),
        date=str(author.get("date") or "")[:10],
    )


def pr_from_url(url: str, source: str) -> PullRequest | None:
    match = PR_URL_RE.search(url or "")
    if not match:
        return None
    return PullRequest(
        repo=f"{match.group(1)}/{match.group(2)}", number=int(match.group(3)),
        title="", url=match.group(0), sources=[source],
    )


def connect(settings: Settings, interactive: bool = False) -> tuple[GitHubClient, str]:
    token, source = resolve_token(settings, interactive=interactive)
    if not token:
        raise GitHubError(401, "No GitHub login found. Use Help > Sign in to GitHub, or set GITHUB_TOKEN in .env.")
    return GitHubClient(token, settings.github_api_url), source
