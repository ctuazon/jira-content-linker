"""Jira Cloud (and Confluence) REST client.

Ported from Peer Review App's app/jira.py: same basic auth (email + API
token), same retry policy, same ADF flattening and acceptance-criteria
detection. Extended with JQL search, issue links, comments, remote links,
development info and Confluence search for finding related work.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import requests

from linker import Settings

TICKET_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
AC_HEADINGS = {"acceptance criteria", "acceptance", "definition of done", "dod", "requirements", "ac"}

SUMMARY_FIELDS = [
    "summary", "description", "issuetype", "status", "resolution", "resolutiondate",
    "priority", "labels", "components", "fixVersions", "project", "assignee",
    "reporter", "created", "updated", "parent",
]
FULL_FIELDS = SUMMARY_FIELDS + ["subtasks", "issuelinks", "comment"]


# --- ADF ---------------------------------------------------------------------

def _flatten_adf(node: Any) -> str:
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_flatten_adf(child) for child in node)
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    attrs = node.get("attrs") or {}
    children = _flatten_adf(node.get("content") or [])
    if kind == "text":
        return str(node.get("text") or "")
    if kind == "hardBreak":
        return "\n"
    if kind == "paragraph":
        return "\n" if not children.strip() else children + "\n\n"
    if kind == "heading":
        return "#" * int(attrs.get("level") or 2) + " " + children.strip() + "\n\n"
    if kind in {"bulletList", "orderedList", "taskList", "table"}:
        return children + "\n"
    if kind == "listItem":
        return "- " + children.strip() + "\n"
    if kind == "taskItem":
        mark = "x" if attrs.get("state") == "DONE" else " "
        return f"- [{mark}] " + children.strip() + "\n"
    if kind == "codeBlock":
        return "```\n" + children.strip() + "\n```\n\n"
    if kind == "blockquote":
        return "> " + children.strip() + "\n\n"
    if kind == "rule":
        return "---\n\n"
    if kind == "tableRow":
        return children.strip() + "\n"
    if kind in {"tableCell", "tableHeader"}:
        return children.strip() + " | "
    if kind == "inlineCard":
        return str(attrs.get("url") or "")
    if kind == "mention":
        return "@" + str(attrs.get("text") or "")
    if kind == "emoji":
        return str(attrs.get("text") or "")
    return children


def adf_to_text(node: Any) -> str:
    return re.sub(r"\n{3,}", "\n\n", _flatten_adf(node)).strip()


def extract_acceptance_section(description: str) -> str | None:
    """Return the text under an acceptance-criteria heading, up to the next same-or-higher heading."""
    collected: list[str] = []
    capturing = False
    captured_level = 0
    for line in description.splitlines():
        heading = re.match(r"^(#{1,6})\s*(.+?)\s*:?\s*$", line)
        if heading:
            level = len(heading.group(1))
            if capturing and level <= captured_level:
                break
            if not capturing and heading.group(2).strip().lower() in AC_HEADINGS:
                capturing, captured_level = True, level
                continue
        if not capturing and re.match(
            r"^\*{0,2}(acceptance criteria|definition of done|acceptance)\*{0,2}\s*:?\s*$",
            line.strip(),
            re.IGNORECASE,
        ):
            capturing, captured_level = True, 6
            continue
        if capturing:
            collected.append(line)
    section = "\n".join(collected).strip()
    return section or None


# --- Models ------------------------------------------------------------------

def _name(value: Any) -> str:
    return str((value or {}).get("name") or "") if isinstance(value, dict) else ""


def _person(value: Any) -> str:
    return str((value or {}).get("displayName") or "") if isinstance(value, dict) else ""


@dataclass
class Issue:
    key: str
    id: str
    summary: str
    description: str
    issue_type: str
    status: str
    resolution: str
    project: str
    priority: str = ""
    labels: list[str] = field(default_factory=list)
    components: list[str] = field(default_factory=list)
    fix_versions: list[str] = field(default_factory=list)
    assignee: str = ""
    reporter: str = ""
    created: str = ""
    updated: str = ""
    resolved: str = ""
    parent_key: str = ""
    parent_summary: str = ""
    acceptance_criteria: str | None = None
    # Populated only on the target ticket.
    subtask_keys: list[str] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)  # (relation phrase, key)
    comments: list[tuple[str, str, str]] = field(default_factory=list)  # (author, created, body)
    # Why this issue was pulled in as a candidate.
    sources: list[str] = field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: dict[str, Any], ac_fields: list[str] | None = None) -> "Issue":
        f = payload.get("fields") or {}
        description = adf_to_text(f.get("description"))
        acceptance = None
        for field_id in ac_fields or []:
            value = adf_to_text(f.get(field_id))
            if value:
                acceptance = value
                break
        if acceptance is None:
            acceptance = extract_acceptance_section(description)
        parent = f.get("parent") or {}
        issue = cls(
            key=str(payload.get("key") or ""),
            id=str(payload.get("id") or ""),
            summary=str(f.get("summary") or ""),
            description=description,
            issue_type=_name(f.get("issuetype")) or "Unknown",
            status=_name(f.get("status")) or "Unknown",
            resolution=_name(f.get("resolution")),
            project=str((f.get("project") or {}).get("key") or ""),
            priority=_name(f.get("priority")),
            labels=[str(x) for x in f.get("labels") or []],
            components=[_name(c) for c in f.get("components") or [] if _name(c)],
            fix_versions=[_name(v) for v in f.get("fixVersions") or [] if _name(v)],
            assignee=_person(f.get("assignee")),
            reporter=_person(f.get("reporter")),
            created=str(f.get("created") or "")[:10],
            updated=str(f.get("updated") or "")[:10],
            resolved=str(f.get("resolutiondate") or "")[:10],
            parent_key=str(parent.get("key") or ""),
            parent_summary=str((parent.get("fields") or {}).get("summary") or ""),
            acceptance_criteria=acceptance,
        )
        issue.subtask_keys = [str(s.get("key")) for s in f.get("subtasks") or [] if s.get("key")]
        for link in f.get("issuelinks") or []:
            kind = link.get("type") or {}
            if link.get("outwardIssue"):
                issue.links.append((str(kind.get("outward") or "relates to"), str(link["outwardIssue"].get("key"))))
            elif link.get("inwardIssue"):
                issue.links.append((str(kind.get("inward") or "relates to"), str(link["inwardIssue"].get("key"))))
        comment_block = f.get("comment") or {}
        for c in comment_block.get("comments") or []:
            issue.comments.append(
                (_person(c.get("author")), str(c.get("created") or "")[:10], adf_to_text(c.get("body")))
            )
        return issue


@dataclass
class Document:
    title: str
    url: str
    excerpt: str = ""
    space: str = ""
    updated: str = ""
    kind: str = "confluence"
    sources: list[str] = field(default_factory=list)


# --- Client ------------------------------------------------------------------

class JiraError(RuntimeError):
    pass


class JiraClient:
    def __init__(self, settings: Settings) -> None:
        if not settings.jira_configured:
            raise JiraError(
                "Jira is not configured. Set JIRA_BASE_URL, JIRA_EMAIL and JIRA_API_TOKEN in "
                "jira-context-linker/.env (or in the Peer Review App's Settings)."
            )
        self.base_url = settings.jira_base_url
        self.session = requests.Session()
        self.session.auth = (settings.jira_email, settings.jira_api_token)
        self.session.headers["Accept"] = "application/json"
        self._ac_fields: list[str] | None = None

    def browse_url(self, key: str) -> str:
        return f"{self.base_url}/browse/{key}"

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response | None:
        url = path if path.startswith("http") else self.base_url + path
        for _ in range(3):
            try:
                response = self.session.request(method, url, timeout=20, **kwargs)
            except requests.RequestException:
                continue
            if response.status_code < 500:
                return response
        return None

    def get(self, path: str, params: dict[str, Any] | None = None) -> requests.Response | None:
        return self._request("GET", path, params=params)

    def post(self, path: str, body: dict[str, Any]) -> requests.Response | None:
        return self._request("POST", path, json=body)

    def myself(self) -> str:
        response = self.get("/rest/api/3/myself")
        if response is None:
            raise JiraError(f"Could not reach {self.base_url}.")
        if response.status_code in (401, 403):
            raise JiraError("Jira rejected the credentials (check JIRA_EMAIL and JIRA_API_TOKEN).")
        if not response.ok:
            raise JiraError(f"Jira returned HTTP {response.status_code}.")
        return str((response.json() or {}).get("displayName") or "")

    def acceptance_fields(self) -> list[str]:
        if self._ac_fields is None:
            response = self.get("/rest/api/3/field")
            self._ac_fields = []
            if response is not None and response.ok:
                self._ac_fields = [
                    str(item["id"])
                    for item in response.json() or []
                    if re.search(r"acceptance", str(item.get("name") or ""), re.IGNORECASE)
                ]
        return self._ac_fields

    def fetch(self, key: str) -> Issue:
        ac_fields = self.acceptance_fields()
        response = self.get(f"/rest/api/3/issue/{key}", params={"fields": ",".join(FULL_FIELDS + ac_fields)})
        if response is None:
            raise JiraError(f"Could not reach {self.base_url}.")
        if response.status_code in (401, 403):
            raise JiraError("Jira rejected the credentials (check JIRA_EMAIL and JIRA_API_TOKEN).")
        if response.status_code == 404:
            raise JiraError(f"{key} was not found, or you don't have permission to view it.")
        if not response.ok:
            raise JiraError(f"Jira returned HTTP {response.status_code} for {key}.")
        return Issue.from_payload(response.json() or {}, ac_fields)

    def search(self, jql: str, limit: int = 20) -> list[Issue]:
        """Run a JQL search. Returns [] on invalid JQL or any error."""
        response = self.post(
            "/rest/api/3/search/jql",
            {"jql": jql, "maxResults": limit, "fields": SUMMARY_FIELDS},
        )
        if response is None or not response.ok:
            return []
        try:
            return [Issue.from_payload(item) for item in (response.json() or {}).get("issues") or []]
        except ValueError:
            return []

    def remote_links(self, key: str) -> list[Document]:
        response = self.get(f"/rest/api/3/issue/{key}/remotelink")
        if response is None or not response.ok:
            return []
        docs = []
        for item in response.json() or []:
            obj = item.get("object") or {}
            if obj.get("url"):
                docs.append(Document(
                    title=str(obj.get("title") or obj["url"]),
                    url=str(obj["url"]),
                    excerpt=str(obj.get("summary") or ""),
                    kind="remote link",
                    sources=["linked from ticket"],
                ))
        return docs

    def development_info(self, issue_id: str) -> list[Document]:
        """Pull requests and branches from connected dev tools. Undocumented API; best effort."""
        summary = self.get("/rest/dev-status/latest/issue/summary", params={"issueId": issue_id})
        if summary is None or not summary.ok:
            return []
        try:
            by_type = ((summary.json() or {}).get("summary") or {}).get("pullrequest", {}).get("byInstanceType") or {}
        except (ValueError, AttributeError):
            return []
        docs: list[Document] = []
        for app_type in by_type:
            detail = self.get(
                "/rest/dev-status/latest/issue/detail",
                params={"issueId": issue_id, "applicationType": app_type, "dataType": "pullrequest"},
            )
            if detail is None or not detail.ok:
                continue
            for entry in (detail.json() or {}).get("detail") or []:
                for pr in entry.get("pullRequests") or []:
                    if pr.get("url"):
                        docs.append(Document(
                            title=f"{pr.get('name') or 'Pull request'} [{pr.get('status') or '?'}]",
                            url=str(pr["url"]),
                            excerpt=str((pr.get("source") or {}).get("branch") or ""),
                            updated=str(pr.get("lastUpdate") or "")[:10],
                            kind="pull request",
                            sources=["development panel"],
                        ))
        return docs

    def confluence_search(self, term: str, limit: int = 5) -> list[Document]:
        """Confluence lives on the same site with the same credentials. [] when unavailable."""
        clause = text_clause(term)
        if not clause:
            return []
        response = self.get(
            "/wiki/rest/api/search",
            params={"cql": f"type = page AND {clause} ORDER BY lastmodified DESC", "limit": limit},
        )
        if response is None or not response.ok:
            return []
        docs = []
        try:
            payload = response.json() or {}
        except ValueError:
            return []
        for item in payload.get("results") or []:
            content = item.get("content") or {}
            webui = (content.get("_links") or {}).get("webui") or item.get("url") or ""
            if not webui:
                continue
            docs.append(Document(
                title=re.sub(r"@@@(end)?hl@@@", "", str(item.get("title") or content.get("title") or "Untitled")),
                url=f"{self.base_url}/wiki{webui}" if webui.startswith("/") else webui,
                excerpt=re.sub(r"@@@(end)?hl@@@", "", str(item.get("excerpt") or ""))[:300],
                space=str(((item.get("resultGlobalContainer") or {}).get("title")) or ""),
                updated=str(item.get("lastModified") or "")[:10],
                sources=[f'search "{term}"'],
            ))
        return docs


def _phrase(term: str) -> str:
    """Reduce a search term to characters that are safe inside a quoted JQL/CQL text phrase."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w .'/-]", " ", term or "")).strip().replace("'", "")[:80]


def text_clause(term: str) -> str:
    """Multi-word terms match as an exact phrase; otherwise Jira matches the words separately."""
    safe = _phrase(term)
    if not safe:
        return ""
    return f'text ~ "\\"{safe}\\""' if " " in safe else f'text ~ "{safe}"'


def quote_list(values: list[str]) -> str:
    return ", ".join('"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"' for v in values)


def normalize_key(raw: str) -> str | None:
    """Accept 'abc-123', 'ABC-123' or a browse/selectedIssue URL."""
    match = TICKET_KEY_RE.search((raw or "").upper())
    return match.group(1) if match else None
