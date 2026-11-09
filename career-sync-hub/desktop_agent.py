"""Step 2: Safe Local Desktop Macro CLI Tool (`desktop_agent.py`).

Launches FlowCV or LinkedIn in your real native desktop browser (zero WebDriver/bot
fingerprints), copies structured text via `pyperclip`, runs a visible 5-second
countdown timer so you can click the target input field, and triggers native OS paste
(`ctrl+v` on Windows/Linux, `cmd+v` on macOS) via `pyautogui`.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import sys
import time
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import webbrowser

from config import Settings
from models import strip_ats_markdown

FLOWCV_URL = "https://app.flowcv.com/resumes"
LINKEDIN_PROJECTS_URL = "https://www.linkedin.com/in/me/"


def get_paste_hotkey() -> tuple[str, str]:
    """Return the native OS paste key chord (`command+v` on macOS, `ctrl+v` elsewhere)."""
    if platform.system() == "Darwin":
        return ("command", "v")
    return ("ctrl", "v")


def format_hotkey_label() -> str:
    mod, key = get_paste_hotkey()
    return "Cmd+V" if mod == "command" else f"{mod.capitalize()}+{key.upper()}"


def fetch_remote_json(url: str, secret: str = "") -> Any:
    """Fetch JSON from a remote `career-sync-hub` server (`/latest` or `/history`)."""
    headers = {
        "Accept": "application/json",
        "User-Agent": "career-sync-hub-desktop/1.0",
    }
    if secret:
        headers["Authorization"] = f"Bearer {secret}"

    req = Request(url, headers=headers, method="GET")
    with urlopen(req, timeout=12) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_history_records(
    settings: Settings,
    force_remote: bool = False,
) -> list[dict[str, Any]]:
    """Load evaluated project history from remote hub or local `data/history.json`."""
    local_history = settings.data_dir / "history.json"
    if (force_remote or not local_history.is_file()) and settings.hub_server_url:
        try:
            data = fetch_remote_json(
                f"{settings.hub_server_url}/history", settings.webhook_secret
            )
            if isinstance(data, list):
                return data
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            if force_remote:
                raise RuntimeError(
                    f"Failed to fetch remote history from {settings.hub_server_url}: {exc}"
                ) from exc

    if local_history.is_file():
        data = json.loads(local_history.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    return []


def load_generation_payload(
    settings: Settings,
    file_override: Optional[str] = None,
    force_remote: bool = False,
    select_query: Optional[str] = None,
) -> dict[str, Any]:
    """Resolve target generation payload from `--file`, `--select`, remote API, or local disk."""
    if file_override:
        fpath = Path(file_override).expanduser().resolve()
        if not fpath.is_file():
            raise FileNotFoundError(f"Specified JSON file does not exist: {fpath}")
        return json.loads(fpath.read_text(encoding="utf-8"))

    if select_query:
        records = load_history_records(settings, force_remote=force_remote)
        if not records:
            raise FileNotFoundError("No generation history records found to select from.")
        # Match by 1-based numeric index, slug `id`, or substring of `project_name`
        if select_query.isdigit():
            idx = int(select_query) - 1
            if 0 <= idx < len(records):
                return records[idx]
            raise ValueError(
                f"History index {select_query} out of range (1..{len(records)})."
            )
        q = select_query.strip().lower()
        for rec in records:
            if (
                str(rec.get("id", "")).lower() == q
                or q in str(rec.get("project_name", "")).lower()
            ):
                return rec
        raise ValueError(f"No project in history matched {select_query!r}.")

    local_latest = settings.data_dir / "latest_generation.json"
    if force_remote:
        if not settings.hub_server_url:
            raise RuntimeError(
                "`--remote` was requested, but HUB_SERVER_URL is not configured in .env."
            )
        data = fetch_remote_json(
            f"{settings.hub_server_url}/latest", settings.webhook_secret
        )
        # Cache locally for offline reuse
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        local_latest.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return data

    if local_latest.is_file():
        return json.loads(local_latest.read_text(encoding="utf-8"))

    # Hybrid auto-fallback: if no local file exists yet, pull from HUB_SERVER_URL if configured
    if settings.hub_server_url:
        try:
            data = fetch_remote_json(
                f"{settings.hub_server_url}/latest", settings.webhook_secret
            )
            settings.data_dir.mkdir(parents=True, exist_ok=True)
            local_latest.write_text(
                json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            return data
        except (HTTPError, URLError, TimeoutError) as exc:
            raise FileNotFoundError(
                f"No local `{local_latest}` found and remote fetch from "
                f"{settings.hub_server_url}/latest failed: {exc}"
            ) from exc

    raise FileNotFoundError(
        f"No evaluation data found at `{local_latest}`. Trigger a push webhook first "
        "or configure `HUB_SERVER_URL` in `.env`."
    )


def build_flowcv_text(data: dict[str, Any], bullet_prefix: str = "") -> str:
    """Format clean ATS-ready plain-text bullets for FlowCV (no markdown bolding)."""
    raw_bullets = data.get("flowcv_format") or []
    cleaned = [strip_ats_markdown(str(b)) for b in raw_bullets if strip_ats_markdown(str(b))]
    if not cleaned and data.get("highlights"):
        cleaned = [strip_ats_markdown(str(h)) for h in data["highlights"]]
    return "\n".join(f"{bullet_prefix}{line}" for line in cleaned)


def build_flowcv_steps(
    data: dict[str, Any], bullet_prefix: str = "", per_bullet: bool = False
) -> list[tuple[str, str]]:
    """Build ordered `(field_label, text_to_paste)` steps for FlowCV Project modal."""
    steps: list[tuple[str, str]] = []
    title = str(data.get("project_name") or "").strip()
    if title:
        steps.append(("FlowCV — Project Title", title))

    stack = [str(s).strip() for s in (data.get("tech_stack") or []) if str(s).strip()]
    subtitle = str(data.get("subtitle") or "").strip()
    sub_line = " | ".join(part for part in [subtitle, ", ".join(stack)] if part)
    if sub_line:
        steps.append(("FlowCV — Subtitle / Tech Stack", sub_line))

    link = str(data.get("live_demo_link") or data.get("source_code_link") or "").strip()
    if link:
        steps.append(("FlowCV — Project URL", link))

    if per_bullet:
        raw_bullets = data.get("flowcv_format") or []
        for idx, b in enumerate(raw_bullets, start=1):
            clean = strip_ats_markdown(str(b))
            if clean:
                steps.append((f"FlowCV — Bullet #{idx}", f"{bullet_prefix}{clean}"))
    else:
        bullets_block = build_flowcv_text(data, bullet_prefix=bullet_prefix)
        if bullets_block:
            steps.append(("FlowCV — ATS Description Bullets", bullets_block))

    return steps


def build_linkedin_text(data: dict[str, Any]) -> str:
    """Format 1st-person narrative + tech stack block for LinkedIn Projects/Experience."""
    narrative = str(data.get("linkedin_profile") or data.get("architecture_summary") or "").strip()
    stack = [str(s).strip() for s in (data.get("tech_stack") or []) if str(s).strip()]
    parts = [p for p in [narrative] if p]
    if stack:
        parts.append(f"Technologies: {', '.join(stack)}")
    return "\n\n".join(parts)


def build_linkedin_steps(data: dict[str, Any]) -> list[tuple[str, str]]:
    """Build ordered `(field_label, text_to_paste)` steps for LinkedIn 'Add project' modal."""
    steps: list[tuple[str, str]] = []
    title = str(data.get("project_name") or "").strip()
    if title:
        steps.append(("LinkedIn — Project Name", title))

    body = build_linkedin_text(data)
    if body:
        steps.append(("LinkedIn — Project Description & Stack", body))

    link = str(data.get("live_demo_link") or data.get("source_code_link") or "").strip()
    if link:
        steps.append(("LinkedIn — Project / Media URL", link))

    return steps


def countdown_and_paste(
    text: str,
    label: str,
    delay_seconds: int = 5,
    dry_run: bool = False,
) -> None:
    """Copy `text` to clipboard, run visible countdown, and fire native OS paste."""
    print(f"\n{'=' * 68}")
    print(f"TARGET FIELD : {label}")
    print(f"HOTKEY       : {format_hotkey_label()}  (Move cursor to top-left corner to abort)")
    print(f"{'-' * 68}")
    print(text)
    print(f"{'=' * 68}")

    if dry_run:
        print("[PREVIEW MODE] Skipping clipboard modification and OS keyboard paste.")
        return

    try:
        import pyperclip
        import pyautogui
    except Exception as import_err:
        raise RuntimeError(
            "PyAutoGUI / Pyperclip could not initialize the GUI display or clipboard. "
            f"Ensure you are running inside an active desktop session. Details: {import_err}"
        ) from import_err

    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.15

    pyperclip.copy(text)
    if pyperclip.paste() != text:
        print(
            "Warning: Clipboard verification mismatch; retrying clipboard write...",
            file=sys.stderr,
        )
        time.sleep(0.15)
        pyperclip.copy(text)

    print(f"\nClick inside the [{label}] input field now!")
    for remaining in range(delay_seconds, 0, -1):
        sys.stdout.write(f"\rPasting via {format_hotkey_label()} in {remaining}s... ")
        sys.stdout.flush()
        time.sleep(1)

    sys.stdout.write("\rTriggering native paste macro now!                 \n")
    sys.stdout.flush()

    modifier, key = get_paste_hotkey()
    # Small settle delay before key chord improves reliability on macOS & Wayland/X11
    time.sleep(0.1)
    pyautogui.hotkey(modifier, key)
    print(f"Done pasting [{label}].")


def run_step_workflow(
    steps: list[tuple[str, str]],
    delay_seconds: int,
    dry_run: bool,
) -> None:
    """Interactive guided loop letting the user paste or skip one field at a time."""
    total = len(steps)
    for idx, (label, content) in enumerate(steps, start=1):
        print(f"\n[{idx}/{total}] Ready for: {label}")
        if dry_run:
            countdown_and_paste(content, label=label, delay_seconds=delay_seconds, dry_run=True)
            continue

        choice = (
            input(
                f"Press [Enter] to start {delay_seconds}s countdown, "
                "[s] to skip field, or [q] to quit: "
            )
            .strip()
            .lower()
        )
        if choice == "q":
            print("Aborted remaining steps.")
            return
        if choice == "s":
            print(f"Skipped [{label}].")
            continue

        countdown_and_paste(content, label=label, delay_seconds=delay_seconds, dry_run=False)


def print_history_table(records: list[dict[str, Any]]) -> None:
    if not records:
        print("No evaluated projects found in history.")
        return

    print(f"\nRecent High-Impact Evaluations ({len(records)}):")
    print(f"{'-' * 76}")
    for idx, rec in enumerate(records, start=1):
        pid = rec.get("id", "unknown")
        name = rec.get("project_name", "Untitled")
        score = rec.get("impact_score", "?")
        ts = str(rec.get("evaluated_at") or "")[:19].replace("T", " ")
        print(f"  [{idx}] {name} (id: {pid}) | score: {score}/10 | {ts}")
    print(f"{'-' * 76}")
    print("Tip: Pass `--select <number_or_id>` with `--flowcv` or `--linkedin`.")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="desktop_agent.py",
        description=(
            "Safe native desktop paste agent for FlowCV and LinkedIn. "
            "Uses clipboard + native OS keyboard shortcuts without browser automation bots."
        ),
    )
    parser.add_argument(
        "--flowcv",
        action="store_true",
        help="Paste ATS-optimized action-verb bullets into FlowCV.",
    )
    parser.add_argument(
        "--linkedin",
        action="store_true",
        help="Paste 1st-person engineering narrative & stack into LinkedIn.",
    )
    parser.add_argument(
        "--step",
        action="store_true",
        help="Interactive field-by-field mode (Title -> Stack -> URL -> Description).",
    )
    parser.add_argument(
        "--per-bullet",
        action="store_true",
        help="With `--flowcv --step`, paste each ATS bullet individually.",
    )
    parser.add_argument(
        "--bullet-prefix",
        default="",
        help="Optional prefix for each FlowCV line (e.g. '• '). Default is clean newline-separated lines.",
    )
    parser.add_argument(
        "--delay",
        type=int,
        default=5,
        help="Countdown timer in seconds before triggering OS paste (default: 5).",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open FlowCV / LinkedIn tab in default system browser.",
    )
    parser.add_argument(
        "--remote",
        action="store_true",
        help="Fetch latest generation directly from HUB_SERVER_URL/latest.",
    )
    parser.add_argument(
        "--file",
        default=None,
        help="Read generation data from a custom JSON file path.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List recent evaluated projects from history and exit.",
    )
    parser.add_argument(
        "--select",
        default=None,
        help="Select a specific project from history by index (1, 2, ...) or slug ID.",
    )
    parser.add_argument(
        "--preview",
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Print formatted output without modifying clipboard or pressing keys.",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    settings = Settings.from_env()

    if args.delay < 1:
        parser.error("--delay must be at least 1 second.")

    try:
        if args.list:
            records = load_history_records(settings, force_remote=args.remote)
            print_history_table(records)
            return 0

        if not args.flowcv and not args.linkedin:
            parser.error("Specify at least one target channel: `--flowcv` and/or `--linkedin` (or `--list`).")

        data = load_generation_payload(
            settings=settings,
            file_override=args.file,
            force_remote=args.remote,
            select_query=args.select,
        )

        project_title = data.get("project_name", "Untitled Project")
        print(f"Loaded project evaluation: {project_title} (id: {data.get('id', 'n/a')})")

        if args.flowcv:
            if not args.no_browser and not args.dry_run:
                print(f"Opening FlowCV in default browser ({FLOWCV_URL})...")
                webbrowser.open(FLOWCV_URL, new=2)

            if args.step:
                steps = build_flowcv_steps(
                    data, bullet_prefix=args.bullet_prefix, per_bullet=args.per_bullet
                )
                run_step_workflow(steps, delay_seconds=args.delay, dry_run=args.dry_run)
            else:
                text = build_flowcv_text(data, bullet_prefix=args.bullet_prefix)
                countdown_and_paste(
                    text,
                    label=f"FlowCV Bullets — {project_title}",
                    delay_seconds=args.delay,
                    dry_run=args.dry_run,
                )

        if args.linkedin:
            if args.flowcv and not args.step and not args.dry_run:
                input("\nPress [Enter] when ready to proceed to LinkedIn...")

            if not args.no_browser and not args.dry_run:
                print(f"Opening LinkedIn in default browser ({LINKEDIN_PROJECTS_URL})...")
                webbrowser.open(LINKEDIN_PROJECTS_URL, new=2)

            if args.step:
                steps = build_linkedin_steps(data)
                run_step_workflow(steps, delay_seconds=args.delay, dry_run=args.dry_run)
            else:
                text = build_linkedin_text(data)
                countdown_and_paste(
                    text,
                    label=f"LinkedIn Narrative — {project_title}",
                    delay_seconds=args.delay,
                    dry_run=args.dry_run,
                )

        return 0

    except KeyboardInterrupt:
        print("\nCancelled by user (KeyboardInterrupt).")
        return 130
    except Exception as err:
        if err.__class__.__name__ == "FailSafeException":
            print("\nAborted immediately by PyAutoGUI corner fail-safe!")
            return 130
        print(f"Error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

