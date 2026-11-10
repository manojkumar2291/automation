"""Configuration loader for Career Sync Hub (`config.py`).

Loads environment variables from `.env` into a clean, typed `Settings` object.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
_LAST_ENV_MTIME_NS: Optional[int] = None


def _reload_dotenv_if_changed() -> None:
    """Reload `.env` automatically whenever it is edited on disk."""
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


_reload_dotenv_if_changed()


@dataclass(frozen=True)
class Settings:
    """Application settings loaded from `.env`."""

    # 1. Local Ollama Model Settings
    ollama_model: str
    ollama_base_url: str
    ollama_num_ctx: int
    llm_temperature: float

    # 2. GitHub & React Portfolio Settings
    github_pat: str
    portfolio_repo_url: str
    portfolio_projects_path: str
    portfolio_branch: str
    git_author_name: str
    git_author_email: str

    # 3. LinkedIn API Settings
    linkedin_access_token: str
    linkedin_person_urn: str
    linkedin_visibility: str

    # 4. Local Server & Storage Settings
    hub_server_url: str
    data_dir: Path
    temp_root_dir: Path
    max_readme_chars: int = 14_000

    @classmethod
    def from_env(cls) -> "Settings":
        _reload_dotenv_if_changed()

        github_pat = os.environ.get("GITHUB_PAT", "").strip()
        if github_pat in {"github_pat_replace_me", "ghp_replace_me", "your_github_pat_here"}:
            github_pat = ""

        visibility = os.environ.get("LINKEDIN_VISIBILITY", "PUBLIC").strip().upper()
        if visibility not in {"PUBLIC", "CONNECTIONS"}:
            visibility = "PUBLIC"

        return cls(
            ollama_model=(
                os.environ.get("OLLAMA_MODEL")
                or os.environ.get("LLM_MODEL")
                or "qwen2.5-coder:7b"
            ).strip(),
            ollama_base_url=(
                os.environ.get("OLLAMA_BASE_URL")
                or os.environ.get("OLLAMA_HOST")
                or "http://localhost:11434"
            ).strip().rstrip("/"),
            ollama_num_ctx=int(os.environ.get("OLLAMA_NUM_CTX", "4096")),
            llm_temperature=float(os.environ.get("LLM_TEMPERATURE", "0.2")),
            github_pat=github_pat,
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
            hub_server_url=os.environ.get(
                "HUB_SERVER_URL", "http://localhost:8000"
            ).strip().rstrip("/"),
            data_dir=Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data"))).resolve(),
            temp_root_dir=Path(
                os.environ.get("TEMP_ROOT_DIR", str(BASE_DIR / "temp_workspace"))
            ).resolve(),
            max_readme_chars=int(os.environ.get("MAX_README_CHARS", "14000")),
        )
