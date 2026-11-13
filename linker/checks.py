"""Ticket checks: the Jira/Confluence/commit context gathered for one ticket, saved for reuse.

Find context writes one every time it runs. Ask Claude reuses a fresh one instead of searching
again, and runs (and saves) one itself when a ticket has none, using the planning call it already
makes, so a check never costs an extra Claude call.

Stored as reports/context/<KEY>.json. Fresh = younger than CONTEXT_MAX_AGE_HOURS (default 24).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from linker import REPORTS_DIR
from linker.gather import Context, SearchPlan
from linker.github import Commit
from linker.jira import Document, Issue

CHECKS_DIR = REPORTS_DIR / "context"


@dataclass
class TicketCheck:
    ctx: Context
    created: datetime
    origin: str  # "find context" | "ask"
    repos: list[str] = field(default_factory=list)  # repos whose commits were scanned
    confluence: bool = True
    report: str = ""  # Find context report file name, when there is one

    @property
    def age(self) -> str:
        minutes = int((datetime.now() - self.created).total_seconds() // 60)
        return f"{minutes} min" if minutes < 120 else f"{minutes // 60} h"


def _path(key: str) -> Path:
    return CHECKS_DIR / f"{key}.json"


def save_check(ctx: Context, *, origin: str, repos: list[str], confluence: bool, report: str = "") -> None:
    CHECKS_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "version": 1,
        "created": datetime.now().isoformat(timespec="seconds"),
        "origin": origin,
        "repos": repos,
        "confluence": confluence,
        "report": report,
        "target": asdict(ctx.target),
        "candidates": [asdict(i) for i in ctx.candidates.values()],
        "documents": [asdict(d) for d in ctx.documents.values()],
        "commits": [asdict(c) for c in ctx.commits.values()],
        "plan": asdict(ctx.plan),
        "log": ctx.log,
    }
    _path(ctx.target.key).write_text(json.dumps(data, indent=1), encoding="utf-8")


def _issue(data: dict[str, Any]) -> Issue:
    issue = Issue(**{k: v for k, v in data.items() if k in Issue.__dataclass_fields__})
    issue.links = [tuple(x) for x in issue.links]
    issue.comments = [tuple(x) for x in issue.comments]
    return issue


def _commit(data: dict[str, Any]) -> Commit:
    commit = Commit(**{k: v for k, v in data.items() if k in Commit.__dataclass_fields__})
    commit.files = [tuple(x) for x in commit.files]
    commit.patches = [tuple(x) for x in commit.patches]
    return commit


def load_check(key: str, max_age_hours: float) -> TicketCheck | None:
    """The saved check for this ticket, or None when missing, unreadable or stale."""
    try:
        data = json.loads(_path(key).read_text(encoding="utf-8"))
        created = datetime.fromisoformat(data["created"])
    except (OSError, ValueError, KeyError):
        return None
    if datetime.now() - created > timedelta(hours=max_age_hours):
        return None
    try:
        ctx = Context(target=_issue(data["target"]), plan=SearchPlan(**data.get("plan") or {}))
        ctx.candidates = {i["key"]: _issue(i) for i in data.get("candidates") or []}
        ctx.documents = {d["url"]: Document(**d) for d in data.get("documents") or []}
        ctx.commits = {c["url"]: _commit(c) for c in data.get("commits") or []}
        ctx.log = list(data.get("log") or [])
    except (TypeError, KeyError):
        return None  # written by an older version with different fields
    return TicketCheck(ctx=ctx, created=created, origin=str(data.get("origin") or ""),
                       repos=list(data.get("repos") or []), confluence=bool(data.get("confluence", True)),
                       report=str(data.get("report") or ""))


def delete_check(key: str) -> None:
    _path(key).unlink(missing_ok=True)
