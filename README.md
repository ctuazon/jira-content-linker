# jira-context-linker

Enter a Jira ticket key and get a briefing: related past tickets, Confluence docs, linked pull
requests, risks, open questions and people to ask.

It reuses the Peer Review App's integrations:

- **Jira**: the same REST client (`app/jira.py`): email + API token basic auth, ADF flattening and
  acceptance-criteria detection. It adds JQL search, issue links, comments, remote links, the
  development panel and Confluence search.
- **Claude**: the same runner (`app/claude_runner.py`): Claude Code through WSL with your Claude
  SSO login, the Windows Claude Code CLI, or the Anthropic API.

## Desktop app

Run `install.bat` once. It installs the dependencies and puts a **Jira Context Linker** shortcut
(with its own icon) on your Desktop. Double-click it to open the window:

- type a ticket key or paste a browse URL (several, separated by spaces, also works) and press
  **Find context**
- progress streams into the Activity panel; finished reports appear in the Reports list and render
  on the right with clickable links
- the status bar shows whether Jira and Claude are signed in; **Settings** opens `.env` in Notepad

### Ask Claude

Type a question in the **Ask Claude** box (Ctrl+Enter), with or without a ticket in the ticket box.
For example: *"Historically, how did other engineers link marketplace PRs to aim-myt PRs so the two
can communicate?"*

1. Claude (cheap model) plans Jira searches, picks out the repos the question names and writes
   GitHub PR searches.
2. Jira context is gathered as for a report. Then pull requests are collected from the Jira
   development panel of the ticket and its closest related tickets, PRs naming those ticket keys,
   PRs in each named repo that mention the other repo, and the planned searches.
3. Up to 25 PRs are read: description, branches, changed files, and diff excerpts (config and
   client/service files first).
4. Claude answers with inline links, sources, a confidence level and gaps. Links it cites that
   were not gathered are dropped.

The answer is saved to `reports/` (`KEY-ask-*.md`, or `ask-*.md` with no ticket) and listed with
the reports.

**Follow-ups.** With an answer open, type in the **Ask follow-up** box under it. Claude gets the
evidence it already gathered plus the conversation so far, so most follow-ups need no new searches
and come back quickly. If a follow-up goes beyond that evidence (for example it names a repo that
was never searched), Claude asks for more searches. The app runs them once, adds the results to the
evidence and answers again. Each follow-up is appended to the same report. The evidence and
conversation live in `<report>.thread.json`. Answers saved before follow-ups existed have no
thread; on their first follow-up it is rebuilt from the report (the PRs it lists are re-read from
GitHub, and the tickets the answer cites from Jira).

**History.** Select reports and use **Delete selected** (or the Delete key; Ctrl/Shift+click selects
several), or **Clear all**. Both move the reports and their saved conversations to the Recycle Bin; Clear
all also removes the ticket checks.

**GitHub sign-in** works the same way as in the GitHub Code Scanner: your Git login (Git Credential
Manager) first, then `GITHUB_TOKEN`. If you have cloned an org repo over HTTPS, nothing needs
setting up. Otherwise use **Help > Sign in to GitHub**. `GITHUB_ORG` / `GITHUB_TOKEN` left blank
fall back to the Code Scanner's `.env`. Without GitHub, questions are answered from Jira alone.

### Ticket checks (shared context, no wasted tokens)

Every **Find context** run saves what it gathered (the ticket, related tickets, documents and
commits) as the ticket's *check* in `reports/context/<KEY>.json`, and saves the report's evidence so
the report gets an **Ask follow-up** box.

**Ask Claude** with a ticket reuses a check younger than `CONTEXT_MAX_AGE_HOURS` (default 24). It
skips gathering and only runs the question's own Jira searches, plus PR searches and commit scans for
repos the check didn't cover. The earlier Find context briefing is included as evidence. If the
ticket has no check yet, Ask runs one with the search plan it already makes and saves it, so the
check costs no extra Claude call. It skips the Find context analysis, the expensive step. A question
then costs two Claude calls: a small planning call and the answer.

Follow-ups on a Find context report answer from its saved evidence. Reports saved before this rebuild
their evidence from the ticket's check, or gather again without Claude.

### Commit scanning

With **Scan commits** on (both **Find context** and **Ask Claude**), the app also searches the commit
history of the repo the ticket is about. Pick the repo in the **Repo** list, or leave it on
**Auto (from ticket)**:

- the Jira project's repos from `PROJECT_REPOS` (ASEARCH → marketplace and marketplace-search-api)
- any repo from `REPO_CHOICES` that the ticket names, e.g. `[AIM-MYT]` in the summary
- repos of the PRs on the ticket's development panel

You can also type a repo that isn't listed, or several separated by commas. In each repo it finds
commits whose message names the ticket or its closest related tickets, commits matching the ticket's
keywords, and the recent history of the files the ticket's PRs changed. The top 20 are read (message,
files, diff excerpts). In a report Claude picks the ones that matter into **Related commits**; in an
answer they are evidence it can cite. GitHub's commit search covers each repo's default branch only.

`run.bat` with no arguments opens the same window. If the window never appears, see
`launch_error.log`.

## Command line

```
run.bat ASEARCH-3565                 # one ticket (a browse URL also works)
python jcl.py                        # prompts for ticket keys
python jcl.py ABC-1 ABC-2 --open     # several tickets, open each report when done
python jcl.py ABC-1 --no-claude      # raw search results only, no Claude
python jcl.py --check                # verify Jira, Claude and GitHub sign-in
python jcl.py ASEARCH-1 --repo marketplace-search-api   # scan that repo's commits
python jcl.py ABC-1 --no-commits     # skip the commit scan
python jcl.py ABC-1 --ask "how do marketplace PRs connect to aim-myt PRs?"
python jcl.py --ask "..." --no-github  # no ticket; Jira/Confluence only
python jcl.py --follow-up reports/ABC-1-ask-<stamp>.md --ask "and how is it deployed?"
```

Reports are written to `reports/<KEY>-<timestamp>.md` (plus `.json` with `--json`).

## How it works

1. **Fetch** the ticket: description, acceptance criteria, comments, links, parent, sub-tasks.
2. **Plan** (Claude, cheap model): pick distinctive search phrases, extra JQL and Confluence terms.
3. **Gather** candidates: linked issues, parent/epic siblings, tickets sharing components or labels,
   exact-phrase full-text searches, the planned JQL, Confluence pages, remote links and PRs.
4. **Analyse** (Claude): keep only what is genuinely related, rank it, explain why, and summarise the
   background, risks, open questions and people to ask. Keys and URLs that were not in the candidate
   set are dropped.

Claude gets no tools. It only reasons over data this tool fetched with your Jira credentials.

## Setup

`install.bat`, or `pip install -r requirements.txt` (`run.bat` also does this the first time).
The icon is drawn by `scripts/make_icon.py` (needs Pillow; only to regenerate it).

Settings come from the environment, then `.env` (see `.env.example`). Jira values left blank
fall back to the Peer Review App: its Settings (including Windows Credential Manager), then its
`.env`. So if the Peer Review App already talks to Jira, nothing else needs configuring.
Set `PEER_REVIEW_APP_DIR` if that app is not in the sibling `Peer Review App` folder.
