"""Surgical updater for React portfolio project catalogs (.js, .ts, and .json).

Specifically engineered to preserve ES module imports (e.g. `import lab from ...`),
unquoted asset identifiers (`image: lab`), inline comments, and derived exports
(`publishedProjects`, `featuredProjects`, `getProjectFilters`) byte-for-byte.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from models import EvaluationResult, slugify_id


JS_IDENT_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
CANDIDATE_PORTFOLIO_PATHS = (
    "src/data/projects.js",
    "src/projects.js",
    "src/constants/projects.js",
    "src/content/projects.js",
    "src/data/projects.ts",
    "src/projects.ts",
    "src/data/projects.json",
)


@dataclass(frozen=True)
class RawJSIdentifier:
    """Represents a bare JS identifier value such as `lab` in `image: lab`."""

    name: str


@dataclass
class ParsedJSProjectSpan:
    """Character slice and extracted top-level metadata for one `{ ... }` entry."""

    start: int
    end: int
    raw_text: str
    props: dict[str, Any]


def normalize_repo_url(url: Optional[str]) -> str:
    """Normalize a GitHub/Git URL for case-insensitive matching."""
    if not url:
        return ""
    cleaned = url.strip().lower()
    cleaned = re.sub(r"\.git$", "", cleaned)
    cleaned = cleaned.rstrip("/")
    return cleaned


def discover_projects_file(portfolio_dir: Path, configured_rel_path: str = "") -> Path:
    """Locate the portfolio projects catalog inside a cloned React repository."""
    if configured_rel_path:
        explicit = portfolio_dir / configured_rel_path
        if explicit.is_file():
            return explicit

    for rel in CANDIDATE_PORTFOLIO_PATHS:
        candidate = portfolio_dir / rel
        if candidate.is_file():
            return candidate

    # Fallback: scan `src/` for any JS/TS module exporting `projects`
    src_dir = portfolio_dir / "src"
    if src_dir.is_dir():
        for ext in ("*.js", "*.ts", "*.jsx", "*.tsx"):
            for path in sorted(src_dir.rglob(ext)):
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                if re.search(r"\bexport\s+const\s+projects\s*=", text):
                    return path

    # If nothing exists yet, return the configured path (defaulting to src/data/projects.js)
    fallback_rel = configured_rel_path or "src/data/projects.js"
    return portfolio_dir / fallback_rel


def _skip_js_comment(source: str, idx: int) -> int:
    """If `source[idx]` begins a `//` or `/*` JS comment, return index past its end."""
    n = len(source)
    if source[idx] == "/" and idx + 1 < n:
        if source[idx + 1] == "/":
            end = source.find("\n", idx + 2)
            return n if end == -1 else end + 1
        if source[idx + 1] == "*":
            end = source.find("*/", idx + 2)
            return n if end == -1 else end + 2
    return idx


def _skip_js_string_or_comment(source: str, idx: int) -> int:
    """If `source[idx]` begins a JS string or comment, return index just past its end.

    Otherwise return `idx` unchanged.
    """
    comment_end = _skip_js_comment(source, idx)
    if comment_end != idx:
        return comment_end

    n = len(source)
    ch = source[idx]

    # Single or double quoted string
    if ch in ('"', "'"):
        quote = ch
        i = idx + 1
        while i < n:
            if source[i] == "\\":
                i += 2
                continue
            if source[i] == quote:
                return i + 1
            i += 1
        return n

    # Template literal
    if ch == "`":
        i = idx + 1
        expr_depth = 0
        while i < n:
            if source[i] == "\\":
                i += 2
                continue
            if expr_depth == 0:
                if source[i] == "`":
                    return i + 1
                if source[i] == "$" and i + 1 < n and source[i + 1] == "{":
                    expr_depth += 1
                    i += 2
                    continue
            else:
                next_i = _skip_js_string_or_comment(source, i)
                if next_i != i:
                    i = next_i
                    continue
                if source[i] == "{":
                    expr_depth += 1
                elif source[i] == "}":
                    expr_depth -= 1
            i += 1
        return n

    return idx


def _find_projects_array_bounds(source: str) -> tuple[int, int]:
    """Return `(open_bracket_idx, close_bracket_idx)` for `export const projects = [...]`."""
    match = re.search(
        r"\b(?:export\s+)?const\s+projects\s*(?::[^=]+)?=\s*\[",
        source,
    )
    if not match:
        raise ValueError(
            "Could not locate `export const projects = [` inside portfolio module."
        )

    open_idx = match.end() - 1
    n = len(source)
    depth = 0
    i = open_idx

    while i < n:
        next_i = _skip_js_string_or_comment(source, i)
        if next_i != i:
            i = next_i
            continue
        ch = source[i]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return open_idx, i
        i += 1

    raise ValueError("Unclosed `projects` array `[` in portfolio module.")


def _decode_js_string_literal(token: str) -> str:
    """Decode a single- or double-quoted JS string literal into a Python str."""
    token = token.strip()
    if len(token) >= 2 and token[0] == '"' and token[-1] == '"':
        try:
            return json.loads(token)
        except json.JSONDecodeError:
            return token[1:-1]
    if len(token) >= 2 and token[0] == "'" and token[-1] == "'":
        inner = token[1:-1].replace('\\"', '"').replace("\\'", "'")
        try:
            return json.loads(f'"{inner}"')
        except json.JSONDecodeError:
            return inner
    return token


def _extract_top_level_object_props(obj_src: str) -> dict[str, Any]:
    """Extract top-level scalar/identifier properties from a `{ ... }` JS object literal.

    Only inspects keys at brace_depth == 1 and bracket_depth == 0 so nested
    `workflow` or `details` properties never clobber top-level keys.
    """
    props: dict[str, Any] = {}
    n = len(obj_src)
    i = 0
    brace_depth = 0
    bracket_depth = 0
    paren_depth = 0
    expecting_key = False

    while i < n:
        if brace_depth == 1 and bracket_depth == 0 and paren_depth == 0 and expecting_key:
            next_i = _skip_js_comment(obj_src, i)
        else:
            next_i = _skip_js_string_or_comment(obj_src, i)
        if next_i != i:
            i = next_i
            continue

        ch = obj_src[i]
        if ch == "{":
            brace_depth += 1
            if brace_depth == 1 and bracket_depth == 0:
                expecting_key = True
            i += 1
            continue
        if ch == "}":
            brace_depth -= 1
            i += 1
            continue
        if ch == "[":
            bracket_depth += 1
            i += 1
            continue
        if ch == "]":
            bracket_depth -= 1
            i += 1
            continue
        if ch == "(":
            paren_depth += 1
            i += 1
            continue
        if ch == ")":
            paren_depth -= 1
            i += 1
            continue

        if brace_depth == 1 and bracket_depth == 0 and paren_depth == 0:
            if ch == ",":
                expecting_key = True
                i += 1
                continue

            if expecting_key and not ch.isspace():
                expecting_key = False
                # Parse key (identifier or quoted string)
                key: Optional[str] = None
                if ch in ('"', "'"):
                    end_key = _skip_js_string_or_comment(obj_src, i)
                    key = _decode_js_string_literal(obj_src[i:end_key])
                    i = end_key
                else:
                    m = re.match(r"[A-Za-z_$][A-Za-z0-9_$]*", obj_src[i:])
                    if m:
                        key = m.group(0)
                        i += len(key)

                if not key:
                    i += 1
                    continue

                # Advance past whitespace/comments to ':'
                while i < n:
                    ni = _skip_js_comment(obj_src, i)
                    if ni != i:
                        i = ni
                        continue
                    if obj_src[i].isspace():
                        i += 1
                        continue
                    break

                if i >= n or obj_src[i] != ":":
                    continue
                i += 1  # consume ':'

                # Advance past whitespace/comments to value start
                while i < n:
                    ni = _skip_js_comment(obj_src, i)
                    if ni != i:
                        i = ni
                        continue
                    if obj_src[i].isspace():
                        i += 1
                        continue
                    break

                if i >= n:
                    break

                val_ch = obj_src[i]
                if val_ch in ('"', "'"):
                    end_val = _skip_js_string_or_comment(obj_src, i)
                    props[key] = _decode_js_string_literal(obj_src[i:end_val])
                    i = end_val
                    continue
                elif val_ch in ("{", "["):
                    # Nested structure; let main loop handle depth tracking
                    continue
                else:
                    m_val = re.match(r"[A-Za-z0-9_$.+-]+", obj_src[i:])
                    if m_val:
                        raw_token = m_val.group(0)
                        if raw_token == "true":
                            props[key] = True
                        elif raw_token == "false":
                            props[key] = False
                        elif raw_token in ("null", "undefined"):
                            props[key] = None
                        else:
                            props[key] = RawJSIdentifier(raw_token)
                        i += len(raw_token)
                        continue

        i += 1

    return props


def parse_js_projects_array(source: str) -> tuple[int, int, list[ParsedJSProjectSpan]]:
    """Parse `export const projects = [...]` and return bounds + top-level entry spans."""
    open_idx, close_idx = _find_projects_array_bounds(source)
    entries: list[ParsedJSProjectSpan] = []

    i = open_idx + 1
    brace_depth = 0
    bracket_depth = 0
    obj_start: Optional[int] = None

    while i < close_idx:
        next_i = _skip_js_string_or_comment(source, i)
        if next_i != i:
            i = next_i
            continue

        ch = source[i]
        if ch == "{" and bracket_depth == 0:
            if brace_depth == 0:
                obj_start = i
            brace_depth += 1
        elif ch == "}" and bracket_depth == 0:
            brace_depth -= 1
            if brace_depth == 0 and obj_start is not None:
                obj_end = i + 1
                raw = source[obj_start:obj_end]
                entries.append(
                    ParsedJSProjectSpan(
                        start=obj_start,
                        end=obj_end,
                        raw_text=raw,
                        props=_extract_top_level_object_props(raw),
                    )
                )
                obj_start = None
        elif brace_depth == 0 and ch == "[":
            bracket_depth += 1
        elif brace_depth == 0 and ch == "]":
            bracket_depth -= 1

        i += 1

    return open_idx, close_idx, entries


def _format_js_key(key: str) -> str:
    if JS_IDENT_RE.match(key):
        return key
    return json.dumps(key, ensure_ascii=False)


def format_js_value(value: Any, indent_level: int = 1) -> str:
    """Format a Python value as Prettier-compliant ES module JS syntax (2-space indent)."""
    pad = "  " * indent_level
    child_pad = "  " * (indent_level + 1)

    if isinstance(value, RawJSIdentifier):
        return value.name
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)

    if isinstance(value, list):
        if not value:
            return "[]"
        # Inline short primitive arrays (like `categories: ["AI / ML"]`) when concise
        if all(isinstance(v, (str, int, float, bool)) for v in value):
            inline_items = ", ".join(format_js_value(v, indent_level + 1) for v in value)
            inline = f"[{inline_items}]"
            if len(inline) <= 68 and len(value) <= 4:
                return inline

        rendered_items = [
            f"{child_pad}{format_js_value(item, indent_level + 1)},"
            for item in value
        ]
        return "[\n" + "\n".join(rendered_items) + f"\n{pad}]"

    if isinstance(value, dict):
        if not value:
            return "{}"
        lines: list[str] = []
        for k, v in value.items():
            formatted_key = _format_js_key(str(k))
            formatted_val = format_js_value(v, indent_level + 1)
            lines.append(f"{child_pad}{formatted_key}: {formatted_val},")
        return "{\n" + "\n".join(lines) + f"\n{pad}}}"

    return json.dumps(str(value), ensure_ascii=False)


# Canonical field ordering matching the user's React portfolio `projects.js`
FIELD_ORDER = (
    "id",
    "name",
    "subtitle",
    "category",
    "categories",
    "featured",
    "published",
    "status",
    "icon",
    "artCaption",
    "image",
    "description",
    "techStack",
    "highlights",
    "workflow",
    "details",
    "source_code_link",
    "live_demo_link",
)


def _order_portfolio_entry(entry: dict[str, Any]) -> dict[str, Any]:
    ordered: dict[str, Any] = {}
    for key in FIELD_ORDER:
        if key in entry and entry[key] is not None:
            ordered[key] = entry[key]
    for key, val in entry.items():
        if key not in ordered and val is not None:
            ordered[key] = val
    return ordered


def _merge_existing_js_props(
    new_entry: dict[str, Any], existing_props: dict[str, Any]
) -> dict[str, Any]:
    """Preserve stable route IDs, imported image identifiers, and live URLs on update."""
    merged = dict(new_entry)

    # 1. Preserve existing `id` so live portfolio detail routes never break
    if existing_props.get("id"):
        merged["id"] = existing_props["id"]

    # 2. Preserve imported image identifier (e.g. `image: lab` or `image: ecommerce`)
    if "image" in existing_props and existing_props["image"] is not None:
        merged["image"] = existing_props["image"]

    # 3. Preserve explicit `featured: false` or `published: false` overrides
    if existing_props.get("featured") is False:
        merged["featured"] = False
    if existing_props.get("published") is False:
        merged["published"] = False

    # 4. Preserve existing `live_demo_link` or `source_code_link` unless newly discovered
    if existing_props.get("live_demo_link") and not merged.get("live_demo_link"):
        merged["live_demo_link"] = existing_props["live_demo_link"]
    if existing_props.get("source_code_link") and not merged.get("source_code_link"):
        merged["source_code_link"] = existing_props["source_code_link"]

    return _order_portfolio_entry(merged)


def _find_matching_js_entry(
    entries: list[ParsedJSProjectSpan], result: EvaluationResult
) -> Optional[ParsedJSProjectSpan]:
    target_repo = normalize_repo_url(result.repo_url or result.source_code_link)
    target_id = slugify_id(result.id if result.id != "project" else result.project_name)
    target_name = result.project_name.strip().lower()

    for span in entries:
        existing_repo = normalize_repo_url(
            str(span.props.get("source_code_link") or "")
        )
        if target_repo and existing_repo and target_repo == existing_repo:
            return span

    for span in entries:
        existing_id = str(span.props.get("id") or "").strip().lower()
        if existing_id and existing_id == target_id:
            return span

    for span in entries:
        existing_name = str(span.props.get("name") or "").strip().lower()
        if existing_name and existing_name == target_name:
            return span

    return None


def upsert_js_projects_content(source: str, result: EvaluationResult) -> tuple[str, str]:
    """Surgically upsert `result` into a JS/TS `export const projects = [...]` string.

    Returns `(updated_source, action)` where `action` is `"updated"` or `"inserted"`.
    """
    open_idx, close_idx, entries = parse_js_projects_array(source)
    new_entry = _order_portfolio_entry(result.to_portfolio_dict())
    matched = _find_matching_js_entry(entries, result)

    if matched is not None:
        merged = _merge_existing_js_props(new_entry, matched.props)
        formatted_obj = format_js_value(merged, indent_level=1)
        updated_source = source[: matched.start] + formatted_obj + source[matched.end :]
        return updated_source, "updated"

    # Insert brand-new project at index 0 (top of `export const projects = [`)
    formatted_obj = format_js_value(new_entry, indent_level=1)
    insertion = f"\n  {formatted_obj},"
    updated_source = source[: open_idx + 1] + insertion + source[open_idx + 1 :]
    return updated_source, "inserted"


def upsert_json_projects_content(source: str, result: EvaluationResult) -> tuple[str, str]:
    """Upsert `result` into a JSON array or `{"projects": [...]}` JSON document."""
    raw_data = json.loads(source) if source.strip() else []
    is_wrapped = isinstance(raw_data, dict) and isinstance(raw_data.get("projects"), list)
    items: list[dict[str, Any]] = raw_data["projects"] if is_wrapped else raw_data

    if not isinstance(items, list):
        raise ValueError("Expected JSON array or `{\"projects\": [...]}` object.")

    new_entry = _order_portfolio_entry(result.to_portfolio_dict())
    target_repo = normalize_repo_url(result.repo_url or result.source_code_link)
    target_id = new_entry["id"]
    target_name = result.project_name.strip().lower()

    match_idx: Optional[int] = None
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        item_repo = normalize_repo_url(item.get("source_code_link") or item.get("repo_url"))
        if target_repo and item_repo and target_repo == item_repo:
            match_idx = idx
            break
        if str(item.get("id", "")).strip().lower() == target_id:
            match_idx = idx
            break
        if str(item.get("name", "")).strip().lower() == target_name:
            match_idx = idx
            break

    if match_idx is not None:
        merged = _merge_existing_js_props(new_entry, items[match_idx])
        items[match_idx] = merged
        action = "updated"
    else:
        items.insert(0, new_entry)
        action = "inserted"

    out_obj = raw_data if is_wrapped else items
    return json.dumps(out_obj, indent=2, ensure_ascii=False) + "\n", action


def upsert_portfolio_project(
    portfolio_dir: Path,
    result: EvaluationResult,
    configured_rel_path: str = "src/data/projects.js",
) -> tuple[Path, str]:
    """Auto-discover the portfolio catalog file and perform a lossless surgical upsert.

    Returns `(target_file_path, action)` where `action` is `"inserted"` or `"updated"`.
    """
    target_path = discover_projects_file(portfolio_dir, configured_rel_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    suffix = target_path.suffix.lower()
    if suffix == ".json":
        existing = target_path.read_text(encoding="utf-8") if target_path.exists() else "[]"
        updated_text, action = upsert_json_projects_content(existing, result)
        target_path.write_text(updated_text, encoding="utf-8", newline="\n")
        return target_path, action

    # JS / TS ES module handling
    if not target_path.exists():
        # Bootstrap a clean projects.js matching the React portfolio's exports
        existing = (
            "// Auto-initialized by career-sync-hub\n"
            "export const projects = [\n];\n\n"
            "export const publishedProjects = projects.filter(\n"
            "  (project) => project.published !== false,\n"
            ");\n"
            "export const featuredProjects = publishedProjects.filter(\n"
            "  (project) => project.featured !== false,\n"
            ");\n"
            "export function getProjectFilters(catalog) {\n"
            '  return ["All", ...new Set(catalog.flatMap((project) => project.categories))];\n'
            "}\n"
        )
    else:
        existing = target_path.read_text(encoding="utf-8")

    updated_text, action = upsert_js_projects_content(existing, result)
    target_path.write_text(updated_text, encoding="utf-8", newline="\n")
    return target_path, action

