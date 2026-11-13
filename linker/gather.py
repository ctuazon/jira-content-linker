"""Collect candidate tickets and documents that may relate to a target ticket.

Deterministic sources (always run):
  * issues linked to the ticket, its parent/epic, sub-tasks and siblings
  * recent issues sharing its components or labels
  * remote links and pull requests attached to the ticket
Search sources (terms planned by Claude, or derived from the summary with --no-claude):
  * Jira full-text searches and extra JQL queries
  * Confluence page searches
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from linker.jira import Document, Issue, JiraClient, quote_list, text_clause

MAX_CANDIDATES = 60
MAX_DOCUMENTS = 20

_STOPWORDS = set(
    "a an and are as at be by for from in into is it of on or that the this to with when should "
    "need needs add update fix make new use using via not can will we our".split()
)


@dataclass
class SearchPlan:
    keywords: list[str] = field(default_factory=list)
    jql: list[str] = field(default_factory=list)
    confluence_terms: list[str] = field(default_factory=list)


@dataclass
class Context:
    target: Issue
    candidates: dict[str, Issue] = field(default_factory=dict)
    documents: dict[str, Document] = field(default_factory=dict)
    plan: SearchPlan = field(default_factory=SearchPlan)
    log: list[str] = field(default_factory=list)
    commits: dict[str, Any] = field(default_factory=dict)  # url -> linker.github.Commit

    def add_issue(self, issue: Issue, source: str) -> None:
        if issue.key == self.target.key or not issue.key:
            return
        existing = self.candidates.get(issue.key)
        if existing is None:
            issue.sources = [source]
            self.candidates[issue.key] = issue
        elif source not in existing.sources:
            existing.sources.append(source)

    def add_document(self, doc: Document) -> None:
        existing = self.documents.get(doc.url)
        if existing is None:
            self.documents[doc.url] = doc
        else:
            existing.sources.extend(s for s in doc.sources if s not in existing.sources)

    def ranked_candidates(self) -> list[Issue]:
        """Directly linked first, then by how many independent searches found it, then recency."""
        def score(issue: Issue) -> tuple[int, int, str]:
            direct = any(s.startswith(("linked", "parent", "sub-task")) for s in issue.sources)
            return (1 if direct else 0, len(issue.sources), issue.updated)

        return sorted(self.candidates.values(), key=score, reverse=True)[:MAX_CANDIDATES]


def fallback_plan(target: Issue) -> SearchPlan:
    """Search terms from the summary alone, used when Claude planning is off or fails."""
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", target.summary) if w.lower() not in _STOPWORDS]
    # Loose (non-phrase) match on the summary words; exact-phrase keyword searches would miss.
    jql = [f'summary ~ "{" ".join(words[:5])}"'] if len(words) >= 2 else []
    return SearchPlan(keywords=words[:4], jql=jql, confluence_terms=words[:2])


def gather(
    client: JiraClient,
    target: Issue,
    plan: SearchPlan,
    *,
    confluence: bool = True,
    on_status: Callable[[str], None] | None = None,
) -> Context:
    """`target` may be a blank placeholder Issue (key "") when a question names no ticket."""
    ctx = Context(target=target, plan=plan)
    say = on_status or (lambda _msg: None)
    not_target = f"key != {target.key}" if target.key else "created is not EMPTY"

    def run(jql: str, source: str, limit: int = 15) -> None:
        found = client.search(jql, limit)
        ctx.log.append(f"{len(found):>3}  {source}  [{jql}]")
        for issue in found:
            ctx.add_issue(issue, source)

    # Direct relationships
    if target.key:
        say("linked issues")
    direct: dict[str, str] = {}
    for phrase, key in target.links:
        direct.setdefault(key, f"linked: {phrase}")
    if target.parent_key:
        direct.setdefault(target.parent_key, "parent / epic")
    for key in target.subtask_keys:
        direct.setdefault(key, "sub-task")
    if direct:
        found = {i.key: i for i in client.search(f"key in ({', '.join(direct)})", limit=len(direct))}
        ctx.log.append(f"{len(found):>3}  direct links  {sorted(direct)}")
        for key, source in direct.items():
            if key in found:
                ctx.add_issue(found[key], source)
    if target.parent_key:
        run(f"parent = {target.parent_key} AND {not_target} ORDER BY updated DESC", "same parent / epic", 25)

    # Same area
    if target.components or target.labels:
        say("same components / labels")
    if target.components and target.project:
        run(
            f"project = {target.project} AND component in ({quote_list(target.components)}) "
            f"AND {not_target} ORDER BY updated DESC",
            "same component",
            20,
        )
    if target.labels:
        run(f"labels in ({quote_list(target.labels)}) AND {not_target} ORDER BY updated DESC", "same label", 15)

    # Text searches
    for term in plan.keywords[:8]:
        clause = text_clause(term)
        if clause:
            say(f'searching "{term}"')
            run(f"{clause} AND {not_target} ORDER BY updated DESC", f'text "{term}"', 10)
    for jql in plan.jql[:5]:
        say("custom JQL")
        run(jql, "planned JQL", 15)

    # Documents
    if target.key:
        say("remote links / pull requests")
        for doc in client.remote_links(target.key) + client.development_info(target.id):
            ctx.add_document(doc)
    if confluence:
        for term in plan.confluence_terms[:5]:
            say(f'Confluence "{term}"')
            docs = client.confluence_search(term)
            ctx.log.append(f"{len(docs):>3}  confluence \"{term}\"")
            for doc in docs:
                ctx.add_document(doc)
    return ctx


def documents(ctx: Context) -> list[Document]:
    return list(ctx.documents.values())[:MAX_DOCUMENTS]


def plan_from_json(data: dict[str, Any]) -> SearchPlan:
    def strings(name: str, limit: int) -> list[str]:
        return [str(x).strip() for x in data.get(name) or [] if str(x).strip()][:limit]

    return SearchPlan(
        keywords=strings("keywords", 8),
        jql=strings("jql", 5),
        confluence_terms=strings("confluence_terms", 5),
    )
