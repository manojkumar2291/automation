"""CLI to read a `README.md` file's content and submit it to `/analyze`.

Examples:
  # Read README.md content and run full release pipeline (Portfolio + LinkedIn + Brief)
  python publish.py --readme C:\\path\\to\\project\\README.md

  # Retry pushing the already-generated `data/latest_generation.json` to your React portfolio
  python publish.py --push-only

  # Dry-run preview (generates brief & copy without pushing to GitHub or posting to LinkedIn)
  python publish.py --readme .\\README.md --dry-run
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from config import Settings
import desktop_agent
from models import AnalyzeRequest, EvaluationResult
import server


# ============================================================================
# Section 1: HTTP Client Helper (`POST /analyze`)
# ============================================================================


def post_to_running_hub(
    settings: Settings,
    req_model: AnalyzeRequest,
) -> Optional[dict[str, Any]]:
    """Send `req_model` (`readme_content`) to a running FastAPI `POST /analyze` server."""
    if not settings.hub_server_url:
        return None

    url = f"{settings.hub_server_url}/analyze"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    body = json.dumps(req_model.model_dump(), ensure_ascii=False).encode("utf-8")
    http_req = Request(url, data=body, headers=headers, method="POST")

    try:
        with urlopen(http_req, timeout=300) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as http_err:
        err_detail = http_err.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"FastAPI /analyze returned HTTP {http_err.code}: {err_detail}"
        ) from http_err
    except (URLError, ConnectionError, OSError):
        return None


# ============================================================================
# Section 2: CLI Arguments & Execution
# ============================================================================


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="publish.py",
        description=(
            "Read a completed project's README.md content, auto-update your React portfolio, "
            "auto-publish a LinkedIn post, and output a complete Executive Brief."
        ),
    )
    parser.add_argument(
        "--readme",
        default=None,
        help="Path to the completed project's README.md file (or folder containing README.md).",
    )
    parser.add_argument(
        "--private",
        action="store_true",
        help="Mark project as private/client work (omits GitHub source_code_link everywhere).",
    )
    parser.add_argument(
        "--no-portfolio",
        action="store_true",
        help="Skip committing & pushing to your React portfolio repo.",
    )
    parser.add_argument(
        "--no-linkedin-post",
        action="store_true",
        help="Skip publishing the automated LinkedIn feed post.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate all copy & brief locally without pushing to GitHub or posting to LinkedIn.",
    )
    parser.add_argument(
        "--direct",
        action="store_true",
        help="Run pipeline directly in this process instead of calling HUB_SERVER_URL/analyze.",
    )
    parser.add_argument(
        "--push-only",
        action="store_true",
        help="Push the existing `data/latest_generation.json` to your React portfolio without re-running Ollama.",
    )
    parser.add_argument(
        "--paste",
        action="store_true",
        help="Immediately launch interactive FlowCV & LinkedIn Profile paste steps after brief.",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    settings = Settings.from_env()

    try:
        # Mode A: Push existing `latest_generation.json` directly to React portfolio
        if args.push_only:
            latest_file = settings.data_dir / "latest_generation.json"
            if not latest_file.is_file():
                parser.error(f"No `{latest_file}` found. Run `publish.py --readme ...` first.")
            raw_data = json.loads(latest_file.read_text(encoding="utf-8"))
            evaluation = EvaluationResult.model_validate(raw_data)
            print(
                f"Syncing `{evaluation.project_name}` (id: `{evaluation.id}`) to "
                f"{settings.portfolio_repo_url} ..."
            )
            action = server.sync_portfolio_repository(settings, evaluation)
            print(json.dumps(action, indent=2))
            return 0 if action.get("status") in {"pushed", "up_to_date"} else 1

        # Mode B: Read `README.md` content directly and analyze
        if not args.readme:
            parser.error("Provide `--readme <path/to/README.md>` or `--push-only`.")

        readme_file = Path(args.readme).expanduser().resolve()
        if readme_file.is_dir():
            readme_file = readme_file / "README.md"
        if not readme_file.is_file():
            raise FileNotFoundError(f"README.md file not found: {readme_file}")

        readme_content = readme_file.read_text(encoding="utf-8", errors="replace").strip()
        req_model = AnalyzeRequest(
            readme_content=readme_content,
            is_private=bool(args.private),
            update_portfolio=not (args.no_portfolio or args.dry_run),
            post_to_linkedin=not (args.no_linkedin_post or args.dry_run),
        )

        response_data: Optional[dict[str, Any]] = None
        if not args.direct:
            print(f"Sending README.md content to {settings.hub_server_url}/analyze ...")
            response_data = post_to_running_hub(settings, req_model)

        if response_data is None:
            if not args.direct:
                print("FastAPI server not detected on localhost; running pipeline directly...")
            resp_obj = server.execute_release_pipeline(req_model, settings=settings)
            response_data = resp_obj.model_dump()

        print("\n" + "=" * 78)
        print(response_data["brief_markdown"])
        print("=" * 78)
        print(f"Brief saved to: {response_data['brief_file']}")

        if args.paste:
            print("\nLaunching guided paste for FlowCV Resume & LinkedIn Profile Project...")
            return desktop_agent.main(["--flowcv", "--linkedin", "--step"])

        return 0
    except KeyboardInterrupt:
        print("\nCancelled by user.")
        return 130
    except Exception as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
