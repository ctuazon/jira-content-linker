#!/usr/bin/env python3
"""Jira Context Linker — desktop UI. Same pipeline as jcl.py, in a window."""
from __future__ import annotations

import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from linker import REPORTS_DIR, load_settings
from linker.ask import AskResult, answer_question, can_follow_up, follow_up, thread_path
from linker.claude import ClaudeError, check_claude_auth
from linker.checks import CHECKS_DIR
from linker.commits import AUTO
from linker.github import GitHubError, connect
from linker.jira import JiraClient, JiraError, TICKET_KEY_RE
from linker.pipeline import BadKeyError, ReportResult, build_report
from ui.report_view import ReportView
from ui.widgets import (
    TERMINAL_BG,
    LinkButton,
    PrimaryButton,
    Section,
    StatusLight,
    ThinkingIndicator,
    apply_theme,
    soften_text,
    tip,
)

_REPORT_NAME_RE = re.compile(r"^(?:(?P<key>[A-Z][A-Z0-9]+-\d+)-)?(?P<ask>ask-)?(?P<stamp>\d{8}-\d{4}(?:\d{2})?)$")
ENV_PATH = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"


def _report_title(path: Path) -> str:
    try:
        with path.open(encoding="utf-8") as handle:
            first = handle.readline().strip()
    except OSError:
        return ""
    title = first.lstrip("# ").strip()
    key, _, summary = title.partition(": ")
    return summary or key


def _report_files(report: Path) -> list[Path]:
    """The .md plus its .json and .thread.json companions, when present."""
    return [f for f in (report, report.with_suffix(".json"), thread_path(report)) if f.exists()]


def _recycle(paths: list[Path]) -> None:
    """Send files to the Recycle Bin (so a mistaken clear can be undone); delete elsewhere."""
    if not paths:
        return
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR),
            ]

        FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
        op = SHFILEOPSTRUCTW(
            wFunc=FO_DELETE,
            pFrom="\0".join(str(p.resolve()) for p in paths) + "\0\0",
            fFlags=FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI,
        )
        if ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) == 0:
            return
    for path in paths:
        path.unlink(missing_ok=True)


def _split_tickets(raw: str) -> list[str]:
    """Space/comma separated keys or browse URLs."""
    return [part for part in re.split(r"[\s,;]+", raw.strip()) if part]


class LinkerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Jira Context Linker")
        self.geometry("1180x800")
        self.minsize(900, 600)
        self._apply_app_icon()

        self._events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._busy = False
        self._current: Path | None = None
        self._scroll_to_last_follow_up = False
        self._jira_base = ""

        apply_theme(self)
        self._build_menu()
        self.status_bar = ttk.Frame(self, style="Statusbar.TFrame")
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Separator(self, orient=tk.HORIZONTAL).pack(side=tk.BOTTOM, fill=tk.X)
        body = ttk.Frame(self, padding=(14, 12))
        body.pack(fill=tk.BOTH, expand=True)

        self._build_input(body)
        self._build_main(body)
        self._build_status_bar()

        self.refresh_reports()
        self.after(100, self._drain_events)
        self.after(300, self.check_connections)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # --- layout ---------------------------------------------------------------

    def _apply_app_icon(self) -> None:
        icon_png = ROOT / "assets" / "app.png"
        icon_ico = icon_png.with_suffix(".ico")
        try:
            if icon_png.is_file():
                self._app_icon = tk.PhotoImage(file=str(icon_png))
                self.iconphoto(True, self._app_icon)
            elif icon_ico.is_file():
                self.iconbitmap(default=str(icon_ico))
        except tk.TclError:
            pass

    def _build_menu(self) -> None:
        menubar = tk.Menu(self)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Edit settings (.env)…", command=self.edit_settings)
        file_menu.add_command(label="Open reports folder", command=self.open_reports_folder)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.on_close)
        menubar.add_cascade(label="File", menu=file_menu)
        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="Check Jira, Claude and GitHub sign-in", command=self.check_connections)
        help_menu.add_command(label="Sign in to GitHub…", command=self.github_sign_in)
        menubar.add_cascade(label="Help", menu=help_menu)
        self.config(menu=menubar)

    def _build_input(self, parent: ttk.Frame) -> None:
        section = Section(parent, "Jira ticket")
        section.pack(fill=tk.X)

        row = ttk.Frame(section.body)
        row.pack(fill=tk.X)
        self.ticket_var = tk.StringVar()
        self.ticket_entry = tip(
            ttk.Entry(row, textvariable=self.ticket_var, font=("Segoe UI", 11)),
            "A key such as ABC-123 or a Jira browse URL. Separate several with spaces or commas.",
        )
        self.ticket_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=3)
        self.ticket_entry.bind("<Return>", lambda _e: self.start_run())
        self.ticket_entry.focus_set()
        self.run_button = PrimaryButton(row, "Find context", self.start_run)
        self.run_button.pack(side=tk.LEFT, padx=(8, 0))
        self.thinking = ThinkingIndicator(row)

        options = ttk.Frame(section.body)
        options.pack(fill=tk.X, pady=(6, 0))
        self.use_claude_var = tk.BooleanVar(value=True)
        self.confluence_var = tk.BooleanVar(value=True)
        self.json_var = tk.BooleanVar(value=False)
        tip(ttk.Checkbutton(options, text="Claude analysis", variable=self.use_claude_var),
            "Plan searches and rank results with Claude. Off: list every raw search hit.").pack(side=tk.LEFT)
        tip(ttk.Checkbutton(options, text="Search Confluence", variable=self.confluence_var),
            "Also search Confluence pages on the same Atlassian site.").pack(side=tk.LEFT, padx=(14, 0))
        tip(ttk.Checkbutton(options, text="Also save JSON", variable=self.json_var),
            "Write a .json next to the .md report.").pack(side=tk.LEFT, padx=(14, 0))

        # Repo for the commit scan (both Find context and Ask Claude). Typing another name works too.
        self.repo_var = tk.StringVar(value=AUTO)
        self.repo_combo = ttk.Combobox(options, textvariable=self.repo_var, width=26,
                                       values=[AUTO] + load_settings().repo_choices)
        self.repo_combo.pack(side=tk.RIGHT)
        tip(self.repo_combo, "Repo whose commits are scanned. Auto: ASEARCH tickets use marketplace and "
                             "marketplace-search-api; otherwise repos the ticket names or links PRs from. "
                             "You can type any repo name, or several separated by commas.")
        ttk.Label(options, text="Repo").pack(side=tk.RIGHT, padx=(14, 6))
        self.commits_var = tk.BooleanVar(value=True)
        tip(ttk.Checkbutton(options, text="Scan commits", variable=self.commits_var),
            "Search the repo's commit history for the ticket keys, the ticket's keywords and the files "
            "its PRs changed (default branch only).").pack(side=tk.RIGHT)

        ask = Section(parent, "Ask Claude", padding=(0, 12, 0, 0))
        ask.pack(fill=tk.X)
        ttk.Label(
            ask.header, style="Muted.TLabel",
            text="about the ticket above (optional). Searches Jira, Confluence, GitHub pull requests and commits.",
        ).pack(side=tk.LEFT, padx=(8, 0))
        ask_row = ttk.Frame(ask.body)
        ask_row.pack(fill=tk.X)
        self.question_text = tk.Text(ask_row, height=3, wrap=tk.WORD, font=("Segoe UI", 10), undo=True)
        soften_text(self.question_text)
        self.question_text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.question_text.bind("<Control-Return>", self._ask_shortcut)
        tip(self.question_text, "e.g. Historically, how did engineers link marketplace PRs to aim-myt PRs so the "
                                "two can communicate? Ctrl+Enter to ask.")
        ask_side = ttk.Frame(ask_row)
        ask_side.pack(side=tk.LEFT, fill=tk.Y, padx=(8, 0))
        self.ask_button = PrimaryButton(ask_side, "Ask Claude", self.start_ask)
        self.ask_button.pack(fill=tk.X)
        self.github_var = tk.BooleanVar(value=True)
        tip(ttk.Checkbutton(ask_side, text="Search GitHub", variable=self.github_var),
            "Read pull requests (descriptions, files, diffs) with your Git login, like the GitHub Code Scanner."
            ).pack(anchor=tk.W, pady=(6, 0))

    def _build_main(self, parent: ttk.Frame) -> None:
        vertical = ttk.PanedWindow(parent, orient=tk.VERTICAL)
        vertical.pack(fill=tk.BOTH, expand=True, pady=(12, 0))
        panes = ttk.PanedWindow(vertical, orient=tk.HORIZONTAL)
        vertical.add(panes, weight=5)

        # Left: saved reports, newest first.
        reports = Section(panes, "Reports")
        LinkButton(reports.header, self.open_reports_folder, text="Open folder").pack(side=tk.RIGHT)
        LinkButton(reports.header, self.refresh_reports, text="Refresh").pack(side=tk.RIGHT, padx=(0, 8))
        self.clear_all_link = LinkButton(reports.header, self.clear_all_reports, text="Clear all")
        self.clear_all_link.pack(side=tk.RIGHT, padx=(0, 8))
        tip(self.clear_all_link, "Move every report and answer to the Recycle Bin.")
        self.delete_link = LinkButton(reports.header, self.delete_selected_reports, text="Delete selected")
        self.delete_link.pack(side=tk.RIGHT, padx=(0, 8))
        tip(self.delete_link, "Move the selected reports to the Recycle Bin. Ctrl+click or Shift+click to select "
                              "several; the Delete key works too.")
        tree_frame = ttk.Frame(reports.body)
        tree_frame.pack(fill=tk.BOTH, expand=True)
        self.reports_tree = ttk.Treeview(
            tree_frame, columns=("ticket", "summary", "when"), show="headings", selectmode="extended"
        )
        self.reports_tree.heading("ticket", text="Ticket")
        self.reports_tree.heading("summary", text="Summary")
        self.reports_tree.heading("when", text="Created")
        self.reports_tree.column("ticket", width=110, stretch=False)
        self.reports_tree.column("summary", width=180, stretch=True)
        self.reports_tree.column("when", width=100, stretch=False)
        scroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.reports_tree.yview)
        self.reports_tree.configure(yscrollcommand=scroll.set)
        self.reports_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.LEFT, fill=tk.Y)
        self.reports_tree.bind("<<TreeviewSelect>>", self._on_report_selected)
        self.reports_tree.bind("<Delete>", lambda _e: self.delete_selected_reports())
        panes.add(reports, weight=2)

        # Right: the selected report.
        viewer = Section(panes, "Report", padding=(12, 0, 0, 0))
        self.copy_link = LinkButton(viewer.header, self.copy_markdown, text="Copy Markdown")
        self.copy_link.pack(side=tk.RIGHT)
        self.open_link = LinkButton(viewer.header, self.open_report_file, text="Open file")
        self.open_link.pack(side=tk.RIGHT, padx=(0, 8))
        self.jira_link = LinkButton(viewer.header, self.open_in_jira, text="Open in Jira")
        self.jira_link.pack(side=tk.RIGHT, padx=(0, 8))
        # Packed first (bottom) so the report keeps the remaining height.
        self.follow_bar = ttk.Frame(viewer.body, padding=(0, 8, 0, 0))
        self.follow_text = tk.Text(self.follow_bar, height=2, wrap=tk.WORD, font=("Segoe UI", 10), undo=True)
        soften_text(self.follow_text)
        self.follow_text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.follow_text.bind("<Control-Return>", self._follow_shortcut)
        tip(self.follow_text, "Ask a follow-up about this report or answer. Claude reuses the evidence it already "
                              "gathered and only searches again if it needs to. Ctrl+Enter to send.")
        self.follow_button = PrimaryButton(self.follow_bar, "Ask follow-up", self.start_follow_up)
        self.follow_button.pack(side=tk.LEFT, padx=(8, 0), fill=tk.Y)
        self.report_view = ReportView(viewer.body)
        self.report_view.pack(fill=tk.BOTH, expand=True)
        panes.add(viewer, weight=3)
        self._set_report_actions(False)

        # Bottom: progress log.
        activity = Section(vertical, "Activity", padding=(0, 10, 0, 0))
        LinkButton(activity.header, self.clear_log, text="Clear").pack(side=tk.RIGHT)
        self.log = scrolledtext.ScrolledText(
            activity.body, height=7, wrap=tk.WORD, font=("Consolas", 9),
            background=TERMINAL_BG, foreground="#c9d1d9", insertbackground="#c9d1d9",
        )
        soften_text(self.log)
        self.log.tag_configure("error", foreground="#ff7b72")
        self.log.tag_configure("ok", foreground="#3fb950")
        self.log.configure(state=tk.DISABLED)
        self.log.pack(fill=tk.BOTH, expand=True)
        vertical.add(activity, weight=1)

    def _build_status_bar(self) -> None:
        self.jira_light = StatusLight(self.status_bar, size=14)
        self.jira_light.pack(side=tk.LEFT)
        self.jira_status = tk.StringVar(value="Jira: not checked")
        ttk.Label(self.status_bar, textvariable=self.jira_status, style="Muted.TLabel").pack(side=tk.LEFT, padx=(4, 16))
        self.claude_light = StatusLight(self.status_bar, size=14)
        self.claude_light.pack(side=tk.LEFT)
        self.claude_status = tk.StringVar(value="Claude: not checked")
        ttk.Label(self.status_bar, textvariable=self.claude_status, style="Muted.TLabel").pack(side=tk.LEFT, padx=(4, 16))
        self.github_light = StatusLight(self.status_bar, size=14)
        self.github_light.pack(side=tk.LEFT)
        self.github_status = tk.StringVar(value="GitHub: not checked")
        ttk.Label(self.status_bar, textvariable=self.github_status, style="Muted.TLabel").pack(side=tk.LEFT, padx=(4, 16))
        self.check_link = LinkButton(self.status_bar, self.check_connections, text="Check again")
        self.check_link.pack(side=tk.LEFT)
        LinkButton(self.status_bar, self.edit_settings, text="Settings").pack(side=tk.RIGHT)

    # --- background work --------------------------------------------------------

    def _post(self, kind: str, payload: object = None) -> None:
        """Thread-safe: queue an event for the Tk thread."""
        self._events.put((kind, payload))

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                handler = getattr(self, f"_on_{kind}")
                handler(payload)
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def check_connections(self) -> None:
        for light in (self.jira_light, self.claude_light, self.github_light):
            light.set("checking")
        self.jira_status.set("Jira: checking…")
        self.claude_status.set("Claude: checking…")
        self.github_status.set("GitHub: checking…")
        self.check_link.configure(state=tk.DISABLED)
        threading.Thread(target=self._check_worker, daemon=True).start()

    def _check_worker(self) -> None:
        settings = load_settings()
        self._post("jira_base", settings.jira_base_url)
        try:
            name = JiraClient(settings).myself()
            self._post("jira_check", (True, f"Jira: {name}"))
        except JiraError as exc:
            self._post("jira_check", (False, f"Jira: {exc}"))
        except Exception as exc:  # noqa: BLE001 -- network errors etc. shouldn't kill the thread
            self._post("jira_check", (False, f"Jira: {exc}"))
        self._github_check(settings, interactive=False)
        ok, detail = check_claude_auth(settings)
        self._post("claude_check", (ok, f"Claude: {detail}"))

    def _github_check(self, settings, interactive: bool) -> None:  # noqa: ANN001
        try:
            gh, source = connect(settings, interactive=interactive)
            self._post("github_check", (True, f"GitHub: {gh.whoami()} ({source})"))
        except GitHubError as exc:
            text = "GitHub: not signed in (Help > Sign in to GitHub)" if exc.status == 401 else f"GitHub: {exc}"
            self._post("github_check", (False, text))
        except Exception as exc:  # noqa: BLE001
            self._post("github_check", (False, f"GitHub: {exc}"))

    def github_sign_in(self) -> None:
        """Let Git Credential Manager show its sign-in window, the same login git clone uses."""
        self.github_light.set("checking")
        self.github_status.set("GitHub: waiting for the Git sign-in window…")
        threading.Thread(target=lambda: self._github_check(load_settings(), interactive=True), daemon=True).start()

    def _on_github_check(self, payload: object) -> None:
        ok, text = payload  # type: ignore[misc]
        self.github_light.set("ok" if ok else "error")
        self.github_status.set(text[:90])

    def _on_jira_base(self, base: object) -> None:
        self._jira_base = str(base or "")

    def _on_jira_check(self, payload: object) -> None:
        ok, text = payload  # type: ignore[misc]
        self.jira_light.set("ok" if ok else "error")
        self.jira_status.set(text[:90])

    def _on_claude_check(self, payload: object) -> None:
        ok, text = payload  # type: ignore[misc]
        self.claude_light.set("ok" if ok else "error")
        self.claude_status.set(text[:90])
        self.check_link.configure(state=tk.NORMAL)

    def start_run(self) -> None:
        if self._busy:
            return
        tickets = _split_tickets(self.ticket_var.get())
        if not tickets:
            messagebox.showinfo("Jira Context Linker", "Enter a Jira key such as ABC-123, or paste a browse URL.", parent=self)
            return
        self._set_busy(True, f"Working on {tickets[0]}")
        options = {
            "use_claude": self.use_claude_var.get(),
            "confluence": self.confluence_var.get(),
            "write_json": self.json_var.get(),
            "scan_commits": self.commits_var.get(),
            "repo_choice": self.repo_var.get(),
        }
        threading.Thread(target=self._run_worker, args=(tickets, options), daemon=True).start()

    def _ask_shortcut(self, _event: object = None) -> str:
        self.start_ask()
        return "break"

    def start_ask(self) -> None:
        if self._busy:
            return
        question = self.question_text.get("1.0", tk.END).strip()
        if not question:
            messagebox.showinfo("Jira Context Linker", "Type a question in the Ask Claude box first.", parent=self)
            self.question_text.focus_set()
            return
        tickets = _split_tickets(self.ticket_var.get())
        if len(tickets) > 1:
            messagebox.showinfo("Jira Context Linker", "Ask Claude works with one ticket (or none). "
                                "Leave a single ticket in the box.", parent=self)
            return
        ticket = tickets[0] if tickets else None
        self._set_busy(True, "Researching")
        options = {"use_github": self.github_var.get(), "confluence": self.confluence_var.get(),
                   "scan_commits": self.commits_var.get(), "repo_choice": self.repo_var.get()}
        threading.Thread(target=self._ask_worker, args=(question, ticket, options), daemon=True).start()

    def _ask_worker(self, question: str, ticket: str | None, options: dict[str, object]) -> None:
        try:
            settings = load_settings()
            try:
                client = JiraClient(settings)
            except JiraError as exc:
                self._post("error", f"{exc}\n\nUse File > Edit settings to add your Jira details.")
                return
            start = time.monotonic()

            def say(message: str) -> None:
                self._post("log", f"[{time.monotonic() - start:5.1f}s] {message}")

            self._post("log_header", f"Ask{' about ' + ticket if ticket else ''}: {question[:80]}")
            try:
                result = answer_question(question, ticket, settings, client, say=say, **options)
            except (BadKeyError, JiraError, ClaudeError) as exc:
                self._post("error", str(exc))
                return
            self._post("report", result)
        except Exception as exc:  # noqa: BLE001
            self._post("error", f"Unexpected error: {exc}")
        finally:
            self._post("finished")

    def _follow_shortcut(self, _event: object = None) -> str:
        self.start_follow_up()
        return "break"

    def start_follow_up(self) -> None:
        if self._busy or not self._current:
            return
        question = self.follow_text.get("1.0", tk.END).strip()
        if not question:
            self.follow_text.focus_set()
            return
        self._set_busy(True, "Following up")
        options = {"use_github": self.github_var.get(), "confluence": self.confluence_var.get()}
        threading.Thread(target=self._follow_worker, args=(self._current, question, options), daemon=True).start()

    def _follow_worker(self, report: Path, question: str, options: dict[str, bool]) -> None:
        try:
            settings = load_settings()
            try:
                client: JiraClient | None = JiraClient(settings)
            except JiraError:
                client = None  # follow-ups still work from the saved evidence, just without new searches
            start = time.monotonic()

            def say(message: str) -> None:
                self._post("log", f"[{time.monotonic() - start:5.1f}s] {message}")

            self._post("log_header", f"Ask follow-up: {question[:80]}")
            try:
                result = follow_up(report, question, settings, say=say, client=client, **options)
            except ClaudeError as exc:
                self._post("error", str(exc))
                return
            self._post("follow_up_done", result)
        except Exception as exc:  # noqa: BLE001
            self._post("error", f"Unexpected error: {exc}")
        finally:
            self._post("finished")

    def _on_follow_up_done(self, result: object) -> None:
        assert isinstance(result, AskResult)
        self._append_log(f"Done: {result.path.name}", "ok")
        self.follow_text.delete("1.0", tk.END)
        self._scroll_to_last_follow_up = True
        self.reports_tree.selection_remove(*self.reports_tree.selection())
        self.refresh_reports(select=result.path)

    def _run_worker(self, tickets: list[str], options: dict[str, object]) -> None:
        try:
            settings = load_settings()
            try:
                client = JiraClient(settings)
            except JiraError as exc:
                self._post("error", f"{exc}\n\nUse File > Edit settings to add your Jira details.")
                return
            for raw in tickets:
                start = time.monotonic()

                def say(message: str, start: float = start) -> None:
                    self._post("log", f"[{time.monotonic() - start:5.1f}s] {message}")

                self._post("log_header", raw)
                try:
                    result = build_report(raw, settings, client, say=say, **options)
                except (BadKeyError, JiraError) as exc:
                    self._post("log_error", str(exc))
                    continue
                self._post("report", result)
        except Exception as exc:  # noqa: BLE001 -- surface anything unexpected instead of hanging busy
            self._post("error", f"Unexpected error: {exc}")
        finally:
            self._post("finished")

    def _on_log(self, message: object) -> None:
        self._append_log(str(message))

    def _on_log_header(self, raw: object) -> None:
        self._append_log(f"── {raw} ── {datetime.now():%H:%M:%S}")
        self.thinking.start("Researching" if str(raw).startswith("Ask") else f"Working on {raw}")

    def _on_log_error(self, message: object) -> None:
        self._append_log(f"Error: {message}", "error")

    def _on_error(self, message: object) -> None:
        self._append_log(f"Error: {message}", "error")
        messagebox.showerror("Jira Context Linker", str(message), parent=self)

    def _on_report(self, result: object) -> None:
        assert isinstance(result, (ReportResult, AskResult))
        self._append_log(f"Done: {result.path.name}", "ok")
        self.refresh_reports(select=result.path)

    def _on_finished(self, _payload: object) -> None:
        self._set_busy(False)

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = busy
        state = tk.DISABLED if busy else tk.NORMAL
        self.run_button.configure(state=state)
        self.ask_button.configure(state=state)
        self.ticket_entry.configure(state=state)
        self.repo_combo.configure(state=state)
        self.question_text.configure(state=state)
        self.follow_button.configure(state=state)
        self.follow_text.configure(state=state)
        if busy:
            self.thinking.start(message)
        else:
            self.thinking.stop()
            self.ticket_entry.focus_set()

    def _append_log(self, line: str, tag: str = "") -> None:
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, line + "\n", (tag,) if tag else ())
        self.log.see(tk.END)
        self.log.configure(state=tk.DISABLED)

    def clear_log(self) -> None:
        self.log.configure(state=tk.NORMAL)
        self.log.delete("1.0", tk.END)
        self.log.configure(state=tk.DISABLED)

    # --- reports ----------------------------------------------------------------

    def refresh_reports(self, select: Path | None = None) -> None:
        tree = self.reports_tree
        keep = select or self._current
        tree.delete(*tree.get_children())
        paths = sorted(REPORTS_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True) if REPORTS_DIR.is_dir() else []
        for path in paths:
            match = _REPORT_NAME_RE.match(path.stem)
            if match:
                key = match["key"] or ""
                if match["ask"]:
                    key = f"{key} (Q)" if key else "Question"
                when = datetime.strptime(match["stamp"][:13], "%Y%m%d-%H%M").strftime("%b %d, %H:%M")
            else:
                key, when = path.stem, datetime.fromtimestamp(path.stat().st_mtime).strftime("%b %d, %H:%M")
            tree.insert("", tk.END, iid=str(path), values=(key, _report_title(path), when))
        if keep and tree.exists(str(keep)):
            tree.selection_set(str(keep))
            tree.see(str(keep))
        elif not paths:
            self._current = None
            self.follow_bar.pack_forget()
            self.report_view.show_placeholder("No reports yet. Enter a ticket above and press Find context.")
            self._set_report_actions(False)

    def _on_report_selected(self, _event: object = None) -> None:
        selection = self.reports_tree.selection()
        if not selection:
            return
        focused = self.reports_tree.focus()
        path = Path(focused if focused in selection else selection[-1])
        if path == self._current and len(selection) > 1:
            return  # extending a multi-selection; keep the open report
        try:
            markdown = path.read_text(encoding="utf-8")
        except OSError as exc:
            self.report_view.show_placeholder(f"Could not read {path.name}: {exc}")
            self._set_report_actions(False)
            return
        self._current = path
        self.report_view.show_markdown(markdown)
        if self._scroll_to_last_follow_up:
            self.report_view.scroll_to_last("## Follow-up:")
            self._scroll_to_last_follow_up = False
        self._set_report_actions(True)
        if can_follow_up(path):
            self.follow_bar.pack(side=tk.BOTTOM, fill=tk.X, before=self.report_view)
        else:
            self.follow_bar.pack_forget()

    def _set_report_actions(self, enabled: bool) -> None:
        state = tk.NORMAL if enabled else tk.DISABLED
        for link in (self.copy_link, self.open_link, self.jira_link):
            link.configure(state=state)

    def _current_key(self) -> str | None:
        if not self._current:
            return None
        match = TICKET_KEY_RE.search(self._current.stem)
        return match.group(1) if match else None

    def delete_selected_reports(self) -> None:
        if self._busy:
            return
        selection = [Path(i) for i in self.reports_tree.selection()]
        if not selection:
            messagebox.showinfo("Jira Context Linker", "Select one or more reports to delete.", parent=self)
            return
        names = "\n".join(f"  {p.stem}" for p in selection[:8]) + ("\n  …" if len(selection) > 8 else "")
        if not messagebox.askyesno(
            "Delete reports",
            f"Move {len(selection)} report{'s' if len(selection) != 1 else ''} to the Recycle Bin?\n\n{names}",
            parent=self,
        ):
            return
        self._delete_reports(selection)

    def clear_all_reports(self) -> None:
        if self._busy:
            return
        reports = sorted(REPORTS_DIR.glob("*.md")) if REPORTS_DIR.is_dir() else []
        if not reports:
            messagebox.showinfo("Jira Context Linker", "There is no history to clear.", parent=self)
            return
        if not messagebox.askyesno(
            "Clear all history",
            f"Move all {len(reports)} reports and answers (with their saved conversations and ticket checks) "
            "to the Recycle Bin?",
            icon=messagebox.WARNING, parent=self,
        ):
            return
        self._delete_reports(reports, extra=sorted(CHECKS_DIR.glob("*.json")) if CHECKS_DIR.is_dir() else [])

    def _delete_reports(self, reports: list[Path], extra: list[Path] | None = None) -> None:
        files = [f for r in reports for f in _report_files(r)] + (extra or [])
        try:
            _recycle(files)
        except OSError as exc:
            messagebox.showerror("Jira Context Linker", f"Could not delete some files:\n{exc}", parent=self)
        if self._current in reports:
            self._current = None
            self.follow_bar.pack_forget()
            self._set_report_actions(False)
            self.report_view.show_placeholder("Deleted. Pick another report, or start a new one above.")
        self._append_log(f"Moved {len(reports)} report{'s' if len(reports) != 1 else ''} to the Recycle Bin.", "ok")
        self.refresh_reports()

    def copy_markdown(self) -> None:
        if not self._current:
            return
        self.clipboard_clear()
        self.clipboard_append(self._current.read_text(encoding="utf-8"))
        self._append_log(f"Copied {self._current.name} to the clipboard.", "ok")

    def open_report_file(self) -> None:
        if self._current and self._current.is_file():
            self._open_path(self._current)

    def open_in_jira(self) -> None:
        key = self._current_key()
        base = self._jira_base or load_settings().jira_base_url
        if key and base:
            webbrowser.open(f"{base}/browse/{key}")

    def open_reports_folder(self) -> None:
        REPORTS_DIR.mkdir(exist_ok=True)
        self._open_path(REPORTS_DIR)

    def edit_settings(self) -> None:
        if not ENV_PATH.exists() and ENV_EXAMPLE.exists():
            shutil.copyfile(ENV_EXAMPLE, ENV_PATH)
        if not ENV_PATH.exists():
            ENV_PATH.write_text("", encoding="utf-8")
        try:
            subprocess.Popen(["notepad.exe", str(ENV_PATH)])  # noqa: S603,S607
        except OSError:
            self._open_path(ENV_PATH)
        self._append_log("Settings are read fresh on every run; use Check again after saving .env.")

    def _open_path(self, path: Path) -> None:
        try:
            os.startfile(path)  # noqa: S606
        except OSError as exc:
            messagebox.showerror("Jira Context Linker", f"Could not open {path}:\n{exc}", parent=self)

    def on_close(self) -> None:
        if self._busy and not messagebox.askyesno(
            "Jira Context Linker", "Claude is still working. Quit anyway?", parent=self
        ):
            return
        self.destroy()


def main() -> None:
    app = LinkerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
