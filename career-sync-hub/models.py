"""Pydantic schemas for README content analysis, portfolio items, resume bullets, and LinkedIn."""

from __future__ import annotations

import re
from typing import Any, Optional
from pydantic import BaseModel, Field, field_validator, model_validator


VALID_PORTFOLIO_ICONS = {"workflow", "ai", "voice", "health", "crm", "leads"}


# ============================================================================
# Section 1: Text & Slug Sanitizers
# ============================================================================


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


def clean_linkedin_plain_text(text: str) -> str:
    """Remove Markdown asterisks/backticks that render literally on LinkedIn while keeping line breaks."""
    cleaned = text.strip()
    cleaned = re.sub(r"\*\*(.+?)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"__(.+?)__", r"\1", cleaned)
    cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
    cleaned = re.sub(r"(?m)^-\s+", "• ", cleaned)
    return cleaned.strip()


# ============================================================================
# Section 2: API Request Schema (`README.md` Content Input)
# ============================================================================


class AnalyzeRequest(BaseModel):
    """Input payload for `POST /analyze` containing the raw `README.md` content."""

    readme_content: str = Field(
        ...,
        min_length=1,
        description="Full Markdown content of your completed project's README.md.",
    )
    is_private: bool = Field(
        default=False,
        description="Set true for private/client projects so source_code_link is omitted.",
    )
    update_portfolio: bool = Field(
        default=True,
        description="Whether to commit & push the project update to PORTFOLIO_REPO_URL.",
    )
    post_to_linkedin: bool = Field(
        default=True,
        description="Whether to publish the generated launch post via LINKEDIN_ACCESS_TOKEN.",
    )


# ============================================================================
# Section 3: Structured LLM Output Schema (`ChatOllama`)
# ============================================================================


class WorkflowStep(BaseModel):
    """Single step inside `project.workflow` in the React portfolio."""

    title: str = Field(description="Short stage title (3-4 words), e.g. 'Ingest documents'.")
    description: str = Field(
        description="1-2 sentence explanation of how data/control flows through this stage."
    )


class DetailSection(BaseModel):
    """Deep-dive engineering section inside `project.details` in the React portfolio."""

    title: str = Field(
        description="Section heading: 'Engineering decisions', 'Testing approach', or 'Current scope'."
    )
    description: Optional[str] = Field(
        default=None,
        description="Optional 1-sentence introductory summary for this detail section.",
    )
    items: list[str] = Field(
        default_factory=list,
        description="2 to 4 concrete technical points grounded in the README.",
    )


class LLMEvaluationSchema(BaseModel):
    """Structured schema bound directly to `ChatOllama.with_structured_output(...)`."""

    id: str = Field(
        default="project",
        description="Clean kebab-case slug identifier for the React portfolio route (e.g. 'multi-agent-rag').",
    )
    project_name: str = Field(
        default="Untitled Project",
        description="Human-readable project display title extracted from the README.",
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
        default="Completed",
        description="Maturity label such as 'Production', 'Live prototype', 'Local prototype', or 'Open source'.",
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
        description="Concrete programming languages, frameworks, and tools mentioned in the README.",
    )
    highlights: list[str] = Field(
        default_factory=list,
        description="4 to 6 concise capability and systems engineering highlights.",
    )
    workflow: list[WorkflowStep] = Field(
        default_factory=list,
        description="3 to 4 ordered architectural workflow steps explaining how the project works.",
    )
    details: list[DetailSection] = Field(
        default_factory=list,
        description="2 to 3 deep-dive sections ('Engineering decisions', 'Testing approach', 'Current scope').",
    )
    flowcv_format: list[str] = Field(
        default_factory=list,
        description="3 to 5 plain-text ATS action-verb resume bullets for FlowCV (NO markdown bolding or leading bullets).",
    )
    linkedin_profile: str = Field(
        default="",
        description="1st-person professional narrative block (4-6 sentences) for LinkedIn's Profile Projects section.",
    )
    linkedin_post: str = Field(
        default="",
        description=(
            "Engaging 1st-person LinkedIn feed launch post (120-220 words): strong opening hook, "
            "problem solved, 3-4 bulleted engineering highlights using '•', tech stack line, and "
            "3-5 relevant hashtags. Plain Unicode text ONLY (no **bold** markdown)."
        ),
    )
    executive_brief: str = Field(
        default="",
        description="Concise 2-3 sentence summary explaining what was extracted and how the project was positioned.",
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

    @field_validator("linkedin_profile", "linkedin_post", mode="before")
    @classmethod
    def _sanitize_linkedin_text(cls, value: Any) -> str:
        return clean_linkedin_plain_text(str(value or ""))

    @field_validator("categories", mode="before")
    @classmethod
    def _ensure_categories(cls, value: Any) -> list[str]:
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        if isinstance(value, list):
            cleaned = [str(v).strip() for v in value if str(v).strip()]
            return cleaned or ["Full Stack"]
        return ["Full Stack"]

    @model_validator(mode="after")
    def _enrich_missing_career_fields(self) -> "LLMEvaluationSchema":
        """Guarantee `flowcv_format`, `linkedin_profile`, and `linkedin_post` are never empty."""
        stack_preview = ", ".join(self.tech_stack[:5]) if self.tech_stack else "modern backend tools"

        # 1. Guarantee 3-5 ATS action-verb bullets for FlowCV Resume
        if not self.flowcv_format:
            synthesized: list[str] = []
            if self.architecture_summary:
                summary_clean = strip_ats_markdown(self.architecture_summary)
                if summary_clean:
                    first_lower = summary_clean[0].lower() + summary_clean[1:]
                    synthesized.append(
                        f"Architected and built {self.project_name} using {stack_preview} — {first_lower}"
                    )
            for step in self.workflow[:2]:
                desc = strip_ats_markdown(step.description).rstrip(".")
                if desc:
                    synthesized.append(
                        f"Engineered {step.title.lower()} pipeline stage: {desc}."
                    )
            for hl in self.highlights:
                if len(synthesized) >= 4:
                    break
                hl_clean = strip_ats_markdown(hl).rstrip(".")
                if hl_clean:
                    synthesized.append(f"Implemented {hl_clean[0].lower() + hl_clean[1:]}.")
            self.flowcv_format = synthesized[:5]

        # 2. Guarantee LinkedIn Profile Project narrative
        if not self.linkedin_profile and self.architecture_summary:
            hl_sentence = (
                " Key capabilities include " + "; ".join(self.highlights[:3]) + "."
                if self.highlights
                else ""
            )
            self.linkedin_profile = clean_linkedin_plain_text(
                f"I designed and built {self.project_name} ({self.subtitle or self.category}). "
                f"{self.architecture_summary}{hl_sentence}"
            )

        # 3. Guarantee LinkedIn Feed Launch Post
        if not self.linkedin_post and self.architecture_summary:
            bullet_lines = "\n".join(f"• {strip_ats_markdown(h)}" for h in self.highlights[:4])
            tags = " ".join(
                f"#{re.sub(r'[^a-zA-Z0-9]', '', t)}" for t in self.tech_stack[:4] if t
            )
            self.linkedin_post = clean_linkedin_plain_text(
                f"Excited to share my latest project: {self.project_name}!\n\n"
                f"{self.architecture_summary}\n\n"
                f"Key Engineering Highlights:\n{bullet_lines}\n\n"
                f"Tech Stack: {', '.join(self.tech_stack)}\n\n"
                f"{tags or '#SoftwareEngineering #AI #FullStack'}"
            )

        return self


# ============================================================================
# Section 4: Persisted Artifact & API Response Models
# ============================================================================


class EvaluationResult(LLMEvaluationSchema):
    """Complete generated artifact persisted to `data/latest_generation.json`."""

    source_code_link: Optional[str] = None
    live_demo_link: Optional[str] = None
    evaluated_at: Optional[str] = None
    linkedin_post_url: Optional[str] = None
    portfolio_status: Optional[str] = None

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


class AnalyzeResponse(BaseModel):
    """Synchronous response returned by `/analyze` with action statuses + brief."""

    status: str = "completed"
    project_id: str
    project_name: str
    actions: dict[str, dict[str, Any]]
    brief_markdown: str
    brief_file: str
    evaluation: EvaluationResult
