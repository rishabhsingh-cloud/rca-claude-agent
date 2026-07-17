"""Offline eval harness for the RCA agent — SANDBOXED, does NOT touch production.

This package exists to run the *real* RCA agent over a batch of resolved tickets,
score each verdict against ground truth (the human discussion on the ticket), and
surface failure PATTERNS that tell us what to change in prompts.py / verify.py.

Isolation guarantees (see README.md):
  - Writes ONLY to rca_agent/eval/data|results (never the webapp's rca_reviews.db).
  - Uses ONLY read paths of Jira/GitLab (search + get); never posts/accepts/writes.
  - Imports the production agent (run_agent), prompts, and verify UNCHANGED — it
    tests reality and never mutates the agent's behavior.
  - Uses the box's ambient Claude auth (the subscription), same as the live agent;
    warns (doesn't block) if no ANTHROPIC_API_KEY, since a large batch on a
    subscription can hit interactive rate limits mid-run.
"""
