"""Centralized configuration for career-sync-hub (README-driven release pipeline).

Configured local-first for `ChatOllama` (`langchain-ollama`), automated GitHub
React portfolio commits, and automated LinkedIn Feed posting via `LINKEDIN_ACCESS_TOKEN`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
_LAST_ENV_MTIME_NS: Optional[int] = None


def _sync_dotenv_if_modified() -> None:
    """Reload `.env` automatically whenever created or edited on disk."""
    global _LAST_ENV_MTIME_NS

    env_path = BASE_DIR / ".env"
    if not env_path.is_file() and (BASE_DIR.parent / ".env").is_file():
        env_path = BASE_DIR.parent / ".env"

    if not env_path.is_file():
        return

    try:
        mtime_ns = env_path.stat().st_mtime_ns
    except OSError:
        return

    if _LAST_ENV_MTIME_NS is None:
        load_dotenv(env_path, override=False)
        _LAST_ENV_MTIME_NS = mtime_ns
    elif mtime_ns != _LAST_ENV_MTIME_NS:
        load_dotenv(env_path, override=True)
        _LAST_ENV_MTIME_NS = mtime_ns


_sync_dotenv_if_modified()


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings loaded from environment variables."""

    # LLM configuration (defaults to local Ollama via LangChain ChatOllama)
    llm_provider: str
    ollama_model: str
    ollama_base_url: str
    ollama_num_ctx: int
    llm_temperature: float

    # Optional Google Gemini configuration
    gemini_api_key: str
    gemini_model: str

    # GitHub & React Portfolio sync
    github_pat: str
    portfolio_repo_url: str
    portfolio_projects_path: str
    portfolio_branch: str
    git_author_name: str
    git_author_email: str

    # Official LinkedIn API configuration (for hands-free LinkedIn feed posts)
    linkedin_access_token: str
    linkedin_person_urn: str
    linkedin_visibility: str

    # Security & Desktop CLI sync
    webhook_secret: str
    hub_server_url: str

    data_dir: Path
    temp_root_dir: Path
    max_readme_chars: int = 14_000
    max_history_items: int = 30
    allowed_git_hosts: tuple[str, ...] = field(
        default=("github.com", "gitlab.com", "bitbucket.org")
    )

    @classmethod
    def from_env(cls) -> "Settings":
        _sync_dotenv_if_modified()

        provider = os.environ.get("LLM_PROVIDER", "ollama").strip().lower() or "ollama"
        ollama_model = (
            os.environ.get("OLLAMA_MODEL")
            or os.environ.get("LLM_MODEL")
            or "qwen2.5-coder:7b"
        ).strip()
        ollama_base_url = (
            os.environ.get("OLLAMA_BASE_URL")
            or os.environ.get("OLLAMA_HOST")
            or "http://localhost:11434"
        ).strip().rstrip("/")

        visibility = (
            os.environ.get("LINKEDIN_VISIBILITY", "PUBLIC").strip().upper() or "PUBLIC"
        )
        if visibility not in {"PUBLIC", "CONNECTIONS"}:
            visibility = "PUBLIC"

        raw_hosts = os.environ.get(
            "ALLOWED_GIT_HOSTS", "github.com,gitlab.com,bitbucket.org"
        )
        allowed_hosts = tuple(
            h.strip().lower() for h in raw_hosts.split(",") if h.strip()
        )

        data_dir = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data"))).resolve()
        temp_root = Path(
            os.environ.get("TEMP_ROOT_DIR", str(BASE_DIR / "temp_workspace"))
        ).resolve()

        webhook_secret = os.environ.get("WEBHOOK_SECRET", "").strip()
        if webhook_secret == "generate-a-strong-random-32-char-secret":
            webhook_secret = ""

        return cls(
            llm_provider=provider,
            ollama_model=ollama_model,
            ollama_base_url=ollama_base_url,
            ollama_num_ctx=int(os.environ.get("OLLAMA_NUM_CTX", "4096")),
            llm_temperature=float(os.environ.get("LLM_TEMPERATURE", "0.2")),
            gemini_api_key=(
                os.environ.get("GEMINI_API_KEY")
                or os.environ.get("GOOGLE_API_KEY")
                or ""
            ).strip(),
            gemini_model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip(),
            github_pat=os.environ.get("GITHUB_PAT", "").strip(),
            portfolio_repo_url=os.environ.get("PORTFOLIO_REPO_URL", "").strip(),
            portfolio_projects_path=os.environ.get(
                "PORTFOLIO_PROJECTS_PATH", "src/data/projects.js"
            ).strip(),
            portfolio_branch=os.environ.get("PORTFOLIO_BRANCH", "").strip(),
            git_author_name=os.environ.get(
                "GIT_AUTHOR_NAME", "career-sync-hub[bot]"
            ).strip(),
            git_author_email=os.environ.get(
                "GIT_AUTHOR_EMAIL", "career-sync-hub@users.noreply.github.com"
            ).strip(),
            linkedin_access_token=os.environ.get("LINKEDIN_ACCESS_TOKEN", "").strip(),
            linkedin_person_urn=os.environ.get("LINKEDIN_PERSON_URN", "").strip(),
            linkedin_visibility=visibility,
            webhook_secret=webhook_secret,
            hub_server_url=os.environ.get(
                "HUB_SERVER_URL", "http://localhost:8000"
            ).strip().rstrip("/"),
            data_dir=data_dir,
            temp_root_dir=temp_root,
            max_readme_chars=int(os.environ.get("MAX_README_CHARS", "14000")),
            max_history_items=int(os.environ.get("MAX_HISTORY_ITEMS", "30")),
            allowed_git_hosts=allowed_hosts,
        )
