"""Unit tests for README-driven release pipeline, LinkedIn API publisher, Brief, and CLI."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
from pathlib import Path
import sys
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import desktop_agent
import linkedin_publisher
from models import (
    AnalyzeRequest,
    EvaluationResult,
    LLMEvaluationSchema,
    clean_linkedin_plain_text,
    strip_ats_markdown,
)
import publish
import server


SAMPLE_PROJECT_README = """# Multi-Agent RAG Document Assistant

A production-ready FastAPI document question-answering backend combining semantic Chroma retrieval
with LangChain query planning, grounded answer drafting, and automated review.

Live Demo: https://rag-assistant-demo.vercel.app

## Tech Stack
- Python, FastAPI, LangChain, Ollama, ChromaDB, Hugging Face Sentence Transformers, Pydantic

## Architecture Workflow
1. Upload & chunk PDF/Markdown files with SHA-256 deduplication.
2. Rewrite user queries with a local Ollama query planner.
3. Retrieve scoped vector evidence and review generated citations.
"""


def test_strip_ats_and_linkedin_markdown_sanitizers() -> None:
    raw_bullet = "- **Architected** a `FastAPI` pipeline reducing latency by __42%__."
    assert (
        strip_ats_markdown(raw_bullet)
        == "Architected a FastAPI pipeline reducing latency by 42%."
    )

    raw_post = "Just shipped **Multi-Agent RAG**!\n- Built with `FastAPI`\n- Zero hallucinations"
    cleaned_post = clean_linkedin_plain_text(raw_post)
    assert "**" not in cleaned_post
    assert "`FastAPI`" not in cleaned_post
    assert "• Built with FastAPI" in cleaned_post


def test_resolve_readme_input_from_local_file_and_extract_demo_link(
    tmp_path: Path,
) -> None:
    proj_dir = tmp_path / "multi-agent-rag"
    proj_dir.mkdir()
    readme_file = proj_dir / "README.md"
    readme_file.write_text(SAMPLE_PROJECT_README, encoding="utf-8")

    cfg = server.Settings.from_env()
    resolved = server.resolve_readme_input(
        AnalyzeRequest(readme_path=str(readme_file)),
        cfg,
    )
    assert "Multi-Agent RAG Document Assistant" in resolved["readme_text"]
    assert resolved["default_slug"] == "multi-agent-rag"
    assert resolved["live_demo_link"] == "https://rag-assistant-demo.vercel.app"


def test_linkedin_api_publisher_resolves_oidc_urn_and_posts_ugc_article(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", "li-test-token-123")
    monkeypatch.setenv("LINKEDIN_PERSON_URN", "")
    monkeypatch.setenv("LINKEDIN_VISIBILITY", "PUBLIC")

    captured_requests: list[tuple[str, str, dict[str, Any]]] = []

    class FakeHTTPResponse:
        def __init__(self, body_dict: dict[str, Any], headers: dict[str, str] | None = None):
            self._raw = json.dumps(body_dict).encode("utf-8")
            self.headers = headers or {}

        def read(self) -> bytes:
            return self._raw

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=15):
        body_dict = json.loads(req.data.decode("utf-8")) if req.data else {}
        captured_requests.append((req.get_method(), req.full_url, body_dict))
        if req.full_url.endswith("/v2/userinfo"):
            return FakeHTTPResponse({"sub": "member987"})
        if req.full_url.endswith("/v2/ugcPosts"):
            return FakeHTTPResponse(
                {"id": "urn:li:share:777888999"},
                headers={"x-restli-id": "urn:li:share:777888999"},
            )
        raise AssertionError(f"Unexpected URL: {req.full_url}")

    monkeypatch.setattr(linkedin_publisher, "urlopen", fake_urlopen)

    sample_eval = EvaluationResult(
        id="multi-agent-rag",
        project_name="Multi-Agent RAG Document Assistant",
        subtitle="Plan. Retrieve. Answer. Review.",
        architecture_summary="Semantic QA API using FastAPI and LangChain.",
        tech_stack=["Python", "FastAPI", "LangChain"],
        linkedin_post="Excited to launch Multi-Agent RAG!\n• Grounded answers\n#Python #AI",
        source_code_link="https://github.com/manojkumar2291/multi-agent-rag",
        live_demo_link="https://rag-assistant-demo.vercel.app",
    )

    cfg = server.Settings.from_env()
    action = linkedin_publisher.publish_linkedin_feed_post(cfg, sample_eval)

    assert action["status"] == "published"
    assert action["author_urn"] == "urn:li:person:member987"
    assert action["post_url"] == "https://www.linkedin.com/feed/update/urn:li:share:777888999/"

    # Verify ugcPosts payload attached ARTICLE media preview card
    post_method, post_url, post_body = captured_requests[1]
    assert post_method == "POST"
    assert post_url.endswith("/v2/ugcPosts")
    share_content = post_body["specificContent"]["com.linkedin.ugc.ShareContent"]
    assert share_content["shareMediaCategory"] == "ARTICLE"
    assert share_content["media"][0]["originalUrl"] == "https://rag-assistant-demo.vercel.app"


def test_end_to_end_readme_release_pipeline_and_publish_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("PORTFOLIO_REPO_URL", "")
    monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", "")
    monkeypatch.setenv("WEBHOOK_SECRET", "")

    readme_file = tmp_path / "README.md"
    readme_file.write_text(SAMPLE_PROJECT_README, encoding="utf-8")

    class FakeStructuredRunnable:
        def invoke(self, _messages):
            return LLMEvaluationSchema(
                id="multi-agent-rag",
                project_name="Multi-Agent RAG Document Assistant",
                subtitle="Plan. Retrieve. Answer. Review.",
                category="AI / RAG",
                categories=["AI / ML"],
                tech_stack=["Python", "FastAPI", "LangChain", "Ollama", "Chroma"],
                architecture_summary="Document QA backend combining Chroma retrieval with LangChain review.",
                highlights=["Three specialized agent roles", "SHA-256 file deduplication"],
                flowcv_format=[
                    "**Architected** a multi-agent RAG API in FastAPI and LangChain.",
                    "Implemented persistent Chroma vector search with source citations.",
                ],
                linkedin_profile="I built Multi-Agent RAG to deliver grounded document answers with citations.",
                linkedin_post="Just completed **Multi-Agent RAG**!\n- Query planning with Ollama\n#AI #FastAPI",
                executive_brief="Positioned Multi-Agent RAG around grounded retrieval and multi-stage agent verification.",
            )

    class FakeChatOllama:
        model = "qwen2.5-coder:7b"

        def with_structured_output(self, schema, method="json_schema"):
            return FakeStructuredRunnable()

    monkeypatch.setattr(server, "build_chat_model", lambda _cfg: FakeChatOllama())

    # Run via publish.py CLI (--direct --dry-run)
    rc = publish.main(["--readme", str(readme_file), "--direct", "--dry-run"])
    assert rc == 0
    cli_out = capsys.readouterr().out
    assert "Career Sync Executive Brief — Multi-Agent RAG Document Assistant" in cli_out
    assert "Positioned Multi-Agent RAG around grounded retrieval" in cli_out
    assert (data_dir / "latest_brief.md").is_file()
    assert (data_dir / "latest_generation.json").is_file()

    # Verify desktop_agent.py previews all 3 channels (--flowcv, --linkedin, --linkedin-post)
    rc_agent = desktop_agent.main(
        ["--flowcv", "--linkedin", "--linkedin-post", "--preview"]
    )
    assert rc_agent == 0
    agent_out = capsys.readouterr().out
    assert "Architected a multi-agent RAG API in FastAPI and LangChain." in agent_out
    assert "LinkedIn Feed Launch Post — Multi-Agent RAG Document Assistant" in agent_out
    assert "**Multi-Agent RAG**" not in agent_out


def test_webhook_auth_validation() -> None:
    secret = "my-secret"
    body = b'{"readme_content": "# Hello"}'
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    assert not server.validate_webhook_credentials(secret, body)
    assert server.validate_webhook_credentials(secret, body, authorization="Bearer my-secret")
    assert server.validate_webhook_credentials(secret, body, x_hub_signature_256=sig)
