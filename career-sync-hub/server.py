"""Central FastAPI Server & Release Pipeline (`server.py`).

Structured into 4 clear sections:
  1. README.md Content Parser (Extracts Title, Slug, Demo Link, and GitHub Link from Markdown)
  2. Ollama LLM Evaluator (`ChatOllama` structured output)
  3. Portfolio Git Updater & Executive Brief Builder
  4. FastAPI HTTP Endpoints (`/analyze`, `/analyze/raw`, `/brief`, `/latest`, `/history`, `/health`)
"""

from __future__ import annotations

from datetime import datetime, timezone
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
from urllib.parse import urlparse
import uuid

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse
import git
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
    description="Direct README.md content analyzer for React Portfolio, FlowCV Resume, and LinkedIn.",
    version="2.1.0",
)

_PORTFOLIO_LOCK = threading.Lock()
_DATA_LOCK = threading.Lock()

DEMO_URL_PATTERN = re.compile(
    r"https?://[^\s)>\]]*(?:vercel\.app|netlify\.app|onrender\.com|railway\.app|fly\.dev|pages\.dev|github\.io|streamlit\.app)[^\s)>\]]*",
    re.IGNORECASE,
)
GITHUB_REPO_URL_PATTERN = re.compile(
    r"https?://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
    re.IGNORECASE,
)
HEADING_PATTERN = re.compile(r"(?m)^#\s+(.+)$")

SYSTEM_PROMPT = """You are an elite Principal Staff Engineer, Technical CV Writer, and Developer Advocate.
Analyze the provided `README.md` content of a completed software project and generate structured content for 4 channels:

1. React Portfolio (`id`, `project_name`, `subtitle`, `category`, `categories`, `status`, `icon`, `artCaption`, `architecture_summary`, `tech_stack`, `highlights`, `workflow`, `details`):
   - `id`: Clean kebab-case slug identifier (e.g. 'multi-agent-rag').
   - `subtitle`: Short 3-6 word tagline.
   - `artCaption`: Uppercase pipeline banner with '→' (e.g. 'DOCUMENTS → GROUNDED ANSWERS').
   - `workflow`: 3-4 ordered architectural steps (`title`, `description`).
   - `details`: 2-3 deep-dive sections (`Engineering decisions`, `Testing approach`, `Current scope`).

2. FlowCV ATS Resume (`flowcv_format`):
   - 3 to 5 plain-text ATS resume bullets starting with past-tense action verbs (Architected, Engineered, Built, Automated, Implemented).
   - Plain text ONLY (never use **bold** markdown or leading bullets).

3. LinkedIn Profile Project (`linkedin_profile`):
   - A 1st-person engineering narrative paragraph (4-6 sentences) for LinkedIn's Profile -> Projects section.

4. LinkedIn Feed Launch Post (`linkedin_post`):
   - An engaging 1st-person LinkedIn post (120-200 words) with an opening hook, 3-4 '• ' bullet highlights, tech stack, and 3-5 hashtags. Plain text ONLY (no **asterisks**).

5. Executive Brief (`executive_brief`):
   - A concise 2-3 sentence summary of the project and how it was positioned.

Ground every claim strictly in the provided `README.md` content."""


# ============================================================================
# Section 1: README.md Content Parser & Workspace Helpers
# ============================================================================


def safe_rmtree(path: Path | str) -> None:
    """Remove a temporary directory tree safely on Windows, macOS, and Linux."""
    target = Path(path)
    if not target.exists():
        return

    def _clear_readonly(func: Any, failed_path: str, _exc: Any) -> None:
        try:
            os.chmod(failed_path, stat.S_IWRITE | stat.S_IREAD)
            func(failed_path)
        except OSError:
            pass

    if sys.version_info >= (3, 12):
        shutil.rmtree(target, onexc=_clear_readonly)
    else:
        shutil.rmtree(target, onerror=lambda f, p, e: _clear_readonly(f, p, e[1]))


