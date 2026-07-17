"""The eval 'task': run the REAL production agent on one ticket.

Phoenix's `run_experiment` calls this once per dataset example. It reuses the
production path verbatim — run.py's `_one` -> run_agent / parse_verdict /
verify_verdict — so the eval measures exactly what ships and mutates nothing.

Bounded concurrency: each run spawns a Claude Code subprocess and the box is
memory-tight, so we cap real parallelism with a process-wide semaphore
(EVAL_CONCURRENCY, default 2). The task is SYNC (Phoenix runs it in a worker
thread) + asyncio.run, which keeps each run in its own loop and lets a plain
threading semaphore bound parallelism regardless of how Phoenix schedules tasks.
"""

from __future__ import annotations

import asyncio
import os
import threading

from ..config import get_settings
from ..gitlab_client import build_client
from ..jira import JiraClient
from .run import _one  # production-faithful single-ticket run, reused verbatim

_MAX_TURNS = int(os.getenv("EVAL_MAX_TURNS", "60"))
_CONCURRENCY = max(1, int(os.getenv("EVAL_CONCURRENCY", "2")))
_sem = threading.BoundedSemaphore(_CONCURRENCY)

# Clients are built once and shared (httpx clients are safe to use concurrently).
_lock = threading.Lock()
_settings = None
_jira = None
_gl = None


def _clients():
    global _settings, _jira, _gl
    with _lock:
        if _settings is None:
            _settings = get_settings()
            if not _settings.has_jira():
                raise SystemExit(
                    "eval: Jira not configured (JIRA_URL / JIRA_EMAIL / JIRA_TOKEN).")
            _jira = JiraClient(_settings.jira_url, _settings.jira_email, _settings.jira_token)
            _gl = build_client(_settings)
    return _settings, _jira, _gl


def _ticket_key(example) -> str:
    """Pull ticket_key out of a Phoenix DatasetExample (a dict at runtime)."""
    inp = example.get("input") if isinstance(example, dict) else getattr(example, "input", None)
    key = (inp or {}).get("ticket_key")
    if not key:
        raise ValueError(f"eval: example has no input.ticket_key: {example!r}")
    return key


def run_rca_task(example) -> dict:
    """Return the verdict dict (from run.py `_one`) or {'key', 'error'}. Never raises
    on an agent failure — a failed run is a data point the evaluators score, not a
    crash that aborts the whole experiment."""
    key = _ticket_key(example)
    settings, jira, gl = _clients()
    with _sem:
        return asyncio.run(_one({"key": key}, jira, gl, settings, _MAX_TURNS))
