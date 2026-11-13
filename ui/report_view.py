"""Read-only Text widget that renders the Markdown subset linker/report.py writes."""
from __future__ import annotations

import re
import tkinter as tk
import webbrowser
from tkinter import scrolledtext

from ui.widgets import ACCENT, MUTED, TEXT, soften_text

_INLINE_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)|\*\*(.+?)\*\*|`([^`]+)`|(?<!\w)_([^_\n]+)_(?!\w)")
_NUMBERED_RE = re.compile(r"^(\d+)[.)]\s+(.*)$")
_CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")
_SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>")


def _cells(line: str) -> list[str]:
    inner = line.strip()
    inner = inner[1:] if inner.startswith("|") else inner
    inner = inner[:-1] if inner.endswith("|") and not inner.endswith("\\|") else inner
    return [c.strip().replace("\\|", "|") for c in _CELL_SPLIT_RE.split(inner)]


def _is_separator(line: str) -> bool:
    return line.strip().startswith("|") and all(set(c) <= {"-", ":"} and c for c in _cells(line))


class ReportView(scrolledtext.ScrolledText):
    def __init__(self, master: tk.Misc, **kw) -> None:
        super().__init__(master, wrap=tk.WORD, font=("Segoe UI", 10), foreground=TEXT, **kw)
        soften_text(self)
        self.configure(padx=14, pady=10, cursor="arrow")
        self.tag_configure("h1", font=("Segoe UI", 15, "bold"), spacing1=4, spacing3=6)
        self.tag_configure("h2", font=("Segoe UI", 12, "bold"), spacing1=14, spacing3=4)
        self.tag_configure("h3", font=("Segoe UI", 10, "bold"), foreground=MUTED, spacing1=12, spacing3=2)
        self.tag_configure("h4", font=("Segoe UI", 10, "bold"), spacing1=10, spacing3=2)
        self.tag_configure("bold", font=("Segoe UI", 10, "bold"))
        self.tag_configure("inline_code", font=("Consolas", 9), background="#eff1f3")
        self.tag_configure("muted", foreground=MUTED)
        self.tag_configure("italic", font=("Segoe UI", 10, "italic"), foreground=MUTED)
        self.tag_configure("bullet", lmargin1=8, lmargin2=22, spacing1=2)
        self.tag_configure("bullet2", lmargin1=26, lmargin2=40, spacing1=2)
        self.tag_configure("row", lmargin1=8, lmargin2=22, spacing1=8)
        self.tag_configure("rowdetail", lmargin1=22, lmargin2=22, foreground=MUTED)
        self.tag_configure("code", font=("Consolas", 9), foreground=MUTED, lmargin1=8, lmargin2=8)
        self.tag_configure("link", foreground=ACCENT)
        self.tag_bind("link", "<Enter>", lambda _e: self.configure(cursor="hand2"))
        self.tag_bind("link", "<Leave>", lambda _e: self.configure(cursor="arrow"))
        self._links: dict[str, str] = {}
        self.show_placeholder("Enter a ticket above, or pick an earlier report on the left.")

    # --- public -------------------------------------------------------------

    def show_placeholder(self, message: str) -> None:
        self._reset()
        self.insert(tk.END, message, ("italic",))
        self.configure(state=tk.DISABLED)

    def show_markdown(self, markdown: str) -> None:
        self._reset()
        lines = markdown.splitlines()
        in_code = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("```"):
                in_code = not in_code
                continue
            if in_code:
                self.insert(tk.END, line + "\n", ("code",))
            elif stripped.startswith("# "):
                self._inline(stripped[2:], ("h1",))
            elif stripped.startswith("## "):
                self._inline(stripped[3:], ("h2",))
            elif stripped.startswith("### ") or stripped.startswith("#### "):
                self._inline(stripped.lstrip("#").strip(), ("h4",))
            elif stripped.startswith("<details>"):
                match = _SUMMARY_RE.search(stripped)
                if match:
                    self.insert(tk.END, match.group(1) + "\n", ("h3",))
            elif stripped == "</details>":
                continue
            elif stripped.startswith("|"):
                if _is_separator(stripped):
                    continue
                if i + 1 < len(lines) and _is_separator(lines[i + 1]):
                    continue  # header row: the layout below labels itself
                self._table_row(_cells(stripped))
            elif stripped.startswith(("- ", "* ")):
                indent = ("bullet2",) if line.startswith(("  ", "	")) else ("bullet",)
                self._inline("•  " + stripped[2:], indent)
            elif _NUMBERED_RE.match(stripped):
                number, rest = _NUMBERED_RE.match(stripped).groups()
                self._inline(f"{number}.  {rest}", ("bullet",))
            elif stripped.startswith("_") and stripped.endswith("_") and len(stripped) > 2:
                self.insert(tk.END, stripped[1:-1] + "\n", ("italic",))
            elif stripped:
                self._inline(stripped, ())
            else:
                self.insert(tk.END, "\n")
        self.configure(state=tk.DISABLED)
        self.yview_moveto(0)

    def scroll_to_last(self, text: str) -> None:
        """Scroll so the last line starting with this heading text is at the top."""
        label = text.lstrip("#").strip()
        index = self.search(label, tk.END, backwards=True, stopindex="1.0")
        if index:
            self.yview(index)

    # --- rendering ----------------------------------------------------------

    def _reset(self) -> None:
        self.configure(state=tk.NORMAL)
        self.delete("1.0", tk.END)
        for tag in self._links:
            self.tag_delete(tag)
        self._links.clear()

    def _table_row(self, cells: list[str]) -> None:
        if len(cells) < 4:
            self._inline("  ·  ".join(c for c in cells if c), ("row",))
            return
        lead = " ".join(c for c in cells[:2] if c)
        self._inline(lead, ("row",))
        middle = "  ·  ".join(c for c in cells[2:-1] if c)
        if middle:
            self.insert(tk.END, middle + "\n", ("rowdetail",))
        if cells[-1]:
            self._inline(cells[-1], ("rowdetail",))

    def _inline(self, text: str, tags: tuple[str, ...]) -> None:
        pos = 0
        for match in _INLINE_RE.finditer(text):
            self.insert(tk.END, text[pos:match.start()], tags)
            if match.group(1) is not None:
                self._insert_link(match.group(1), match.group(2), tags)
            elif match.group(4) is not None:
                self.insert(tk.END, match.group(4), tags + ("inline_code",))
            elif match.group(5) is not None:
                self.insert(tk.END, match.group(5), tags + ("italic",))
            else:
                self.insert(tk.END, match.group(3), tags + ("bold",))
            pos = match.end()
        self.insert(tk.END, text[pos:] + "\n", tags)

    def _insert_link(self, label: str, url: str, tags: tuple[str, ...]) -> None:
        tag = f"link-{len(self._links)}"
        self._links[tag] = url
        self.tag_bind(tag, "<Button-1>", lambda _e, u=url: webbrowser.open(u))
        self.insert(tk.END, label, tags + ("link", tag))
