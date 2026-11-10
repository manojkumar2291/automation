"""Unit tests for lossless ES-module (`projects.js`) and JSON portfolio upserts.

Validates against the exact React portfolio structure (`imports`, `image: lab`,
inline comments, `workflow`, `details`, and bottom filter exports).
"""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models import DetailSection, EvaluationResult, WorkflowStep
from portfolio_updater import (
    discover_projects_file,
    parse_js_projects_array,
    upsert_js_projects_content,
    upsert_portfolio_project,
)

USER_PORTFOLIO_FIXTURE = '''import ecommerce from "./assets/projects/ecommerce.jpeg";
import interest from "./assets/projects/interest.jpeg";
import lab from "./assets/projects/labbooking.webp";

// Add new projects here. Array order controls display order.
// featured: false keeps an older project in All projects and preserves its URL.
// published: false hides a draft from both the listing and its detail route.
// See docs/CONTENT_UPDATES.md for a copyable template and resume instructions.
// Only supplied or existing public URLs belong here. Client source stays private.
export const projects = [
  {
    id: "multi-agent-rag",
    name: "Multi-Agent RAG Document Assistant",
    subtitle: "Plan. Retrieve. Answer. Review.",
    category: "AI / RAG",
    categories: ["AI / ML"],
    featured: true,
    status: "Local prototype",
    icon: "workflow",
    artCaption: "DOCUMENTS → GROUNDED ANSWERS",
    description:
      "A document question-answering API that combines semantic retrieval with LangChain query planning, answer writing, and review.",
    techStack: [
      "Python",
      "FastAPI",
      "LangChain",
    ],
    highlights: [
      "Three language-agent roles: query planner, answer writer, and reviewer",
    ],
  },
  {
    id: "6",
    name: "Diagnostic Lab Booking Platform",
    category: "Full Stack / Healthcare",
    categories: ["Full Stack", "Healthcare"],
    image: lab,
    description:
      "A diagnostic lab booking platform connecting users with laboratories.",
    techStack: ["React.js", "Node.js"],
    highlights: ["Lab and test discovery"],
  },
  {
    id: "1",
    // featured: false,
    name: "E-Commerce Web Application",
    category: "Web Application",
    categories: ["Web Applications"],
    image: ecommerce,
    description:
      "A responsive e-commerce application with an intuitive shopping interface.",
    techStack: ["React", "Node.js", "Express", "MongoDB", "JWT"],
    highlights: ["Responsive shopping interface", "JWT authentication"],
    source_code_link: "https://github.com/manojkumar2291/vibe",
    live_demo_link: "https://vibe-nine-omega.vercel.app/",
  },
];

// Defaults keep additions simple: a project is published and featured unless
// explicitly disabled. Derive categories so future additions need no UI changes.
export const publishedProjects = projects.filter(
  (project) => project.published !== false,
);
export const featuredProjects = publishedProjects.filter(
  (project) => project.featured !== false,
);
export function getProjectFilters(catalog) {
  return ["All", ...new Set(catalog.flatMap((project) => project.categories))];
}
'''


def _sample_result(**overrides) -> EvaluationResult:
    base = dict(
        is_high_impact=True,
        impact_score=9,
        impact_reason="Full webhook orchestration pipeline and desktop macro CLI.",
        id="career-sync-hub",
        project_name="Career Sync Hub",
        subtitle="Push. Evaluate. Publish. Paste.",
        category="AI / Automation",
        categories=["AI / ML", "Full Stack"],
        featured=True,
        status="Active development",
        icon="workflow",
        artCaption="GIT PUSH → PORTFOLIO & CV SYNC",
        architecture_summary="Decoupled webhook hub that evaluates commits and updates React portfolios.",
        tech_stack=["Python", "FastAPI", "GitPython", "OpenAI SDK", "PyAutoGUI"],
        highlights=[
            "Webhook-driven git diff evaluation",
            "Lossless ES-module React portfolio upserter",
        ],
        workflow=[
            WorkflowStep(
                title="Receive push webhook",
                description="Validate HMAC/Bearer token and clone target repository into an isolated workspace.",
            ),
            WorkflowStep(
                title="Evaluate & publish",
                description="Score engineering impact via LLM and push structured updates to the portfolio repo.",
            ),
        ],
        details=[
            DetailSection(
                title="Engineering decisions",
                items=[
                    "Comment-aware JS span parser preserves asset imports and helper exports.",
                    "Windows-safe read-only packfile cleanup prevents temporary directory leaks.",
                ],
            )
        ],
        flowcv_format=[
            "**Architected** a FastAPI webhook hub automating portfolio and ATS resume updates.",
            "- Implemented lossless JavaScript ES-module surgical AST span replacement.",
        ],
        linkedin_profile="I built Career Sync Hub to turn meaningful git pushes into live portfolio and CV updates.",
        source_code_link="https://github.com/manojkumar2291/career-sync-hub",
    )
    base.update(overrides)
    return EvaluationResult(**base)


def test_insert_new_project_at_top_preserves_imports_and_exports() -> None:
    res = _sample_result()
    updated, action = upsert_js_projects_content(USER_PORTFOLIO_FIXTURE, res)

    assert action == "inserted"
    # Imports at top untouched
    assert 'import ecommerce from "./assets/projects/ecommerce.jpeg";' in updated
    assert 'import lab from "./assets/projects/labbooking.webp";' in updated
    # Existing bare identifier `image: lab` and comments untouched
    assert "image: lab," in updated
    assert "// featured: false," in updated
    # Bottom helper functions untouched
    assert "export function getProjectFilters(catalog)" in updated

    # Verify new project is inserted at index 0 of `export const projects = [...]`
    _, _, spans = parse_js_projects_array(updated)
    assert len(spans) == 4
    assert spans[0].props["id"] == "career-sync-hub"
    assert spans[0].props["name"] == "Career Sync Hub"
    assert spans[0].props["artCaption"] == "GIT PUSH → PORTFOLIO & CV SYNC"
    assert spans[1].props["id"] == "multi-agent-rag"


def test_update_existing_project_by_source_link_preserves_id_image_and_demo() -> None:
    res = _sample_result(
        id="vibe-ecommerce",
        project_name="Vibe E-Commerce Platform",
        source_code_link="https://github.com/manojkumar2291/vibe",
        live_demo_link=None,
    )
    updated, action = upsert_js_projects_content(USER_PORTFOLIO_FIXTURE, res)

    assert action == "updated"
    _, _, spans = parse_js_projects_array(updated)
    assert len(spans) == 3  # No duplicate added!

    # Entry at index 2 was updated in-place while preserving route ID "1", `image: ecommerce`, and live demo URL
    ecommerce_span = spans[2]
    assert ecommerce_span.props["id"] == "1"
    assert ecommerce_span.props["name"] == "Vibe E-Commerce Platform"
    assert "image: ecommerce," in ecommerce_span.raw_text
    assert ecommerce_span.props["live_demo_link"] == "https://vibe-nine-omega.vercel.app/"


def test_auto_discovery_finds_custom_src_projects_js(tmp_path: Path) -> None:
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True)
    js_file = src_dir / "projects.js"
    js_file.write_text(USER_PORTFOLIO_FIXTURE, encoding="utf-8")

    discovered = discover_projects_file(tmp_path, "src/data/projects.js")
    assert discovered == js_file

    target, action = upsert_portfolio_project(tmp_path, _sample_result())
    assert target == js_file
    assert action == "inserted"

