"""Optional Langfuse tracing for RCA runs — see where a run took the wrong turn.

Design rules (do not break these):
  * NO-OP by default. Tracing is active only when RCA_TRACE=1 AND the `langfuse`
    package is installed AND LANGFUSE_HOST/keys are configured. Otherwise every
    function here is a cheap no-op.
  * A failure in tracing must NEVER fail or slow an RCA. Every Langfuse call is
    wrapped; on any error we silently degrade to no-op. Losing a trace is fine;
    breaking a run is not.
  * Nothing else in the codebase imports `langfuse` directly — only this module.

Layers (see rca_agent/agent.py and rca_agent/tools.py):
  * a per-run root trace (run_trace) with turn spans carrying the agent's reasoning,
  * a child span per tool call (tool_span) carrying full input + output.
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager

_client = None
_init_done = False


def _flag() -> bool:
    return os.getenv("RCA_TRACE", "").strip().lower() in ("1", "true", "yes", "on")


def _get_client():
    """Lazily build the Langfuse client once. Returns None when disabled/unavailable
    (which makes everything below a no-op). Never raises."""
    global _client, _init_done
    if _init_done:
        return _client
    _init_done = True
    if not _flag():
        return None
    try:
        from langfuse import Langfuse  # imported only when enabled
        _client = Langfuse()  # reads LANGFUSE_HOST / _PUBLIC_KEY / _SECRET_KEY from env
    except Exception:
        _client = None  # not installed / misconfigured -> stay a no-op
    return _client


def is_enabled() -> bool:
    return _get_client() is not None


def _truncate(v, limit: int = 20000):
    """Keep payloads sane — full inputs/outputs, but not unbounded (a base64 image
    or a huge log dump would bloat every trace)."""
    try:
        s = v if isinstance(v, str) else repr(v)
    except Exception:
        return "<unrepr-able>"
    return s if len(s) <= limit else s[:limit] + f"…(+{len(s) - limit} chars)"


class _Run:
    """Handle for one RCA run's root trace. All methods are guarded no-ops on error."""

    def __init__(self, client, span):
        self._c = client
        self._span = span  # the root span (its trace is the run)

    def record_turn(self, n: int, reasoning: str, tools: list[str]) -> None:
        """One span per agent turn, carrying the model's REASONING text (what it
        decided and why) + the tools it then called — the turn-by-turn narration
        that shows where a run went wrong."""
        if self._c is None:
            return
        try:
            with self._c.start_as_current_span(
                name=f"turn-{n}",
                input=None,
                output=_truncate(reasoning),
                metadata={"tools_called": tools, "tool_count": len(tools)},
            ):
                pass
        except Exception:
            pass

    def finish(self, *, status: str, emitted_verdict: bool, turns_used: int,
               hit_max_turns: bool, tools_used, verdict: dict | None,
               output: str = "") -> None:
        """Stamp the trace with the outcome + the attributes that make wrong-turns
        filterable in the dashboard."""
        if self._c is None:
            return
        tags = [f"status:{status}"]
        if not emitted_verdict:
            tags.append("no-verdict")     # the worst outcome — filter these first
        if hit_max_turns:
            tags.append("hit-max-turns")
        meta = {
            "status": status,
            "emitted_verdict": emitted_verdict,
            "turns_used": turns_used,
            "hit_max_turns": hit_max_turns,
            "tools_used": sorted(tools_used) if tools_used else [],
        }
        if verdict:
            meta.update({
                "cause_categories": verdict.get("cause_categories"),
                "confidence": verdict.get("confidence"),
                "is_regression": verdict.get("is_regression"),
                "verdict_label": verdict.get("verdict_label"),
            })
        try:
            self._c.update_current_trace(output=_truncate(output), metadata=meta, tags=tags)
        except Exception:
            pass


class _NoopRun:
    def record_turn(self, *a, **k) -> None: ...
    def finish(self, *a, **k) -> None: ...


@contextmanager
def run_trace(name: str, metadata: dict | None = None):
    """Root trace for one RCA run. Yields a handle (_Run or _NoopRun). Flushes on
    exit so short-lived processes (webapp thread / CLI) don't lose the trace.

    The body's exceptions (e.g. AgentRunError) MUST propagate — so setup is guarded
    separately from the single `yield`; we never yield twice."""
    client = _get_client()
    if client is None:
        yield _NoopRun()
        return
    cm = None
    run = _NoopRun()
    try:  # setup only — must not wrap the yield (a 2nd yield breaks @contextmanager)
        cm = client.start_as_current_span(name=name, metadata=metadata or {})
        cm.__enter__()
        try:
            client.update_current_trace(name=name, metadata=metadata or {})
        except Exception:
            pass
        run = _Run(client, cm)
    except Exception:
        cm = None
        run = _NoopRun()
    try:
        yield run  # body runs here; its exceptions propagate normally
    finally:
        if cm is not None:
            try:
                cm.__exit__(None, None, None)
            except Exception:
                pass
        flush()


@contextmanager
def tool_span(name: str, args):
    """Child span for one tool call: full input (args) + output (set by the caller
    via the yielded setter). Nests under the active run trace via Langfuse context.
    Yields a callable `set_output(payload, ok=True)`."""
    client = _get_client()
    if client is None:
        yield lambda payload=None, ok=True: None
        return
    start = time.time()
    cm = span = None
    holder: dict = {}

    def _set(payload=None, ok=True):
        holder["payload"] = payload
        holder["ok"] = ok

    try:  # setup only — separate from the single yield
        cm = client.start_as_current_span(name=name, input=_truncate(args))
        span = cm.__enter__()
    except Exception:
        cm = span = None
    try:
        yield _set  # body (the tool call) runs here
    except Exception:
        holder["ok"] = False  # tool raised -> mark the span errored, then re-raise
        raise
    finally:
        if span is not None:
            try:
                dur = round((time.time() - start) * 1000)
                ok = holder.get("ok", True)
                span.update(output=_truncate(holder.get("payload")),
                            metadata={"duration_ms": dur, "ok": ok},
                            level=("DEFAULT" if ok else "ERROR"))
            except Exception:
                pass
            try:
                cm.__exit__(None, None, None)
            except Exception:
                pass


def flush() -> None:
    """Force-send buffered traces. Call before a short-lived process exits."""
    c = _client
    if c is None:
        return
    try:
        c.flush()
    except Exception:
        pass
