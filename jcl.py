"""jira-context-linker: find past tickets, docs and people related to a Jira ticket.

    python jcl.py                 # prompts for ticket keys until you press Enter on a blank line
    python jcl.py ABC-123         # one ticket (a browse URL works too)
    python jcl.py ABC-123 --no-claude --no-confluence
    python jcl.py --check         # verify Jira, Claude and GitHub sign-in
    python jcl.py ABC-123 --ask "how did marketplace PRs connect to aim-myt PRs?"
    python jcl.py --ask "..."     # a question with no ticket
    python jcl.py --follow-up reports/ABC-123-ask-....md --ask "and how is that deployed?"

For the desktop window, double-click the Desktop shortcut (see install.bat) or run launch.pyw.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from linker import Settings, load_settings
from linker.ask import answer_activity, answer_question, follow_up
from linker.claude import ClaudeError, check_claude_auth
from linker.github import GitHubError, connect
from linker.jira import JiraClient, JiraError
from linker.pipeline import BadKeyError, build_report


class Progress:
    def __init__(self) -> None:
        self.start = time.monotonic()

    def __call__(self, message: str) -> None:
        print(f"  [{time.monotonic() - self.start:5.1f}s] {message}", file=sys.stderr, flush=True)


def check(settings: Settings) -> int:
    ok = True
    print(f"Jira:   {settings.jira_base_url or '(no base URL)'}"
          + (f"  [from {settings.jira_source}]" if settings.jira_source else ""))
    try:
        name = JiraClient(settings).myself()
        print(f"        signed in as {name}")
    except JiraError as exc:
        ok = False
        print(f"        FAILED: {exc}")
    claude_ok, detail = check_claude_auth(settings)
    ok = ok and claude_ok
    print(f"Claude: {'ok' if claude_ok else 'FAILED'} - {detail}")
    print(f"        models: plan={settings.claude_plan_model} analysis={settings.claude_model} "
          f"effort={settings.claude_effort} mode={settings.claude_mode}")
    try:
        gh, source = connect(settings)
        print(f"GitHub: {gh.whoami()} via {source}" + (f"  (org {settings.github_org})" if settings.github_org else ""))
    except GitHubError as exc:
        print(f"GitHub: not available - {exc}  (only needed for --ask)")
    return 0 if ok else 1


def run_one(raw_key: str, settings: Settings, client: JiraClient, args: argparse.Namespace) -> int:
    try:
        result = build_report(
            raw_key, settings, client, say=Progress(),
            use_claude=not args.no_claude, confluence=not args.no_confluence,
            model=args.model, write_json=args.json,
            scan_commits=not args.no_commits, repo_choice=",".join(args.repo or []),
        )
    except BadKeyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except JiraError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print()
    print(result.markdown)
    print(f"Saved: {result.path}", file=sys.stderr)
    if args.open and sys.platform == "win32":
        os.startfile(result.path)  # noqa: S606 -- opens the report in the default .md viewer
    return 0


def ask_one(question: str, ticket: str | None, settings: Settings, client: JiraClient, args: argparse.Namespace) -> int:
    try:
        result = answer_question(
            question, ticket, settings, client, say=Progress(),
            use_github=not args.no_github, confluence=not args.no_confluence, model=args.model,
            scan_commits=not args.no_commits, repo_choice=",".join(args.repo or []),
        )
    except BadKeyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (JiraError, ClaudeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print()
    print(result.markdown)
    print(f"Saved: {result.path}", file=sys.stderr)
    if args.open and sys.platform == "win32":
        os.startfile(result.path)  # noqa: S606
    return 0


def activity_one(args: argparse.Namespace, settings: Settings, client: JiraClient) -> int:
    import calendar

    try:
        year, month = (int(x) for x in args.month.split("-"))
        last = calendar.monthrange(year, month)[1]
    except ValueError:
        print(f"--month must look like 2026-10, not '{args.month}'.", file=sys.stderr)
        return 2
    who = "I" if args.person.lower() == "me" else args.person
    question = args.ask or f"What did {who} work on in {calendar.month_name[month]} {year}? Write it up for a monthly check-in."
    try:
        result = answer_activity(question, args.person, f"{year}-{month:02d}-01", f"{year}-{month:02d}-{last}",
                                 settings, client, say=Progress(), model=args.model)
    except (JiraError, ClaudeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print()
    print(result.markdown)
    print(f"Saved: {result.path}", file=sys.stderr)
    if args.open and sys.platform == "win32":
        os.startfile(result.path)  # noqa: S606
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass
    parser = argparse.ArgumentParser(description="Find tickets, docs and people related to a Jira ticket.")
    parser.add_argument("tickets", nargs="*", help="Jira keys or browse URLs (prompted for when omitted)")
    parser.add_argument("--no-claude", action="store_true", help="skip Claude; list raw search results")
    parser.add_argument("--no-confluence", action="store_true", help="skip Confluence search")
    parser.add_argument("--model", help="model for the analysis pass (default CLAUDE_MODEL)")
    parser.add_argument("--json", action="store_true", help="also write a .json next to the .md report")
    parser.add_argument("--open", action="store_true", help="open the report when done")
    parser.add_argument("--check", action="store_true", help="verify Jira, Claude and GitHub access, then exit")
    parser.add_argument("--ask", metavar="QUESTION", help="ask Claude a question about the ticket (Jira + GitHub history)")
    parser.add_argument("--no-github", action="store_true", help="with --ask: skip GitHub")
    parser.add_argument("--repo", action="append", metavar="NAME",
                        help="repo whose commits to scan (repeatable; default: guessed from the ticket)")
    parser.add_argument("--no-commits", action="store_true", help="don't scan commits")
    parser.add_argument("--follow-up", metavar="REPORT", type=Path,
                        help="with --ask: continue the conversation saved with this answer (.md)")
    parser.add_argument("--month", metavar="YYYY-MM",
                        help="summarise a person's Jira activity for that month (check-in style)")
    parser.add_argument("--person", default="me", help="with --month: whose activity (default: you)")
    args = parser.parse_args()

    settings = load_settings()
    if args.check:
        return check(settings)
    try:
        client = JiraClient(settings)
    except JiraError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.month and not args.follow_up:
        return activity_one(args, settings, client)
    if args.ask and args.follow_up:
        try:
            result = follow_up(args.follow_up, args.ask, settings, say=Progress(), client=client,
                               use_github=not args.no_github, confluence=not args.no_confluence, model=args.model)
        except ClaudeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print(result.markdown)
        print(f"Updated: {result.path}", file=sys.stderr)
        return 0
    if args.ask:
        return ask_one(args.ask, args.tickets[0] if args.tickets else None, settings, client, args)
    if args.tickets:
        return max(run_one(t, settings, client, args) for t in args.tickets)

    status = 0
    while True:
        try:
            raw = input("\nJira ticket (blank to quit): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not raw:
            break
        status = run_one(raw, settings, client, args)
    return status


if __name__ == "__main__":
    sys.exit(main())
