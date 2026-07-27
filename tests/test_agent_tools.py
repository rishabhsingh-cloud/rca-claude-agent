"""The RCA agent must run confined to its read-only MCP tools — never the
built-in Task (subagents) / Write / Edit tools."""
from __future__ import annotations

import asyncio

import pytest

import rca_agent.agent as agent


def test_default_block_list_covers_task_write_edit():
    # Regression: Task caused runs to end on prose about a subagent instead of the
    # JSON verdict; Write/Edit would break the read-only guarantee.
    assert {"Task", "Write", "Edit"} <= set(agent.DEFAULT_DISALLOWED_TOOLS)


def _run_and_capture(monkeypatch, disallowed_arg):
    """Invoke run_agent with the SDK stubbed out; return the disallowed_tools that
    reached ClaudeAgentOptions."""
    captured: dict = {}
    monkeypatch.setattr(agent, "build_rca_server",
                        lambda client, search_scope=None: (object(), []))
    monkeypatch.setattr(agent._trace, "setup_tracing", lambda: None)
    monkeypatch.setattr(agent, "build_system_prompt", lambda url: "sys")

    async def fake_query(prompt, options):
        captured["disallowed"] = options.disallowed_tools
        return
        yield  # noqa: unreachable — makes this an async generator

    monkeypatch.setattr(agent.claude_agent_sdk, "query", fake_query)

    class _S:
        gitlab_url = "http://gl"
        model = "claude-opus-4-8"

    # No messages are yielded, so no verdict is emitted -> run_agent raises. We only
    # care about what options it built before that.
    with pytest.raises(agent.AgentRunError):
        asyncio.run(agent.run_agent("AUT-1", "text", client=object(), settings=_S(),
                                    disallowed_tools=disallowed_arg))
    return captured["disallowed"]


def test_run_agent_blocks_builtins_by_default(monkeypatch):
    # Caller passes nothing -> the default block-list is applied.
    assert _run_and_capture(monkeypatch, None) == ["Task", "Write", "Edit"]


def test_run_agent_allows_explicit_override(monkeypatch):
    # An explicit list (including []) overrides the default, so callers stay in control.
    assert _run_and_capture(monkeypatch, []) == []
    assert _run_and_capture(monkeypatch, ["Task"]) == ["Task"]


def test_run_agent_soft_budget_keeps_finished_verdict(monkeypatch):
    # AUT-9864 fix (Option A): a soft time budget must STOP consuming further turns
    # and RETURN the verdict already produced — not discard it the way an external
    # wait_for cancel did. Here the verdict arrives on turn 1; the budget then trips,
    # and the later prose turns (which would overwrite final_text) must never run.
    from claude_agent_sdk import AssistantMessage, TextBlock

    monkeypatch.setattr(agent, "build_rca_server",
                        lambda client, search_scope=None: (object(), []))
    monkeypatch.setattr(agent._trace, "setup_tracing", lambda: None)
    monkeypatch.setattr(agent, "build_system_prompt", lambda url: "sys")

    verdict = '{"triage": "real_bug", "confidence": "high", "probable_root_cause": "x"}'

    async def fake_query(prompt, options):
        # Real time passes before the verdict turn lands, so the budget is already
        # exceeded when the loop checks it right after processing this turn.
        await asyncio.sleep(0.05)
        yield AssistantMessage(content=[TextBlock(text=verdict)], model="m")
        # Later turns that WOULD overwrite final_text — must never be consumed.
        yield AssistantMessage(content=[TextBlock(text="still investigating...")], model="m")
        yield AssistantMessage(content=[TextBlock(text="more prose, not a verdict")], model="m")

    monkeypatch.setattr(agent.claude_agent_sdk, "query", fake_query)

    class _S:
        gitlab_url = "http://gl"
        model = "claude-opus-4-8"

    text, turns, _tools = asyncio.run(
        agent.run_agent("AUT-1", "text", client=object(), settings=_S(),
                        time_budget_s=0.01))
    assert text == verdict   # the finished verdict is kept...
    assert turns == 1        # ...and no further turn was consumed past the budget


def test_find_screen_tool_registered():
    # The frontend-index localizer is a general read-only tool, always available.
    import rca_agent.tools as t
    _server, names = t.build_rca_server(object())
    assert "mcp__rca__find_screen" in names


def test_image_ticket_note_points_to_find_screen(monkeypatch):
    # On a ticket WITH screenshots, the user-turn note must tell the agent to use
    # mcp__rca__find_screen — and that instruction must NOT leak into the system prompt
    # (prompts.py stays untouched; the nudge lives only in the image branch of run_agent).
    monkeypatch.setattr(agent, "build_rca_server",
                        lambda client, search_scope=None: (object(), []))
    monkeypatch.setattr(agent._trace, "setup_tracing", lambda: None)
    monkeypatch.setattr(agent, "build_system_prompt", lambda url: "SYSTEM-PROMPT")

    captured = {}

    async def fake_query(prompt, options):
        captured["sys"] = options.system_prompt
        async for msg in prompt:                     # streaming-input async generator
            captured["content"] = msg["message"]["content"]
        return
        yield  # noqa: unreachable — makes this an async generator

    monkeypatch.setattr(agent.claude_agent_sdk, "query", fake_query)

    class _S:
        gitlab_url = "http://gl"
        model = "claude-opus-4-8"

    images = [{"mimeType": "image/png", "content": "base64data"}]
    with pytest.raises(agent.AgentRunError):
        asyncio.run(agent.run_agent("AUT-1", "ticket text", client=object(),
                                    settings=_S(), images=images))
    user_text = captured["content"][0]["text"]
    assert "mcp__rca__find_screen" in user_text        # nudge is in the user turn
    assert "find_screen" not in captured["sys"]        # ...never in the system prompt
