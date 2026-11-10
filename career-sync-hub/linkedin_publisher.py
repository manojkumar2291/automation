"""Official LinkedIn API publisher (`w_member_social`) for hands-free launch posts.

Automatically resolves your member URN via `/v2/userinfo` (OpenID Connect) or
`/v2/me` when `LINKEDIN_PERSON_URN` is not explicitly set in `.env`, then publishes
the launch post via `POST https://api.linkedin.com/v2/ugcPosts`.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from config import Settings
from models import EvaluationResult

logger = logging.getLogger("career-sync-hub.linkedin")

LINKEDIN_USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
LINKEDIN_ME_URL = "https://api.linkedin.com/v2/me"
LINKEDIN_UGC_POSTS_URL = "https://api.linkedin.com/v2/ugcPosts"


def normalize_person_urn(raw_urn: str) -> str:
    """Normalize a bare LinkedIn ID or full URN into `urn:li:person:<id>`."""
    cleaned = raw_urn.strip()
    if not cleaned:
        return ""
    if cleaned.startswith("urn:li:"):
        return cleaned
    return f"urn:li:person:{cleaned}"


def resolve_linkedin_author_urn(access_token: str, configured_urn: str = "") -> str:
    """Resolve the authenticated member's `urn:li:person:<id>` using their OAuth token."""
    if configured_urn.strip():
        return normalize_person_urn(configured_urn)

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": "career-sync-hub/2.0",
    }

    # 1. Modern LinkedIn OpenID Connect (`openid` + `profile` + `w_member_social`)
    req_oidc = Request(LINKEDIN_USERINFO_URL, headers=headers, method="GET")
    try:
        with urlopen(req_oidc, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            sub = str(data.get("sub") or "").strip()
            if sub:
                return normalize_person_urn(sub)
    except HTTPError as http_err:
        logger.debug("LinkedIn /v2/userinfo returned HTTP %s; trying /v2/me...", http_err.code)
    except (URLError, TimeoutError, json.JSONDecodeError) as err:
        raise RuntimeError(f"Could not reach LinkedIn API (/v2/userinfo): {err}") from err

    # 2. Legacy LinkedIn v2 profile (`r_liteprofile`)
    req_me = Request(LINKEDIN_ME_URL, headers=headers, method="GET")
    try:
        with urlopen(req_me, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            member_id = str(data.get("id") or "").strip()
            if member_id:
                return normalize_person_urn(member_id)
    except HTTPError as http_err:
        detail = ""
        try:
            detail = http_err.read().decode("utf-8", errors="replace")[:300]
        except OSError:
            pass
        raise RuntimeError(
            f"Could not resolve LinkedIn member URN (HTTP {http_err.code}). "
            "Ensure your LINKEDIN_ACCESS_TOKEN has `openid`, `profile`, and `w_member_social` "
            f"scopes, or set LINKEDIN_PERSON_URN directly in .env. Response: {detail}"
        ) from http_err

    raise RuntimeError("LinkedIn profile lookup returned neither `sub` nor `id`.")


def compose_linkedin_post_body(result: EvaluationResult) -> str:
    """Compose the final LinkedIn feed post text including public links if available."""
    base_text = (result.linkedin_post or "").strip()
    if not base_text:
        stack_str = ", ".join(result.tech_stack)
        base_text = (
            f"Excited to share my latest project: {result.project_name}!\n\n"
            f"{result.linkedin_profile or result.architecture_summary}\n\n"
            f"Tech Stack: {stack_str}"
        ).strip()

    link_lines: list[str] = []
    if result.live_demo_link and result.live_demo_link not in base_text:
        link_lines.append(f"Live Demo: {result.live_demo_link}")
    if result.source_code_link and result.source_code_link not in base_text:
        link_lines.append(f"Source Code: {result.source_code_link}")

    if link_lines:
        base_text = f"{base_text}\n\n" + "\n".join(link_lines)

    # LinkedIn ugcPosts max commentary length is 3,000 characters
    return base_text[:2950]


def publish_linkedin_feed_post(
    settings: Settings,
    result: EvaluationResult,
) -> dict[str, Any]:
    """Publish `result.linkedin_post` to LinkedIn via `POST /v2/ugcPosts`.

    Returns a structured action dictionary for the executive brief.
    """
    if not settings.linkedin_access_token:
        return {
            "status": "skipped",
            "detail": (
                "LINKEDIN_ACCESS_TOKEN is not set in .env. "
                "Add your token to auto-publish, or run `python desktop_agent.py --linkedin-post`."
            ),
        }

    try:
        author_urn = resolve_linkedin_author_urn(
            access_token=settings.linkedin_access_token,
            configured_urn=settings.linkedin_person_urn,
        )
        commentary_text = compose_linkedin_post_body(result)
        primary_url: Optional[str] = result.live_demo_link or result.source_code_link

        share_content: dict[str, Any] = {
            "shareCommentary": {"text": commentary_text},
        }
        if primary_url:
            share_content["shareMediaCategory"] = "ARTICLE"
            share_content["media"] = [
                {
                    "status": "READY",
                    "originalUrl": primary_url,
                    "title": {"text": result.project_name[:200]},
                    "description": {
                        "text": (result.subtitle or result.architecture_summary)[:250]
                    },
                }
            ]
        else:
            share_content["shareMediaCategory"] = "NONE"

        ugc_payload = {
            "author": author_urn,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": share_content,
            },
            "visibility": {
                "com.linkedin.ugc.MemberNetworkVisibility": settings.linkedin_visibility,
            },
        }

        raw_bytes = json.dumps(ugc_payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {settings.linkedin_access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Restli-Protocol-Version": "2.0.0",
            "User-Agent": "career-sync-hub/2.0",
        }

        req = Request(LINKEDIN_UGC_POSTS_URL, data=raw_bytes, headers=headers, method="POST")
        with urlopen(req, timeout=15) as resp:
            resp_body = resp.read().decode("utf-8", errors="replace")
            resp_json = json.loads(resp_body) if resp_body.strip() else {}
            post_urn = (
                str(resp_json.get("id") or resp.headers.get("x-restli-id") or "").strip()
            )

        post_url = (
            f"https://www.linkedin.com/feed/update/{post_urn}/" if post_urn else None
        )
        logger.info("Published LinkedIn launch post -> %s", post_url or post_urn)
        return {
            "status": "published",
            "author_urn": author_urn,
            "post_urn": post_urn,
            "post_url": post_url,
            "visibility": settings.linkedin_visibility,
            "detail": f"Published live to LinkedIn ({settings.linkedin_visibility}).",
        }

    except HTTPError as http_err:
        err_body = ""
        try:
            err_body = http_err.read().decode("utf-8", errors="replace")[:400]
        except OSError:
            pass
        logger.error("LinkedIn API HTTP %s error: %s", http_err.code, err_body)
        return {
            "status": "failed",
            "http_status": http_err.code,
            "detail": f"LinkedIn API returned HTTP {http_err.code}: {err_body}",
        }
    except Exception as exc:
        logger.error("LinkedIn post failed: %s", exc)
        return {
            "status": "failed",
            "detail": str(exc),
        }

