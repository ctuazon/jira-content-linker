"""Invoke Claude the same way Peer Review App does (app/claude_runner.py):

  * WSL Claude Code (your Claude SSO login) -- the default
  * Windows Claude Code CLI, when installed
  * Anthropic API, when CLAUDE_MODE=api and an API key is available

Each call returns structured JSON validated against a schema. No tools are
granted: Claude only reasons over the Jira data this tool already fetched.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

from linker import Settings

StatusCallback = Callable[[str], None]

NO_TOOLS = "Read,Grep,Glob,Bash,Edit,Write,WebFetch,WebSearch,Task"
_SAFE_MODEL_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
CREATE_NO_WINDOW = 0x08000000


class ClaudeError(RuntimeError):
    pass


# --- process helpers (from app/process_util.py, app/auth_flows.py) -------------

def _no_window_kwargs() -> dict[str, Any]:
    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    return {"startupinfo": startupinfo, "creationflags": CREATE_NO_WINDOW}


def _kill_tree(process: subprocess.Popen) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)],
                           capture_output=True, check=False, **_no_window_kwargs())
        else:
            process.kill()
    except OSError:
        pass


_WSL_CLAUDE_CACHE: list[str | None] = []


def find_wsl_claude() -> str | None:
    """Locate the Claude Code binary inside WSL (PATH, or the VS Code / Cursor extension)."""
    if _WSL_CLAUDE_CACHE:
        return _WSL_CLAUDE_CACHE[0]
    found: str | None = None
    if shutil.which("wsl"):
        script = r"""
if command -v claude >/dev/null 2>&1; then command -v claude; exit 0; fi
for base in "$HOME/.vscode-server/extensions" "$HOME/.cursor-server/extensions"; do
  if [ -d "$base" ]; then
    f=$(find "$base" -path '*/native-binary/claude' -type f 2>/dev/null | sort | tail -n 1)
    if [ -n "$f" ]; then printf '%s\n' "$f"; exit 0; fi
  fi
