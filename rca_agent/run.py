"""CLI entry point.

Examples:
  # Deterministic trace-first pipeline against mock fixtures (no API key):
  python -m rca_agent.run --ticket RCA-101

  # Same, but drive it through the Claude Agent SDK (needs ANTHROPIC_API_KEY):
  python -m rca_agent.run --ticket RCA-101 --agent

  # Investigate an arbitrary Jira-shaped JSON file:
  python -m rca_agent.run --ticket-file ./my-ticket.json

Backend is chosen by RCA_BACKEND (mock|live); see .env.example.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .config import get_settings
from .gitlab_client import build_client
from .investigation import investigate
from .schema import render_verdict
from .tickets import build_ticket_source, load_ticket_file


def _load(args, settings) -> tuple[str, str]:
    if args.ticket_file:
        return load_ticket_file(Path(args.ticket_file))
    # Live Jira (if JIRA_* configured) fetches by key; else reads the fixture.
    src = build_ticket_source(settings)
    if getattr(args, "no_comments", False):
        # Match the webapp: strip ALL comments so the run is independent (the
        # agent isn't influenced by prior human/RCA guesses). Fall back gracefully
        # for sources whose get() doesn't accept the flag (e.g. the mock source).
        try:
            return src.get(args.ticket, drop_all_comments=True)
        except TypeError:
            return src.get(args.ticket)
    return src.get(args.ticket)


def main(argv: list[str] | None = None) -> int:
    # Print UTF-8 regardless of the host console codepage (Windows cp1252, etc.).
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(prog="rca_agent.run", description="RCA triage agent")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--ticket", default="RCA-101", help="fixture ticket key")
    src.add_argument("--ticket-file", help="path to a Jira-shaped JSON ticket")
    ap.add_argument("--agent", action="store_true",
                    help="drive via the Claude Agent SDK (needs ANTHROPIC_API_KEY)")
    ap.add_argument("--project", help="pin the GitLab repo (skip summary routing)")
    ap.add_argument("--ref", default="main", help="git ref to fetch from")
    ap.add_argument("--json", action="store_true", help="print raw verdict JSON")
    ap.add_argument("--brief", action="store_true",
                    help="print the short QA view (headline + summary + key links)")
    ap.add_argument("--no-comments", action="store_true",
                    help="strip ALL ticket comments (match the webapp — independent, "
                         "comment-blind RCA)")
    args = ap.parse_args(argv)

    settings = get_settings()
    client = build_client(settings)
    ticket_key, ticket_text = _load(args, settings)

    if args.agent:
        # Imported lazily so the deterministic path doesn't require the SDK.
        from .agent import parse_verdict, run_agent
        raw, _ = asyncio.run(run_agent(ticket_key, ticket_text, client, settings))
        verdict = parse_verdict(raw, ticket_key)
    else:
        verdict = investigate(ticket_key, ticket_text, client, settings,
                              project_override=args.project, ref=args.ref)

    if args.json:
        print(json.dumps(verdict.to_dict(), indent=2, default=str))
    else:
        print(render_verdict(verdict, brief=args.brief))
    return 0


if __name__ == "__main__":
    sys.exit(main())
