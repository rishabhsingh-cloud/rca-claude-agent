"""Agent-SDK driver: let Claude orchestrate the read-only RCA tools.

Use this path for fuzzier tickets where judgement helps; the deterministic
pipeline (investigation.py) covers the clean trace-first case with no model.
Both share the same tools and the same verdict schema.

Requires `claude-agent-sdk` and ANTHROPIC_API_KEY in the environment.
"""

from __future__ import annotations

import json

import claude_agent_sdk
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from . import trace as _trace
from .config import Settings
from .gitlab_client import GitLabClient
from .jira import atlassian_allowed_tools, atlassian_mcp_server_config
from .profiles import AgentProfile, build_profile_system_prompt
from .prompts import build_system_prompt
from .schema import CauseCategory, Confidence, EvidenceLink, Triage, Verdict
from .tools import build_rca_server


class AgentRunError(RuntimeError):
    """The agent run failed for an infrastructure reason (out of credits, rate
    limit, overload, no output) — distinct from a low-confidence verdict."""


# Built-in Claude Code tools the RCA agent must never use. `Task` (subagents) made
# runs end on prose ABOUT a subagent instead of the JSON verdict — and burned the
# time budget — while `Write`/`Edit` have no place in a read-only investigator.
# Applied to every RCA run by default. `Bash`/`Read` are intentionally NOT blocked
# yet: the agent still leans on them to page through large tool output. Revisit once
# get_repo_summary is trimmed and the eval harness confirms no regression.
DEFAULT_DISALLOWED_TOOLS = ["Task", "Write", "Edit"]


async def run_agent(ticket_key: str, ticket_text: str | None, client: GitLabClient,
                    settings: Settings, jira_mcp: bool = False,
                    max_turns: int = 60,
                    images: list[dict] | None = None,
                    disallowed_tools: list[str] | None = None,
                    profile: AgentProfile | None = None) -> tuple[str, int, set[str]]:
    """Run one investigation through the agent loop; return
    (final_text, turns_used, tools_used) — tools_used is the set of tool names the
    agent actually invoked (e.g. "mcp__rca__git_blame"), used by post-hoc guards.

    Jira integration (two modes):
      - FETCH-THEN-PASS (default, recommended): the orchestrator fetches the
        issue via Atlassian and passes `ticket_text` in (use `normalize_jira_issue`
        on a `getJiraIssue` payload). Works everywhere — no MCP auth in the agent.
      - jira_mcp=True: attach the hosted Atlassian MCP so the agent fetches by key
        itself. Only works when that MCP server is actually authenticated for the
        SDK (e.g. inherited from the host via ClaudeAgentOptions.setting_sources);
        the hosted server will NOT complete OAuth headlessly on its own.
    """
    _trace.setup_tracing()  # no-op unless RCA_TRACE=1 + Phoenix configured
    server, tool_names = build_rca_server(
        client, search_scope=(profile.search_scope if profile else None))
    mcp_servers = {"rca": server}
    # A profile may restrict the tool set; default is the full read-only set.
    allowed = (list(profile.allowed_tools) if (profile and profile.allowed_tools)
               else list(tool_names))
    if jira_mcp:
        mcp_servers["atlassian"] = atlassian_mcp_server_config()
        allowed += atlassian_allowed_tools("atlassian")

    options = ClaudeAgentOptions(
        # Reco (or any profile) gets its OWN system prompt, composed in profiles/ from
        # the unmodified general prompt. profile=None -> the general prompt, unchanged.
        system_prompt=(build_profile_system_prompt(profile, settings.gitlab_url)
                       if profile else build_system_prompt(settings.gitlab_url)),
        mcp_servers=mcp_servers,
        allowed_tools=allowed,      # read-only tools, pre-approved -> no prompts
        # Block the dangerous/derailing built-ins (Task/Write/Edit by default).
        # `None` -> the default block-list; pass an explicit list (incl. []) to override.
        disallowed_tools=(DEFAULT_DISALLOWED_TOOLS if disallowed_tools is None
                          else disallowed_tools),
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

    # A profile can front-load a module summary so the agent always orients on it
    # (no dependence on it choosing to call get_repo_summary).
    if profile and profile.summary:
        text_part = (f"# {profile.name.upper()} MODULE REFERENCE (read this first)\n\n"
                     f"{profile.summary}\n\n---\n\n{text_part}")

    if images:
        img_note = (f"\n\nThe ticket also has {len(images)} screenshot(s) attached. "
                    "Read them carefully — they may show the exact error dialog, "
                    "UI state, or stack trace that caused the bug.")
        content: list = [{"type": "text", "text": text_part + img_note}]
        for img in images:
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img["mimeType"],
                    "data": img["content"],
                },
            })

        # The SDK only writes input for a `str` or an `AsyncIterable` prompt — a
        # plain list of content blocks is silently dropped (no input sent, stdin
        # never closed), so the run hangs forever. Wrap the multimodal message in
        # a single-item async stream in the streaming-input shape the SDK expects.
        async def _prompt_stream():
            yield {
                "type": "user",
                "session_id": "",
                "message": {"role": "user", "content": content},
                "parent_tool_use_id": None,
            }

        prompt: object = _prompt_stream()
    else:
        prompt = text_part

    final_text = ""
    turns_used = 0
    tools_used: set[str] = set()
    try:
        # Call through the module (not a `from … import query` binding) so the
        # Phoenix auto-instrumentor's wrapper — installed on claude_agent_sdk.query
        # when setup_tracing() runs — is actually used. A pre-bound `query` name
        # would still point at the original, untraced function.
        async for message in claude_agent_sdk.query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                turns_used += 1
                for block in message.content:
                    if isinstance(block, TextBlock):
                        final_text = block.text
                    elif isinstance(block, ToolUseBlock):
                        tools_used.add(block.name)
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
    return final_text, turns_used, tools_used


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


def _parse_cause_categories(d: dict) -> list[CauseCategory]:
    """Read `cause_categories` (list). Falls back to the legacy single
    `cause_category` key for older stored verdicts. Keeps only valid values,
    de-duplicated in order; defaults to [UNKNOWN]."""
    valid = {c.value for c in CauseCategory}
    raw = d.get("cause_categories")
    if not isinstance(raw, list):
        one = d.get("cause_category")           # legacy shape
        raw = [one] if one else []
    out: list[CauseCategory] = []
    for v in raw:
        if v in valid and CauseCategory(v) not in out:
            out.append(CauseCategory(v))
    return out or [CauseCategory.UNKNOWN]


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
        cause_categories=_parse_cause_categories(d),
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


def blame_dropped_note(verdict: Verdict, tools_used: set[str]) -> str:
    """Post-hoc guard: if the agent ran git_blame but the verdict doesn't record
    it, warn. Blame is real work — dropping it means the introducing commit isn't
    clickable and `is_regression` silently stays unknown. Returns a one-line
    `[auto-check]` note (empty string when nothing's wrong)."""
    if not any(t.endswith("git_blame") for t in tools_used):
        return ""
    problems = []
    if not any(e.kind in ("blame", "commit") for e in verdict.evidence_chain):
        problems.append("git_blame was run but no blame/commit evidence was recorded")
    if verdict.is_regression is None:
        problems.append("is_regression left unset despite blame being gathered")
    return "[auto-check] " + "; ".join(problems) if problems else ""
