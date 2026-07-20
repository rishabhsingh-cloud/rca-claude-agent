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
    monkeypatch.setattr(agent, "build_rca_server", lambda client: (object(), []))
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
