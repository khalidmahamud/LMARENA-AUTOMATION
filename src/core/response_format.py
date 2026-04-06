from __future__ import annotations

import json
import re
from typing import Tuple

_FENCED_BLOCK_RE = re.compile(
    r"^\s*```(?:[a-zA-Z0-9_+-]+)?\s*\n(?P<body>[\s\S]*?)\n```\s*$"
)
_HTML_TAG_RE = re.compile(r"</?([A-Za-z][A-Za-z0-9:-]*)\b[^>]*>")
_KNOWN_HTML_TAGS = {
    "html", "head", "body", "div", "span", "p", "section", "article", "main",
    "header", "footer", "nav", "aside", "form", "input", "button", "label",
    "table", "thead", "tbody", "tr", "td", "th", "ul", "ol", "li", "a", "img",
    "style", "script", "link", "meta", "title", "h1", "h2", "h3", "h4", "h5",
    "h6", "pre", "code", "canvas", "svg",
}


def _unwrap_code_fence(text: str) -> str:
    stripped = (text or "").strip()
    match = _FENCED_BLOCK_RE.match(stripped)
    if not match:
        return stripped
    return match.group("body").strip()


def _validate_json(text: str) -> Tuple[bool, str | None]:
    candidate = _unwrap_code_fence(text)
    if not candidate:
        return False, "empty response"
    try:
        json.loads(candidate)
        return True, None
    except json.JSONDecodeError as exc:
        return False, f"invalid JSON: {exc.msg}"


def _validate_html(text: str) -> Tuple[bool, str | None]:
    candidate = _unwrap_code_fence(text)
    if not candidate:
        return False, "empty response"
    if candidate.lower().startswith("<!doctype html"):
        return True, None
    tags = [tag.lower() for tag in _HTML_TAG_RE.findall(candidate)]
    if not tags:
        return False, "no HTML tags found"
    if not any(tag in _KNOWN_HTML_TAGS for tag in tags):
        return False, "response does not look like HTML"
    return True, None


def _validate_plain_text(text: str) -> Tuple[bool, str | None]:
    candidate = _unwrap_code_fence(text)
    if not candidate:
        return False, "empty response"
    is_json, _ = _validate_json(candidate)
    if is_json:
        return False, "response is JSON"
    is_html, _ = _validate_html(candidate)
    if is_html:
        return False, "response is HTML"
    return True, None


def validate_response_format(
    text: str,
    expected_format: str,
) -> Tuple[bool, str | None]:
    normalized = (expected_format or "any").strip().lower()
    if normalized == "any":
        return True, None
    if normalized == "json":
        return _validate_json(text)
    if normalized == "html":
        return _validate_html(text)
    if normalized == "plain_text":
        return _validate_plain_text(text)
    return False, f"unsupported format '{expected_format}'"
