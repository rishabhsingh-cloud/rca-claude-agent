"""Agent-SDK driver: let Claude orchestrate the read-only RCA tools.

Use this path for fuzzier tickets where judgement helps; the deterministic
pipeline (investigation.py) covers the clean trace-first case with no model.
Both share the same tools and the same verdict schema.

Requires `claude-agent-sdk` and ANTHROPIC_API_KEY in the environment.
"""

from __future__ import annotations

import json

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

from .config import Settings
from .gitlab_client import GitLabClient
from .jira import atlassian_allowed_tools, atlassian_mcp_server_config
from .prompts import build_system_prompt
from .schema import Confidence, EvidenceLink, Triage, Verdict
from .tools import build_rca_server


class AgentRunError(RuntimeError):
    """The agent run failed for an infrastructure reason (out of credits, rate
    limit, overload, no output) — distinct from a low-confidence verdict."""


async def run_agent(ticket_key: str, ticket_text: str | None, client: GitLabClient,
                    settings: Settings, jira_mcp: bool = False,
                    max_turns: int = 60,
                    images: list[dict] | None = None) -> str:
    """Run one investigation through the agent loop; return its final text
    (expected to be the JSON verdict).

    Jira integration (two modes):
      - FETCH-THEN-PASS (default, recommended): the orchestrator fetches the
        issue via Atlassian and passes `ticket_text` in (use `normalize_jira_issue`
        on a `getJiraIssue` payload). Works everywhere — no MCP auth in the agent.
      - jira_mcp=True: attach the hosted Atlassian MCP so the agent fetches by key
        itself. Only works when that MCP server is actually authenticated for the
        SDK (e.g. inherited from the host via ClaudeAgentOptions.setting_sources);
        the hosted server will NOT complete OAuth headlessly on its own.
    """
    server, tool_names = build_rca_server(client)
    mcp_servers = {"rca": server}
    allowed = list(tool_names)
    if jira_mcp:
        mcp_servers["atlassian"] = atlassian_mcp_server_config()
        allowed += atlassian_allowed_tools("atlassian")

    options = ClaudeAgentOptions(
        system_prompt=build_system_prompt(settings.gitlab_url),
        mcp_servers=mcp_servers,
        allowed_tools=allowed,      # read-only tools, pre-approved -> no prompts
        model=settings.model,
        permission_mode="default",
        # Real cross-service investigations fan out across many tools; 20 was too
        # tight (hit the cap mid-run on a large repo).
        max_turns=max_turns,
    )

    text_part = (f"Investigate Jira ticket {ticket_key}. Ticket content:\n\n{ticket_text}"
                 if ticket_text else
                 f"Investigate Jira ticket {ticket_key}. Fetch it with "
                 f"mcp__atlassian__getJiraIssue first, then analyze.")

    if images:
        img_note = (f"\n\nThe ticket also has {len(images)} screenshot(s) attached. "
                    "Read them carefully — they may show the exact error dialog, "
                    "UI state, or stack trace that caused the bug.")
        prompt: list | str = [{"type": "text", "text": text_part + img_note}]
        for img in images:
            prompt.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img["mimeType"],
                    "data": img["content"],
                },
            })
    else:
        prompt = text_part

    final_text = ""
    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        final_text = block.text
            elif isinstance(message, ResultMessage):
                subtype = getattr(message, "subtype", None)
                result = getattr(message, "result", None) or ""
                # Infra failure (out of credits, rate limit, overload, …) surfaces
                # as a non-success result — raise loudly so it can't be parsed into
                # a fake "insufficient_evidence" verdict and posted to a ticket.
                if subtype not in (None, "success") or getattr(message, "is_error", False):
                    raise AgentRunError(f"agent run failed ({subtype}): {result[:200]}")
                if result:
                    final_text = result
    except AgentRunError:
        raise  # infra failure always propagates
    except Exception:
        # Hit max_turns mid-run — keep a partial verdict rather than crashing.
        if not final_text:
            raise
    # A real verdict must PARSE as a JSON object with verdict fields. Prose (even
    # with stray braces from set notation), an error string, or a cut-off
    # mid-compose all fail here — raise loudly instead of faking an LOW verdict.
    if not _looks_like_verdict(final_text):
        raise AgentRunError(
            "agent did not emit a verdict (likely hit max_turns mid-compose): "
            f"{final_text[:150] or 'no output'}")
    return final_text


def _looks_like_verdict(text: str) -> bool:
    """True only if `text` contains a JSON object carrying verdict fields."""
    if "{" not in text or "}" not in text:
        return False
    candidate = text[text.find("{"): text.rfind("}") + 1]
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, dict) and bool(
        {"confidence", "probable_root_cause", "triage"} & parsed.keys())


def parse_verdict(text: str, ticket_key: str) -> Verdict:
    """Best-effort parse of the agent's JSON output into a Verdict for rendering.

    Tolerant of code fences / surrounding prose. Falls back to a low-confidence
    verdict carrying the raw text if parsing fails — we never fabricate a
    confident result from unparseable output.
    """
    raw = text.strip()
    # Strip ```json fences if present.
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip().rstrip("`").strip()
    # Isolate the outermost JSON object.
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        raw = raw[start:end + 1]

    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        return Verdict(
            ticket=ticket_key,
            probable_root_cause="Agent output was not valid JSON.",
            triage=Triage.INSUFFICIENT_EVIDENCE,
            confidence=Confidence.LOW,
            notes=text[:500],
        )

    return Verdict(
        ticket=d.get("ticket", ticket_key),
        probable_root_cause=d.get("probable_root_cause", ""),
        headline=d.get("headline", ""),
        plain_summary=d.get("plain_summary", ""),
        evidence_chain=[
            EvidenceLink(kind=e["kind"], ref=e["ref"], detail=e["detail"],
                         url=e.get("url", ""))
            for e in d.get("evidence_chain", [])
            if isinstance(e, dict) and {"kind", "ref", "detail"} <= e.keys()
        ],
        is_regression=d.get("is_regression"),
        introducing_mr=d.get("introducing_mr"),
        triage=Triage(d["triage"]) if d.get("triage") in {t.value for t in Triage}
        else Triage.INSUFFICIENT_EVIDENCE,
        confidence=Confidence(d["confidence"]) if d.get("confidence") in {c.value for c in Confidence}
        else Confidence.LOW,
        suggested_next_action=d.get("suggested_next_action", ""),
        candidates=d.get("candidates", []) or [],
        notes=d.get("notes", ""),
    )
