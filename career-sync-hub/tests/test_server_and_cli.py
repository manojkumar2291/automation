"""Unit tests for LangChain ChatOllama evaluator, git context extraction, auth, and CLI."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import stat
import sys

from fastapi import BackgroundTasks
import git
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import desktop_agent
from models import EvaluationResult, LLMEvaluationSchema, strip_ats_markdown
import server


def test_strip_ats_markdown_removes_bold_code_and_bullets() -> None:
    raw = "- **Architected** a `FastAPI` pipeline reducing latency by __42%__."
    assert (
        strip_ats_markdown(raw)
        == "Architected a FastAPI pipeline reducing latency by 42%."
    )


def test_build_chat_model_uses_chat_ollama_with_configured_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5-coder:7b")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    monkeypatch.setenv("OLLAMA_NUM_CTX", "16384")

    cfg = server.Settings.from_env()
    chat_model = server.build_chat_model(cfg)
    assert chat_model.__class__.__name__ == "ChatOllama"
    assert chat_model.model == "qwen2.5-coder:7b"
    assert chat_model.num_ctx == 16384


def test_invoke_llm_evaluator_binds_structured_schema_on_chat_ollama(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5-coder:7b")

    captured: dict[str, object] = {}

    class FakeStructuredRunnable:
        def invoke(self, messages):
            captured["messages"] = messages
            return LLMEvaluationSchema(
                is_high_impact=True,
                impact_score=9,
                impact_reason="Adds multi-stage webhook and AST portfolio sync.",
                id="project",  # Should auto-populate from repo URL slug!
                project_name="Career Sync Hub",
                subtitle="Push. Evaluate. Publish. Paste.",
                category="AI / Automation",
                categories=["AI / ML"],
                tech_stack=["Python", "FastAPI", "LangChain", "Ollama"],
                architecture_summary="Local-first commit evaluator using ChatOllama.",
                highlights=["GBNF structured schema output"],
                flowcv_format=["**Engineered** local ChatOllama commit evaluator."],
                linkedin_profile="I built Career Sync Hub with LangChain and Ollama.",
            )

    class FakeChatOllama:
        def with_structured_output(self, schema, method="json_schema"):
            captured["schema"] = schema
            captured["method"] = method
            return FakeStructuredRunnable()

    monkeypatch.setattr(server, "build_chat_model", lambda _cfg: FakeChatOllama())

    cfg = server.Settings.from_env()
    res = server.invoke_llm_evaluator(
        cfg,
        "https://github.com/manojkumar2291/career-sync-hub",
        {
            "resolved_sha": "abc12345",
            "commit_message": "feat: add ChatOllama structured output",
            "tree": ["server.py", "models.py"],
            "manifests": {"README.md": "# Career Sync Hub"},
            "diff": "+from langchain_ollama import ChatOllama",
        },
    )

    assert captured["schema"] is LLMEvaluationSchema
    assert captured["method"] == "json_schema"
    assert res.id == "career-sync-hub"
    assert res.flowcv_format == ["Engineered local ChatOllama commit evaluator."]


def test_initial_single_commit_and_multi_commit_git_context_plus_safe_rmtree(
    tmp_path: Path,
) -> None:
    repo_dir = tmp_path / "demo_repo"
    repo_dir.mkdir()

    repo = git.Repo.init(str(repo_dir))
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "Test Engineer")
        cw.set_value("user", "email", "test@example.com")

    (repo_dir / "README.md").write_text("# Demo Repo\nFastAPI microservice.", encoding="utf-8")
    (repo_dir / "app.py").write_text("def run():\n    return 'v1'\n", encoding="utf-8")
    (repo_dir / "package-lock.json").write_text('{"lockfileVersion": 3}\n', encoding="utf-8")
    repo.git.add(A=True)
    first_commit = repo.index.commit("feat: initial microservice implementation")

    ctx_initial = server.extract_repository_context(repo, first_commit.hexsha)
    assert "app.py" in ctx_initial["tree"]
    assert "def run():" in ctx_initial["diff"]
    assert "lockfileVersion" not in ctx_initial["diff"]
    assert "README.md" in ctx_initial["manifests"]

    (repo_dir / "app.py").write_text("def run():\n    return 'v2-upgraded'\n", encoding="utf-8")
    repo.git.add(A=True)
    second_commit = repo.index.commit("feat: upgrade handler to v2")

    ctx_second = server.extract_repository_context(repo, second_commit.hexsha)
    assert "v2-upgraded" in ctx_second["diff"]
    repo.close()

    ro_file = repo_dir / ".git" / "HEAD"
    ro_file.chmod(stat.S_IREAD)
    server.safe_rmtree(repo_dir)
    assert not repo_dir.exists()


def test_webhook_auth_bearer_and_hmac_and_queueing() -> None:
    secret = "super-secret-key"

    # 1. Missing/invalid credentials fail validation
    assert not server.validate_webhook_credentials(secret=secret, body_bytes=b"{}")
    assert not server.validate_webhook_credentials(
        secret=secret, body_bytes=b"{}", authorization="Bearer wrong-key"
    )

    # 2. Valid Bearer token succeeds
    assert server.validate_webhook_credentials(
        secret=secret,
        body_bytes=b"{}",
        authorization="Bearer super-secret-key",
    )

    # 3. Valid GitHub HMAC-SHA256 signature succeeds
    native_dict = {
        "after": "def5678",
        "repository": {
            "html_url": "https://github.com/manojkumar2291/interest_calculator",
            "private": False,
        },
    }
    native_payload = json.dumps(native_dict).encode("utf-8")
    sig = "sha256=" + hmac.new(
        secret.encode("utf-8"), native_payload, hashlib.sha256
    ).hexdigest()
    assert server.validate_webhook_credentials(
        secret=secret,
        body_bytes=native_payload,
        x_hub_signature_256=sig,
    )

    # 4. Webhook endpoint queues task cleanly
    bg = BackgroundTasks()
    resp = server.handle_webhook(
        payload=server.WebhookPayload.model_validate(native_dict),
        background_tasks=bg,
    )
    assert resp == {
        "status": "queued",
        "repo_url": "https://github.com/manojkumar2291/interest_calculator",
        "commit_sha": "def5678",
    }
    assert len(bg.tasks) == 1


def test_desktop_agent_preview_and_history_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("WEBHOOK_SECRET", "")

    sample = EvaluationResult(
        is_high_impact=True,
        impact_score=9,
        id="career-sync-hub",
        project_name="Career Sync Hub",
        subtitle="Automated Career Pipeline",
        tech_stack=["Python", "FastAPI", "LangChain", "Ollama", "PyAutoGUI"],
        architecture_summary="Automated webhook-to-portfolio pipeline.",
        flowcv_format=[
            "**Engineered** webhook server in FastAPI and LangChain ChatOllama.",
            "Built safe local OS paste agent.",
        ],
        linkedin_profile="I engineered Career Sync Hub to automate developer portfolio updates.",
        repo_url="https://github.com/manojkumar2291/career-sync-hub",
        source_code_link="https://github.com/manojkumar2291/career-sync-hub",
        commit_sha="123456",
        evaluated_at="2026-11-09T16:00:00+00:00",
    )
    cfg = server.Settings.from_env()
    server.persist_generation_result(cfg, sample)

    rc_list = desktop_agent.main(["--list"])
    assert rc_list == 0
    assert "Career Sync Hub" in capsys.readouterr().out

    rc_preview = desktop_agent.main(["--flowcv", "--linkedin", "--preview"])
    assert rc_preview == 0
    out_preview = capsys.readouterr().out
    assert "Engineered webhook server in FastAPI and LangChain ChatOllama." in out_preview
    assert "**Engineered**" not in out_preview
    assert "Technologies: Python, FastAPI, LangChain, Ollama, PyAutoGUI" in out_preview

    rc_step = desktop_agent.main(["--flowcv", "--step", "--preview"])
    assert rc_step == 0
    out_step = capsys.readouterr().out
    assert "FlowCV — Project Title" in out_step
    assert "FlowCV — Subtitle / Tech Stack" in out_step
