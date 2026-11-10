"""Step 1: README-Driven Career Sync Hub (`server.py`).

Workflow:
1. Receive a completed project's `README.md` (via local path, raw markdown, or GitHub URL).
2. Extract structured portfolio, ATS resume, LinkedIn profile, and LinkedIn post copy via LangChain `ChatOllama`.
3. Surgically upsert your React portfolio's `projects.js` and push to GitHub.
4. Automatically publish a launch post to LinkedIn via `LINKEDIN_ACCESS_TOKEN`.
5. Save `data/latest_generation.json` + `data/latest_brief.md` and return a complete Executive Brief.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import threading
import time
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import uuid

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request as FastAPIRequest
from fastapi.responses import PlainTextResponse
import git
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_ollama import ChatOllama

from config import Settings
from linkedin_publisher import compose_linkedin_post_body, publish_linkedin_feed_post
from models import (
    AnalyzeRequest,
    AnalyzeResponse,
    EvaluationResult,
    LLMEvaluationSchema,
    slugify_id,
)
from portfolio_updater import normalize_repo_url, upsert_portfolio_project

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("career-sync-hub.server")

app = FastAPI(
    title="career-sync-hub",
    description="README-driven career automation hub for Portfolio, FlowCV Resume, and LinkedIn.",
    version="2.0.0",
)

_PORTFOLIO_LOCK = threading.Lock()
_DATA_LOCK = threading.Lock()

EMBEDDING_MODEL_KEYWORDS = ("embed", "minilm", "bge-", "e5-")

DEMO_HOST_PATTERNS = re.compile(
    r"https?://[^\s)>\]]*(?:vercel\.app|netlify\.app|onrender\.com|railway\.app|fly\.dev|pages\.dev|github\.io)[^\s)>\]]*",
    re.IGNORECASE,
)

SYSTEM_PROMPT = """You are an elite Principal Staff Engineer, Technical CV Writer, and Developer Advocate.
Analyze the provided `README.md` of a newly completed software engineering project and generate tailored career content across 4 channels:

1. React Portfolio Catalog (`id`, `project_name`, `subtitle`, `category`, `categories`, `status`, `icon`, `artCaption`, `architecture_summary`, `tech_stack`, `highlights`, `workflow`, `details`):
   - `id`: Clean kebab-case slug identifier for the portfolio route.
   - `subtitle`: Crisp 3-6 word tagline (e.g. 'Plan. Retrieve. Answer. Review.').
   - `artCaption`: Short uppercase visual pipeline banner with '→' (e.g. 'README → PORTFOLIO & LINKEDIN').
   - `workflow`: 3-4 ordered architectural steps (`title`, `description`) explaining end-to-end execution.
   - `details`: 2-3 deep-dive sections (`Engineering decisions`, `Testing approach`, `Current scope`).

2. FlowCV ATS Resume (`flowcv_format`):
   - 3 to 5 high-impact ATS resume bullets starting with strong past-tense action verbs (Architected, Engineered, Built, Automated, Implemented).
   - Plain text ONLY: NEVER use **bold** markdown or leading bullet symbols.

3. LinkedIn Profile Project (`linkedin_profile`):
   - A 1st-person systems engineering paragraph (4-6 sentences) written for LinkedIn's Profile -> Projects section.

4. LinkedIn Public Launch Post (`linkedin_post`):
   - An authentic, engaging 1st-person LinkedIn feed post (120-200 words) announcing the completed project:
     • Punchy 1-2 line opening hook on what you built and the real problem it solves
     • 3-4 concise engineering highlights prefixed with '• '
     • Brief tech stack mention + 3-5 relevant hashtags at the end
   - Plain Unicode text ONLY (do NOT use **asterisks** for bolding).

5. Executive Brief (`executive_brief`):
   - A crisp 2-3 sentence summary of the project's core engineering value and positioning.