def clean_github_https_url(raw_url: str) -> str:
    """Normalize a Git SSH or HTTPS URL into `https://github.com/owner/repo`."""
    cleaned = raw_url.strip()
    ssh_match = re.match(r"^git@([^:]+):(.+)$", cleaned)
    if ssh_match:
        cleaned = f"https://{ssh_match.group(1)}/{ssh_match.group(2)}"

    parsed = urlparse(cleaned)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(f"Expected an https:// repository URL (got {raw_url!r}).")

    clean_path = re.sub(r"\.git$", "", parsed.path.rstrip("/"))
    return f"https://{parsed.hostname.lower()}{clean_path}"


def parse_readme_content(req: AnalyzeRequest, settings: Settings) -> dict[str, Any]:
    """Extract clean Markdown text, project slug, demo URL, and GitHub link from `readme_content`."""
    readme_text = (req.readme_content or "").strip()
    if not readme_text:
        raise ValueError("`readme_content` cannot be empty.")

    if len(readme_text) > settings.max_readme_chars:
        readme_text = readme_text[: settings.max_readme_chars]

    # 1. Extract default project slug from the first `# Heading` in the README
    heading_match = HEADING_PATTERN.search(readme_text)
    heading_title = heading_match.group(1).strip() if heading_match else ""
    default_slug = slugify_id(heading_title) if heading_title else "project"

    # 2. Extract Live Demo URL if present inside the README content
    demo_match = DEMO_URL_PATTERN.search(readme_text)
    live_demo_link = demo_match.group(0).rstrip(".,;)") if demo_match else None

    # 3. Extract GitHub source_code_link from README content (unless `is_private=True`)
    source_code_link: Optional[str] = None
    if not req.is_private:
        gh_match = GITHUB_REPO_URL_PATTERN.search(readme_text)
        if gh_match:
            candidate = clean_github_https_url(gh_match.group(0).rstrip(".,;)"))
            if not settings.portfolio_repo_url or normalize_repo_url(
                candidate
            ) != normalize_repo_url(settings.portfolio_repo_url):
                source_code_link = candidate

    return {
        "readme_text": readme_text,
        "default_slug": default_slug,
        "live_demo_link": live_demo_link,
        "source_code_link": source_code_link,
    }


# ============================================================================
# Section 2: Ollama LLM Evaluator (`ChatOllama`)
# ============================================================================


def build_chat_model(settings: Settings) -> ChatOllama:
    """Create the local LangChain `ChatOllama` model configured in `.env`."""
    return ChatOllama(
        model=settings.ollama_model,
        base_url=settings.ollama_base_url,
        num_ctx=settings.ollama_num_ctx,
        num_predict=2048,
        temperature=settings.llm_temperature,
    )


def evaluate_readme_with_llm(
    settings: Settings,
    parsed_ctx: dict[str, Any],
) -> EvaluationResult:
    """Analyze `README.md` content with `ChatOllama` and return a validated `EvaluationResult`."""
    prompt_parts = [f"Suggested Project Slug ID: {parsed_ctx['default_slug']}"]
    if parsed_ctx.get("source_code_link"):
        prompt_parts.append(f"GitHub Source Link: {parsed_ctx['source_code_link']}")
    if parsed_ctx.get("live_demo_link"):
        prompt_parts.append(f"Live Demo URL: {parsed_ctx['live_demo_link']}")
    prompt_parts.append(f"\n--- PROJECT README.MD CONTENT ---\n{parsed_ctx['readme_text']}")

    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content="\n".join(prompt_parts)),
    ]

    llm = build_chat_model(settings)
    logger.info(
        "[Stage 1/4] Analyzing README.md content (%d chars) with Ollama model %r...",
        len(parsed_ctx["readme_text"]),
        settings.ollama_model,
    )
    t0 = time.monotonic()

    structured_llm = llm.with_structured_output(
        LLMEvaluationSchema,
        method="json_schema",
    )
    llm_output: LLMEvaluationSchema = structured_llm.invoke(messages)

    logger.info(
        "[Stage 1/4] Completed README analysis in %.1fs -> %r (id=%s)",
        time.monotonic() - t0,
        llm_output.project_name,
        llm_output.id,
    )

    data = llm_output.model_dump()
    if not data.get("id") or data.get("id") == "project":
        data["id"] = (
            parsed_ctx["default_slug"]
            if parsed_ctx["default_slug"] != "project"
            else slugify_id(data["project_name"])
        )

    result = EvaluationResult.model_validate(data)
    result.source_code_link = parsed_ctx.get("source_code_link")
    result.live_demo_link = parsed_ctx.get("live_demo_link")
    result.evaluated_at = datetime.now(timezone.utc).isoformat()
    return result


