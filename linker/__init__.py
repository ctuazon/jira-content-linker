"""Settings for jira-context-linker.

Every value resolves in this order:
  1. the process environment
  2. a .env file next to jcl.py
  3. (Jira and Anthropic key only) the Peer Review App's data/config.json and
     its Windows Credential Manager entries, so credentials saved there work
     here without being entered twice.
     (GitHub only) the GitHub Code Scanner's .env.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = ROOT / "reports"
DEFAULT_PEER_REVIEW_DIR = ROOT.parent / "Peer Review App"
DEFAULT_CODE_SCANNER_DIR = ROOT.parent / "Github Code Scanner"
_PEER_REVIEW_KEYRING_SERVICE = "peer-review-app"
DEFAULT_REPO_CHOICES = "marketplace,marketplace-search-api,aim-myt,marketplace-ssr,lib-importer-tasks-php"
DEFAULT_PROJECT_REPOS = "ASEARCH:marketplace,marketplace-search-api"


def _read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name] = value
    return values


def _parse_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _parse_float(value: str, default: float) -> float:
    try:
        return float(value) if value else default
    except ValueError:
        return default


def _parse_list(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _parse_project_repos(value: str) -> dict[str, list[str]]:
    """'ASEARCH:marketplace,marketplace-search-api;AESP:aim-myt' -> {project: [repos]}."""
    mapping: dict[str, list[str]] = {}
    for part in value.split(";"):
        project, _, repos = part.partition(":")
        if project.strip() and repos.strip():
            mapping[project.strip().upper()] = _parse_list(repos)
    return mapping


def _peer_review_secret(name: str) -> str:
    try:
        import keyring

        return keyring.get_password(_PEER_REVIEW_KEYRING_SERVICE, name) or ""
    except Exception:  # noqa: BLE001 -- missing package or no usable backend
        return ""


@dataclass
class Settings:
    jira_base_url: str
    jira_email: str
    jira_api_token: str
    claude_mode: str  # "cli" | "wsl" | "api"
    use_wsl_claude: bool
    claude_cli_path: str
    claude_model: str
    claude_plan_model: str
    claude_effort: str
    anthropic_api_key: str
    timeout_seconds: float
    search_confluence: bool
    github_auth: str = "auto"  # "auto" | "git" | "token"
    github_token: str = ""
    github_org: str = ""
    github_api_url: str = "https://api.github.com"
    repo_choices: list[str] = field(default_factory=list)
    project_repos: dict[str, list[str]] = field(default_factory=dict)
    context_max_age_hours: float = 24.0
    jira_source: str = ""

    @property
    def jira_configured(self) -> bool:
        return bool(self.jira_base_url and self.jira_email and self.jira_api_token)


def load_settings() -> Settings:
    dotenv = _read_dotenv(ROOT / ".env")

    def env(name: str, default: str = "") -> str:
        return (os.environ.get(name) or dotenv.get(name) or default).strip()

    peer_dir = Path(env("PEER_REVIEW_APP_DIR") or DEFAULT_PEER_REVIEW_DIR)
    peer_config: dict[str, Any] = {}
    try:
        peer_config = json.loads((peer_dir / "data" / "config.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    peer_dotenv = _read_dotenv(peer_dir / ".env")
    scanner_dotenv = _read_dotenv(Path(env("CODE_SCANNER_DIR") or DEFAULT_CODE_SCANNER_DIR) / ".env")

    def github(name: str, default: str = "") -> str:
        return env(name) or (scanner_dotenv.get(name) or "").strip() or default

    def jira(name: str, config_key: str) -> tuple[str, str]:
        value = env(name)
        if value:
            return value, "jira-context-linker .env"
        value = str(peer_config.get(config_key) or "").strip() or (
            _peer_review_secret(config_key) if config_key.endswith("token") else ""
        )
        if value:
            return value, "Peer Review App settings"
        value = (peer_dotenv.get(name) or "").strip()
        return value, ("Peer Review App .env" if value else "")

    base_url, src_url = jira("JIRA_BASE_URL", "jira_base_url")
    email, _ = jira("JIRA_EMAIL", "jira_email")
    token, src_token = jira("JIRA_API_TOKEN", "jira_api_token")

    try:
        minutes = float(env("CLAUDE_TIMEOUT_MINUTES", "10"))
    except ValueError:
        minutes = 10.0

    return Settings(
        jira_base_url=base_url.rstrip("/"),
        jira_email=email,
        jira_api_token=token,
        claude_mode=env("CLAUDE_MODE", "cli").lower(),
        use_wsl_claude=_parse_bool(env("USE_WSL_CLAUDE"), default=True),
        claude_cli_path=env("CLAUDE_CLI_PATH", "claude"),
        claude_model=env("CLAUDE_MODEL", "claude-sonnet-5-5"),
        claude_plan_model=env("CLAUDE_PLAN_MODEL", "claude-haiku-4-5-20251001"),
        claude_effort=env("CLAUDE_EFFORT", "medium"),
        anthropic_api_key=env("ANTHROPIC_API_KEY")
        or str(peer_config.get("anthropic_api_key") or "")
        or _peer_review_secret("anthropic_api_key"),
        timeout_seconds=max(1.0, minutes) * 60,
        search_confluence=_parse_bool(env("SEARCH_CONFLUENCE"), default=True),
        github_auth=github("GITHUB_AUTH", "auto").lower(),
        github_token=github("GITHUB_TOKEN"),
        github_org=github("GITHUB_ORG"),
        github_api_url=github("GITHUB_API_URL", "https://api.github.com").rstrip("/"),
        repo_choices=_parse_list(env("REPO_CHOICES", DEFAULT_REPO_CHOICES)),
        project_repos=_parse_project_repos(env("PROJECT_REPOS", DEFAULT_PROJECT_REPOS)),
        context_max_age_hours=_parse_float(env("CONTEXT_MAX_AGE_HOURS"), 24.0),
        jira_source=", ".join(sorted({s for s in (src_url, src_token) if s})),
    )
