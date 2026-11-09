"""Pydantic models for webhook payloads, LangChain structured output, and portfolio items."""

from __future__ import annotations

import re
from typing import Any, Optional
from pydantic import BaseModel, Field, field_validator


VALID_PORTFOLIO_ICONS = {"workflow", "ai", "voice", "health", "crm", "leads"}


def slugify_id(value: str) -> str:
    """Convert any project name or raw slug into a clean kebab-case portfolio ID."""
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return cleaned or "project"


def strip_ats_markdown(bullet: str) -> str:
    """Guarantee FlowCV bullets are pure ATS-safe plain text with zero markdown bolding."""
    text = bullet.strip()
    text = re.sub(r"^(?:[-*•‣▪–—]|\d+[.)])\s+", "", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


class WebhookRepositoryInfo(BaseModel):
    """Subset of native GitHub push webhook `repository` object."""

    clone_url: Optional[str] = None
    html_url: Optional[str] = None
    private: Optional[bool] = None
    homepage: Optional[str] = None
    default_branch: Optional[str] = None


class WebhookPayload(BaseModel):
    """Supports both lightweight workflow pings and native GitHub push webhooks."""

    repo_url: Optional[str] = None
    commit_sha: Optional[str] = "HEAD"
    branch: Optional[str] = None
    is_private: Optional[bool] = None
    live_demo_link: Optional[str] = None

    after: Optional[str] = None
    ref: Optional[str] = None
    repository: Optional[WebhookRepositoryInfo] = None

    def resolved_repo_url(self) -> str:
        url = self.repo_url
        if not url and self.repository:
            url = self.repository.html_url or self.repository.clone_url
        if not url:
            raise ValueError("Payload must include `repo_url` or `repository.html_url`.")
        return url.strip()

    def resolved_commit_sha(self) -> str:
        sha = self.commit_sha
        if (not sha or sha == "HEAD") and self.after:
            if set(self.after) != {"0"}:
                sha = self.after
        return (sha or "HEAD").strip()

    def is_branch_deletion(self) -> bool:
        return bool(self.after and set(self.after) == {"0"})


class WorkflowStep(BaseModel):
    """Single step inside `project.workflow` in the React portfolio."""

    title: str = Field(description="Short stage title (3-4 words), e.g. 'Ingest documents'.")
    description: str = Field(
        description="1-2 sentence explanation of how data/control flows through this stage."
    )


class DetailSection(BaseModel):
    """Deep-dive engineering section inside `project.details` in the React portfolio."""

    title: str = Field(
        description="Section heading such as 'Engineering decisions', 'Testing approach', or 'Current scope'."
    )
    description: Optional[str] = Field(
        default=None,
        description="Optional 1-sentence introductory summary for this detail section.",
    )
    items: list[str] = Field(
        default_factory=list,
        description="2 to 4 concrete technical observations grounded in the codebase.",
    )


class LLMEvaluationSchema(BaseModel):
    """Schema bound directly to LangChain `llm.with_structured_output(...)`."""

    is_high_impact: bool = Field(
        description="True only when the commit/project reflects substantive engineering work rather than trivial edits."
    )
    impact_score: int = Field(
        default=5,
        ge=1,
        le=10,
        description="Engineering impact score from 1 (trivial typo) to 10 (major system architecture).",
    )
    impact_reason: str = Field(
        default="",
        description="Concise 1-sentence justification of why this commit is or is not high-impact.",
    )

    id: str = Field(
        default="project",
        description="Preferred kebab-case slug identifier for the React portfolio route (e.g. 'multi-agent-rag').",
    )
    project_name: str = Field(
        default="Untitled Project",
        description="Human-readable project display title.",
    )
    subtitle: str = Field(
        default="",
        description="Short punchy 3-6 word tagline (e.g. 'Plan. Retrieve. Answer. Review.').",
    )
    category: str = Field(
        default="Software Engineering",
        description="Card domain badge (e.g. 'AI / Automation', 'AI / RAG', 'Full Stack', 'Web Application').",
    )
    categories: list[str] = Field(
        default_factory=lambda: ["Full Stack"],
        description="1-2 filter tags; prefer 'AI / ML', 'Full Stack', 'Healthcare', 'Web Applications', or 'DevOps / Automation'.",
    )
    featured: bool = Field(default=True, description="Whether to feature the project on the home view.")
    status: str = Field(
        default="Active development",
        description="Maturity label such as 'Production', 'Active development', 'Local prototype', or 'Open source'.",
    )
    icon: str = Field(
        default="workflow",
        description="Portfolio icon identifier; choose from: 'workflow', 'ai', 'voice', 'health', 'crm', 'leads'.",
    )
    artCaption: str = Field(
        default="CODE → PRODUCTION",
        description="Short uppercase visual pipeline banner using '→', e.g. 'DOCUMENTS → GROUNDED ANSWERS'.",
    )
    architecture_summary: str = Field(
        default="",
        description="2-3 sentence grounded technical description for the React portfolio.",
    )
    tech_stack: list[str] = Field(
        default_factory=list,
        description="Concrete programming languages, frameworks, and tools actually used in the repository.",
    )
    highlights: list[str] = Field(
        default_factory=list,
        description="4 to 6 concise capability and systems engineering highlights.",
    )
    workflow: list[WorkflowStep] = Field(
        default_factory=list,
        description="3 to 4 ordered architectural workflow steps explaining system execution.",
    )
    details: list[DetailSection] = Field(
        default_factory=list,
        description="Deep-dive sections covering 'Engineering decisions', 'Testing approach', and 'Current scope'.",
    )
    flowcv_format: list[str] = Field(
        default_factory=list,
        description="3 to 5 plain-text ATS action-verb resume bullets (NO markdown bolding or leading bullet symbols).",
    )
    linkedin_profile: str = Field(
        default="",
        description="1st-person professional narrative block (4-6 sentences) showcasing systems engineering choices.",
    )

    @field_validator("id", mode="before")
    @classmethod
    def _clean_id(cls, value: Any) -> str:
        if not value or not str(value).strip():
            return "project"
        return slugify_id(str(value))

    @field_validator("icon", mode="before")
    @classmethod
    def _validate_icon(cls, value: Any) -> str:
        raw = str(value or "workflow").strip().lower()
        if raw in VALID_PORTFOLIO_ICONS:
            return raw
        if raw in {"ml", "bot", "llm", "brain", "sparkle"}:
            return "ai"
        if raw in {"audio", "speech", "mic"}:
            return "voice"
        if raw in {"medical", "clinic", "hospital"}:
            return "health"
        return "workflow"

    @field_validator("artCaption", mode="before")
    @classmethod
    def _clean_art_caption(cls, value: Any) -> str:
        caption = str(value or "ARCHITECTURE → EXECUTION").strip().upper()
        return caption.replace("->", "→")

    @field_validator("flowcv_format", mode="before")
    @classmethod
    def _sanitize_flowcv_bullets(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            lines = [line for line in value.splitlines() if line.strip()]
            return [strip_ats_markdown(line) for line in lines if strip_ats_markdown(line)]
        if isinstance(value, list):
            cleaned = [strip_ats_markdown(str(item)) for item in value]
            return [item for item in cleaned if item]
        return []

    @field_validator("categories", mode="before")
    @classmethod
    def _ensure_categories(cls, value: Any) -> list[str]:
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        if isinstance(value, list):
            cleaned = [str(v).strip() for v in value if str(v).strip()]
            return cleaned or ["Full Stack"]
        return ["Full Stack"]


class EvaluationResult(LLMEvaluationSchema):
    """Complete evaluation artifact combining LLM structured output + git metadata."""

    repo_url: Optional[str] = None
    commit_sha: Optional[str] = None
    source_code_link: Optional[str] = None
    live_demo_link: Optional[str] = None
    evaluated_at: Optional[str] = None

    def to_portfolio_dict(self) -> dict[str, Any]:
        """Build the structured dictionary matching the user's React `projects.js` catalog."""
        entry: dict[str, Any] = {
            "id": slugify_id(self.id if self.id != "project" else self.project_name),
            "name": self.project_name,
        }
        if self.subtitle:
            entry["subtitle"] = self.subtitle
        entry["category"] = self.category
        entry["categories"] = self.categories
        entry["featured"] = self.featured
        if self.status:
            entry["status"] = self.status
        entry["icon"] = self.icon
        if self.artCaption:
            entry["artCaption"] = self.artCaption
        entry["description"] = self.architecture_summary
        entry["techStack"] = self.tech_stack
        entry["highlights"] = self.highlights
        if self.workflow:
            entry["workflow"] = [step.model_dump() for step in self.workflow]
        if self.details:
            serialized_details: list[dict[str, Any]] = []
            for section in self.details:
                sec_dict: dict[str, Any] = {"title": section.title}
                if section.description:
                    sec_dict["description"] = section.description
                sec_dict["items"] = section.items
                serialized_details.append(sec_dict)
            entry["details"] = serialized_details
        if self.source_code_link:
            entry["source_code_link"] = self.source_code_link
        if self.live_demo_link:
            entry["live_demo_link"] = self.live_demo_link
        return entry
