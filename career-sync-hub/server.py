"""Step 1: Central Webhook Controller, LangChain ChatOllama Evaluator & Portfolio Committer.

Exposes:
- POST /analyze  (GitHub Actions / GitHub Webhook push receiver)
- GET  /latest   (Authenticated endpoint for local/remote desktop_agent.py sync)
- GET  /history  (Authenticated recent evaluations feed)
- GET  /health   (Readiness probe)
"""

from __future__ import annotations

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
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import uuid

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request as FastAPIRequest
import git
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_ollama import ChatOllama

from config import Settings
from models import EvaluationResult, LLMEvaluationSchema, WebhookPayload, slugify_id
from portfolio_updater import normalize_repo_url, upsert_portfolio_project

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("career-sync-hub.server")

app = FastAPI(
    title="career-sync-hub",
    description="Central webhook orchestrator powered by LangChain ChatOllama.",
    version="1.1.0",
)

_PORTFOLIO_LOCK = threading.Lock()
_DATA_LOCK = threading.Lock()

IGNORED_TREE_PREFIXES = (
    ".git/",
    "node_modules/",
    ".venv/",
    "venv/",
    "__pycache__/",
    "dist/",
    "build/",
    ".next/",
    "coverage/",
)

DIFF_EXCLUSION_PATHSPECS = (
    ":(exclude)package-lock.json",
    ":(exclude)pnpm-lock.yaml",
    ":(exclude)yarn.lock",
    ":(exclude)poetry.lock",
    ":(exclude)uv.lock",
    ":(exclude)Cargo.lock",
    ":(exclude)*.min.js",
    ":(exclude)*.min.css",
    ":(exclude)*.map",
    ":(exclude)*.svg",
    ":(exclude)*.lock",
)

MANIFEST_CANDIDATES = (
    "README.md",
    "pyproject.toml",
    "requirements.txt",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "Dockerfile",
)

SYSTEM_PROMPT = """You are an elite Principal Staff Engineer and Technical CV Architect.
Analyze the provided repository file tree, manifest excerpts, commit metadata, and git diff.

1. Impact Gate:
   Set `is_high_impact` to `true` ONLY if this commit/repository represents a meaningful engineering implementation, architectural feature, or substantive application structure. Set `is_high_impact` to `false` for trivial typo fixes, formatting tweaks, or minor config bumps.

2. Portfolio & Career Generation (grounded strictly in the code):
   - `id`: Clean kebab-case slug identifier for the React portfolio route.
   - `project_name`, `subtitle`, `category`, `categories`, `status`, `icon`, `artCaption`: Rich metadata matching the React portfolio's `export const projects` catalog.
   - `architecture_summary`: 2-3 sentence technical overview for the React portfolio card & detail page.
   - `tech_stack`: Concrete languages, frameworks, and libraries verified in the repository manifests/imports.
   - `highlights`: 4-6 concise feature & engineering highlights.
   - `workflow`: 3-4 sequential architectural stages (`title`, `description`) showing how requests/data flow.
   - `details`: Structured deep-dive sections (`Engineering decisions`, `Testing approach`, `Current scope`).
   - `flowcv_format`: 3-5 ATS-optimized resume bullets starting with strong past-tense action verbs (Architected, Engineered, Automated, Implemented). Plain text ONLY—never use **bold** markdown or leading bullet symbols.
   - `linkedin_profile`: 1st-person professional narrative block (4-6 sentences) showcasing systems engineering choices.

Never invent technologies or metrics that are not supported by the repository files and diff."""


def safe_rmtree(path: Path | str) -> None:
    """Safely remove a directory tree across Windows, macOS, and Linux.

    Clears read-only flags on `.git/objects/pack/*` files so Windows never raises
    `PermissionError: [WinError 5] Access is denied`.
    """
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
    """Scrub sensitive tokens from log or exception messages."""
    if not secret:
        return text
    return text.replace(secret, "***")


def validate_and_clean_repo_url(raw_url: str, allowed_hosts: tuple[str, ...]) -> str:
    """Ensure the repository URL uses HTTPS on an allowed Git host and strip credentials."""
    parsed = urlparse(raw_url.strip())
    if parsed.scheme != "https":
        raise ValueError(f"Only https:// repository URLs are permitted (got {parsed.scheme!r}).")

    hostname = (parsed.hostname or "").lower()
    if not hostname or (allowed_hosts and hostname not in allowed_hosts):
        raise ValueError(
            f"Repository host {hostname!r} is not in ALLOWED_GIT_HOSTS {allowed_hosts}."
        )

    clean_path = parsed.path.rstrip("/")
    if not clean_path or clean_path == "/":
        raise ValueError("Repository URL must include an owner/repo path.")
    return f"https://{hostname}{clean_path}"


