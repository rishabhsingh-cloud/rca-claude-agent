"""Offline eval harness for the RCA agent — SANDBOXED, does NOT touch production.

This package exists to run the *real* RCA agent over a batch of resolved tickets,
score each verdict against ground truth (the human discussion on the ticket), and
surface failure PATTERNS that tell us what to change in prompts.py / verify.py.

Isolation guarantees (see README.md):
  - Writes ONLY to rca_agent/eval/data|results (never the webapp's rca_reviews.db).
  - Uses ONLY read paths of Jira/GitLab (search + get); never posts/accepts/writes.
  - Imports the production agent (run_agent), prompts, and verify UNCHANGED — it
    tests reality and never mutates the agent's behavior.
  - Refuses to run any model batch without ANTHROPIC_API_KEY (batch work must use
    the API key, not a subscription/OAuth profile, which would hit rate limits).
"""
