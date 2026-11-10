"""One-command CLI to submit a completed project's `README.md` to `/analyze`.

Examples:
  # Submit a local README.md (auto-detects GitHub repo URL from .git/config if inside repo!)
  python publish.py --readme C:\\path\\to\\project\\README.md

  # Submit with explicit GitHub repo & live Vercel URL
  python publish.py --readme .\\README.md --repo-url https://github.com/manojkumar2291/vibe --demo https://vibe.vercel.app

  # Fetch README.md directly from GitHub API without needing local files
  python publish.py --repo-url https://github.com/manojkumar2291/vibe

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
from models import AnalyzeRequest
import server


def _post_to_running_hub(
    settings: Settings,
    req_model: AnalyzeRequest,
) -> Optional[dict[str, Any]]:
    """Send `req_model` to running FastAPI `POST /analyze` if reachable; else return None."""
    if not settings.hub_server_url:
        return None

    url = f"{settings.hub_server_url}/analyze"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if settings.webhook_secret:
        headers["Authorization"] = f"Bearer {settings.webhook_secret}"

    body = json.dumps(req_model.model_dump(), ensure_ascii=False).encode("utf-8")
    http_req = Request(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(http_req, timeout=300) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as http_err:
        err_detail = http_err.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"FastAPI /analyze returned HTTP {http_err.code}: {err_detail}") from http_err
    except (URLError, ConnectionError, OSError):
        # FastAPI server is not running locally; fall back to direct in-process execution!
        return None


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="publish.py",
        description=(
            "Analyze a completed project's README.md, auto-update your React portfolio, "
            "auto-publish a LinkedIn post, and output a complete Executive Brief."
        ),
    )
    parser.add_argument(
        "--readme",
        default=None,
        help="Path to the completed project's README.md (or project folder).",
    )
    parser.add_argument(
        "--repo-url",
        default=None,
        help="GitHub repository URL (optional if `--readme` is inside a git repo; or pass alone to fetch README from GitHub).",
    )
    parser.add_argument(
        "--name",
        default=None,
        help="Optional explicit project display name override.",
    )
    parser.add_argument(
        "--demo",
        default=None,
        help="Optional live demo URL (e.g. https://my-app.vercel.app).",
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
        "--paste",
        action="store_true",
        help="Immediately launch interactive FlowCV & LinkedIn Profile paste steps after brief.",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    settings = Settings.from_env()

    if not args.readme and not args.repo_url:
        parser.error("Provide `--readme <path/to/README.md>` and/or `--repo-url <https://github.com/...>`.")

    readme_path_str: Optional[str] = None
    if args.readme:
        readme_path_str = str(Path(args.readme).expanduser().resolve())

    update_portfolio = not (args.no_portfolio or args.dry_run)
    post_to_linkedin = not (args.no_linkedin_post or args.dry_run)

    req_model = AnalyzeRequest(
        readme_path=readme_path_str,
        repo_url=args.repo_url,
        project_name_hint=args.name,
        live_demo_link=args.demo,
        is_private=True if args.private else None,
        update_portfolio=update_portfolio,
        post_to_linkedin=post_to_linkedin,
    )

    try:
        response_data: Optional[dict[str, Any]] = None
        if not args.direct:
            print(f"Sending README analysis request to {settings.hub_server_url}/analyze ...")
            response_data = _post_to_running_hub(settings, req_model)

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