Ground every single technology, feature, and claim strictly in the provided `README.md`. Never fabricate metrics."""


def safe_rmtree(path: Path | str) -> None:
    """Safely remove temporary git workspaces across Windows, macOS, and Linux."""
    target = Path(path)
    if not target.exists():
        return

    def _handle_readonly_exc(func: Any, failed_path: str, _exc: BaseException) -> None:
        try:
            os.chmod(failed_path, stat.S_IWRITE | stat.S_IREAD)
            func(failed_path)
        except OSError as cleanup_err:
            logger.warning("Could not remove %s: %s", failed_path, cleanup_err)

    def _handle_readonly_err(func: Any, failed_path: str, exc_info: Any) -> None:
        _handle_readonly_exc(func, failed_path, exc_info[1])

    if sys.version_info >= (3, 12):
        shutil.rmtree(target, onexc=_handle_readonly_exc)
    else:
        shutil.rmtree(target, onerror=_handle_readonly_err)


def sanitize_secret(text: str, secret: str) -> str:
    if not secret:
        return text
    return text.replace(secret, "***")


def validate_and_clean_repo_url(raw_url: str, allowed_hosts: tuple[str, ...]) -> str:
    """Normalize git SSH or HTTPS URLs into clean `https://host/owner/repo`."""
    cleaned = raw_url.strip()
    # Convert git@github.com:owner/repo.git -> https://github.com/owner/repo
    ssh_match = re.match(r"^git@([^:]+):(.+)$", cleaned)
    if ssh_match:
        cleaned = f"https://{ssh_match.group(1)}/{ssh_match.group(2)}"

    parsed = urlparse(cleaned)
    if parsed.scheme != "https":
        raise ValueError(f"Only https:// repository URLs are permitted (got {parsed.scheme!r}).")

    hostname = (parsed.hostname or "").lower()
    if not hostname or (allowed_hosts and hostname not in allowed_hosts):
        raise ValueError(
            f"Repository host {hostname!r} is not in ALLOWED_GIT_HOSTS {allowed_hosts}."
        )

    clean_path = re.sub(r"\.git$", "", parsed.path.rstrip("/"))
    if not clean_path or clean_path == "/":
        raise ValueError("Repository URL must include an owner/repo path.")
    return f"https://{hostname}{clean_path}"


def build_authenticated_git_url(clean_https_url: str, gh_token: str) -> str:
    if not gh_token:
        return clean_https_url
    return clean_https_url.replace("https://", f"https://x-access-token:{gh_token}@", 1)


def detect_git_remote_from_local_path(readme_file: Path, allowed_hosts: tuple[str, ...]) -> Optional[str]:
    """If `readme_file` lives inside a local git repo, infer its `origin` HTTPS URL."""
    try:
        repo = git.Repo(str(readme_file.parent), search_parent_directories=True)
        try:
            if "origin" in [r.name for r in repo.remotes]:
                raw_url = repo.remotes.origin.url
                return validate_and_clean_repo_url(raw_url, allowed_hosts)
        finally:
            repo.close()
    except Exception:
        return None
    return None


def fetch_github_repo_metadata(clean_repo_url: str, gh_token: str) -> dict[str, Any]:
    """Fetch repository visibility (`private`), `homepage`, and `description` from GitHub API."""
    parsed = urlparse(clean_repo_url)
    if (parsed.hostname or "").lower() != "github.com":
        return {}

    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) < 2:
        return {}

    api_url = f"https://api.github.com/repos/{parts[0]}/{parts[1]}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "career-sync-hub/2.0",
    }
    if gh_token:
        headers["Authorization"] = f"Bearer {gh_token}"

    req = Request(api_url, headers=headers, method="GET")
    try:
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        logger.debug("GitHub metadata lookup skipped for %s: %s", clean_repo_url, exc)
        return {}


def fetch_github_readme(clean_repo_url: str, gh_token: str) -> str:
    """Fetch `README.md` directly via GitHub REST API in ~0.2s (zero git clone needed!)."""
    parsed = urlparse(clean_repo_url)
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if (parsed.hostname or "").lower() != "github.com" or len(parts) < 2:
        raise ValueError(
            "Automatic remote README fetch requires a github.com URL, or pass `readme_path` / `readme_content`."
        )

    api_url = f"https://api.github.com/repos/{parts[0]}/{parts[1]}/readme"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "career-sync-hub/2.0",
    }
    if gh_token:
        headers["Authorization"] = f"Bearer {gh_token}"

    req = Request(api_url, headers=headers, method="GET")
    try:
        with urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            encoded = str(data.get("content") or "")
            if encoded:
                return base64.b64decode(encoded).decode("utf-8", errors="replace")
    except HTTPError as err:
        raise FileNotFoundError(
            f"Could not fetch README.md from {clean_repo_url} (HTTP {err.code}). "
            "Ensure README.md exists on the default branch or pass `--readme` locally."
        ) from err

    raise FileNotFoundError(f"Empty README response from {clean_repo_url}.")


def extract_demo_link_from_readme(readme_text: str) -> Optional[str]:
    """Auto-detect a live deployment link (Vercel, Netlify, Render, Railway, etc.) in README."""
    match = DEMO_HOST_PATTERNS.search(readme_text)
    if match:
        return match.group(0).rstrip(".,;)")
    return None


def _looks_like_local_path(value: str) -> bool:
    """Return True if `value` is a Windows/POSIX local file path rather than a URL."""
    cleaned = value.strip().strip('"').strip("'")
    if not cleaned:
        return False
    if cleaned.lower().startswith(("http://", "https://", "git@")):
        return False
    if re.match(r"^[a-zA-Z]:[\\/]", cleaned) or cleaned.startswith((".", "/", "\\")):
        return True
    if cleaned.lower().endswith(".md") or Path(cleaned).expanduser().exists():
        return True
    return False


def resolve_readme_input(
    req: AnalyzeRequest,
    settings: Settings,
) -> dict[str, Any]:
    """Resolve README text, repository URL, privacy flag, and live demo link from any input mode."""
    raw_repo_url = (req.repo_url or "").strip().strip('"').strip("'")
    raw_readme_path = (req.readme_path or "").strip().strip('"').strip("'")

    # Smart UX auto-fix: if the user pasted a local `C:\...\README.md` path into `repo_url`
    # (e.g. inside Swagger UI), treat it as `readme_path` automatically!
    if raw_repo_url and not raw_readme_path and _looks_like_local_path(raw_repo_url):
        raw_readme_path = raw_repo_url
        raw_repo_url = ""

    clean_repo_url: Optional[str] = None
    if raw_repo_url:
        clean_repo_url = validate_and_clean_repo_url(
            raw_repo_url, settings.allowed_git_hosts
        )

    readme_text = ""
    source_label = ""

    if req.readme_content and req.readme_content.strip():
        readme_text = req.readme_content.strip()
        source_label = "inline README markdown"
    elif raw_readme_path:
        local_file = Path(raw_readme_path).expanduser().resolve()
        if local_file.is_dir():
            # Allow passing project folder path directly—auto-locate README.md inside it!
            for candidate in ("README.md", "readme.md", "Readme.md"):
                if (local_file / candidate).is_file():
                    local_file = local_file / candidate
                    break
        if not local_file.is_file():
            raise FileNotFoundError(f"Local README file not found: {local_file}")
        readme_text = local_file.read_text(encoding="utf-8", errors="replace").strip()
        source_label = str(local_file)
        if not clean_repo_url:
            clean_repo_url = detect_git_remote_from_local_path(
                local_file, settings.allowed_git_hosts
            )
    elif clean_repo_url:
        readme_text = fetch_github_readme(clean_repo_url, settings.github_pat).strip()
        source_label = f"{clean_repo_url}/README.md"
    else:
        raise ValueError(
            "Provide at least one of `readme_content`, `readme_path`, or `repo_url`."
        )

    if not readme_text:
        raise ValueError(f"The resolved README ({source_label}) is empty.")

    if len(readme_text) > settings.max_readme_chars:
        readme_text = (
            readme_text[: settings.max_readme_chars]
            + f"\n\n... [README truncated at {settings.max_readme_chars} chars] ..."
        )

    gh_meta = (
        fetch_github_repo_metadata(clean_repo_url, settings.github_pat)
        if clean_repo_url
        else {}
    )
    is_private = (
        req.is_private
        if req.is_private is not None
        else bool(gh_meta.get("private", False))
    )

    live_demo = (
        req.live_demo_link
        or str(gh_meta.get("homepage") or "").strip()
        or extract_demo_link_from_readme(readme_text)
        or None
    )

    source_code_link: Optional[str] = None
    if clean_repo_url and not is_private:
        if "private" in gh_meta or req.is_private is False or not settings.github_pat:
            source_code_link = clean_repo_url

    default_slug = "project"
    if req.project_name_hint:
        default_slug = slugify_id(req.project_name_hint)
    elif clean_repo_url:
        default_slug = slugify_id(urlparse(clean_repo_url).path.rstrip("/").split("/")[-1])
    elif raw_readme_path:
        p = Path(raw_readme_path).expanduser().resolve()
        folder = p.name if p.is_dir() else p.parent.name
        default_slug = slugify_id(folder)

    return {
        "readme_text": readme_text,
        "source_label": source_label,
        "repo_url": clean_repo_url,
        "source_code_link": source_code_link,
        "live_demo_link": live_demo,
        "default_slug": default_slug,
        "project_name_hint": req.project_name_hint,
    }


def list_installed_ollama_chat_models(ollama_base_url: str) -> Optional[list[str]]:
    tags_url = f"{ollama_base_url.rstrip('/')}/api/tags"
    req = Request(tags_url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urlopen(req, timeout=4) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None

    raw_models = payload.get("models")
    if not isinstance(raw_models, list):
        return []

    chat_models: list[str] = []
    for item in raw_models:
        name = str(item.get("name") or item.get("model") or "").strip()
        if name and not any(kw in name.lower() for kw in EMBEDDING_MODEL_KEYWORDS):
            chat_models.append(name)
    return chat_models


def resolve_ollama_model_name(configured_model: str, installed: Optional[list[str]]) -> str:
    if installed is None:
        return configured_model

    if not installed:
        raise RuntimeError(
            f"No local chat models are pulled in Ollama yet (requested {configured_model!r}). "
            f"Run `ollama pull {configured_model}` in PowerShell."
        )

    for candidate in installed:
        if candidate == configured_model or candidate == f"{configured_model}:latest":
            return candidate
        if ":" not in configured_model and candidate.split(":", 1)[0] == configured_model:
            return candidate

    fallback = installed[0]
    logger.warning(
        "Configured OLLAMA_MODEL %r not found locally; auto-using installed model %r.",
        configured_model,
        fallback,
    )
    return fallback


def build_chat_model(settings: Settings) -> BaseChatModel:
    if settings.llm_provider in {"gemini", "google", "google_genai"}:
        if not settings.gemini_api_key:
            raise RuntimeError(
                "LLM_PROVIDER=gemini requires GEMINI_API_KEY in .env."
            )
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=settings.gemini_model,
            google_api_key=settings.gemini_api_key,
            temperature=settings.llm_temperature,
        )

    installed = list_installed_ollama_chat_models(settings.ollama_base_url)
    resolved_model = resolve_ollama_model_name(settings.ollama_model, installed)
    return ChatOllama(
        model=resolved_model,
        base_url=settings.ollama_base_url,
        num_ctx=settings.ollama_num_ctx,
        temperature=settings.llm_temperature,
    )


def evaluate_readme_with_llm(
    settings: Settings,
    resolved_ctx: dict[str, Any],
) -> EvaluationResult:
    """Generate Portfolio, FlowCV Resume, LinkedIn Profile, and LinkedIn Post from `README.md`."""
    user_prompt_lines = [
        f"Suggested Project Slug ID: {resolved_ctx['default_slug']}",
    ]
    if resolved_ctx.get("project_name_hint"):
        user_prompt_lines.append(f"Project Title Hint: {resolved_ctx['project_name_hint']}")
    if resolved_ctx.get("repo_url"):
        user_prompt_lines.append(f"Repository URL: {resolved_ctx['repo_url']}")
    if resolved_ctx.get("live_demo_link"):
        user_prompt_lines.append(f"Live Demo URL: {resolved_ctx['live_demo_link']}")
    user_prompt_lines.append(f"\n--- PROJECT README.MD ---\n{resolved_ctx['readme_text']}")
    user_prompt = "\n".join(user_prompt_lines)

    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=user_prompt),
    ]

    llm = build_chat_model(settings)
    model_label = getattr(llm, "model", settings.ollama_model)
    logger.info(
        "[Stage 1/4] Analyzing README.md (%d chars) with %s...",
        len(resolved_ctx["readme_text"]),
        model_label,
    )
    t0 = time.monotonic()

    try:
        structured_llm = llm.with_structured_output(
            LLMEvaluationSchema,
            method="json_schema",
        )
        llm_output: LLMEvaluationSchema = structured_llm.invoke(messages)
    except Exception as schema_err:
        if "not found" in str(schema_err).lower():
            raise
        logger.info("Falling back to `json_mode` structured output (%s)", schema_err)
        structured_llm = llm.with_structured_output(
            LLMEvaluationSchema,
            method="json_mode",
        )
        llm_output = structured_llm.invoke(messages)

    elapsed = time.monotonic() - t0
    logger.info(
        "[Stage 1/4] Completed README analysis in %.1fs -> %r (id=%s)",
        elapsed,
        llm_output.project_name,
        llm_output.id,
    )

    data = llm_output.model_dump()
    if resolved_ctx.get("project_name_hint"):
        data["project_name"] = resolved_ctx["project_name_hint"]
    if not data.get("id") or data.get("id") == "project":
        data["id"] = (
            resolved_ctx["default_slug"]
            if resolved_ctx["default_slug"] != "project"
            else slugify_id(data["project_name"])
        )

    result = EvaluationResult.model_validate(data)
    result.repo_url = resolved_ctx.get("repo_url")
    result.source_code_link = resolved_ctx.get("source_code_link")
    result.live_demo_link = resolved_ctx.get("live_demo_link")
    result.evaluated_at = datetime.now(timezone.utc).isoformat()
    return result


def commit_and_push_portfolio_update(
    settings: Settings,
    result: EvaluationResult,
) -> dict[str, Any]:
    """Clone the React portfolio repo, surgically upsert `projects.js`, commit, and push."""
    if not settings.portfolio_repo_url:
        return {
            "status": "skipped",
            "detail": "PORTFOLIO_REPO_URL is not configured in .env.",
        }

    clean_port_url = validate_and_clean_repo_url(
        settings.portfolio_repo_url, settings.allowed_git_hosts
    )
    if result.repo_url and normalize_repo_url(clean_port_url) == normalize_repo_url(result.repo_url):
        return {
            "status": "skipped_self",
            "detail": "Target README belongs to PORTFOLIO_REPO_URL itself; skipped self-push.",
        }

    job_dir = settings.temp_root_dir / f"portfolio_{uuid.uuid4().hex[:8]}"
    auth_port_url = build_authenticated_git_url(clean_port_url, settings.github_pat)

    with _PORTFOLIO_LOCK:
        p_repo: Optional[git.Repo] = None
        try:
            job_dir.mkdir(parents=True, exist_ok=True)
            logger.info("[Stage 2/4] Updating React portfolio repository %s...", clean_port_url)
            clone_kwargs: dict[str, Any] = {"depth": 1}
            if settings.portfolio_branch:
                clone_kwargs["branch"] = settings.portfolio_branch

            p_repo = git.Repo.clone_from(auth_port_url, str(job_dir), **clone_kwargs)
            target_file, action = upsert_portfolio_project(
                portfolio_dir=job_dir,
                result=result,
                configured_rel_path=settings.portfolio_projects_path,
            )
            rel_target = target_file.relative_to(job_dir).as_posix()

            with p_repo.config_writer() as cw:
                cw.set_value("user", "name", settings.git_author_name)
                cw.set_value("user", "email", settings.git_author_email)

            p_repo.git.add(A=True)
            if not p_repo.is_dirty(untracked_files=True):
                return {
                    "status": "unchanged",
                    "file": rel_target,
                    "detail": f"Entry `{result.id}` in `{rel_target}` was already identical.",
                }

            commit_msg = f"docs(portfolio): dynamic {action} for {result.project_name}"
            commit_obj = p_repo.index.commit(commit_msg)
            p_repo.remotes.origin.push()
            logger.info("[Stage 2/4] Pushed %s to %s (%s)", action, rel_target, commit_obj.hexsha[:8])
            return {
                "status": action,
                "file": rel_target,
                "commit_sha": commit_obj.hexsha[:8],
                "detail": f"Committed `{commit_msg}` and pushed to `{clean_port_url}`.",
            }
        except Exception as exc:
            safe_err = sanitize_secret(str(exc), settings.github_pat)
            logger.error("[Stage 2/4] Portfolio update failed: %s", safe_err)
            return {
                "status": "failed",
                "detail": safe_err,
            }
        finally:
            if p_repo is not None:
                p_repo.close()
            safe_rmtree(job_dir)


def build_executive_brief(
    result: EvaluationResult,
    actions: dict[str, dict[str, Any]],
) -> str:
    """Format a clean Markdown Executive Brief summarizing actions taken & generated content."""
    port_act = actions["portfolio"]
    li_post_act = actions["linkedin_post"]

    bullets_md = "\n".join(f"- {b}" for b in result.flowcv_format) or "- (None generated)"
    highlights_md = "\n".join(f"- {h}" for h in result.highlights) or "- (None generated)"
    final_post_preview = compose_linkedin_post_body(result)

    lines = [
        f"# Career Sync Executive Brief — {result.project_name}",
        f"**Project ID:** `{result.id}` | **Category:** {result.category} | **Tech Stack:** {', '.join(result.tech_stack)}",
        "",
        "## 1. Automation Actions Completed",
        f"- **React Portfolio (`projects.js`):** `{port_act['status'].upper()}` — {port_act.get('detail', '')}",
        f"- **LinkedIn Feed Launch Post (API):** `{li_post_act['status'].upper()}` — {li_post_act.get('detail', '')}"
        + (f" ([View Live Post]({li_post_act['post_url']}))" if li_post_act.get("post_url") else ""),
        "- **FlowCV Resume:** `READY` — Run `.\\.venv\\Scripts\\python.exe desktop_agent.py --flowcv --step`",
        "- **LinkedIn Profile Project:** `READY` — Run `.\\.venv\\Scripts\\python.exe desktop_agent.py --linkedin --step`",
        "",
        "## 2. Executive Summary",
        result.executive_brief or result.architecture_summary,
        "",
        "## 3. Portfolio Summary & Highlights",
        f"**Tagline:** {result.subtitle}  ",
        f"**Banner:** `{result.artCaption}`",
        "",
        result.architecture_summary,
        "",
        highlights_md,
        "",
        "## 4. FlowCV Resume Bullets (ATS Plain Text)",
        bullets_md,
        "",
        "## 5. LinkedIn Profile — Project Section Narrative",
        result.linkedin_profile,
        "",
        "## 6. LinkedIn Public Feed Launch Post",
        "```text",
        final_post_preview,
        "```",
        "",
    ]
    return "\n".join(lines)


def persist_generation_and_brief(
    settings: Settings,
    result: EvaluationResult,
    brief_markdown: str,
) -> tuple[Path, Path]:
    """Save `data/latest_generation.json`, `data/latest_brief.md`, and `data/history.json`."""
    with _DATA_LOCK:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        latest_json = settings.data_dir / "latest_generation.json"
        latest_brief = settings.data_dir / "latest_brief.md"
        history_path = settings.data_dir / "history.json"

        payload_dict = result.model_dump()
        latest_json.write_text(
            json.dumps(payload_dict, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        latest_brief.write_text(brief_markdown, encoding="utf-8")

        history: list[dict[str, Any]] = []
        if history_path.is_file():
            try:
                loaded = json.loads(history_path.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    history = loaded
            except json.JSONDecodeError:
                history = []

        history = [item for item in history if item.get("id") != result.id]
        history.insert(0, payload_dict)
        history = history[: settings.max_history_items]
        history_path.write_text(
            json.dumps(history, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return latest_json, latest_brief


def execute_release_pipeline(
    req: AnalyzeRequest,
    settings: Optional[Settings] = None,
) -> AnalyzeResponse:
    """Run the complete synchronous README -> Portfolio + LinkedIn Post + Resume/Profile Brief flow."""
    cfg = settings or Settings.from_env()

    # Stage 1: Resolve README & generate all 4 career channels with LLM
    resolved_ctx = resolve_readme_input(req, cfg)
    result = evaluate_readme_with_llm(cfg, resolved_ctx)

    # Stage 2: Update & push React Portfolio (`projects.js`)
    if req.update_portfolio:
        portfolio_action = commit_and_push_portfolio_update(cfg, result)
    else:
        portfolio_action = {
            "status": "skipped",
            "detail": "`update_portfolio=false` requested (dry-run).",
        }
    result.portfolio_status = portfolio_action["status"]

    # Stage 3: Publish LinkedIn Feed Post automatically via Official LinkedIn API
    if req.post_to_linkedin:
        logger.info("[Stage 3/4] Publishing launch post to LinkedIn API...")
        linkedin_action = publish_linkedin_feed_post(cfg, result)
    else:
        linkedin_action = {
            "status": "skipped",
            "detail": "`post_to_linkedin=false` requested (dry-run).",
        }
    result.linkedin_post_url = linkedin_action.get("post_url")

    actions: dict[str, dict[str, Any]] = {
        "portfolio": portfolio_action,
        "linkedin_post": linkedin_action,
        "resume_flowcv": {
            "status": "ready",
            "command": r".\.venv\Scripts\python.exe desktop_agent.py --flowcv --step",
            "detail": f"{len(result.flowcv_format)} ATS action-verb bullets ready to paste.",
        },
        "linkedin_profile_project": {
            "status": "ready",
            "command": r".\.venv\Scripts\python.exe desktop_agent.py --linkedin --step",
            "detail": "1st-person narrative & stack ready for LinkedIn Profile -> Projects.",
        },
    }

    # Stage 4: Build Executive Brief & persist artifacts locally
    brief_md = build_executive_brief(result, actions)
    _, brief_path = persist_generation_and_brief(cfg, result, brief_md)
    logger.info("[Stage 4/4] Saved Executive Brief -> %s", brief_path)

    return AnalyzeResponse(
        status="completed",
        project_id=result.id,
        project_name=result.project_name,
        actions=actions,
        brief_markdown=brief_md,
        brief_file=str(brief_path),
        evaluation=result,
    )


def validate_webhook_credentials(
    secret: str,
    body_bytes: bytes,
    authorization: Optional[str] = None,
    x_hub_token: Optional[str] = None,
    x_hub_signature_256: Optional[str] = None,
) -> bool:
    if not secret:
        return True

    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        if hmac.compare_digest(token, secret):
            return True

    if x_hub_token and hmac.compare_digest(x_hub_token.strip(), secret):
        return True

    if x_hub_signature_256 and x_hub_signature_256.startswith("sha256="):
        expected = "sha256=" + hmac.new(
            secret.encode("utf-8"), body_bytes, hashlib.sha256
        ).hexdigest()
        if hmac.compare_digest(expected, x_hub_signature_256.strip()):
            return True

    return False


async def verify_webhook_auth(
    request: FastAPIRequest,
    authorization: Optional[str] = Header(default=None),
    x_hub_token: Optional[str] = Header(default=None),
    x_hub_signature_256: Optional[str] = Header(default=None),
) -> None:
    settings = Settings.from_env()
    if not settings.webhook_secret:
        return

    # Allow direct local browser / Swagger UI requests (`127.0.0.1` / `::1`) when not
    # proxied through an external tunnel and when no explicit auth header is being tested
    client_host = (request.client.host if request.client else "").lower()
    is_proxied = any(
        h in request.headers
        for h in ("x-forwarded-for", "cf-connecting-ip", "x-real-ip")
    )
    has_auth_header = bool(authorization or x_hub_token or x_hub_signature_256)
    if client_host in {"127.0.0.1", "::1", "localhost"} and not is_proxied and not has_auth_header:
        return

    body = await request.body() if x_hub_signature_256 else b""
    if not validate_webhook_credentials(
        secret=settings.webhook_secret,
        body_bytes=body,
        authorization=authorization,
        x_hub_token=x_hub_token,
        x_hub_signature_256=x_hub_signature_256,
    ):
        raise HTTPException(status_code=401, detail="Invalid or missing webhook credentials.")


@app.get("/health")
def health_check() -> dict[str, Any]:
    settings = Settings.from_env()
    latest_file = settings.data_dir / "latest_generation.json"
    installed_ollama = (
        list_installed_ollama_chat_models(settings.ollama_base_url)
        if settings.llm_provider not in {"gemini", "google", "google_genai"}
        else None
    )
    active_model = (
        settings.gemini_model
        if settings.llm_provider in {"gemini", "google", "google_genai"}
        else settings.ollama_model
    )
    return {
        "status": "ok",
        "mode": "readme_release_pipeline",
        "llm_provider": settings.llm_provider,
        "llm_model": active_model,
        "installed_ollama_models": installed_ollama,
        "portfolio_configured": bool(settings.portfolio_repo_url),
        "linkedin_api_configured": bool(settings.linkedin_access_token),
        "has_latest_generation": latest_file.is_file(),
        "auth_enabled": bool(settings.webhook_secret),
    }


@app.post("/analyze", response_model=AnalyzeResponse, dependencies=[Depends(verify_webhook_auth)])
def handle_analyze(payload: AnalyzeRequest) -> AnalyzeResponse:
    """Analyze a project `README.md`, update Portfolio + LinkedIn Post, and return the Brief."""
    try:
        return execute_release_pipeline(payload)
    except (ValueError, FileNotFoundError) as bad_req:
        raise HTTPException(status_code=400, detail=str(bad_req)) from bad_req
    except Exception as err:
        logger.exception("Release pipeline error")
        raise HTTPException(status_code=500, detail=str(err)) from err


@app.post(
    "/analyze/raw",
    response_model=AnalyzeResponse,
    dependencies=[Depends(verify_webhook_auth)],
)
async def handle_analyze_raw_markdown(
    request: FastAPIRequest,
    readme_path: Optional[str] = Query(
        default=None,
        description="Local path to README.md (e.g. C:\\Users\\...\\README.md)",
    ),
    repo_url: Optional[str] = Query(
        default=None,
        description="GitHub repo URL (or local README.md path)",
    ),
    live_demo_link: Optional[str] = Query(default=None),
    is_private: Optional[bool] = Query(default=None),
    update_portfolio: bool = Query(default=True),
    post_to_linkedin: bool = Query(default=True),
) -> AnalyzeResponse:
    """Upload raw `README.md` bytes or pass `readme_path` / `repo_url` query params."""
    raw_bytes = await request.body()
    readme_md = raw_bytes.decode("utf-8", errors="replace").strip() or None
    req = AnalyzeRequest(
        readme_content=readme_md,
        readme_path=readme_path,
        repo_url=repo_url,
        live_demo_link=live_demo_link,
        is_private=is_private,
        update_portfolio=update_portfolio,
        post_to_linkedin=post_to_linkedin,
    )
    return handle_analyze(req)


@app.get("/brief", response_class=PlainTextResponse, dependencies=[Depends(verify_webhook_auth)])
def get_latest_brief() -> str:
    """Return the latest Markdown Executive Brief as plain text."""
    settings = Settings.from_env()
    brief_path = settings.data_dir / "latest_brief.md"
    if not brief_path.is_file():
        raise HTTPException(status_code=404, detail="No brief has been generated yet.")
    return brief_path.read_text(encoding="utf-8")


@app.get("/latest", dependencies=[Depends(verify_webhook_auth)])
def get_latest_generation() -> dict[str, Any]:
    settings = Settings.from_env()
    latest_path = settings.data_dir / "latest_generation.json"
    if not latest_path.is_file():
        raise HTTPException(status_code=404, detail="No evaluation has been generated yet.")
    return json.loads(latest_path.read_text(encoding="utf-8"))


@app.get("/history", dependencies=[Depends(verify_webhook_auth)])
def get_generation_history() -> list[dict[str, Any]]:
    settings = Settings.from_env()
    history_path = settings.data_dir / "history.json"
    if not history_path.is_file():
        return []
    return json.loads(history_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