def build_authenticated_git_url(clean_https_url: str, gh_token: str) -> str:
    """Inject PAT using `x-access-token` (compatible with classic & fine-grained GitHub PATs)."""
    if not gh_token:
        return clean_https_url
    return clean_https_url.replace("https://", f"https://x-access-token:{gh_token}@", 1)


def fetch_github_repo_metadata(clean_repo_url: str, gh_token: str) -> dict[str, Any]:
    """Query GitHub REST API for repository visibility (`private`) and `homepage` URL."""
    parsed = urlparse(clean_repo_url)
    if (parsed.hostname or "").lower() != "github.com":
        return {}

    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) < 2:
        return {}

    owner = parts[0]
    repo_name = re.sub(r"\.git$", "", parts[1])
    api_url = f"https://api.github.com/repos/{owner}/{repo_name}"

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "career-sync-hub/1.1",
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


def extract_repository_context(
    repo: git.Repo, commit_sha: str, max_diff_chars: int = 24_000
) -> dict[str, Any]:
    """Extract commit metadata, filtered file tree, manifest excerpts, and git diff."""
    try:
        commit = repo.commit(commit_sha) if commit_sha and commit_sha != "HEAD" else repo.head.commit
    except (git.BadName, ValueError):
        logger.warning("Commit %r not found directly; falling back to HEAD.", commit_sha)
        commit = repo.head.commit

    tree_paths: list[str] = []
    for item in commit.tree.traverse():
        if getattr(item, "type", "") != "blob":
            continue
        rel_path = item.path.replace("\\", "/")
        if any(rel_path.startswith(prefix) for prefix in IGNORED_TREE_PREFIXES):
            continue
        tree_paths.append(rel_path)
    tree_paths = tree_paths[:350]

    if commit.parents:
        parent_sha = commit.parents[0].hexsha
        diff_text = repo.git.diff(
            parent_sha,
            commit.hexsha,
            "--",
            ".",
            *DIFF_EXCLUSION_PATHSPECS,
        )
    else:
        diff_text = repo.git.show(
            commit.hexsha,
            "--format=",
            "--patch",
            "--",
            ".",
            *DIFF_EXCLUSION_PATHSPECS,
        )

    if len(diff_text) > max_diff_chars:
        diff_text = (
            diff_text[:max_diff_chars]
            + f"\n\n... [Diff truncated at {max_diff_chars} chars for LLM context window] ..."
        )

    workdir = Path(repo.working_tree_dir or ".")
    manifest_snippets: dict[str, str] = {}
    for candidate in MANIFEST_CANDIDATES:
        fpath = workdir / candidate
        if fpath.is_file():
            try:
                snippet = fpath.read_text(encoding="utf-8", errors="replace")[:2500]
                manifest_snippets[candidate] = snippet
            except OSError:
                continue

    return {
        "resolved_sha": commit.hexsha,
        "commit_message": commit.message.strip(),
        "tree": tree_paths,
        "manifests": manifest_snippets,
        "diff": diff_text,
    }


def build_chat_model(settings: Settings) -> BaseChatModel:
    """Instantiate LangChain `ChatOllama` (default) or `ChatGoogleGenerativeAI` from env."""
    if settings.llm_provider in {"gemini", "google", "google_genai"}:
        if not settings.gemini_api_key:
            raise RuntimeError(
                "LLM_PROVIDER=gemini requires GEMINI_API_KEY (from Google AI Studio) in .env."
            )
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=settings.gemini_model,
            google_api_key=settings.gemini_api_key,
            temperature=settings.llm_temperature,
        )

    # Default: Local Ollama via ChatOllama (`num_ctx` expanded so git diffs never truncate)
    return ChatOllama(
        model=settings.ollama_model,
        base_url=settings.ollama_base_url,
        num_ctx=settings.ollama_num_ctx,
        temperature=settings.llm_temperature,
    )