done
exit 1
"""
        try:
            completed = subprocess.run(
                ["wsl", "-e", "bash", "-lc", script], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=60, check=False, **_no_window_kwargs(),
            )
            lines = (completed.stdout or "").strip().splitlines()
            if lines and completed.returncode == 0:
                found = lines[-1].strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    _WSL_CLAUDE_CACHE.append(found)
    return found


def win_path_to_wsl(path: Path) -> str:
    resolved = path.resolve()
    drive = resolved.drive.rstrip(":").lower()
    rest = resolved.as_posix()
    if ":" in rest:
        rest = rest.split(":", 1)[1]
    return f"/mnt/{drive}{rest}"


def resolve_claude_cli(configured_path: str = "claude") -> str | None:
    path = shutil.which(configured_path)
    if path:
        return path
    for candidate in (configured_path, r"%LOCALAPPDATA%\Programs\claude\claude.exe"):
        expanded = os.path.expandvars(candidate)
        if shutil.which(expanded) or os.path.isfile(expanded):
            return expanded
    return None


def check_claude_auth(settings: Settings) -> tuple[bool, str]:
    """(ok, detail) for whichever Claude path will be used."""
    if _use_api(settings):
        return True, "Anthropic API key"
    cli = resolve_claude_cli(settings.claude_cli_path)
    if cli and not (settings.use_wsl_claude or settings.claude_mode == "wsl"):
        command = [cli, "auth", "status"]
    else:
        claude = find_wsl_claude()
        if not claude:
            if cli:
                command = [cli, "auth", "status"]
            else:
                return False, "Claude Code not found (Windows or WSL)"
        else:
            command = ["wsl", "-e", "bash", "-lc", f'"{claude}" auth status 2>/dev/null']
    try:
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=30, check=False, **_no_window_kwargs())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"status check failed ({exc})"
    try:
        data = json.loads((completed.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        return False, (completed.stdout or "").strip()[:120] or "not signed in"
    if data.get("loggedIn"):
        org = f" / {data['orgName']}" if data.get("orgName") else ""
        return True, f"{data.get('email') or 'unknown'}{org} ({data.get('authMethod') or 'claude.ai'})"
    return False, "not signed in -- run `claude` in WSL and use /login"


# --- CLI / WSL path ------------------------------------------------------------

def _effort_ok(model: str, effort: str | None) -> bool:
    return bool(effort in _EFFORTS and not model.startswith("claude-haiku-4"))


def _parse_stream(process: subprocess.Popen, timeout: float, on_status: StatusCallback | None) -> tuple[dict[str, Any] | None, str, str, int]:
    """Read stream-json output. Returns (structured_output, final_text, stderr, return code)."""
    structured: dict[str, Any] | None = None
    final_text = ""
    timed_out = threading.Event()

    def on_timeout() -> None:
        timed_out.set()
        _kill_tree(process)

    timer = threading.Timer(timeout, on_timeout)
    timer.daemon = True
    timer.start()
    try:
        assert process.stdout is not None
        for raw in process.stdout:
            try:
                obj = json.loads(raw.strip() or "null")
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("type") == "stream_event" and on_status:
                ev = obj.get("event") or {}
                block = ev.get("content_block") or {}
                if ev.get("type") == "content_block_start" and block.get("type") == "thinking":
                    on_status("thinking")
            elif obj.get("type") == "result":
                if isinstance(obj.get("structured_output"), dict):
                    structured = obj["structured_output"]
                if isinstance(obj.get("result"), str):
                    final_text = obj["result"]
                if on_status and isinstance(obj.get("total_cost_usd"), (int, float)):
                    on_status(f"cost ${obj['total_cost_usd']:.4f}")
        stderr = process.stderr.read() if process.stderr is not None else ""
        try:
            code = process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            _kill_tree(process)
            code = 1
    finally:
        timer.cancel()
    if timed_out.is_set():
        raise ClaudeError(f"Claude timed out after {timeout / 60:g} min.")
    return structured, final_text, stderr, code


def _flags(model: str, effort: str | None, schema_arg: str) -> list[str]:
    flags = ["--allowedTools", "", "--disallowedTools", NO_TOOLS, "--model", model]
    if _effort_ok(model, effort):
        flags += ["--effort", str(effort)]
    return flags + ["--json-schema", schema_arg, "--output-format", "stream-json",
                    "--verbose", "--include-partial-messages"]


def _run_wsl(prompt_path: Path, schema: dict[str, Any], model: str, effort: str | None,
             timeout: float, on_status: StatusCallback | None) -> tuple[dict[str, Any] | None, str, str, int]:
    claude = find_wsl_claude()
    if not claude:
        raise ClaudeError("Claude Code not found in WSL. Install it or sign in via the Peer Review App.")
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", prefix="jcl-", delete=False) as handle:
        handle.write(json.dumps(schema))
        schema_path = Path(handle.name)
    try:
        args = _flags(model, effort, "__SCHEMA__")
        quoted = " ".join(
            f'"$(cat {shlex.quote(win_path_to_wsl(schema_path))})"' if a == "__SCHEMA__" else shlex.quote(a)
            for a in args
        )
        bash = f'set -euo pipefail; "{claude}" -p {quoted} < {shlex.quote(win_path_to_wsl(prompt_path))}'
        process = subprocess.Popen(["wsl", "-e", "bash", "-lc", bash], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                   errors="replace", **_no_window_kwargs())
        return _parse_stream(process, timeout, on_status)
    finally:
        schema_path.unlink(missing_ok=True)


def _run_windows_cli(cli: str, prompt_path: Path, schema: dict[str, Any], model: str, effort: str | None,
                     timeout: float, on_status: StatusCallback | None) -> tuple[dict[str, Any] | None, str, str, int]:
    with open(prompt_path, "r", encoding="utf-8") as stdin_handle:
        process = subprocess.Popen([cli, "-p", *_flags(model, effort, json.dumps(schema))],
                                   stdin=stdin_handle, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", errors="replace", **_no_window_kwargs())
        return _parse_stream(process, timeout, on_status)


def _coerce(structured: dict[str, Any] | None, text: str) -> dict[str, Any] | None:
    if structured is not None:
        return structured
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None


def _run_cli(prompt: str, schema: dict[str, Any], settings: Settings, model: str, effort: str | None,
             on_status: StatusCallback | None) -> dict[str, Any]:
    if not _SAFE_MODEL_RE.match(model):
        raise ClaudeError(f"Invalid model id: {model!r}")
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", prefix="jcl-", delete=False) as handle:
        handle.write(prompt)
        prompt_path = Path(handle.name)
    try:
        cli = resolve_claude_cli(settings.claude_cli_path)
        prefer_wsl = settings.claude_mode == "wsl" or settings.use_wsl_claude or not cli
        if prefer_wsl and find_wsl_claude():
            result = _run_wsl(prompt_path, schema, model, effort, settings.timeout_seconds, on_status)
        elif cli:
            result = _run_windows_cli(cli, prompt_path, schema, model, effort, settings.timeout_seconds, on_status)
        else:
            raise ClaudeError("Claude Code was not found in WSL or on Windows.")
    finally:
        prompt_path.unlink(missing_ok=True)
    structured, text, stderr, code = result
    data = _coerce(structured, text)
    if data is not None:
        return data
    err = (stderr or text).strip()
    if "not logged in" in err.lower() or "/login" in err.lower():
        raise ClaudeError("Claude is not signed in. Run `claude` in WSL and use /login (or sign in via the Peer Review App).")
    raise ClaudeError(err[:500] or f"Claude returned no structured output (exit {code}).")


# --- API path ------------------------------------------------------------------

def _use_api(settings: Settings) -> bool:
    return settings.claude_mode == "api" and bool(settings.anthropic_api_key)


def _run_api(prompt: str, schema: dict[str, Any], settings: Settings, model: str, effort: str | None,
             on_status: StatusCallback | None) -> dict[str, Any]:
    try:
        import anthropic
    except ImportError as exc:
        raise ClaudeError("CLAUDE_MODE=api needs the anthropic package: pip install anthropic") from exc
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key).with_options(timeout=settings.timeout_seconds)
    output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
    if _effort_ok(model, effort):
        output_config["effort"] = effort
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": 16_000,
        "messages": [{"role": "user", "content": prompt}],
        "output_config": output_config,
    }
    # Fable reasons on every request and rejects an explicit thinking config;
    # Haiku 4.5 has no adaptive thinking.
    if not (model.startswith("claude-haiku-4") or model.startswith("claude-fable")):
        kwargs["thinking"] = {"type": "adaptive"}
    try:
        with client.messages.stream(**kwargs) as stream:
            message = stream.get_final_message()
    except anthropic.APIStatusError as exc:
        raise ClaudeError(f"Claude API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise ClaudeError(f"Could not reach the Claude API: {exc}") from exc
    if getattr(message, "stop_reason", "") == "refusal":
        raise ClaudeError("The model declined this request.")
    text = "".join(getattr(b, "text", "") or "" for b in message.content)
    data = _coerce(None, text)
    if data is None:
        raise ClaudeError("Claude API returned no JSON.")
    return data


def ask_json(prompt: str, schema: dict[str, Any], settings: Settings, *, model: str,
             effort: str | None = None, on_status: StatusCallback | None = None) -> dict[str, Any]:
    if _use_api(settings):
        return _run_api(prompt, schema, settings, model, effort, on_status)
    return _run_cli(prompt, schema, settings, model, effort, on_status)
