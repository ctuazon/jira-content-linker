"""One ticket -> one saved report. Shared by the command line (jcl.py) and the desktop UI (gui.py)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from linker import REPORTS_DIR, Settings
from linker.analyze import (
    ANALYSIS_SCHEMA,
    PLAN_SCHEMA,
    build_analysis_prompt,
    build_plan_prompt,
    clean_analysis,
)
from linker.claude import ClaudeError, ask_json
from linker.commits import chosen_repos, collect_commits
from linker.gather import Context, fallback_plan, gather, plan_from_json
from linker.github import PR_URL_RE, GitHubError, connect
from linker.jira import JiraClient, normalize_key
from linker.report import render


class BadKeyError(ValueError):
    pass


@dataclass
class ReportResult:
    key: str
    summary: str
    path: Path
    markdown: str


def scan_ticket_commits(ctx: Context, settings: Settings, keywords: list[str], repo_choice: str | None,
                        say: Callable[[str], None]) -> list[str]:
    """Fill ctx.commits from the repos the ticket is about; returns the repos scanned.
    Never fails the report."""
    pr_urls = [d.url for d in ctx.documents.values() if PR_URL_RE.search(d.url)]
    repos = chosen_repos(repo_choice, ctx.target, settings, pr_urls)
    if not repos:
        ctx.log.append("  -  commits: no repo picked or guessed (choose one in the Repo list)")
        say("commit scan skipped: no repo picked or guessed")
        return []
    try:
        gh, _source = connect(settings)
    except GitHubError as exc:
        ctx.log.append(f"  -  commits: GitHub unavailable ({exc})")
        say(f"commit scan skipped: {exc}")
        return []
    org = settings.github_org or next((PR_URL_RE.search(u).group(1) for u in pr_urls), "")
    keys = [ctx.target.key] + [i.key for i in ctx.ranked_candidates()[:6]]
    notes: list[str] = []
    commits = collect_commits(gh, org, repos, keys=keys, keywords=keywords, trace_prs=pr_urls,
                              say=say, log=ctx.log, notes=notes)
    ctx.log.extend(notes)
    ctx.commits = {c.url: c for c in commits}
    say(f"{len(commits)} commits read")
    return repos


def build_report(
    raw_key: str,
    settings: Settings,
    client: JiraClient,
    *,
    say: Callable[[str], None],
    use_claude: bool = True,
    confluence: bool = True,
    model: str | None = None,
    write_json: bool = False,
    scan_commits: bool = True,
    repo_choice: str | None = None,
) -> ReportResult:
    """Fetch, gather, analyse and write the report. Raises BadKeyError or JiraError."""
    key = normalize_key(raw_key)
    if not key:
        raise BadKeyError(f"'{raw_key}' doesn't look like a Jira key (e.g. ABC-123).")
    say(f"fetching {key}")
    target = client.fetch(key)
    say(f"{target.key}: {target.summary}")

    plan = fallback_plan(target)
    if use_claude:
        say(f"planning searches with {settings.claude_plan_model}")
        try:
            planned = plan_from_json(ask_json(
                build_plan_prompt(target), PLAN_SCHEMA, settings,
                model=settings.claude_plan_model, effort="low", on_status=say,
            ))
            if planned.keywords or planned.jql:
                plan = planned
        except ClaudeError as exc:
            say(f"search planning failed, using summary keywords ({exc})")
    say("keywords: " + ", ".join(plan.keywords))

    ctx = gather(client, target, plan, confluence=settings.search_confluence and confluence, on_status=say)
    say(f"{len(ctx.candidates)} candidate tickets, {len(ctx.documents)} documents")
    scanned = scan_ticket_commits(ctx, settings, plan.keywords, repo_choice, say) if scan_commits else []

    analysis = None
    if use_claude and (ctx.candidates or ctx.documents or ctx.commits):
        say(f"analysing with {model or settings.claude_model}")
        try:
            analysis = clean_analysis(ask_json(
                build_analysis_prompt(ctx), ANALYSIS_SCHEMA, settings,
                model=model or settings.claude_model, effort=settings.claude_effort, on_status=say,
            ), ctx)
        except ClaudeError as exc:
            say(f"analysis failed, writing the raw candidate list instead ({exc})")

    markdown = render(ctx, client, analysis)
    REPORTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    path = REPORTS_DIR / f"{target.key}-{stamp}.md"
    path.write_text(markdown, encoding="utf-8")
    # Save what was gathered: as the ticket check Ask Claude reuses, and as this report's
    # conversation so follow-ups can be asked about it.
    from linker.ask import save_report_thread
    from linker.checks import save_check

    save_check(ctx, origin="find context", repos=scanned, confluence=settings.search_confluence and confluence,
               report=path.name)
    save_report_thread(path, ctx, markdown, client)
    if write_json:
        payload = {
            "ticket": target.key,
            "analysis": analysis,
            "candidates": [
                {"key": i.key, "summary": i.summary, "status": i.status, "url": client.browse_url(i.key), "sources": i.sources}
                for i in ctx.ranked_candidates()
            ],
            "documents": [{"title": d.title, "url": d.url, "kind": d.kind} for d in ctx.documents.values()],
            "commits": [
                {"repo": c.repo, "sha": c.sha, "url": c.url, "headline": c.headline, "author": c.author,
                 "date": c.date, "sources": c.sources}
                for c in ctx.commits.values()
            ],
        }
        path.with_suffix(".json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    say(f"saved {path.name}")
    return ReportResult(key=target.key, summary=target.summary, path=path, markdown=markdown)
