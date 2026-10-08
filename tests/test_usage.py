"""RunUsage: the per-run token/cost meter behind the Cost tab."""
from __future__ import annotations

import asyncio

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

import rca_agent.agent as agent
from rca_agent.usage import RunUsage, estimate_cost

U = {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 1000,
     "cache_creation_input_tokens": 100}


def _am(mid, usage=U, model="claude-opus-4-8"):
    return AssistantMessage(content=[TextBlock(text="x")], model=model, usage=usage,
                            message_id=mid)


def _rm(cost=1.25, usage=None):
    return ResultMessage(subtype="success", duration_ms=5000, duration_api_ms=4000,
                         is_error=False, num_turns=7, session_id="s", total_cost_usd=cost,
                         usage=usage or {"input_tokens": 1, "output_tokens": 2,
                                         "cache_read_input_tokens": 3,
                                         "cache_creation_input_tokens": 4},
                         result="",
                         model_usage={"claude-haiku-4-5": {"outputTokens": 1},
                                      "claude-opus-4-8": {"outputTokens": 99}})


def test_assistant_usage_deduped_by_message_id():
    u = RunUsage()
    for m in (_am("a"), _am("a"), _am("b")):  # "a" split into two blocks
        u.observe(m)
    assert (u.input_tokens, u.output_tokens, u.cache_read_tokens) == (20, 40, 2000)
    assert u.num_turns == 2


def test_result_message_is_authoritative():
    u = RunUsage()
    u.observe(_am("a"))
    u.observe(_rm())
    u.finish()
    assert u.cost_usd == 1.25 and u.cost_source == "sdk"
    assert (u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens) == (1, 2, 3, 4)
    assert u.model == "claude-opus-4-8"           # biggest output in model_usage
    assert u.num_turns == 7 and u.duration_ms == 5000


def test_estimated_when_no_result_message():
    u = RunUsage()
    u.observe(_am("a"))
    u.finish()
    assert u.cost_source == "estimated"
    assert u.cost_usd == estimate_cost("claude-opus-4-8", 10, 20, 1000, 100)
    assert u.duration_ms >= 0


def test_estimate_prefix_match():
    # Opus 5.5 is cheaper than the generic Opus row; longest prefix must win.
    assert estimate_cost("claude-opus-5-5", 1_000_000, 0, 0, 0) == 4.0
    assert estimate_cost("claude-opus-4-8", 1_000_000, 0, 0, 0) == 5.0
    assert estimate_cost("gpt-x", 1, 1, 0, 0) is None


def test_nothing_observed():
    u = RunUsage().finish()
    assert not u.observed and u.cost_usd is None


def test_observe_never_raises():
    RunUsage().observe(object())


def test_run_agent_fills_meter(monkeypatch):
    monkeypatch.setattr(agent, "build_rca_server",
                        lambda client, search_scope=None: (object(), []))
    monkeypatch.setattr(agent._trace, "setup_tracing", lambda: None)
    monkeypatch.setattr(agent, "build_system_prompt", lambda url: "sys")
    verdict = '{"triage": "real_bug", "confidence": "high", "probable_root_cause": "x"}'

    async def fake_query(prompt, options):
        yield AssistantMessage(content=[TextBlock(text=verdict)], model="claude-opus-4-8",
                               usage=U, message_id="m1")
        yield _rm(cost=0.5)

    monkeypatch.setattr(agent.claude_agent_sdk, "query", fake_query)

    class _S:
        gitlab_url = "http://gl"
        model = "claude-opus-4-8"

    u = RunUsage()
    text, turns, _ = asyncio.run(agent.run_agent("AUT-1", "t", client=object(),
                                                 settings=_S(), usage=u))
    assert text and turns == 1
    assert u.cost_usd == 0.5 and u.cost_source == "sdk"
