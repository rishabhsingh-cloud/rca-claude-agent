"""Load Jira tickets.

Two sources behind one interface:
  - MockTicketSource: JSON fixtures under fixtures/tickets/ (no network). Default.
  - The live path is the Atlassian MCP server, called by the agent (see jira.py).
    A raw Atlassian `getJiraIssue` payload is normalized to ticket text by
    `normalize_jira_issue` — which flattens Atlassian Document Format (ADF), so a
    stack trace sitting in an ADF code block survives into the text the trace
    parser reads. That normalizer is the load-bearing live-Jira glue, and it's
    unit-tested against a recorded issue fixture.

We flatten summary + description + comments into one text blob — what both trace
parsing and routing consume.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol, runtime_checkable

from .config import FIXTURES_DIR

# ADF block-level node types that should end with a newline when flattened.
_ADF_BLOCKS = {
    "paragraph", "heading", "codeBlock", "blockquote", "listItem",
    "panel", "rule", "tableRow",
}


def flatten_adf(node) -> str:
    """Flatten an Atlassian Document Format node (or plain string) to text.

    Preserves code-block content verbatim — that's where tracebacks live. Plain
    strings pass through unchanged (some payloads already deliver rendered text).
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    t = node.get("type")
    if t == "text":
        return node.get("text", "")
    if t == "hardBreak":
        return "\n"
    body = "".join(flatten_adf(c) for c in node.get("content", []))
    return body + "\n" if t in _ADF_BLOCKS else body


def normalize_jira_response(resp: dict) -> tuple[str, str]:
    """Normalize an Atlassian MCP response into (key, ticket_text).

    Tolerates the `getJiraIssue` / `searchJiraIssuesUsingJql` envelope
    (`{"issues": {"nodes": [issue, ...]}}`) as well as a bare issue dict. This is
    the fetch-then-pass entry point: orchestrator fetches by key → this → pipeline.
    """
    issues = resp.get("issues")
    if isinstance(issues, dict) and issues.get("nodes"):
        return normalize_jira_issue(issues["nodes"][0])
    return normalize_jira_issue(resp)


def _name(obj) -> str:
    if isinstance(obj, dict):
        return obj.get("name") or obj.get("displayName") or ""
    return obj or ""


# Markers identifying a previously-posted automated RCA comment — excluded so the
# agent can't crib an earlier answer when re-investigating a ticket.
_RCA_COMMENT_MARKERS = ("automated rca", "verify before acting", "verified against code")


def normalize_jira_issue(issue: dict, drop_rca_comments: bool = False) -> tuple[str, str]:
    """Normalize a raw Atlassian `getJiraIssue` payload to (key, ticket_text).

    Tolerates both the Jira-REST shape (`fields.*`, description/comment bodies as
    ADF) and an already-flattened shape (top-level string fields). With
    `drop_rca_comments`, prior automated-RCA comments are skipped (fair re-runs).
    """
    key = issue.get("key") or issue.get("id") or "UNKNOWN"
    fields = issue.get("fields", issue)

    summary = fields.get("summary", "")
    status = _name(fields.get("status"))
    priority = _name(fields.get("priority"))
    desc = fields.get("description")
    desc_text = flatten_adf(desc) if isinstance(desc, dict) else (desc or "")

    parts = [
        f"[{key}] {summary}",
        f"Status: {status}  Priority: {priority}",
        "",
        desc_text.rstrip(),
    ]

    comment_field = fields.get("comment")
    comments = (comment_field.get("comments") if isinstance(comment_field, dict)
                else fields.get("comments")) or []
    for c in comments:
        if drop_rca_comments:
            continue  # drop all comments — agent reads title + description only
        author = _name(c.get("author"))
        body = c.get("body")
        body_text = flatten_adf(body) if isinstance(body, dict) else (body or "")
        parts.append(f"\n--- comment by {author or '?'} ---\n{body_text.rstrip()}")

    return key, "\n".join(parts)


# --- fixture loading (mock backend) -------------------------------------------

def load_ticket_file(path: Path) -> tuple[str, str]:
    """Return (ticket_key, ticket_text) from a Jira-shaped JSON file.

    Accepts our simplified fixture shape OR a raw Atlassian issue payload (it
    routes the latter through `normalize_jira_issue`).
    """
    # utf-8-sig tolerates an optional BOM (common on Windows-authored files).
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if "fields" in data or _looks_like_adf(data.get("description")):
        return normalize_jira_issue(data)
    return _flatten(data)


def load_fixture_ticket(key: str) -> tuple[str, str]:
    fp = FIXTURES_DIR / "tickets" / f"{key}.json"
    if not fp.exists():
        raise FileNotFoundError(f"no ticket fixture for {key} (looked for {fp})")
    return load_ticket_file(fp)


def _looks_like_adf(desc) -> bool:
    return isinstance(desc, dict) and desc.get("type") == "doc"


def _flatten(data: dict) -> tuple[str, str]:
    key = data.get("key", "UNKNOWN")
    parts = [
        f"[{key}] {data.get('summary', '')}",
        f"Status: {data.get('status', '')}  Priority: {data.get('priority', '')}",
        "",
        data.get("description", ""),
    ]
    for c in data.get("comments", []):
        parts.append(f"\n--- comment by {c.get('author', '?')} ---\n{c.get('body', '')}")
    return key, "\n".join(parts)


# --- ticket source abstraction -------------------------------------------------

@runtime_checkable
class TicketSource(Protocol):
    def get(self, key: str) -> tuple[str, str]: ...


class MockTicketSource:
    """Reads ticket fixtures from disk (the offline default)."""

    def get(self, key: str) -> tuple[str, str]:
        return load_fixture_ticket(key)


def build_ticket_source(settings) -> TicketSource:
    """Live Jira REST client when JIRA_URL/EMAIL/TOKEN are set, else fixtures."""
    if getattr(settings, "has_jira", False):
        from .jira import JiraClient  # lazy: avoids import cycle + httpx on mock path
        return JiraClient(settings.jira_url, settings.jira_email, settings.jira_token)
    return MockTicketSource()
