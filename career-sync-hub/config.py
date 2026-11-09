"""Centralized configuration for career-sync-hub.

Configured local-first for `ChatOllama` (`langchain-ollama`) with zero API keys
required, plus optional Google Gemini (`ChatGoogleGenerativeAI`) support via `.env`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
# Load `career-sync-hub/.env` first, falling back to parent `automation/.env` if placed there
if (BASE_DIR / ".env").is_file():
    load_dotenv(BASE_DIR / ".env")
elif (BASE_DIR.parent / ".env").is_file():
    load_dotenv(BASE_DIR.parent / ".env")


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings loaded from environment variables."""

    # LLM configuration (defaults to local Ollama via LangChain ChatOllama)
    llm_provider: str
    ollama_model: str
    ollama_base_url: str
    ollama_num_ctx: int
    llm_temperature: float

    # Optional Gemini configuration (if GEMINI_API_KEY is supplied in .env)
    gemini_api_key: str
    gemini_model: str

    # GitHub & React Portfolio sync
    github_pat: str
    portfolio_repo_url: str
    portfolio_projects_path: str
    portfolio_branch: str

    git_author_name: str
    git_author_email: str

    # Security & Desktop CLI sync
    webhook_secret: str
    hub_server_url: str

    data_dir: Path
    temp_root_dir: Path
    max_diff_chars: int = 24_000
    max_history_items: int = 30
    allowed_git_hosts: tuple[str, ...] = field(
        default=("github.com", "gitlab.com", "bitbucket.org")
    )

    @classmethod
    def from_env(cls) -> "Settings":
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
        ).strip()

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

        return cls(
            llm_provider=provider,
            ollama_model=ollama_model,
            ollama_base_url=ollama_base_url,
            ollama_num_ctx=int(os.environ.get("OLLAMA_NUM_CTX", "16384")),
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
            webhook_secret=os.environ.get("WEBHOOK_SECRET", "").strip(),
            hub_server_url=os.environ.get("HUB_SERVER_URL", "").strip().rstrip("/"),
            data_dir=data_dir,
            temp_root_dir=temp_root,
            max_diff_chars=int(os.environ.get("MAX_DIFF_CHARS", "24000")),
            max_history_items=int(os.environ.get("MAX_HISTORY_ITEMS", "30")),
            allowed_git_hosts=allowed_hosts,
        )