# ============================================================================
# Section 3: Portfolio Git Updater & Executive Brief Builder
# ============================================================================


def sync_portfolio_repository(
    settings: Settings,
    result: EvaluationResult,
) -> dict[str, Any]:
    """Clone the React portfolio repo, update `projects.js`, commit, and push to GitHub."""
    if not settings.portfolio_repo_url:
        return {
            "status": "skipped",
            "detail": "PORTFOLIO_REPO_URL is not configured in .env.",
        }

    clean_port_url = clean_github_https_url(settings.portfolio_repo_url)
    job_dir = settings.temp_root_dir / f"portfolio_{uuid.uuid4().hex[:8]}"
    auth_port_url = (
        clean_port_url.replace("https://", f"https://x-access-token:{settings.github_pat}@", 1)
        if settings.github_pat
        else clean_port_url
    )

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
                    "status": "up_to_date",
                    "file": rel_target,
                    "detail": f"Entry `{result.id}` in `{rel_target}` was already up-to-date.",
                }

            commit_msg = f"docs(portfolio): dynamic {action} for {result.project_name}"
            commit_obj = p_repo.index.commit(commit_msg)

            try:
                p_repo.remotes.origin.push()
            except git.GitCommandError as push_err:
                if settings.github_pat and ("403" in str(push_err) or "401" in str(push_err)):
                    logger.warning(
                        "GITHUB_PAT returned 403; retrying push with Windows Git Credential Manager..."
                    )
                    p_repo.remotes.origin.set_url(clean_port_url)
                    p_repo.remotes.origin.push()
                else:
                    raise

            logger.info(
                "[Stage 2/4] Pushed %s to %s (%s)", action, rel_target, commit_obj.hexsha[:8]
            )
            return {
                "status": "pushed",
                "action": action,
                "file": rel_target,
                "commit_sha": commit_obj.hexsha[:8],
                "detail": f"Committed `{commit_msg}` and pushed to `{clean_port_url}`.",
            }
        except Exception as exc:
            safe_err = (
                str(exc).replace(settings.github_pat, "***") if settings.github_pat else str(exc)
            )
            logger.error("[Stage 2/4] Portfolio update failed: %s", safe_err)
            return {"status": "failed", "detail": safe_err}
        finally:
            if p_repo is not None:
                p_repo.close()
            safe_rmtree(job_dir)