def invoke_llm_evaluator(
    settings: Settings,
    repo_url: str,
    repo_context: dict[str, Any],
) -> EvaluationResult:
    """Evaluate repository impact via LangChain `ChatOllama.with_structured_output(...)`."""
    repo_slug = slugify_id(
        re.sub(r"\.git$", "", urlparse(repo_url).path.rstrip("/").split("/")[-1])
    )
    user_prompt = (
        f"Repository URL: {repo_url}\n"
        f"Suggested Default Slug ID: {repo_slug}\n"
        f"Commit SHA: {repo_context['resolved_sha']}\n"
        f"Commit Message:\n{repo_context['commit_message']}\n\n"
        f"Repository File Tree ({len(repo_context['tree'])} files):\n"
        f"{json.dumps(repo_context['tree'], indent=2)}\n\n"
        f"Key Project Manifests & README Excerpts:\n"
        f"{json.dumps(repo_context['manifests'], indent=2)}\n\n"
        f"Git Diff:\n{repo_context['diff']}"
    )

    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=user_prompt),
    ]

    llm = build_chat_model(settings)
    # Bind Pydantic schema natively (uses Ollama GBNF `json_schema` grammar constraints)
    structured_llm = llm.with_structured_output(
        LLMEvaluationSchema,
        method="json_schema",
    )
    llm_output: LLMEvaluationSchema = structured_llm.invoke(messages)

    data = llm_output.model_dump()
    if not data.get("id") or data.get("id") == "project":
        data["id"] = repo_slug

    return EvaluationResult.model_validate(data)


