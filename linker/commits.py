"""Find commits in the repos a ticket is about.

Which repos ("Auto"):
  * the Jira project's mapping (PROJECT_REPOS, e.g. ASEARCH -> marketplace, marketplace-search-api)
  * any repo from REPO_CHOICES the ticket names (summary, labels, components, description)
  * repos of the PRs on the ticket's Jira development panel
A repo picked in the UI (or --repo) replaces the guess.

Which commits, per repo:
  * commit messages naming the ticket or its closest related tickets
  * commit messages matching the ticket's keywords
  * the recent history of the files the ticket's own PRs changed
GitHub's commit search only covers each repo's default branch.
"""
from __future__ import annotations

import re
from typing import Callable

from linker import Settings
from linker.github import PR_URL_RE, Commit, GitHubClient, GitHubError
from linker.jira import Issue

MAX_REPOS = 3
MAX_COMMITS = 20
AUTO = "Auto (from ticket)"


def guess_repos(target: Issue | None, settings: Settings, pr_urls: list[str]) -> list[str]:
    """Short repo names the ticket is about, most likely first."""
    found: list[str] = []
    if target is not None:
        found += settings.project_repos.get(target.project.upper(), [])
        haystack = " ".join([target.summary, " ".join(target.labels), " ".join(target.components),
                             target.description[:3000]]).lower()
        # Longest names first so "marketplace-ssr" wins over "marketplace" inside it.
        for name in sorted(settings.repo_choices, key=len, reverse=True):
            if re.search(rf"(?<![\w-]){re.escape(name.lower())}(?![\w-])", haystack):
                found.append(name)
    for url in pr_urls:
        match = PR_URL_RE.search(url)
        if match:
            found.append(match.group(2))
    return list(dict.fromkeys(found))[:MAX_REPOS]


def chosen_repos(choice: str | None, target: Issue | None, settings: Settings, pr_urls: list[str]) -> list[str]:
    """`choice` is the dropdown value: blank/Auto guesses; anything else is one or more repo names."""
    if choice and choice.strip() and choice.strip() != AUTO:
        return [r.strip() for r in re.split(r"[,\s]+", choice) if r.strip()][:MAX_REPOS]
    return guess_repos(target, settings, pr_urls)


def _full_name(gh: GitHubClient, org: str, name: str) -> str | None:
    if "/" in name:
        return name
    matches = gh.find_repos(org, name, limit=1)
    return matches[0] if matches else None


def collect_commits(
    gh: GitHubClient,
    org: str,
    repos: list[str],
    *,
    keys: list[str],
    keywords: list[str],
    trace_prs: list[str],
    say: Callable[[str], None],
    log: list[str],
    notes: list[str],
    skip: set[str] | None = None,
) -> list[Commit]:
    """Search, rank and read commits. `trace_prs` are PR urls whose files' history is traced;
    `skip` holds commit urls already in the evidence."""
    skip = skip or set()
    found: dict[str, Commit] = {}

    def add(commit: Commit, source: str) -> None:
        if not commit.url or commit.url in skip:
            return
        existing = found.get(commit.url)
        if existing is None:
            commit.sources = [source]
            found[commit.url] = commit
        elif source not in existing.sources:
            existing.sources.append(source)

    def search(query: str, source: str, limit: int = 10) -> None:
        try:
            hits = gh.search_commits(query, limit)
        except GitHubError as exc:
            notes.append(f"Commit search failed ({source}): {exc}")
            return
        log.append(f"{len(hits):>3}  commits {source}  [{query}]")
        for commit in hits:
            add(commit, source)

    full_names = []
    for name in repos[:MAX_REPOS]:
        full = _full_name(gh, org, name) if org else (name if "/" in name else None)
        if full:
            full_names.append(full)
        else:
            notes.append(f'Commit scan: no repo matched "{name}"' + ("" if org else " (set GITHUB_ORG)"))
    if not full_names:
        return []
    say("scanning commits in " + ", ".join(full_names))

    keys = list(dict.fromkeys(k for k in keys if k))[:5]
    words = [w for w in dict.fromkeys(keywords) if w][:3]
    for repo in full_names:
        short = repo.split("/")[-1]
        if keys:
            search(f"repo:{repo} " + " OR ".join(keys), f"{short} naming ticket keys")
        if words:
            phrase = " OR ".join(f'"{w}"' if " " in w else w for w in words)
            search(f"repo:{repo} {phrase}", f"{short} matching keywords", 8)

    # History of the files the ticket's own PRs changed, in the repos being scanned.
    paths: dict[str, list[str]] = {}
    for url in trace_prs[:3]:
        match = PR_URL_RE.search(url)
        if not match or f"{match.group(1)}/{match.group(2)}" not in full_names:
            continue
        repo = f"{match.group(1)}/{match.group(2)}"
        try:
            files = gh.pr_files(repo, int(match.group(3)))
        except GitHubError:
            continue
        code = [f for f in files if not re.search(r"(^|/)(tests?|spec|fixtures?)/|\.(md|lock|snap)$", f, re.I)]
        paths.setdefault(repo, []).extend(code[:3])
    for repo, files in paths.items():
        for path in list(dict.fromkeys(files))[:3]:
            try:
                hits = gh.path_commits(repo, path, 5)
            except GitHubError as exc:
                notes.append(f"History of {path} failed: {exc}")
                continue
            log.append(f"{len(hits):>3}  commits touching {repo.split('/')[-1]}:{path}")
            for commit in hits:
                add(commit, f"touches {path}")

    # Key mentions first, then commits several searches agree on, then the newest.
    def rank(c: Commit) -> tuple[int, int, str]:
        names_key = any("ticket keys" in s for s in c.sources)
        return (1 if names_key else 0, len(c.sources), c.date)

    ranked = sorted(found.values(), key=rank, reverse=True)[:MAX_COMMITS]
    if len(found) > MAX_COMMITS:
        notes.append(f"{len(found)} commits found; read the top {MAX_COMMITS}.")
    loaded = []
    for i, commit in enumerate(ranked, 1):
        say(f"reading commit {i}/{len(ranked)}: {commit.repo.split('/')[-1]}@{commit.sha[:7]}")
        try:
            loaded.append(gh.load_commit(commit))
        except GitHubError as exc:
            notes.append(f"Could not read {commit.url}: {exc}")
            loaded.append(commit)
    return loaded


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + " …"


def describe_commit(c: Commit, *, with_patches: bool) -> str:
    lines = [
        f"[{c.repo.split('/')[-1]}@{c.sha[:7]}] {c.headline}",
        f"    url: {c.url}",
        f"    {c.author or '?'} | {c.date or '?'} | found via: {'; '.join(c.sources)}",
    ]
    body = " ".join(c.message.strip().splitlines()[1:]).strip()
    if body:
        lines.append("    message: " + _clip(" ".join(body.split()), 600))
    if c.files:
        lines.append("    files: " + ", ".join(f"{n} (+{a}/-{d})" for n, a, d in c.files))
    if with_patches:
        for name, patch in c.patches:
            lines.append(f"    --- diff {name}\n" + "\n".join("      " + l for l in patch.splitlines()))
    return "\n".join(lines)


def commit_label(c: Commit) -> str:
    return f"{c.repo.split('/')[-1]}@{c.sha[:7]}"