def build_executive_brief(
    result: EvaluationResult,
    actions: dict[str, dict[str, Any]],
) -> str:
    """Create a readable Markdown summary of completed actions and generated content."""
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
        history_path.write_text(
            json.dumps(history[:30], indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return latest_json, latest_brief


def execute_release_pipeline(
    req: AnalyzeRequest,
    settings: Optional[Settings] = None,
) -> AnalyzeResponse:
    """Orchestrate the full README Content -> Portfolio + LinkedIn Post + Brief pipeline."""
    cfg = settings or Settings.from_env()

    # Stage 1: Parse README.md content and evaluate with Ollama
    parsed_ctx = parse_readme_content(req, cfg)
    result = evaluate_readme_with_llm(cfg, parsed_ctx)

    # Stage 2: Commit & push update to React Portfolio repo
    if req.update_portfolio:
        portfolio_action = sync_portfolio_repository(cfg, result)
    else:
        portfolio_action = {"status": "skipped", "detail": "Skipped (dry-run)."}
    result.portfolio_status = portfolio_action["status"]

    # Stage 3: Publish LinkedIn Feed Post via LinkedIn API
    if req.post_to_linkedin:
        logger.info("[Stage 3/4] Publishing launch post to LinkedIn API...")
        linkedin_action = publish_linkedin_feed_post(cfg, result)
    else:
        linkedin_action = {"status": "skipped", "detail": "Skipped (dry-run)."}
    result.linkedin_post_url = linkedin_action.get("post_url")

    actions: dict[str, dict[str, Any]] = {
        "portfolio": portfolio_action,
        "linkedin_post": linkedin_action,
        "resume_flowcv": {
            "status": "ready",
            "command": r".\.venv\Scripts\python.exe desktop_agent.py --flowcv --step",
        },
        "linkedin_profile_project": {
            "status": "ready",
            "command": r".\.venv\Scripts\python.exe desktop_agent.py --linkedin --step",
        },
    }

    # Stage 4: Save Executive Brief & JSON artifacts
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


# ============================================================================
# Section 4: FastAPI HTTP Endpoints
# ============================================================================


@app.get("/health")
def health_check() -> dict[str, Any]:
    settings = Settings.from_env()
    latest_file = settings.data_dir / "latest_generation.json"
    return {
        "status": "ok",
        "ollama_model": settings.ollama_model,
        "ollama_base_url": settings.ollama_base_url,
        "portfolio_configured": bool(settings.portfolio_repo_url),
        "linkedin_api_configured": bool(settings.linkedin_access_token),
        "has_latest_generation": latest_file.is_file(),
    }


@app.post("/analyze", response_model=AnalyzeResponse)
def handle_analyze(payload: AnalyzeRequest) -> AnalyzeResponse:
    """Analyze `README.md` content (JSON body), update Portfolio + LinkedIn, and return Brief."""
    try:
        return execute_release_pipeline(payload)
    except ValueError as bad_req:
        raise HTTPException(status_code=400, detail=str(bad_req)) from bad_req
    except Exception as err:
        logger.exception("Release pipeline error")
        raise HTTPException(status_code=500, detail=str(err)) from err


@app.post("/analyze/raw", response_model=AnalyzeResponse)
def handle_analyze_raw_markdown(
    readme_content: str = Body(
        ...,
        media_type="text/plain",
        description="Paste your full README.md content directly here as plain text.",
    ),
    is_private: bool = Query(
        default=False,
        description="Set true for private projects so source_code_link is omitted.",
    ),
    update_portfolio: bool = Query(default=True),
    post_to_linkedin: bool = Query(default=True),
) -> AnalyzeResponse:
    """Paste raw `README.md` Markdown text directly into the request body (ideal for Swagger UI)."""
    req = AnalyzeRequest(
        readme_content=readme_content,
        is_private=is_private,
        update_portfolio=update_portfolio,
        post_to_linkedin=post_to_linkedin,
    )
    return handle_analyze(req)


@app.get("/brief", response_class=PlainTextResponse)
def get_latest_brief() -> str:
    """Return the latest Markdown Executive Brief."""
    settings = Settings.from_env()
    brief_path = settings.data_dir / "latest_brief.md"
    if not brief_path.is_file():
        raise HTTPException(status_code=404, detail="No brief has been generated yet.")
    return brief_path.read_text(encoding="utf-8")


@app.get("/latest")
def get_latest_generation() -> dict[str, Any]:
    """Return the latest generated JSON payload."""
    settings = Settings.from_env()
    latest_path = settings.data_dir / "latest_generation.json"
    if not latest_path.is_file():
        raise HTTPException(status_code=404, detail="No evaluation has been generated yet.")
    return json.loads(latest_path.read_text(encoding="utf-8"))


@app.get("/history")
def get_generation_history() -> list[dict[str, Any]]:
    """Return the history of evaluated projects."""
    settings = Settings.from_env()
    history_path = settings.data_dir / "history.json"
    if not history_path.is_file():
        return []
    return json.loads(history_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("server:app", host="127.0.0.1", port=port, reload=False)
