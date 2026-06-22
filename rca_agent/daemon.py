"""Autonomous RCA loop — the deployable daemon.

Polls Jira for new tickets, investigates each through the agent, and (optionally)
posts the verdict back. Designed to run unattended on a server (systemd/cron).

Safety by design:
  - DRY-RUN by default: it posts NOTHING unless `--live` is given. So a
    misconfigured deploy logs verdicts instead of spamming real tickets.
  - Idempotent: skips any ticket that already carries our "Automated RCA"
    comment, so restarts/overlapping polls never double-post (no state DB needed).
  - Bounded: processes at most `--max` tickets per poll (cost/blast-radius cap).
  - Infra failures (out of credits, etc.) are logged and never posted.

Usage:
  python -m rca_agent.daemon --once                 # one poll, dry-run (safe)
  python -m rca_agent.daemon --once --live          # one poll, actually post
  python -m rca_agent.daemon --interval 600 --live  # loop every 10 min, posting
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from .agent import AgentRunError, parse_verdict, run_agent
from .config import get_settings
from .gitlab_client import build_client
from .jira import JiraClient
from .schema import Confidence
from .tickets import flatten_adf

# Bugs filed today in AUT; comment-dedup handles already-processed tickets.
DEFAULT_JQL = ("project = AUT AND issuetype = Bug AND statusCategory != Done "
               "AND created >= startOfDay() ORDER BY created DESC")
RCA_MARKER = "automated rca"  # footer text on every verdict we post


def _log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def already_processed(issue: dict) -> bool:
    """True if the ticket already has an Automated-RCA comment (idempotency)."""
    comment = (issue.get("fields", {}) or {}).get("comment") or {}
    for c in comment.get("comments", []) or []:
        body = c.get("body")
        text = flatten_adf(body) if isinstance(body, dict) else (body or "")
        if RCA_MARKER in (text or "").lower():
            return True
    return False


def _should_post(v, post_mode: str) -> bool:
    if post_mode == "dry":
        return False
    if post_mode == "gated":          # skip LOW/insufficient
        return v.confidence is not Confidence.LOW
    return True                        # "all"


async def poll_once(jql: str, post_mode: str, max_tickets: int) -> int:
    """One sweep: find unprocessed tickets, investigate, optionally post.
    Returns the number of tickets processed."""
    settings = get_settings()
    if not settings.has_jira:
        _log("ERROR: Jira not configured (JIRA_URL/EMAIL/TOKEN). Aborting poll.")
        return 0
    jira = JiraClient(settings.jira_url, settings.jira_email, settings.jira_token)
    client = build_client(settings)

    issues = jira.search(jql, max_results=50)
    todo = [i for i in issues if not already_processed(i)][:max_tickets]
    _log(f"poll: {len(issues)} match, {len(todo)} unprocessed (cap {max_tickets}), "
         f"mode={post_mode}")

    done = 0
    for issue in todo:
        key = issue.get("key")
        try:
            tkey, text = jira.get(key, drop_rca_comments=True)
            raw = await run_agent(tkey, text, client, settings, jira_mcp=False)
            v = parse_verdict(raw, tkey)
            if _should_post(v, post_mode):
                res = jira.post_verdict(key, v)
                _log(f"{key}: {v.confidence.value}/{v.triage.value} -> POSTED "
                     f"(comment {res.get('id')})")
            else:
                _log(f"{key}: {v.confidence.value}/{v.triage.value} -> not posted "
                     f"({'dry-run' if post_mode == 'dry' else 'gated'})")
            done += 1
        except AgentRunError as e:
            _log(f"{key}: SKIPPED — agent run failed: {str(e)[:160]}")
        except Exception as e:
            _log(f"{key}: ERROR — {type(e).__name__}: {str(e)[:160]}")
    return done


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rca_agent.daemon", description="Autonomous RCA loop")
    ap.add_argument("--once", action="store_true", help="run a single poll and exit")
    ap.add_argument("--interval", type=int, default=600, help="seconds between polls")
    ap.add_argument("--live", action="store_true",
                    help="actually post to Jira (default: dry-run, posts nothing)")
    ap.add_argument("--gated", action="store_true",
                    help="with --live, post only HIGH/MEDIUM (skip LOW)")
    ap.add_argument("--max", type=int, default=5, help="max tickets per poll")
    ap.add_argument("--jql", default=DEFAULT_JQL, help="ticket selection JQL")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    post_mode = "dry" if not args.live else ("gated" if args.gated else "all")
    _log(f"RCA daemon starting (post_mode={post_mode}, max={args.max}, "
         f"{'single poll' if args.once else f'interval={args.interval}s'})")

    if args.once:
        asyncio.run(poll_once(args.jql, post_mode, args.max))
        return 0
    while True:
        try:
            asyncio.run(poll_once(args.jql, post_mode, args.max))
        except Exception as e:           # keep the loop alive across poll failures
            _log(f"poll crashed: {type(e).__name__}: {str(e)[:160]}")
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
