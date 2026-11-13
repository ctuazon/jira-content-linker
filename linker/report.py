"""Render the gathered context (and Claude's analysis, when present) as Markdown."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from linker.commits import commit_label
from linker.gather import Context, documents
from linker.jira import JiraClient

_REL_LABEL = {
    "duplicate": "Possible duplicate",
    "prior_work": "Prior work",
    "dependency": "Dependency",
    "same_feature": "Same feature",
    "same_area": "Same area",
    "regression_source": "Possible regression source",
    "reference": "Reference",
}


def _row(cells: list[str]) -> str:
    return "| " + " | ".join(c.replace("|", "\\|").replace("\n", " ") for c in cells) + " |"


def render(ctx: Context, client: JiraClient, analysis: dict[str, Any] | None) -> str:
    t = ctx.target
    out = [
        f"# {t.key}: {t.summary}",
        "",
        f"[Open in Jira]({client.browse_url(t.key)}) · {t.issue_type} · {t.status}"
        + (f" · {', '.join(t.components)}" if t.components else "")
        + f" · generated {datetime.now():%Y-%m-%d %H:%M}",
        "",
    ]

    if analysis:
        out += ["## What this ticket asks for", "", analysis.get("summary", "").strip(), ""]
        out += ["## Background", "", analysis.get("background", "").strip(), ""]

        related = analysis.get("related") or []
        out += [f"## Related tickets ({len(related)})", ""]
        if related:
            out += [_row(["", "Ticket", "Relationship", "Status", "Why"]), _row(["-"] * 5)]
            for r in related:
                issue = ctx.candidates[r["key"]]
                stars = "★" * int(r.get("relevance") or 0)
                out.append(_row([
                    stars,
                    f"[{issue.key}]({client.browse_url(issue.key)}) {issue.summary}",
                    _REL_LABEL.get(r.get("relationship", ""), r.get("relationship", "")),
                    issue.status + (f" ({issue.resolved})" if issue.resolved else ""),
                    r.get("why", ""),
                ]))
        else:
            out.append("_Nothing clearly related was found._")
        out.append("")

        docs = analysis.get("documents") or []
        if docs:
            out += ["## Documents", ""]
            for d in docs:
                doc = ctx.documents[d["url"]]
                out.append(f"- [{doc.title}]({doc.url}) — {d.get('why', '')}")
            out.append("")

        picked = analysis.get("commits") or []
        if picked:
            out += [f"## Related commits ({len(picked)})", ""]
            for c in picked:
                commit = ctx.commits[c["url"]]
                out.append(f"- [{commit_label(commit)}]({commit.url}) {commit.headline} — {commit.author}, "
                           f"{commit.date} — {c.get('why', '')}")
            out.append("")

        for heading, name in (("Risks and gotchas", "risks"), ("Open questions", "open_questions")):
            items = analysis.get(name) or []
            if items:
                out += [f"## {heading}", ""] + [f"- {x}" for x in items] + [""]
        people = analysis.get("people") or []
        if people:
            out += ["## People to ask", ""] + [f"- **{p['name']}** — {p['why']}" for p in people] + [""]
    else:
        out += ["_Claude analysis was skipped; listing every candidate found._", ""]

    shown = {r["key"] for r in (analysis or {}).get("related") or []}
    rest = [i for i in ctx.ranked_candidates() if i.key not in shown]
    if rest:
        title = "Other candidates searched" if analysis else "Candidates"
        out += [f"<details><summary>{title} ({len(rest)})</summary>", ""] if analysis else [f"## {title}", ""]
        for i in rest:
            out.append(
                f"- [{i.key}]({client.browse_url(i.key)}) {i.summary} — {i.status}, updated {i.updated} "
                f"_(via {'; '.join(i.sources)})_"
            )
        out += ["", "</details>", ""] if analysis else [""]

    if ctx.commits:
        shown_commits = {c["url"] for c in (analysis or {}).get("commits") or []}
        rest_commits = [c for c in ctx.commits.values() if c.url not in shown_commits]
        if rest_commits:
            title = "Other commits scanned" if analysis else "Commits"
            out += [f"<details><summary>{title} ({len(rest_commits)})</summary>", ""] if analysis else [f"## {title}", ""]
            for c in rest_commits:
                out.append(f"- [{commit_label(c)}]({c.url}) {c.headline} — {c.author}, {c.date} "
                           f"_(via {'; '.join(c.sources)})_")
            out += ["", "</details>", ""] if analysis else [""]

    if not analysis and ctx.documents:
        out += ["## Documents", ""] + [f"- [{d.title}]({d.url}) — {d.kind}" for d in documents(ctx)] + [""]

    out += ["<details><summary>Searches run</summary>", "", "```"] + ctx.log + ["```", "", "</details>", ""]
    return "\n".join(out)
