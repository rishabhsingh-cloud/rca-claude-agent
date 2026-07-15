"""Optional Phoenix tracing for RCA runs.

When enabled, the Claude Agent SDK's telemetry is auto-captured (every tool call,
model input/output, and the agent's reasoning) and sent to a SELF-HOSTED Phoenix,
so the team can open a run in the Phoenix dashboard and see where it went wrong.

Rules (do not break these):
  * NO-OP by default. Active only when RCA_TRACE=1 AND the trace packages are
    installed (pip install -e ".[trace]") AND PHOENIX_COLLECTOR_ENDPOINT points at
    a Phoenix server. Otherwise setup_tracing() does nothing.
  * A tracing failure must NEVER fail or slow an RCA — setup is fully guarded and
    degrades to a no-op. Losing traces is fine; breaking a run is not.
  * Traces contain customer data → Phoenix must be self-hosted and internal-only.
"""
from __future__ import annotations

import os

_setup_done = False


def _flag() -> bool:
    return os.getenv("RCA_TRACE", "").strip().lower() in ("1", "true", "yes", "on")


def setup_tracing() -> None:
    """Point the Claude Agent SDK's telemetry at Phoenix. Idempotent (only the first
    call does anything) and safe to call at the start of every run. Never raises."""
    global _setup_done
    if _setup_done or not _flag():
        return
    _setup_done = True
    try:
        from phoenix.otel import register
        # Force HTTP OTLP to Phoenix's :6006 (the `/v1/traces` path signals HTTP).
        # Without this, register() defaults to gRPC on :4317 — a different port that
        # an SSH tunnel / nginx proxy on 6006 does NOT carry, so traces would vanish.
        base = os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006").rstrip("/")
        register(
            project_name=os.getenv("PHOENIX_PROJECT", "rca-agent"),
            endpoint=f"{base}/v1/traces",
        )
        from openinference.instrumentation.claude_agent_sdk import (
            ClaudeAgentSDKInstrumentor,
        )
        ClaudeAgentSDKInstrumentor().instrument()
    except Exception:
        # best-effort: not installed / Phoenix unreachable / API drift — stay a no-op
        pass