def persist_generation_result(settings: Settings, result: EvaluationResult) -> Path:
    """Atomically write `data/latest_generation.json` and prepend to `data/history.json`."""
    with _DATA_LOCK:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        latest_path = settings.data_dir / "latest_generation.json"
        history_path = settings.data_dir / "history.json"

        payload_dict = result.model_dump()
        latest_path.write_text(
            json.dumps(payload_dict, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        history: list[dict[str, Any]] = []
        if history_path.is_file():
            try:
                loaded = json.loads(history_path.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    history = loaded
            except json.JSONDecodeError:
                history = []

        history = [
            item
            for item in history
            if not (
                normalize_repo_url(item.get("repo_url")) == normalize_repo_url(result.repo_url)
                and item.get("commit_sha") == result.commit_sha
            )
        ]
        history.insert(0, payload_dict)
        history = history[: settings.max_history_items]

        history_path.write_text(
            json.dumps(history, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return latest_path


def commit_and_push_portfolio_update(
    settings: Settings,
    result: EvaluationResult,
    portfolio_dir: Path,
) -> Optional[str]:
    """Clone the React portfolio repo, surgically upsert `projects.js`, commit, and push."""
    if not settings.portfolio_repo_url:
        logger.warning("PORTFOLIO_REPO_URL is not configured; skipping portfolio git push.")
        return None

    clean_port_url = validate_and_clean_repo_url(
        settings.portfolio_repo_url, settings.allowed_git_hosts
    )
    auth_port_url = build_authenticated_git_url(clean_port_url, settings.github_pat)

    with _PORTFOLIO_LOCK:
        p_repo: Optional[git.Repo] = None
        try:
            clone_kwargs: dict[str, Any] = {"depth": 1}
            if settings.portfolio_branch:
                clone_kwargs["branch"] = settings.portfolio_branch

            p_repo = git.Repo.clone_from(auth_port_url, str(portfolio_dir), **clone_kwargs)

            target_file, action = upsert_portfolio_project(
                portfolio_dir=portfolio_dir,
                result=result,
                configured_rel_path=settings.portfolio_projects_path,
            )
            rel_target = target_file.relative_to(portfolio_dir).as_posix()

            with p_repo.config_writer() as cw:
                cw.set_value("user", "name", settings.git_author_name)
                cw.set_value("user", "email", settings.git_author_email)

            p_repo.git.add(A=True)
            if not p_repo.is_dirty(untracked_files=True):
                logger.info("Portfolio file %s already up-to-date; skipping commit.", rel_target)
                return action

            commit_msg = f"docs(portfolio): dynamic {action} for {result.project_name}"
            p_repo.index.commit(commit_msg)
            p_repo.remotes.origin.push()
            logger.info(
                "Successfully %s %r in %s and pushed to portfolio origin.",
                action,
                result.project_name,
                rel_target,
            )
            return action
        finally:
            if p_repo is not None:
                p_repo.close()


def process_pipeline(
    repo_url: str,
    commit_sha: str = "HEAD",
    is_private_hint: Optional[bool] = None,
    live_demo_hint: Optional[str] = None,
    settings: Optional[Settings] = None,
) -> Optional[EvaluationResult]:
    """End-to-end background orchestration pipeline with guaranteed workspace cleanup."""
    cfg = settings or Settings.from_env()
    job_id = uuid.uuid4().hex[:10]
    job_root = cfg.temp_root_dir / f"job_{job_id}"
    project_dir = job_root / "temp_eval"
    portfolio_dir = job_root / "temp_portfolio"

    repo: Optional[git.Repo] = None
    try:
        clean_repo_url = validate_and_clean_repo_url(repo_url, cfg.allowed_git_hosts)
        clean_public_url = re.sub(r"\.git$", "", clean_repo_url)
        logger.info("[%s] Starting evaluation for %s @ %s", job_id, clean_public_url, commit_sha)

        job_root.mkdir(parents=True, exist_ok=True)

        gh_meta = fetch_github_repo_metadata(clean_public_url, cfg.github_pat)
        is_private = (
            is_private_hint
            if is_private_hint is not None
            else bool(gh_meta.get("private", False))
        )
        discovered_homepage = (
            live_demo_hint
            or str(gh_meta.get("homepage") or "").strip()
            or None
        )

        auth_url = build_authenticated_git_url(clean_repo_url, cfg.github_pat)
        repo = git.Repo.clone_from(auth_url, str(project_dir))
        repo_context = extract_repository_context(
            repo, commit_sha=commit_sha, max_diff_chars=cfg.max_diff_chars
        )
        repo.close()
        repo = None

        result = invoke_llm_evaluator(cfg, clean_public_url, repo_context)
        result.repo_url = clean_public_url
        result.commit_sha = repo_context["resolved_sha"]
        result.evaluated_at = datetime.now(timezone.utc).isoformat()

        # Respect portfolio rule: "Only supplied or existing public URLs belong here. Client source stays private."
        if not is_private and "private" not in gh_meta:
            if not cfg.github_pat:
                result.source_code_link = clean_public_url
        elif not is_private:
            result.source_code_link = clean_public_url
        else:
            result.source_code_link = None

        if discovered_homepage:
            result.live_demo_link = discovered_homepage

        if not result.is_high_impact:
            logger.info(
                "[%s] Commit skipped (impact_score=%s): %s",
                job_id,
                result.impact_score,
                result.impact_reason,
            )
            return result

        saved_path = persist_generation_result(cfg, result)
        logger.info(
            "[%s] High-impact update saved to %s (score=%s, id=%s)",
            job_id,
            saved_path,
            result.impact_score,
            result.id,
        )

        commit_and_push_portfolio_update(cfg, result, portfolio_dir)
        return result

    except Exception as exc:
        safe_msg = sanitize_secret(str(exc), cfg.github_pat)
        logger.error("[%s] Pipeline failed for %s: %s", job_id, repo_url, safe_msg)
        return None
    finally:
        if repo is not None:
            repo.close()
        safe_rmtree(job_root)


def validate_webhook_credentials(
    secret: str,
    body_bytes: bytes,
    authorization: Optional[str] = None,
    x_hub_token: Optional[str] = None,
    x_hub_signature_256: Optional[str] = None,
) -> bool:
    """Pure constant-time credential validator for Bearer, X-Hub-Token, and GitHub HMAC."""
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
    """FastAPI dependency enforcing `WEBHOOK_SECRET` when configured."""
    settings = Settings.from_env()
    if not settings.webhook_secret:
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
    active_model = (
        settings.gemini_model
        if settings.llm_provider in {"gemini", "google", "google_genai"}
        else settings.ollama_model
    )
    return {
        "status": "ok",
        "llm_provider": settings.llm_provider,
        "llm_model": active_model,
        "ollama_base_url": settings.ollama_base_url,
        "has_latest_generation": latest_file.is_file(),
        "portfolio_configured": bool(settings.portfolio_repo_url),
        "auth_enabled": bool(settings.webhook_secret),
    }


@app.post("/analyze", dependencies=[Depends(verify_webhook_auth)])
def handle_webhook(
    payload: WebhookPayload,
    background_tasks: BackgroundTasks,
    x_github_event: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    if x_github_event and x_github_event == "ping":
        return {"status": "pong"}

    if payload.is_branch_deletion():
        return {"status": "ignored", "reason": "branch_deletion"}

    settings = Settings.from_env()
    try:
        raw_url = payload.resolved_repo_url()
        clean_url = validate_and_clean_repo_url(raw_url, settings.allowed_git_hosts)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err

    commit_sha = payload.resolved_commit_sha()
    is_private = (
        payload.is_private
        if payload.is_private is not None
        else (payload.repository.private if payload.repository else None)
    )
    live_demo = payload.live_demo_link or (
        payload.repository.homepage if payload.repository else None
    )

    background_tasks.add_task(
        process_pipeline,
        repo_url=clean_url,
        commit_sha=commit_sha,
        is_private_hint=is_private,
        live_demo_hint=live_demo,
    )
    return {
        "status": "queued",
        "repo_url": clean_url,
        "commit_sha": commit_sha,
    }


@app.get("/latest", dependencies=[Depends(verify_webhook_auth)])
def get_latest_generation() -> dict[str, Any]:
    settings = Settings.from_env()
    latest_path = settings.data_dir / "latest_generation.json"
    if not latest_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="No high-impact evaluation has been generated yet.",
        )
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
